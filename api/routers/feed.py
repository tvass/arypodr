"""
Feed router — public endpoints consumed by podcast clients.

/catalog        JSON list of all active shows with local feed URLs.

/catalog.opml   Same catalogue as OPML (local feed URLs).
                Import this into AntennaPod to subscribe to every show at once.

/feed/{slug}    RSS feed for a single show, served to AntennaPod.
                Only includes episodes with status='published' (audio archived locally).
                Episode metadata is read entirely from the DB — no disk reads required.
                AntennaPod never needs to contact the internet.
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from api.database import get_db
from api.models.episode import Episode
from api.models.show import Show

router = APIRouter()

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"

ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def _local_feed_url(slug: str) -> str:
    return f"{settings.BASE_URL.rstrip('/')}/feed/{slug}"


# ---------------------------------------------------------------------------
# GET /catalog
# ---------------------------------------------------------------------------


@router.get("/catalog")
async def catalog(db: AsyncSession = Depends(get_db)):
    """
    JSON list of all active shows with their local Arypodr feed URLs.
    Share this with users so they can discover available shows.
    """
    result = await db.execute(
        select(Show, func.count(Episode.id).label("episode_count"))
        .outerjoin(Episode, Episode.show_id == Show.id)
        .where(Show.status == "active")
        .group_by(Show.id)
        .order_by(Show.title)
    )
    rows = result.all()

    return [
        {
            "slug": show.slug,
            "title": show.title,
            "description": show.description,
            "author": show.author,
            "language": show.language,
            "image_url": f"{settings.BASE_URL.rstrip('/')}/media/{show.image_url_local}"
            if show.image_url_local
            else None,
            "feed_url": _local_feed_url(show.slug),
            "episode_count": episode_count,
        }
        for show, episode_count in rows
    ]


# ---------------------------------------------------------------------------
# GET /catalog.opml
# ---------------------------------------------------------------------------


@router.get("/catalog.opml")
async def catalog_opml(db: AsyncSession = Depends(get_db)):
    """
    All active shows as OPML with local Arypodr feed URLs.
    Import into AntennaPod to subscribe to every available show in one step.
    """
    result = await db.execute(
        select(Show).where(Show.status == "active").order_by(Show.title)
    )
    shows = result.scalars().all()

    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "Arypodr catalogue"
    body = ET.SubElement(opml, "body")

    for show in shows:
        ET.SubElement(
            body,
            "outline",
            attrib={
                "type": "rss",
                "text": show.title,
                "title": show.title,
                "xmlUrl": _local_feed_url(show.slug),
                "description": show.description or "",
            },
        )

    return Response(
        content=ET.tostring(opml, encoding="unicode", xml_declaration=True),
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# GET /feed/{slug}
# ---------------------------------------------------------------------------


@router.get("/{slug}")
async def show_feed(slug: str, db: AsyncSession = Depends(get_db)):
    """
    RSS feed for a single show, served to AntennaPod.

    Only episodes with status='published' appear here — meaning their audio
    is fully archived locally. AntennaPod receives local enclosure URLs and
    never contacts the original upstream source.
    """
    result = await db.execute(
        select(Show).where(Show.slug == slug, Show.status == "active")
    )
    show = result.scalar_one_or_none()
    if show is None:
        raise HTTPException(status_code=404, detail="Feed not found")

    result = await db.execute(
        select(Episode).where(Episode.show_id == show.id, Episode.status == "published")
    )
    episodes = result.scalars().all()

    xml_str = _build_rss(show, episodes, _local_feed_url(slug))
    return Response(content=xml_str, media_type="application/rss+xml")


# ---------------------------------------------------------------------------
# RSS builder
# ---------------------------------------------------------------------------


def _build_rss(show: Show, episodes: list[Episode], feed_url: str) -> str:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = show.title
    ET.SubElement(channel, "link").text = feed_url
    ET.SubElement(channel, "description").text = show.description or ""

    if show.language:
        ET.SubElement(channel, "language").text = show.language

    if show.author:
        ET.SubElement(channel, f"{{{ITUNES}}}author").text = show.author

    ET.SubElement(
        channel,
        f"{{{ATOM}}}link",
        attrib={
            "href": feed_url,
            "rel": "self",
            "type": "application/rss+xml",
        },
    )

    base_url = settings.BASE_URL.rstrip("/")

    image_url = (
        f"{base_url}/media/{show.image_url_local}" if show.image_url_local else None
    )
    if image_url:
        img = ET.SubElement(channel, "image")
        ET.SubElement(img, "url").text = image_url
        ET.SubElement(img, "title").text = show.title
        ET.SubElement(img, "link").text = feed_url
        ET.SubElement(channel, f"{{{ITUNES}}}image", href=image_url)

    # Sort by pub_date descending (episodes with no pub_date go last).
    sorted_eps = sorted(
        episodes,
        key=lambda e: e.pub_date or datetime.min,
        reverse=True,
    )

    for ep in sorted_eps:
        if not ep.audio_path:
            continue  # audio not yet on disk (shouldn't happen for published, but be safe)

        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = ep.title or ""
        ET.SubElement(item, "guid", isPermaLink="false").text = ep.upstream_guid

        if ep.description:
            ET.SubElement(item, "description").text = ep.description

        if ep.pub_date:
            dt = ep.pub_date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            ET.SubElement(item, "pubDate").text = format_datetime(dt)

        ET.SubElement(
            item,
            "enclosure",
            attrib={
                "url": f"{base_url}/media/{ep.audio_path}",
                "length": str(ep.enclosure_length or 0),
                "type": ep.enclosure_type or "audio/mpeg",
            },
        )

        if ep.duration:
            ET.SubElement(item, f"{{{ITUNES}}}duration").text = ep.duration

        if ep.cover_path:
            ET.SubElement(
                item, f"{{{ITUNES}}}image", href=f"{base_url}/media/{ep.cover_path}"
            )

    return ET.tostring(rss, encoding="unicode", xml_declaration=True)
