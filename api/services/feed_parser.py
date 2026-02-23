import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from pydantic import BaseModel

from api.config import proxy_map, settings


class FeedFetchError(Exception):
    pass


class FeedParseError(Exception):
    pass


class ParsedEpisode(BaseModel):
    upstream_guid: str
    title: str
    description: str | None
    pub_date: datetime | None
    duration: str | None
    enclosure_url: str | None
    enclosure_length: int | None
    enclosure_type: str
    image_url: str | None
    vendor_metadata: dict


class ParsedShow(BaseModel):
    title: str
    description: str | None
    language: str | None
    author: str | None
    image_url: str | None
    new_feed_url: str | None
    episodes: list[ParsedEpisode] = []


def _parse_pub_date(entry) -> datetime | None:
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _episode_image(entry) -> str | None:
    itunes_image = entry.get("image")
    if itunes_image:
        href = itunes_image.get("href")
        if href:
            return href
    media_thumbnail = entry.get("media_thumbnail")
    if media_thumbnail and isinstance(media_thumbnail, list) and media_thumbnail:
        return media_thumbnail[0].get("url")
    return None


def _vendor_metadata(entry) -> dict:
    """Extract podcastRF:* namespace fields from a feed entry."""
    meta: dict = {}
    for key, value in entry.items():
        # feedparser flattens namespaced tags: podcastRF:magnetothequeID → podcastrf_magnetothecaid
        if key.startswith("podcastrf_"):
            clean_key = key[len("podcastrf_") :]
            meta[clean_key] = value
    return meta


def _parse_episodes(feed) -> list[ParsedEpisode]:
    episodes: list[ParsedEpisode] = []
    for entry in feed.entries:
        # GUID: verbatim, never normalized
        guid = entry.get("id") or entry.get("guid")
        if not guid:
            continue

        title = entry.get("title", "").strip()
        if not title:
            continue

        description = entry.get("summary")
        if not description:
            content = entry.get("content", [])
            if content:
                description = content[0].get("value")

        # Enclosure
        enclosure_url = None
        enclosure_length = None
        enclosure_type = "audio/mpeg"
        enclosures = entry.get("enclosures", [])
        if enclosures:
            enc = enclosures[0]
            enclosure_url = enc.get("href") or enc.get("url")
            enclosure_type = enc.get("type", "audio/mpeg")
            raw_len = enc.get("length")
            if raw_len:
                try:
                    enclosure_length = int(raw_len)
                except (ValueError, TypeError):
                    pass

        # Duration: raw string from itunes:duration, stored verbatim
        duration = entry.get("itunes_duration") or entry.get("duration")
        if duration:
            duration = str(duration).strip()

        episodes.append(
            ParsedEpisode(
                upstream_guid=str(guid),
                title=title,
                description=description,
                pub_date=_parse_pub_date(entry),
                duration=duration,
                enclosure_url=enclosure_url,
                enclosure_length=enclosure_length,
                enclosure_type=enclosure_type,
                image_url=_episode_image(entry),
                vendor_metadata=_vendor_metadata(entry),
            )
        )

    # Normalise to newest-first regardless of feed order so that slicing by
    # INITIAL_EPISODES_PER_SHOW always keeps the most recent episodes.
    # Episodes without a pub_date sort last.
    episodes.sort(key=lambda e: (e.pub_date is None, e.pub_date), reverse=True)
    return episodes


async def _parse_feed_content(content: str) -> ParsedShow:
    # feedparser.parse is CPU-bound and synchronous; run it in a thread pool
    # so it doesn't block the event loop while parsing large feeds.
    loop = asyncio.get_running_loop()
    feed = await loop.run_in_executor(None, feedparser.parse, content)

    if feed.get("bozo") and not feed.entries:
        raise FeedParseError(f"Feed parse error: {feed.get('bozo_exception')}")

    ch = feed.feed

    title = ch.get("title", "").strip()
    if not title:
        raise FeedParseError("Feed has no title")

    description = ch.get("subtitle") or ch.get("description") or ch.get("summary")

    image_url = None
    if ch.get("image"):
        image_url = ch["image"].get("href") or ch["image"].get("url")
    if not image_url and ch.get("itunes_image"):
        image_url = ch["itunes_image"].get("href")

    # itunes:new-feed-url at channel level
    new_feed_url = (
        ch.get("itunes_new-feed-url")
        or ch.get("itunes_newfeedurl")
        or ch.get("newFeedUrl")
    )

    author = ch.get("author") or ch.get("itunes_author") or ch.get("publisher")
    language = ch.get("language")

    return ParsedShow(
        title=title,
        description=description,
        language=language,
        author=author,
        image_url=image_url,
        new_feed_url=new_feed_url,
        episodes=_parse_episodes(feed),
    )


async def fetch_and_parse(url: str) -> ParsedShow:
    max_bytes = settings.MAX_FEED_SIZE_MB * 1024 * 1024
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": settings.USER_AGENT},
            mounts=proxy_map(),
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes(65536):
                    received += len(chunk)
                    if received > max_bytes:
                        raise FeedFetchError(
                            f"Feed exceeds {settings.MAX_FEED_SIZE_MB} MB limit"
                        )
                    chunks.append(chunk)
                content = b"".join(chunks).decode(
                    response.encoding or "utf-8", errors="replace"
                )
    except httpx.HTTPError as exc:
        raise FeedFetchError(f"Failed to fetch feed: {exc}") from exc

    return await _parse_feed_content(content)
