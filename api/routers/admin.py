import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlparse, urlunparse

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import AsyncSessionLocal, get_db
from api.models.episode import Episode
from api.models.episode_version import EpisodeVersion
from api.models.show import Show
from api.schemas.episode import EpisodeResponse, EpisodeVersionResponse
from api.schemas.show import ShowCreate, ShowResponse, ShowUpdate
from api.services import worker
from api.services.feed_parser import FeedFetchError, FeedParseError, fetch_and_parse
from api.services.ingestion import (
    check_episode_version_updates,
    ingest_episodes,
    sync_show_metadata,
)

logger = logging.getLogger(__name__)

router = APIRouter()

DB = Annotated[AsyncSession, Depends(get_db)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_show_or_404(slug: str, db: AsyncSession) -> Show:
    result = await db.execute(select(Show).where(Show.slug == slug))
    show = result.scalar_one_or_none()
    if show is None:
        raise HTTPException(status_code=404, detail=f"Show '{slug}' not found")
    return show


def _normalize_url(url: str) -> str:
    """Normalize a URL for duplicate detection: lowercase scheme+host, strip trailing slash."""
    p = urlparse(url.strip())
    return urlunparse(
        p._replace(
            scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=p.path.rstrip("/")
        )
    )


async def _url_already_registered(url: str, db: AsyncSession) -> bool:
    normalized = _normalize_url(url)
    rows = (
        await db.execute(select(Show.upstream_url, Show.upstream_url_history))
    ).all()
    for current_url, history in rows:
        all_urls = [current_url] + (history or [])
        if any(_normalize_url(u) == normalized for u in all_urls):
            return True
    return False


async def _unique_slug(base: str, db: AsyncSession) -> str:
    slug = base
    counter = 2
    while True:
        result = await db.execute(select(Show).where(Show.slug == slug))
        if result.scalar_one_or_none() is None:
            return slug
        slug = f"{base}-{counter}"
        counter += 1


def _episode_response(ep: Episode, show_slug: str) -> dict:
    """
    Build the EpisodeResponse dict from DB fields.

    For published episodes all metadata comes from the Episode columns
    (title, description, audio_path, etc.) — no disk reads required.
    For pending/downloading/failed episodes metadata still comes from
    the pending_metadata blob set at ingestion.
    """
    base_url = settings.BASE_URL.rstrip("/")

    if ep.status == "published":
        enclosure_url_local = (
            f"{base_url}/media/{ep.audio_path}" if ep.audio_path else None
        )
        image_url_local = f"{base_url}/media/{ep.cover_path}" if ep.cover_path else None
        return {
            "id": ep.id,
            "show_id": ep.show_id,
            "upstream_guid": ep.upstream_guid,
            "item_number": ep.item_number,
            "episode_version": ep.episode_version,
            "status": ep.status,
            "title": ep.title,
            "description": ep.description,
            "pub_date": ep.pub_date,
            "duration": ep.duration,
            "enclosure_url_upstream": ep.enclosure_url_upstream,
            "enclosure_length": ep.enclosure_length,
            "enclosure_type": ep.enclosure_type,
            "image_url_upstream": (
                ep.pending_metadata.get("image_url") if ep.pending_metadata else None
            ),
            "vendor_metadata": ep.vendor_metadata or {},
            "enclosure_url_local": enclosure_url_local,
            "image_url_local": image_url_local,
            "processing_error": ep.processing_error,
            "created_at": ep.created_at,
        }

    # Pending / downloading / failed — metadata from the DB blob.
    meta = ep.pending_metadata or {}
    pub_date = None
    pub_date_str = meta.get("pub_date")
    if pub_date_str:
        try:
            pub_date = datetime.fromisoformat(pub_date_str)
        except ValueError:
            pass

    return {
        "id": ep.id,
        "show_id": ep.show_id,
        "upstream_guid": ep.upstream_guid,
        "item_number": ep.item_number,
        "episode_version": ep.episode_version,
        "status": ep.status,
        "title": meta.get("title"),
        "description": meta.get("description"),
        "pub_date": pub_date,
        "duration": meta.get("duration"),
        "enclosure_url_upstream": ep.enclosure_url_upstream,
        "enclosure_length": meta.get("enclosure_length"),
        "enclosure_type": meta.get("enclosure_type"),
        "image_url_upstream": meta.get("image_url"),
        "vendor_metadata": meta.get("vendor_metadata", {}),
        "enclosure_url_local": None,
        "image_url_local": None,
        "processing_error": ep.processing_error,
        "created_at": ep.created_at,
    }


async def _show_response(show: Show, db: AsyncSession) -> ShowResponse:
    total = await db.scalar(select(func.count()).where(Episode.show_id == show.id))
    published = await db.scalar(
        select(func.count()).where(
            Episode.show_id == show.id, Episode.status == "published"
        )
    )
    data = {col.name: getattr(show, col.name) for col in Show.__table__.columns}
    data["episode_count"] = total or 0
    data["published_episode_count"] = published or 0
    if data.get("image_url_local"):
        base_url = settings.BASE_URL.rstrip("/")
        data["image_url_local"] = f"{base_url}/media/{data['image_url_local']}"
    return ShowResponse.model_validate(data)


# ---------------------------------------------------------------------------
# POST /admin/shows
# ---------------------------------------------------------------------------


@router.post("/shows", response_model=ShowResponse, status_code=201)
async def create_show(body: ShowCreate, db: DB):
    url = str(body.upstream_url)

    if await _url_already_registered(url, db):
        raise HTTPException(
            status_code=409, detail="A show with this URL is already registered"
        )

    try:
        parsed = await fetch_and_parse(url)
    except FeedFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except FeedParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Slug
    base_slug = slugify(body.slug) if body.slug else slugify(parsed.title)
    slug = await _unique_slug(base_slug, db)

    # If feed advertises a new URL, use it straight away
    upstream_url = parsed.new_feed_url or url

    show = Show(
        slug=slug,
        title=parsed.title,
        description=parsed.description,
        upstream_url=upstream_url,
        upstream_url_history=[url] if parsed.new_feed_url else [],
        image_url_upstream=parsed.image_url,
        language=parsed.language,
        author=parsed.author,
        status="active",
        archive_diff=body.archive_diff,
    )
    db.add(show)
    await db.flush()  # get show.id without committing

    episodes = parsed.episodes
    if settings.INITIAL_EPISODES_PER_SHOW > 0:
        episodes = episodes[: settings.INITIAL_EPISODES_PER_SHOW]
    await ingest_episodes(show, episodes, db)
    worker.notify()
    return await _show_response(show, db)


# ---------------------------------------------------------------------------
# GET /admin/shows
# ---------------------------------------------------------------------------


@router.get("/shows", response_model=list[ShowResponse])
async def list_shows(
    db: DB,
    status: str | None = Query(None),
    search: str | None = Query(None),
):
    q = select(Show)
    if status:
        q = q.where(Show.status == status)
    if search:
        q = q.where(Show.title.ilike(f"%{search}%"))
    result = await db.execute(q.order_by(Show.created_at.desc()))
    shows = result.scalars().all()
    return [await _show_response(s, db) for s in shows]


# ---------------------------------------------------------------------------
# GET /admin/shows/{slug}
# ---------------------------------------------------------------------------


@router.get("/shows/{slug}", response_model=ShowResponse)
async def get_show(slug: str, db: DB):
    show = await _get_show_or_404(slug, db)
    return await _show_response(show, db)


# ---------------------------------------------------------------------------
# PUT /admin/shows/{slug}
# ---------------------------------------------------------------------------


@router.put("/shows/{slug}", response_model=ShowResponse)
async def update_show(slug: str, body: ShowUpdate, db: DB):
    show = await _get_show_or_404(slug, db)
    if body.status is not None:
        show.status = body.status
    if body.archive_diff is not None:
        show.archive_diff = body.archive_diff
    if body.upstream_url is not None:
        old_url = show.upstream_url
        new_url = str(body.upstream_url)
        if old_url != new_url:
            history = list(show.upstream_url_history or [])
            history.append(old_url)
            show.upstream_url_history = history
            show.upstream_url = new_url
    show.updated_at = datetime.now(UTC)
    return await _show_response(show, db)


# ---------------------------------------------------------------------------
# DELETE /admin/shows/{slug}
# ---------------------------------------------------------------------------


@router.delete("/shows/{slug}", status_code=204)
async def delete_show(
    slug: str,
    db: DB,
    keep_archive: bool = Query(True),
):
    show = await _get_show_or_404(slug, db)
    if keep_archive:
        show.status = "abandoned"
        show.updated_at = datetime.now(UTC)
    else:
        await db.delete(show)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /admin/shows/{slug}/refresh
# ---------------------------------------------------------------------------


@router.post("/shows/{slug}/refresh", status_code=202)
async def refresh_show(slug: str, db: DB):
    """
    Re-fetch the upstream RSS feed for a single show.
    Returns 202 immediately — feed fetch, ingestion and version checks run
    in the background so the HTTP request is never held waiting.
    """
    show = await _get_show_or_404(slug, db)
    asyncio.create_task(_refresh_show_background(show.slug, show.upstream_url))
    return {"status": "accepted", "slug": show.slug}


async def _refresh_show_background(slug: str, upstream_url: str) -> None:
    """Fetch upstream RSS, ingest new episodes and check for version updates."""
    try:
        parsed = await fetch_and_parse(upstream_url)
    except (FeedFetchError, FeedParseError) as exc:
        logger.error("Refresh failed for %s: %s", slug, exc)
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Show).where(Show.slug == slug))
        show = result.scalar_one_or_none()
        if show is None:
            return

        if parsed.new_feed_url and parsed.new_feed_url != show.upstream_url:
            history = list(show.upstream_url_history or [])
            history.append(show.upstream_url)
            show.upstream_url_history = history
            show.upstream_url = parsed.new_feed_url
            logger.info("Feed URL updated for %s → %s", slug, parsed.new_feed_url)

        image_changed = sync_show_metadata(show, parsed)
        show.updated_at = datetime.now(UTC)

        if image_changed:
            cover_dir = Path(settings.MEDIA_PATH) / show.slug
            for f in cover_dir.glob("cover.*"):
                if f.suffix != ".tmp":
                    f.unlink(missing_ok=True)
            logger.info(
                "Show image changed for %s — cover queued for re-download", slug
            )

        added = await ingest_episodes(show, parsed.episodes, db)
        versioned = await check_episode_version_updates(
            show.id, show.slug, parsed.episodes, db
        )
        await db.commit()

    worker.notify()
    logger.info(
        "Refresh complete for %s: added=%d versioned=%d", slug, added, versioned
    )


# ---------------------------------------------------------------------------
# GET /admin/shows/{slug}/episodes
# ---------------------------------------------------------------------------


@router.get("/shows/{slug}/episodes", response_model=list[EpisodeResponse])
async def list_episodes(
    slug: str,
    db: DB,
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    show = await _get_show_or_404(slug, db)
    q = select(Episode).where(Episode.show_id == show.id)
    if status:
        q = q.where(Episode.status == status)
    q = q.order_by(Episode.item_number.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    episodes = result.scalars().all()
    return [_episode_response(ep, show.slug) for ep in episodes]


# ---------------------------------------------------------------------------
# GET /admin/shows/{slug}/episodes/{upstream_guid}
# ---------------------------------------------------------------------------


@router.get(
    "/shows/{slug}/episodes/{upstream_guid:path}", response_model=EpisodeResponse
)
async def get_episode(slug: str, upstream_guid: str, db: DB):
    show = await _get_show_or_404(slug, db)
    guid = unquote(upstream_guid)
    result = await db.execute(
        select(Episode).where(
            Episode.show_id == show.id,
            Episode.upstream_guid == guid,
        )
    )
    episode = result.scalar_one_or_none()
    if episode is None:
        raise HTTPException(status_code=404, detail="Episode not found")
    return _episode_response(episode, show.slug)


# ---------------------------------------------------------------------------
# GET /admin/shows/{slug}/episodes/{upstream_guid}/versions
# ---------------------------------------------------------------------------


@router.get(
    "/shows/{slug}/episodes/{upstream_guid:path}/versions",
    response_model=list[EpisodeVersionResponse],
)
async def get_episode_versions(slug: str, upstream_guid: str, db: DB):
    """Return the full version history (snapshots + diffs) for one episode."""
    show = await _get_show_or_404(slug, db)
    guid = unquote(upstream_guid)
    ep = (
        await db.execute(
            select(Episode).where(
                Episode.show_id == show.id,
                Episode.upstream_guid == guid,
            )
        )
    ).scalar_one_or_none()
    if ep is None:
        raise HTTPException(status_code=404, detail="Episode not found")

    versions = (
        (
            await db.execute(
                select(EpisodeVersion)
                .where(EpisodeVersion.episode_id == ep.id)
                .order_by(EpisodeVersion.version)
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "version": v.version,
            "snapshot": v.snapshot,
            "changes": v.changes,
            "archived_at": v.archived_at,
        }
        for v in versions
    ]


# ---------------------------------------------------------------------------
# POST /admin/import/opml
# ---------------------------------------------------------------------------


@router.post("/import/opml", status_code=202)
async def import_opml(file: UploadFile = File(...)):
    """
    Parse the OPML synchronously (instant), return 202, then fetch and ingest
    all feeds in the background. The HTTP client is never held waiting.
    """
    content = await file.read()
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid OPML: {exc}")

    feed_urls = [
        outline.get("xmlUrl")
        for outline in root.iter("outline")
        if outline.get("xmlUrl")
    ]
    if not feed_urls:
        raise HTTPException(status_code=422, detail="No feed URLs found in OPML")

    asyncio.create_task(_import_feeds_background(feed_urls))
    return {"status": "accepted", "queued": len(feed_urls)}


async def _import_feeds_background(feed_urls: list[str]) -> None:
    """Fetch and ingest each feed from an OPML import. Runs detached from the request."""
    imported = skipped = 0
    errors = []

    for url in feed_urls:
        async with AsyncSessionLocal() as db:
            if await _url_already_registered(url, db):
                skipped += 1
                continue

        try:
            parsed = await fetch_and_parse(url)
        except Exception as exc:
            logger.warning("OPML import: failed to fetch %s — %s", url, exc)
            errors.append({"url": url, "error": str(exc)})
            continue

        async with AsyncSessionLocal() as db:
            base_slug = slugify(parsed.title)
            slug = await _unique_slug(base_slug, db)
            upstream_url = parsed.new_feed_url or url

            show = Show(
                slug=slug,
                title=parsed.title,
                description=parsed.description,
                upstream_url=upstream_url,
                upstream_url_history=[url] if parsed.new_feed_url else [],
                image_url_upstream=parsed.image_url,
                language=parsed.language,
                author=parsed.author,
                status="active",
            )
            db.add(show)
            await db.flush()
            episodes = parsed.episodes
            if settings.INITIAL_EPISODES_PER_SHOW > 0:
                episodes = episodes[: settings.INITIAL_EPISODES_PER_SHOW]
            await ingest_episodes(show, episodes, db)
            await db.commit()
        imported += 1

    if imported:
        worker.notify()
    logger.info(
        "OPML import complete: imported=%d skipped=%d errors=%d",
        imported,
        skipped,
        len(errors),
    )


# ---------------------------------------------------------------------------
# POST /admin/suspend
# ---------------------------------------------------------------------------


@router.post("/suspend")
async def suspend():
    """
    Toggle the background worker on/off.
    - suspended=true  → no new downloads or feed refreshes; running downloads finish naturally.
    - suspended=false → worker resumes immediately.
    The API keeps serving feeds and audio regardless of this flag.
    """
    suspended = worker.toggle_suspended()
    return {"suspended": suspended}


@router.get("/suspend")
async def suspend_status():
    """Return the current worker suspended state and number of active downloads."""
    return {
        "suspended": worker.is_suspended(),
        "active_downloads": worker.active_download_count(),
    }


# ---------------------------------------------------------------------------
# GET /admin/export/opml
# ---------------------------------------------------------------------------


@router.get("/export/opml")
async def export_opml(db: DB):
    result = await db.execute(select(Show).where(Show.status == "active"))
    shows = result.scalars().all()

    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(
        head, "title"
    ).text = f"Arypodr subscriptions {datetime.now(UTC).strftime('%Y-%m-%d')}"
    body = ET.SubElement(opml, "body")

    for show in shows:
        ET.SubElement(
            body,
            "outline",
            attrib={
                "type": "rss",
                "text": show.title,
                "title": show.title,
                "xmlUrl": show.upstream_url,
            },
        )

    xml_str = ET.tostring(opml, encoding="unicode", xml_declaration=True)

    return Response(content=xml_str, media_type="application/xml")
