import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from api.version import VERSION  # noqa: F401


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )

    DATABASE_URL: str
    BASE_URL: (
        str  # public URL of this Arypodr instance, used to build feed and media URLs
    )
    MEDIA_PATH: str = "/podcasts"  # local directory where audio files are stored
    REFRESH_INTERVAL_HOURS: int = 4  # how often to re-fetch upstream feeds
    MAX_CONCURRENT_SHOWS: int = 3  # how many shows to download in parallel
    INITIAL_EPISODES_PER_SHOW: int = (
        25  # how many episodes to ingest when a show is first added (0 = all)
    )
    MAX_FEED_SIZE_MB: int = 20  # refuse RSS feeds larger than this limit
    ARCHIVE_DIFF_DEFAULT: bool = (
        True  # keep old version folders when episode metadata changes
    )
    USER_AGENT: str = f"Arypodr/{VERSION}"
    HTTP_PROXY: str | None = None
    HTTPS_PROXY: str | None = None
    DEBUG: bool = False


settings = Settings()


def proxy_map() -> dict:
    """Return an httpx mounts dict for use with AsyncClient(mounts=...).
    Returns an empty dict when no proxy is configured."""
    p: dict = {}
    if settings.HTTP_PROXY:
        p["http://"] = httpx.AsyncHTTPTransport(proxy=settings.HTTP_PROXY)
    if settings.HTTPS_PROXY:
        p["https://"] = httpx.AsyncHTTPTransport(proxy=settings.HTTPS_PROXY)
    return p
