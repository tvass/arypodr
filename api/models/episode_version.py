import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base


class EpisodeVersion(Base):
    """
    Immutable snapshot of an episode's metadata at each version.

    One row is written for every successful archive (v1) or re-archive (v2+).

    snapshot  — full ParsedEpisode metadata at time of archiving.
    changes   — {field: {"from": x, "to": y}} for each changed field; null for v1.
    """

    __tablename__ = "episode_versions"
    __table_args__ = (
        UniqueConstraint("episode_id", "version", name="uq_episode_versions_ep_ver"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("episodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Full ParsedEpisode metadata snapshot at time of archiving.
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Computed diff vs previous version; null for v1.
    changes: Mapped[dict | None] = mapped_column(JSON)
    archived_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )

    episode: Mapped["Episode"] = relationship("Episode", back_populates="versions")  # noqa: F821
