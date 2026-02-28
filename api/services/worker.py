"""
Background download worker.

Started as an asyncio task in the FastAPI lifespan — no separate process needed.

Architecture: one asyncio task per show, bounded by MAX_CONCURRENT_SHOWS.

  Orchestrator loop
    └─ _spawn_show_tasks()   — finds shows with pending work, creates per-show tasks
    └─ _maybe_refresh()      — re-fetches upstream feeds on schedule

  Per-show task (_show_worker)
    └─ cover first           — show artwork (small, users see it immediately)
    └─ episodes in order     — audio + episode cover, newest-first
    └─ exits when done       — orchestrator respawns on next iteration if more work arrives

Different shows hit different upstream servers, so parallel tasks are polite.
Within a single show, downloads are sequential (no hammering one server).

Folder layout on disk:

  {MEDIA_PATH}/
    {show-slug}/
      cover.jpg                         ← show artwork
      item-0001/
        audio.mp3                       ← current episode audio
        audio-change-1.mp3              ← previous audio (archive_diff=True only)
        cover.jpg                       ← episode cover (only if distinct from show)
        cover-change-1.jpg              ← previous cover (archive_diff=True only)
      item-0002/
        audio.mp3

Episode lifecycle:
  pending → downloading → published
                       ↘ failed
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select, update

from api.config import proxy_map, settings
from api.database import AsyncSessionLocal
from api.media_types import AUDIO_CONTENT_TYPES, IMAGE_CONTENT_TYPES
from api.models.episode import Episode
from api.models.episode_version import EpisodeVersion
from api.models.show import Show
from api.services import hooks
from api.services.feed_parser import FeedFetchError, FeedParseError, fetch_and_parse
from api.services.ingestion import (
    check_episode_version_updates,
    ingest_episodes,
    sync_show_metadata,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # max seconds to wait when the queue is empty
DOWNLOAD_TIMEOUT = 600  # max seconds for a single episode download (10 min)
IMAGE_TIMEOUT = 30  # max seconds for an image download

# Set by notify() whenever new work is added (new show / OPML import / refresh).
_work_available: asyncio.Event = asyncio.Event()

# show_id → running asyncio Task; managed exclusively by the orchestrator.
_show_tasks: dict[uuid.UUID, asyncio.Task] = {}


# Initialised to now so we don't hammer all feeds immediately on every restart.
_last_refresh: datetime = datetime.now(UTC)

# When True: no new show tasks are spawned and feeds are not refreshed.
# Currently running downloads finish naturally (soft suspend — no partial files).
_suspended: bool = False


# ---------------------------------------------------------------------------
# Entry point + public signals
# ---------------------------------------------------------------------------


async def start() -> None:
    """Called from the FastAPI lifespan to start the background worker."""
    await _reset_stuck_downloads()
    await _loop()


def notify() -> None:
    """
    Signal the orchestrator that new pending work has been added to the DB.
    If the orchestrator is sleeping, it wakes immediately and spawns tasks.
    Safe to call from any async context — just sets an asyncio.Event.
    """
    _work_available.set()


def toggle_suspended() -> bool:
    """
    Toggle the suspended flag and return the new state.
    - suspended=True  → no new downloads or refreshes start; running downloads finish.
    - suspended=False → worker resumes immediately (notify() is called internally).
    """
    global _suspended
    _suspended = not _suspended
    if not _suspended:
        notify()  # wake the orchestrator so it picks up work right away
    else:
        logger.info(
            "Worker suspended — running downloads will finish, no new ones will start"
        )
    return _suspended


def is_suspended() -> bool:
    return _suspended


def active_download_count() -> int:
    """Number of per-show tasks currently running."""
    return len(_show_tasks)


# ---------------------------------------------------------------------------
# Orchestrator loop
# ---------------------------------------------------------------------------


async def _loop() -> None:
    while True:
        # Clear at the top: any notify() during the iteration is caught on
        # the *next* wait, not the current one — prevents missed signals.
        _work_available.clear()
        try:
            if not _suspended:
                await _spawn_show_tasks()
                await _maybe_refresh()

            if _show_tasks:
                # Wait for any show task to finish, a new-work signal, or timeout.
                waiter = asyncio.create_task(_work_available.wait())
                await asyncio.wait(
                    {waiter} | set(_show_tasks.values()),
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=POLL_INTERVAL,
                )
                if not waiter.done():
                    waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await waiter

                # Prune completed show tasks.
                for show_id in [sid for sid, t in _show_tasks.items() if t.done()]:
                    task = _show_tasks.pop(show_id)
                    if not task.cancelled() and task.exception():
                        logger.error(
                            "Show worker raised an exception for %s: %s",
                            show_id,
                            task.exception(),
                        )
            else:
                # Queue empty — sleep until notified or poll interval elapses.
                try:
                    await asyncio.wait_for(_work_available.wait(), POLL_INTERVAL)
                except TimeoutError:
                    pass

        except asyncio.CancelledError:
            # Shut down all show tasks cleanly on server shutdown.
            for task in _show_tasks.values():
                task.cancel()
            await asyncio.gather(*_show_tasks.values(), return_exceptions=True)
            raise
        except Exception:
            logger.exception("Unexpected error in worker loop")
            await asyncio.sleep(10)


# ---------------------------------------------------------------------------
# Task spawner
# ---------------------------------------------------------------------------


async def _spawn_show_tasks() -> None:
    """
    Find shows that have pending work (cover or episodes) and no running task,
    then start a per-show worker task for each — up to MAX_CONCURRENT_SHOWS.
    """
    slots = settings.MAX_CONCURRENT_SHOWS - len(_show_tasks)
    if slots <= 0:
        return

    async with AsyncSessionLocal() as session:
        # Shows missing their local cover.
        cover_ids = set(
            (
                await session.execute(
                    select(Show.id)
                    .where(Show.status == "active")
                    .where(Show.image_url_upstream.is_not(None))
                    .where(Show.image_url_local.is_(None))
                )
            ).scalars()
        )

        # Shows with at least one pending episode that has an audio URL.
        # Must match the filter in _process_next_episode — episodes without
        # enclosure_url_upstream are skipped by the worker and must not cause
        # the spawner to loop infinitely.
        ep_ids = set(
            (
                await session.execute(
                    select(Episode.show_id)
                    .join(Show, Show.id == Episode.show_id)
                    .where(Episode.status == "pending")
                    .where(Episode.enclosure_url_upstream.is_not(None))
                    .where(Show.status == "active")
                    .distinct()
                )
            ).scalars()
        )

        candidate_ids = (cover_ids | ep_ids) - set(_show_tasks.keys())
        if not candidate_ids:
            return

        rows = (
            await session.execute(
                select(Show.id, Show.slug)
                .where(Show.id.in_(candidate_ids))
                .limit(slots)
            )
        ).all()

    for show_id, show_slug in rows:
        logger.info("Spawning worker for show %s", show_slug)
        _show_tasks[show_id] = asyncio.create_task(_show_worker(show_id, show_slug))


# ---------------------------------------------------------------------------
# Per-show worker
# ---------------------------------------------------------------------------


async def _show_worker(show_id: uuid.UUID, show_slug: str) -> None:
    """
    Download all pending covers and episodes for one show, then exit.
    The orchestrator will respawn this task if new episodes arrive later
    (via a periodic refresh or manual show-refresh).
    """
    while True:
        did_work = await _process_show_cover(show_id, show_slug)
        if not did_work:
            did_work = await _process_next_episode(show_id, show_slug)
        if not did_work:
            break
    logger.debug("Worker done for show %s", show_slug)


# ---------------------------------------------------------------------------
# Periodic upstream refresh
# ---------------------------------------------------------------------------


async def _maybe_refresh() -> None:
    global _last_refresh
    elapsed_hours = (datetime.now(UTC) - _last_refresh).total_seconds() / 3600
    if elapsed_hours < settings.REFRESH_INTERVAL_HOURS:
        return

    logger.info(
        "Starting periodic feed refresh (interval: %dh)",
        settings.REFRESH_INTERVAL_HOURS,
    )
    _last_refresh = datetime.now(UTC)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Show).where(Show.status == "active"))
        show_data = [(s.id, s.slug, s.upstream_url) for s in result.scalars().all()]

    for show_id, show_slug, upstream_url in show_data:
        try:
            parsed = await fetch_and_parse(upstream_url)
        except (FeedFetchError, FeedParseError) as exc:
            logger.warning("Refresh failed for %s: %s", show_slug, exc)
            continue

        image_changed = False
        added = versioned = 0
        async with AsyncSessionLocal() as session:
            show = await session.get(Show, show_id)
            if not show:
                continue
            if parsed.new_feed_url:
                show.apply_feed_redirect(parsed.new_feed_url)
                logger.info("Feed moved for %s -> %s", show_slug, parsed.new_feed_url)
            image_changed = sync_show_metadata(show, parsed)
            show.updated_at = datetime.now(UTC)
            added = await ingest_episodes(show, parsed.episodes, session)
            versioned = await check_episode_version_updates(
                show_id, show_slug, parsed.episodes, session
            )
            await session.commit()

        if image_changed:
            _delete_show_cover(show_slug)
            logger.info(
                "Show image changed for %s — cover queued for re-download", show_slug
            )
        if added:
            logger.info("Refreshed %s: +%d new episode(s)", show_slug, added)
        if versioned:
            logger.info(
                "Refreshed %s: %d episode(s) re-queued (metadata changed)",
                show_slug,
                versioned,
            )
        if added or versioned:
            notify()

    logger.info("Periodic feed refresh complete")


# ---------------------------------------------------------------------------
# Show cover
# ---------------------------------------------------------------------------


async def _process_show_cover(show_id: uuid.UUID, show_slug: str) -> bool:
    """Download the cover for this show if it is missing. Returns True if work was done."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Show)
            .where(Show.id == show_id)
            .where(Show.image_url_upstream.is_not(None))
            .where(Show.image_url_local.is_(None))
        )
        show = result.scalar_one_or_none()
        if show is None:
            return False
        image_url = show.image_url_upstream

    cover_base = Path(settings.MEDIA_PATH) / show_slug / "cover"
    logger.info("Downloading cover for show %s", show_slug)

    try:
        cover_path = await _download_image(image_url, cover_base, IMAGE_TIMEOUT)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code < 500:
            # 4xx — permanent failure, the URL will never work; stop trying.
            logger.warning(
                "Cover for show %s returned %d — clearing image URL to skip future attempts",
                show_slug,
                exc.response.status_code,
            )
            async with AsyncSessionLocal() as session:
                show = await session.get(Show, show_id)
                if show:
                    show.image_url_upstream = None
                    show.updated_at = datetime.now(UTC)
                await session.commit()
        else:
            # 5xx — transient; return False so the task exits and retries next cycle.
            logger.error(
                "Cover download for show %s failed (%d) — will retry next cycle",
                show_slug,
                exc.response.status_code,
            )
        return False
    except Exception as exc:
        # Network error, timeout, etc. — transient; retry next cycle.
        logger.error(
            "Cover download for show %s failed: %s — will retry next cycle",
            show_slug,
            exc,
        )
        return False

    async with AsyncSessionLocal() as session:
        show = await session.get(Show, show_id)
        if show:
            # Store the relative media path; the full URL is built at serve time
            # so it always reflects the current BASE_URL setting.
            show.image_url_local = f"{show_slug}/{cover_path.name}"
            show.updated_at = datetime.now(UTC)
        await session.commit()

    logger.info("Saved cover for show %s -> %s", show_slug, cover_path)
    return True


# ---------------------------------------------------------------------------
# Episode audio + cover
# ---------------------------------------------------------------------------


async def _process_next_episode(show_id: uuid.UUID, show_slug: str) -> bool:
    """
    Claim the next pending episode for this show.

    For new episodes (v1): download audio and cover, write EpisodeVersion row.
    For version bumps (v2+):
      - Audio changed  → rename old audio/cover, download new files.
      - Metadata only  → skip download entirely; just write EpisodeVersion row.

    Returns True if an episode was processed (success or failure).
    """
    async with AsyncSessionLocal() as session:
        show = await session.get(Show, show_id)
        if not show or show.status != "active":
            return False
        archive_diff = show.archive_diff
        show_image_upstream = show.image_url_upstream

        # Mark any pending episodes with no audio URL as failed so they
        # don't prevent the spawner from ever settling.
        no_url = (
            (
                await session.execute(
                    select(Episode)
                    .where(Episode.show_id == show_id)
                    .where(Episode.status == "pending")
                    .where(Episode.enclosure_url_upstream.is_(None))
                )
            )
            .scalars()
            .all()
        )
        for orphan in no_url:
            orphan.status = "failed"
            orphan.processing_error = "no enclosure URL"
            logger.warning(
                "Episode %s has no enclosure URL — marking failed", orphan.id
            )
        if no_url:
            await session.commit()

        result = await session.execute(
            select(Episode)
            .where(Episode.show_id == show_id)
            .where(Episode.status == "pending")
            .where(Episode.enclosure_url_upstream.is_not(None))
            .order_by(Episode.item_number.desc())
            .limit(1)
        )
        episode = result.scalar_one_or_none()
        if episode is None:
            return False

        episode_id = episode.id
        upstream_url = episode.enclosure_url_upstream
        ep_meta = dict(episode.pending_metadata or {})
        pending_changes = dict(episode.pending_changes or {})
        item_number = episode.item_number
        episode_version = episode.episode_version

        if not ep_meta:
            logger.error(
                "No metadata for pending episode %s — marking failed", episode_id
            )
            episode.status = "failed"
            episode.processing_error = "metadata missing"
            await session.commit()
            return True

        episode.status = "downloading"
        episode.updated_at = datetime.now(UTC)
        await session.commit()

    ep_folder = Path(settings.MEDIA_PATH) / show_slug / f"item-{item_number:04d}"
    logger.info(
        "Processing episode %s -> %s (v%d)", episode_id, ep_folder.name, episode_version
    )

    # Determine what actually needs to happen on disk.
    is_new = episode_version == 1
    audio_chgd = "enclosure_url" in pending_changes
    cover_chgd = "image_url" in pending_changes
    need_audio = is_new or audio_chgd
    need_cover = is_new or cover_chgd

    # --- Audio ---
    audio_path: Path | None = None
    if need_audio:
        if audio_chgd and archive_diff:
            _rename_current_file(ep_folder, "audio", episode_version - 1)
        try:
            audio_path = await _download_audio(
                upstream_url, ep_folder, DOWNLOAD_TIMEOUT
            )
        except Exception as exc:
            logger.error("Audio download failed for episode %s: %s", episode_id, exc)
            async with AsyncSessionLocal() as session:
                ep = await session.get(Episode, episode_id)
                if ep:
                    ep.status = "failed"
                    ep.processing_error = str(exc)[:1000]
                    ep.updated_at = datetime.now(UTC)
                await session.commit()
            return True
    else:
        # Metadata-only change — reuse the existing audio file.
        audio_path = _find_current_file(ep_folder, "audio")
        logger.info(
            "Metadata-only change for episode %s — skipping audio download", episode_id
        )

    # --- Episode cover (only if distinct from show cover) ---
    episode_image_upstream = ep_meta.get("image_url")
    cover_path: Path | None = None
    if (
        need_cover
        and episode_image_upstream
        and episode_image_upstream != show_image_upstream
    ):
        if cover_chgd and archive_diff:
            _rename_current_file(ep_folder, "cover", episode_version - 1)
        try:
            cover_path = await _download_image(
                episode_image_upstream,
                ep_folder / "cover",
                IMAGE_TIMEOUT,
            )
        except Exception as exc:
            # Non-fatal: podcast client will fall back to the show cover.
            logger.warning("Episode cover download failed for %s: %s", episode_id, exc)
    else:
        # Reuse existing cover if present.
        cover_path = _find_current_file(ep_folder, "cover")

    # --- Write EpisodeVersion row ---
    archived_at = datetime.now(UTC)
    async with AsyncSessionLocal() as session:
        session.add(
            EpisodeVersion(
                episode_id=episode_id,
                version=episode_version,
                snapshot=ep_meta,
                changes=pending_changes or None,
                archived_at=archived_at,
            )
        )

        ep = await session.get(Episode, episode_id)
        if ep:
            ep.status = "published"
            ep.pending_metadata = None
            ep.pending_changes = None
            ep.updated_at = archived_at
            # Refresh current metadata columns.
            ep.title = ep_meta.get("title")
            ep.description = ep_meta.get("description")
            ep.duration = ep_meta.get("duration")
            ep.enclosure_length = ep_meta.get("enclosure_length")
            ep.enclosure_type = ep_meta.get("enclosure_type")
            ep.vendor_metadata = ep_meta.get("vendor_metadata")
            if audio_path:
                ep.audio_path = str(audio_path.relative_to(settings.MEDIA_PATH))
            if cover_path:
                ep.cover_path = str(cover_path.relative_to(settings.MEDIA_PATH))
            elif is_new:
                ep.cover_path = None

        await session.commit()

    if audio_path:
        enclosure_url_local = (
            f"{settings.BASE_URL.rstrip('/')}/media/{ep.audio_path}"
            if ep and ep.audio_path
            else ""
        )
        logger.info("Published episode %s -> %s", episode_id, audio_path)

        await hooks.fire(
            {
                "show_slug": show_slug,
                "episode_id": str(episode_id),
                "title": ep_meta.get("title", ""),
                "local_path": str(audio_path),
                "enclosure_url_local": enclosure_url_local,
            }
        )

    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_current_file(folder: Path, stem: str) -> Path | None:
    """Return the file matching stem.* (e.g. audio.mp3) in folder, or None."""
    return next(
        (
            p
            for p in folder.glob(f"{stem}.*")
            if p.suffix != ".tmp" and "-change-" not in p.name
        ),
        None,
    )


def _rename_current_file(folder: Path, stem: str, prev_version: int) -> None:
    """
    Rename e.g. audio.mp3 → audio-change-1.mp3 before downloading a new file.
    No-op if no matching file exists.
    """
    current = _find_current_file(folder, stem)
    if current is None:
        return
    target = folder / f"{stem}-change-{prev_version}{current.suffix}"
    try:
        current.rename(target)
        logger.info("Renamed %s → %s", current.name, target.name)
    except OSError as exc:
        logger.warning("Could not rename %s: %s", current, exc)


def _delete_show_cover(show_slug: str) -> None:
    """
    Delete any downloaded cover file for a show so the cover worker
    re-downloads the new one. Ignores .tmp files (handled at startup).
    """
    cover_dir = Path(settings.MEDIA_PATH) / show_slug
    for f in cover_dir.glob("cover.*"):
        if f.suffix != ".tmp":
            try:
                f.unlink()
                logger.info("Deleted stale cover for show %s: %s", show_slug, f.name)
            except OSError as exc:
                logger.warning("Could not delete cover %s: %s", f, exc)


async def _stream_to_tmp(url: str, tmp: Path, timeout: float) -> str:
    """Stream url into tmp, return the raw Content-Type value.
    Cleans up tmp on any error so callers never leave partial files."""
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": settings.USER_AGENT},
            mounts=proxy_map(),
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = (
                    resp.headers.get("content-type", "").split(";")[0].strip()
                )
                with tmp.open("wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
        return content_type
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def _download_audio(url: str, folder: Path, timeout: float) -> Path:
    """Download audio to folder/, detecting extension from Content-Type.
    No-op if audio.* already exists (guaranteed complete via .tmp rename)."""
    existing = _find_current_file(folder, "audio")
    if existing is not None:
        logger.debug("Audio already on disk, skipping download: %s", existing)
        return existing
    tmp = folder / "audio.tmp"
    content_type = await _stream_to_tmp(url, tmp, timeout)
    ext = AUDIO_CONTENT_TYPES.get(content_type)
    if not ext and content_type.startswith("audio/"):
        ext = "." + content_type.split("/", 1)[1]
    if not ext:
        ext = Path(url.split("?")[0]).suffix
    if not ext:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"Cannot determine audio extension "
            f"(content-type: {content_type!r}, url: {url!r})"
        )
    final = folder / f"audio{ext}"
    tmp.rename(final)
    return final


async def _download_image(url: str, base_path: Path, timeout: float) -> Path:
    """Download an image, detect extension from Content-Type, return final path.
    No-op if any file matching base_path.* already exists on disk."""
    existing = next(
        (p for p in base_path.parent.glob(base_path.name + ".*") if p.suffix != ".tmp"),
        None,
    )
    if existing is not None:
        logger.debug("Image already on disk, skipping download: %s", existing)
        return existing
    tmp = base_path.with_suffix(".tmp")
    content_type = await _stream_to_tmp(url, tmp, timeout)
    ext = IMAGE_CONTENT_TYPES.get(content_type, ".jpg")
    final = base_path.with_suffix(ext)
    tmp.rename(final)
    return final


# ---------------------------------------------------------------------------
# Startup cleanup
# ---------------------------------------------------------------------------


async def _reset_stuck_downloads() -> None:
    """Reset episodes stuck in 'downloading' back to 'pending' after a restart."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            update(Episode)
            .where(Episode.status == "downloading")
            .values(status="pending", updated_at=datetime.now(UTC))
            .returning(Episode.id)
        )
        count = len(result.fetchall())
        await session.commit()
    if count:
        logger.info("Reset %d stuck downloading episode(s) to pending", count)

    await asyncio.to_thread(_cleanup_tmp_files)


def _cleanup_tmp_files() -> None:
    """Delete any leftover .tmp files under MEDIA_PATH from interrupted downloads."""
    media_root = Path(settings.MEDIA_PATH)
    if not media_root.exists():
        return
    removed = 0
    for tmp in media_root.rglob("*.tmp"):
        try:
            tmp.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Could not remove stale tmp file %s: %s", tmp, exc)
    if removed:
        logger.info("Removed %d stale .tmp file(s) from previous run", removed)
