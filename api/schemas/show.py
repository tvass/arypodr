from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, HttpUrl

from api.config import settings


class ShowCreate(BaseModel):
    upstream_url: HttpUrl
    slug: str | None = None  # auto-derived from title if not provided
    archive_diff: bool = settings.ARCHIVE_DIFF_DEFAULT


class ShowUpdate(BaseModel):
    status: Literal["active", "paused", "abandoned"] | None = None
    upstream_url: HttpUrl | None = None
    archive_diff: bool | None = None


class ShowResponse(BaseModel):
    id: UUID
    slug: str
    title: str
    description: str | None
    upstream_url: str
    image_url_upstream: str | None
    image_url_local: str | None
    language: str | None
    author: str | None
    status: str
    archive_diff: bool
    episode_count: int
    published_episode_count: int
    created_at: datetime
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)
