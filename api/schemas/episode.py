from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class EpisodeVersionResponse(BaseModel):
    version: int
    snapshot: dict
    changes: dict | None
    archived_at: datetime


class EpisodeResponse(BaseModel):
    id: UUID
    show_id: UUID
    upstream_guid: str
    item_number: int
    episode_version: int
    status: str
    # Current/latest metadata — from DB columns for published, from pending_metadata otherwise.
    title: str | None = None
    description: str | None = None
    pub_date: datetime | None = None
    duration: str | None = None
    enclosure_url_upstream: str | None = None
    enclosure_length: int | None = None
    enclosure_type: str | None = None
    image_url_upstream: str | None = None
    vendor_metadata: dict = {}
    # Local URLs for published episodes.
    enclosure_url_local: str | None = None
    image_url_local: str | None = None
    processing_error: str | None = None
    created_at: datetime
