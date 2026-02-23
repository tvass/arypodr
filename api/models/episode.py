import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base

EpisodeStatus = Enum(
    "pending",
    "downloading",
    "published",
    "failed",
    name="episode_status",
)


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        UniqueConstraint("show_id", "upstream_guid", name="uq_episodes_show_guid"),
        UniqueConstraint("show_id", "item_number", name="uq_episodes_show_number"),
        Index("ix_episodes_show_status_num", "show_id", "status", "item_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    show_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shows.id", ondelete="CASCADE"), nullable=False
    )
    upstream_guid: Mapped[str] = mapped_column(String(500), nullable=False)
    # Per-show sequential number — used as the folder name on disk (item-0042/).
    item_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Incremented each time upstream metadata changes for an already-archived episode.
    episode_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(
        EpisodeStatus, nullable=False, server_default=text("'pending'")
    )
    # Publication date from the feed — used to filter out old episodes on refresh.
    pub_date: Mapped[datetime | None] = mapped_column(DateTime)
    # Upstream audio URL — the worker needs this to download.
    enclosure_url_upstream: Mapped[str | None] = mapped_column(String(2048))
    # Full ParsedEpisode metadata held while pending; cleared to null after archiving.
    pending_metadata: Mapped[dict | None] = mapped_column(JSON)
    # Diff payload set by ingestion on version bumps; cleared after archiving.
    pending_changes: Mapped[dict | None] = mapped_column(JSON)
    # Current/latest episode metadata — populated on first archive, updated on re-archive.
    title: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[str | None] = mapped_column(String(50))
    enclosure_length: Mapped[int | None] = mapped_column(Integer)
    enclosure_type: Mapped[str | None] = mapped_column(String(100))
    vendor_metadata: Mapped[dict | None] = mapped_column(JSON)
    # Relative paths from MEDIA_PATH, e.g. "show-slug/item-0042/audio.mp3".
    # Set on first archive; audio_path updates if audio is replaced.
    audio_path: Mapped[str | None] = mapped_column(String(500))
    cover_path: Mapped[str | None] = mapped_column(String(500))
    processing_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    show: Mapped["Show"] = relationship("Show", back_populates="episodes")  # noqa: F821
    versions: Mapped[list["EpisodeVersion"]] = relationship(  # noqa: F821
        "EpisodeVersion",
        back_populates="episode",
        cascade="all, delete-orphan",
        order_by="EpisodeVersion.version",
    )
