"""
Optional webhook support.

Create hooks.yaml alongside your .env to POST episode data to external
services after each episode is downloaded.  If the file doesn't exist,
nothing happens and there is no performance impact.

hooks.yaml example:

  webhooks:
    - url: http://my-service.lan/hook
    - url: http://ntfy.sh/my-topic
      headers:
        Authorization: Bearer secret

Payload sent on each completed episode:

  {
    "show_slug": "le-code-a-change",
    "episode_id": "...",
    "title": "Episode title",
    "local_path": "/podcasts/le-code-a-change/.../audio.mp3",
    "enclosure_url_local": "http://arypodr.lan/media/..."
  }
"""

import asyncio
import logging
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

# Resolved relative to the working directory (project root when launched with
# `uvicorn api.main:app` or via Docker).  Place it next to your .env file.
# Copy hooks.yaml.dist to hooks.yaml to get started.
_HOOKS_FILE = Path("hooks.yaml")


def _load() -> list[dict]:
    """Read hooks.yaml each call so changes take effect without a restart."""
    if not _HOOKS_FILE.exists():
        return []
    try:
        data = yaml.safe_load(_HOOKS_FILE.read_text())
        return (data or {}).get("webhooks", [])
    except Exception as exc:
        logger.warning("Could not load hooks.yaml: %s", exc)
        return []


async def fire(payload: dict) -> None:
    """
    Dispatch all configured webhooks with payload.
    Non-blocking — each POST runs as a background task.
    Failures are logged but never propagated.
    """
    for hook in _load():
        asyncio.create_task(_post(hook, payload))


async def _post(hook: dict, payload: dict) -> None:
    url = hook.get("url")
    if not url:
        return
    headers = hook.get("headers") or {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        logger.info("Hook %s responded %s", url, resp.status_code)
    except Exception as exc:
        logger.warning("Hook %s failed: %s", url, exc)
