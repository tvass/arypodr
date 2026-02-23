#!/usr/bin/env python3
"""
Mock podcast server changes for testing Arypodr versioning.

Episodes:
  ep1  2025-01-15  static — never changes
  ep2  2025-02-15  static — never changes
  ep3  2025-03-15  mutates on every /feed.rss request, cycling through:

    Req  Stage               What changes
    ───────────────────────────────────────────────────────────────────
    1    baseline            initial state
    2    description         description updated          → metadata-only
    3    title               title renamed                → metadata-only
    4    audio URL           enclosure → ep3b.mp3         → re-download
    5    cover added         <itunes:image> appears       → cover download
    6    cover changed       cover URL swaps              → cover re-download
    7    audio + title       enclosure → ep3.mp3, title reset
    (cycles back to 1 on request 8)

Endpoints:
  GET /feed.rss          RSS feed (increments mutation counter)
  GET /ep1.mp3           episode 1 audio
  GET /ep2.mp3           episode 2 audio
  GET /ep3.mp3           episode 3 audio v1 (96 KB)
  GET /ep3b.mp3          episode 3 audio v2 (72 KB)
  GET /cover.jpg         show cover
  GET /cover-ep3-v1.jpg  episode 3 cover v1
  GET /cover-ep3-v2.jpg  episode 3 cover v2
  GET /status            JSON: current request count and ep3 stage info
"""

import json
import os
import random
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = 9000
HOST = "localhost"
BASE_URL = f"http://{HOST}:{PORT}"

MOCK_DIR = Path(__file__).parent.resolve()


# ---------------------------------------------------------------------------
# Generate fake files at startup (idempotent)
# ---------------------------------------------------------------------------


def _make_mp3(path: Path, size: int) -> None:
    if not path.exists():
        path.write_bytes(bytes([0xFF, 0xFB]) + os.urandom(size - 2))
        print(f"Created {path.name} ({size} bytes)")


def _make_jpg(path: Path, seed: int) -> None:
    if not path.exists():
        rng = random.Random(seed)
        path.write_bytes(bytes(rng.getrandbits(8) for _ in range(512)))
        print(f"Created {path.name} (512 bytes)")


_make_mp3(MOCK_DIR / "ep1.mp3", 65_536)  #  64 KB
_make_mp3(MOCK_DIR / "ep2.mp3", 131_072)  # 128 KB
_make_mp3(MOCK_DIR / "ep3.mp3", 98_304)  #  96 KB
_make_mp3(MOCK_DIR / "ep3b.mp3", 73_728)  #  72 KB  ← alternate audio for ep3

_make_jpg(MOCK_DIR / "cover.jpg", seed=1)
_make_jpg(MOCK_DIR / "cover-ep3-v1.jpg", seed=2)
_make_jpg(MOCK_DIR / "cover-ep3-v2.jpg", seed=3)


# ---------------------------------------------------------------------------
# Feed data
# ---------------------------------------------------------------------------

_feed_request_count = 0

STATIC_EPISODES = [
    {
        "guid": "mock-ep-001",
        "title": "Episode 1: Getting Started",
        "pub_date": "Wed, 15 Jan 2025 10:00:00 +0000",
        "mp3": "ep1.mp3",
        "desc": "The first episode. Always the same.",
        "cover": None,
    },
    {
        "guid": "mock-ep-002",
        "title": "Episode 2: Going Deeper",
        "pub_date": "Sat, 15 Feb 2025 10:00:00 +0000",
        "mp3": "ep2.mp3",
        "desc": "The second episode. Also always the same.",
        "cover": None,
    },
]

EP3_GUID = "mock-ep-003"
EP3_PUBDATE = "Sat, 15 Mar 2025 10:00:00 +0000"

# Each tuple: (label, title, description_template, mp3, cover_file | None)
# {n} in description_template is replaced with the request number.
MUTATION_STAGES = [
    (
        "baseline",
        "Episode 3: The Mutating One",
        "Original description.",
        "ep3.mp3",
        None,
    ),
    (
        "description changed",
        "Episode 3: The Mutating One",
        "Description updated on feed request #{n}.",
        "ep3.mp3",
        None,
    ),
    (
        "title changed",
        "Episode 3: Renamed",
        "Title was just renamed on feed request #{n}.",
        "ep3.mp3",
        None,
    ),
    (
        "audio URL changed",
        "Episode 3: Renamed",
        "Audio file swapped to ep3b on feed request #{n}.",
        "ep3b.mp3",
        None,
    ),
    (
        "cover added",
        "Episode 3: Renamed",
        "Cover image appeared on feed request #{n}.",
        "ep3b.mp3",
        "cover-ep3-v1.jpg",
    ),
    (
        "cover changed",
        "Episode 3: Renamed",
        "Cover image swapped on feed request #{n}.",
        "ep3b.mp3",
        "cover-ep3-v2.jpg",
    ),
    (
        "audio + title reset",
        "Episode 3: The Mutating One",
        "Audio and title reset on feed request #{n}.",
        "ep3.mp3",
        "cover-ep3-v2.jpg",
    ),
]


def _ep3_for_request(req_n: int) -> dict:
    idx = (req_n - 1) % len(MUTATION_STAGES)
    label, title, desc_tpl, mp3, cover = MUTATION_STAGES[idx]
    return {
        "guid": EP3_GUID,
        "title": title,
        "pub_date": EP3_PUBDATE,
        "mp3": mp3,
        "desc": desc_tpl.format(n=req_n),
        "cover": cover,
        "stage": idx,
        "label": label,
    }


def _item_xml(ep: dict) -> str:
    mp3_path = MOCK_DIR / ep["mp3"]
    size = mp3_path.stat().st_size
    url = f"{BASE_URL}/{ep['mp3']}"
    cover_tag = ""
    if ep.get("cover"):
        cover_tag = f'\n      <itunes:image href="{BASE_URL}/{ep["cover"]}"/>'
    return f"""
    <item>
      <title>{ep["title"]}</title>
      <guid isPermaLink="false">{ep["guid"]}</guid>
      <pubDate>{ep["pub_date"]}</pubDate>
      <description><![CDATA[{ep["desc"]}]]></description>
      <enclosure url="{url}" length="{size}" type="audio/mpeg"/>{cover_tag}
    </item>"""


def _build_feed(req_n: int) -> bytes:
    ep3 = _ep3_for_request(req_n)
    print(f"  [feed] req #{req_n} — ep3 stage {ep3['stage']}: {ep3['label']!r}")

    items = "".join(_item_xml(ep) for ep in STATIC_EPISODES)
    items += _item_xml(ep3)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Mock Podcast</title>
    <link>{BASE_URL}</link>
    <description>A mock podcast for testing Arypodr versioning.</description>
    <language>en</language>
    <itunes:image href="{BASE_URL}/cover.jpg"/>
    {items}
  </channel>
</rss>"""
    return xml.encode("utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".jpg": "image/jpeg",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()}  {fmt % args}")

    def do_GET(self):
        global _feed_request_count
        path = urlparse(self.path).path  # strip query string

        if path == "/feed.rss":
            _feed_request_count += 1
            self._send(
                200,
                "application/rss+xml; charset=utf-8",
                _build_feed(_feed_request_count),
            )

        elif path == "/status":
            if _feed_request_count:
                ep3 = _ep3_for_request(_feed_request_count)
                next_idx = (ep3["stage"] + 1) % len(MUTATION_STAGES)
                payload = {
                    "feed_requests": _feed_request_count,
                    "ep3_stage": ep3["stage"],
                    "ep3_label": ep3["label"],
                    "ep3_title": ep3["title"],
                    "ep3_mp3": ep3["mp3"],
                    "ep3_cover": ep3["cover"],
                    "next_stage": next_idx,
                    "next_label": MUTATION_STAGES[next_idx][0],
                }
            else:
                payload = {"feed_requests": 0, "note": "no feed requests yet"}
            self._send(200, "application/json", json.dumps(payload, indent=2).encode())

        else:
            name = path.lstrip("/")
            file_path = (MOCK_DIR / name).resolve()
            ct = _CONTENT_TYPES.get(file_path.suffix)
            # Serve only files inside MOCK_DIR (no path traversal)
            if ct and file_path.parent == MOCK_DIR and file_path.exists():
                self._send(200, ct, file_path.read_bytes())
            else:
                self._send(404, "text/plain", b"not found")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Mock podcast server  →  {BASE_URL}")
    print(f"Feed URL             →  {BASE_URL}/feed.rss")
    print(f"Status               →  {BASE_URL}/status")
    print()
    print("ep3 mutation cycle (one stage per /feed.rss request):")
    for i, (label, *_) in enumerate(MUTATION_STAGES):
        print(f"  stage {i}  {label}")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
