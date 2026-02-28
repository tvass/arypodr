import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, String, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.database import Base

ShowStatus = Enum("active", "paused", "abandoned", name="show_status")


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    upstream_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    upstream_url_history: Mapped[list] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'")
    )
    image_url_upstream: Mapped[str | None] = mapped_column(String(2048))
    image_url_local: Mapped[str | None] = mapped_column(String(2048))
    language: Mapped[str | None] = mapped_column(String(10))
    author: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        ShowStatus, nullable=False, server_default=text("'active'")
    )
    # When True, old version folders are kept when upstream metadata changes.
    # When False (default), only the latest version is retained on disk.
    archive_diff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())

    episodes: Mapped[list["Episode"]] = relationship(  # noqa: F821
        "Episode", back_populates="show", cascade="all, delete-orphan"
    )

    def apply_feed_redirect(self, new_url: str) -> None:
        """Record a feed URL change: push current URL into history, set new URL."""
        if new_url == self.upstream_url:
            return
        self.upstream_url_history = list(self.upstream_url_history or []) + [
            self.upstream_url
        ]
        self.upstream_url = new_url
