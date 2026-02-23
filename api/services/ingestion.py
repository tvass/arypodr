"""
Episode ingestion — shared between the admin router and the background worker.

ingest_episodes()
    GUID-based dedup: episodes already known by GUID are skipped.
    New episodes are inserted as 'pending' with their full ParsedEpisode
    metadata stored in the Episode.pending_metadata JSON column (cleared to
    null after the episode is archived).

check_episode_version_updates()
    For already-published episodes, compares the upstream parsed metadata
    with the latest snapshot stored in the episode_versions table.  If
    anything relevant changed, the episode is re-queued ('pending') with an
    incremented episode_version so the worker creates new audio/cover files.

    Audio is only re-downloaded when the enclosure URL itself changes.
    Metadata-only changes (title, description, …) never trigger a download.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.episode import Episode
from api.models.episode_version import EpisodeVersion
from api.models.show import Show
from api.services.feed_parser import ParsedEpisode, ParsedShow

logger = logging.getLogger(__name__)


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Convert a datetime to naive UTC (strip tzinfo). SQLite stores datetimes
    without timezone info, so all pub_dates are stored and compared as naive UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


# Per-show lock prevents two concurrent callers (e.g. periodic refresh and a
# manual /refresh) from computing the same max item_number and racing to
# insert duplicate rows.
_ingest_locks: defaultdict[uuid.UUID, asyncio.Lock] = defaultdict(asyncio.Lock)


def sync_show_metadata(show: Show, parsed: ParsedShow) -> bool:
    """
    Update show fields from a freshly parsed feed.

    Syncs title, description, author and language unconditionally.
    If image_url_upstream changed, clears image_url_local so the worker
    re-downloads the new cover.

    Returns True if the image URL changed (caller should delete the old
    cover file from disk so the worker does not skip the re-download).
    Does not commit — caller is responsible.
    """
    show.title = parsed.title
    show.description = parsed.description
    show.author = parsed.author
    show.language = parsed.language

    image_changed = False
    if parsed.image_url and parsed.image_url != show.image_url_upstream:
        show.image_url_upstream = parsed.image_url
        show.image_url_local = None
        image_changed = True

    return image_changed


async def ingest_episodes(
    show: Show,
    parsed_episodes: list[ParsedEpisode],
    db: AsyncSession,
) -> int:
    """
    Insert new episodes for show, deduplicating by upstream GUID.
    Returns number of episodes added.
    Serialised per-show to prevent concurrent callers racing on item_number.
    """
    async with _ingest_locks[show.id]:
        return await _ingest_episodes_locked(show, parsed_episodes, db)


async def _ingest_episodes_locked(
    show: Show,
    parsed_episodes: list[ParsedEpisode],
    db: AsyncSession,
) -> int:
    existing_guids = set(
        (
            await db.execute(
                select(Episode.upstream_guid).where(Episode.show_id == show.id)
            )
        ).scalars()
    )

    # Filter out already-known GUIDs, then deduplicate within the batch itself
    # (some feeds contain duplicate GUIDs for the same episode).
    seen: set[str] = existing_guids
    new_eps = []
    for ep in parsed_episodes:
        if ep.upstream_guid not in seen:
            seen.add(ep.upstream_guid)
            new_eps.append(ep)
    if not new_eps:
        return 0

    max_num = (
        await db.execute(
            select(func.max(Episode.item_number)).where(Episode.show_id == show.id)
        )
    ).scalar() or 0

    # If this show already has episodes, only ingest episodes that are newer
    # than the most recent one. This prevents a refresh from pulling in old
    # episodes that were intentionally excluded by INITIAL_EPISODES_PER_SHOW.
    if existing_guids:
        latest_pub_date = (
            await db.execute(
                select(func.max(Episode.pub_date)).where(Episode.show_id == show.id)
            )
        ).scalar()
        if latest_pub_date is not None:
            before = len(new_eps)
            new_eps = [
                ep
                for ep in new_eps
                if _naive_utc(ep.pub_date) is None
                or _naive_utc(ep.pub_date) > latest_pub_date
            ]
            skipped = before - len(new_eps)
            if skipped:
                logger.debug(
                    "Skipped %d episode(s) older than latest known pub_date (%s) for show %s",
                    skipped,
                    latest_pub_date,
                    show.slug,
                )
    if not new_eps:
        return 0

    # Sort oldest-first so episode numbers ascend chronologically.
    sorted_new = sorted(new_eps, key=lambda e: (e.pub_date is None, e.pub_date))

    for i, ep in enumerate(sorted_new):
        db.add(
            Episode(
                show_id=show.id,
                upstream_guid=ep.upstream_guid,
                item_number=max_num + 1 + i,
                episode_version=1,
                pub_date=_naive_utc(ep.pub_date),
                enclosure_url_upstream=ep.enclosure_url,
                pending_metadata=ep.model_dump(mode="json"),
                status="pending",
            )
        )

    return len(new_eps)


async def check_episode_version_updates(
    show_id: uuid.UUID,
    show_slug: str,
    parsed_episodes: list[ParsedEpisode],
    db: AsyncSession,
) -> int:
    """
    For published episodes whose upstream metadata has changed, bump
    episode_version and set status back to 'pending' so the worker
    creates new audio/cover files (or just a new EpisodeVersion row for
    metadata-only changes).

    Returns the number of episodes re-queued.
    Serialised per-show (same lock as ingest_episodes).
    """
    async with _ingest_locks[show_id]:
        return await _check_version_updates_locked(
            show_id, show_slug, parsed_episodes, db
        )


async def _check_version_updates_locked(
    show_id: uuid.UUID,
    show_slug: str,
    parsed_episodes: list[ParsedEpisode],
    db: AsyncSession,
) -> int:
    result = await db.execute(
        select(Episode)
        .where(Episode.show_id == show_id)
        .where(Episode.status == "published")
    )
    published = {ep.upstream_guid: ep for ep in result.scalars()}
    if not published:
        return 0

    logger.info(
        "Version check for %s: %d published episode(s), %d in feed",
        show_slug,
        len(published),
        len(parsed_episodes),
    )

    # Fetch the latest snapshot for every published episode in one query.
    ep_ids = [ep.id for ep in published.values()]
    latest_sub = (
        select(
            EpisodeVersion.episode_id, func.max(EpisodeVersion.version).label("max_ver")
        )
        .where(EpisodeVersion.episode_id.in_(ep_ids))
        .group_by(EpisodeVersion.episode_id)
        .subquery()
    )
    snap_rows = await db.execute(
        select(EpisodeVersion.episode_id, EpisodeVersion.snapshot).join(
            latest_sub,
            and_(
                EpisodeVersion.episode_id == latest_sub.c.episode_id,
                EpisodeVersion.version == latest_sub.c.max_ver,
            ),
        )
    )
    snapshots: dict = {row.episode_id: row.snapshot for row in snap_rows}

    updated = 0
    for parsed in parsed_episodes:
        ep = published.get(parsed.upstream_guid)
        if ep is None:
            continue

        current_snap = snapshots.get(ep.id)
        if current_snap is None:
            # Legacy episode published before episode_versions were introduced.
            # Synthesise a snapshot from the episode's own DB columns so we can
            # still detect upstream changes going forward.
            current_snap = {
                "title": ep.title,
                "description": ep.description,
                "enclosure_url": ep.enclosure_url_upstream,
                "image_url": None,
                "duration": ep.duration,
            }

        if changes := _compute_changes(current_snap, parsed):
            ep.episode_version += 1
            ep.enclosure_url_upstream = parsed.enclosure_url
            ep.pending_metadata = parsed.model_dump(mode="json")
            ep.pending_changes = changes
            ep.status = "pending"
            updated += 1
            logger.info(
                "Re-queuing %s (guid=%s): changes=%s",
                show_slug,
                parsed.upstream_guid,
                list(changes.keys()),
            )

    logger.info("Version check for %s: %d re-queued", show_slug, updated)
    return updated


def _compute_changes(current: dict, parsed: ParsedEpisode) -> dict:
    """Return {field: {"from": old, "to": new}} for every field that differs."""
    changes: dict = {}
    for field, new_val in [
        ("title", parsed.title),
        ("description", parsed.description),
        ("enclosure_url", parsed.enclosure_url),
        ("image_url", parsed.image_url),
        ("duration", parsed.duration),
    ]:
        old_val = current.get(field)
        if old_val != new_val:
            changes[field] = {"from": old_val, "to": new_val}
    return changes
