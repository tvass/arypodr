# AryPodr
AryPodr [*pronounced Harry Podder*] Handles your Podcasts with magic 💫

**A**utomatically **R**eplicates **Y**our **Pod**cast **R**SS.

![logo](arypodr.png)

AryPodr mirrors your subscriptions locally and generates an OPML file where every feed points to your own server. Keep your player (e.g., AntennaPod) and just swap in your list. Simple as that.

Use cases:
- Serve subscriptions from a local URL (Reduce dependence on upstream servers).
- Serve subscriptions from a custom URL (Bypass basic URL/DNS filtering).
- Serve subscriptions if removed upstream.
- Archive with versions history for metadata and media
- Server-side automation (Trigger custom pipelines on new episodes. e.g., transcription, transcoding, ...).
- Full local ownership – keep complete control of your podcasts.

Limitations & Opinionated Rules:
- Single-user.
- No accounts – zero sign-ups or logins.
- No subscription management – nothing to track or sync.
- All-or-nothing – you get everything AryPodr has archived, and nothing more.

## How it works

```
1. Export OPML from your podcast app (AntennaPod, Pocket Casts, etc.)
        ↓
2. Import into AryPodr  →  shows and episodes are registered
        ↓
3. AryPodr fetches upstream feeds and archives audio + artwork locally
        ↓
4. Export the AryPodr OPML  →  all feeds now point to your server
        ↓
5. Update your player with the new OPML to replace your subscriptions
```
New shows can be added at any time via the API — see [API Reference](#api-reference).


## Quick Start

```bash
cp .env.example .env          # set BASE_URL to your server's address
pip install -r requirements.txt
uvicorn api.main:app --host 0.0.0.0 --port 8000 --log-level info
curl http://localhost:8000/healthz
```

## Typical workflow

**Step 1 — import your existing subscriptions:**
```bash
# Export subscriptions.opml from your podcast app, then:
curl -X POST http://localhost:8000/admin/import/opml \
  -F "file=@subscriptions.opml"
# Returns 202 immediately — feeds are fetched in the background.
```

**Step 2 — get the local OPML and import it into AntennaPod:**
```bash
curl http://localhost:8000/feed/catalog.opml > arypodr.opml
# AntennaPod → Backup → Import OPML → arypodr.opml
```

All feed URLs in `arypodr.opml` point to your AryPodr instance, not the internet.

---

## Background worker

AryPodr runs a background worker inside the same process. It:

1. Downloads **show artwork** first — small files, appear quickly in your podcast app
2. Downloads **episode audio** — newest episodes first, up to `MAX_CONCURRENT_SHOWS` shows in parallel
   - Episode artwork is also saved if it differs from the show artwork
3. **Refreshes upstream feeds** every `REFRESH_INTERVAL_HOURS` to pick up new episodes

The worker is designed for air-gapped clients: feeds and media served to AntennaPod
contain **only local URLs** — no upstream links are ever exposed. Images and audio
are omitted from the feed until they are fully archived locally.

Files are stored under `MEDIA_PATH` with a human-readable layout:

```
{MEDIA_PATH}/
  {show-slug}/
    cover.jpg                      ← show artwork
    item-0001/
      audio.mp3                    ← current episode audio (.mp3, .m4a, .ogg, .opus, .aac, .flac)
      audio-change-1.mp3           ← previous audio (kept when archive_diff=true)
      cover.jpg                    ← episode artwork (only if distinct from show)
    item-0002/
      audio.mp3
```

Folders are named `item-{N}` — a per-show **archive sequence number**, not the podcast's
own episode numbering. Numbers are assigned at ingestion time sorted oldest-first and are
stable: the same episode always maps to the same slot.

When upstream audio changes for an already-archived episode, `audio.mp3` is the current
file and the previous one is renamed to `audio-change-{N}.mp3`. Old files are kept only
when `archive_diff` is enabled on the show; otherwise the old file is replaced in-place.
Metadata-only changes (title, description, …) never touch files on disk — they are
recorded as a new entry in the version history table in the DB.

Audio is served to AntennaPod at `/media/...` directly from disk (FastAPI StaticFiles).

### File safety

- Downloads use a `.tmp` → rename pattern: a file at its final path is always complete.
- On startup, any leftover `.tmp` files from a previous crash are deleted automatically.
- If a file already exists on disk (e.g. after a DB reset), the download is skipped — no re-download, no duplicate.

### Suspend / resume

The worker can be paused without stopping the API:

```bash
# Toggle: running → suspended, or suspended → running
curl -X POST http://localhost:8000/admin/suspend

# Check current state
curl http://localhost:8000/admin/suspend
# {"suspended": true, "active_downloads": 2}
```

When suspended, no new downloads or feed refreshes start.
Downloads already in progress finish naturally — no partial files.
Feeds and media continue to be served normally.

---

## Webhooks (optional)

After each episode is fully archived, Arypodr can POST a JSON payload to
one or more webhook URLs. This is useful for triggering post-processing
(e.g. transcription via Whisper).

Copy the template and fill in your URLs:

```bash
cp hooks.yaml.dist hooks.yaml
```

`hooks.yaml` is gitignored. AryPodr reloads it on every episode — no restart needed.

Payload sent to each webhook:

```json
{
  "show_slug":           "le-code-a-change",
  "episode_id":          "3f2a1b...",
  "title":               "Le numérique en milieu rural",
  "local_path":          "/podcasts/le-code-a-change/2024-01-15-.../audio.mp3",
  "enclosure_url_local": "http://arypodr.home.arpa/media/..."
}
```

See [hooks.yaml.dist](hooks.yaml.dist) for a full example.

---

## API Reference

### Shows

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/shows` | Register a show from an upstream RSS URL |
| `GET` | `/admin/shows` | List shows (`?status=active`, `?search=title`) |
| `GET` | `/admin/shows/{slug}` | Get a single show |
| `PUT` | `/admin/shows/{slug}` | Update status, upstream URL, or `archive_diff` |
| `DELETE` | `/admin/shows/{slug}` | Soft-delete (`?keep_archive=true`) or hard-delete |
| `POST` | `/admin/shows/{slug}/refresh` | Re-fetch upstream RSS, add new episodes — returns 202, runs in background |

### Episodes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin/shows/{slug}/episodes` | List episodes (`?status=pending`, paginated) |
| `GET` | `/admin/shows/{slug}/episodes/{guid}` | Get a single episode |
| `GET` | `/admin/shows/{slug}/episodes/{guid}/versions` | Full version history — snapshots + diffs |

### OPML

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/import/opml` | Bulk-register shows from an OPML export — returns 202, runs in background |
| `GET` | `/admin/export/opml` | Export upstream URLs — for migration or backup, **not** for AntennaPod |
| `GET` | `/feed/catalog.opml` | Export local URLs — import this into AntennaPod |

### Feeds

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/feed/catalog` | JSON list of all shows with local feed URLs |
| `GET` | `/feed/{slug}` | RSS feed for a single show, served to AntennaPod |

### Worker

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/suspend` | Toggle worker on/off (running downloads finish naturally) |
| `GET` | `/admin/suspend` | Current state: `{"suspended": bool, "active_downloads": int}` |

### Media

| Path | Description |
|------|-------------|
| `/media/{show-slug}/cover.jpg` | Show artwork |
| `/media/{show-slug}/{episode-folder}/audio.{ext}` | Episode audio — extension matches the feed's MIME type (`.mp3`, `.m4a`, `.ogg`, `.opus`, `.aac`, `.flac`) |
| `/media/{show-slug}/{episode-folder}/cover.jpg` | Episode artwork (if distinct) |

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | `{"status": "ok"}` |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | _(required)_ | SQLite path, e.g. `sqlite+aiosqlite:///./arypodr.db` |
| `BASE_URL` | _(required)_ | Public URL of this instance, e.g. `http://192.168.1.10:8000` — used to build all local URLs |
| `MEDIA_PATH` | `/podcasts` | Local directory where audio and artwork are stored |
| `REFRESH_INTERVAL_HOURS` | `4` | How often the worker re-fetches upstream feeds to pick up new episodes |
| `MAX_CONCURRENT_SHOWS` | `3` | Number of shows downloading in parallel |
| `INITIAL_EPISODES_PER_SHOW` | `25` | How many of the most recent episodes to ingest when a show is **first added** (0 = all) — subsequent refreshes only pick up episodes newer than the most recent one already stored, so old excluded episodes are never back-filled |
| `MAX_FEED_SIZE_MB` | `20` | Refuse RSS XML feeds larger than this (audio downloads are unlimited) |
| `ARCHIVE_DIFF_DEFAULT` | `true` | Default `archive_diff` value for newly added shows — when `true`, replaced audio/cover files are renamed and kept on disk; when `false`, they are replaced in-place. Version history is always recorded in the DB regardless of this setting |
| `USER_AGENT` | `Arypodr/{VERSION}` | `User-Agent` header sent with all outgoing HTTP requests (feed fetches, audio and image downloads) |
| `HTTP_PROXY` | _(unset)_ | Proxy URL for plain-HTTP requests, e.g. `http://proxy.lan:8080` |
| `HTTPS_PROXY` | _(unset)_ | Proxy URL for HTTPS requests, e.g. `http://proxy.lan:8080` |
| `DEBUG` | `false` | Enables SQLAlchemy query logging |

See [.env.example](.env.example).

---

## Deployment

### Docker Compose

```bash
cp .env.example .env
docker compose -f docker/docker-compose.yml up -d
```

### Kubernetes (Helm)

```bash
# 1. Copy and edit values
cp helm/values.yaml my-values.yaml   # set baseUrl, image.repository, etc.
helm install arypodr ./helm -f my-values.yaml
```

The deployment is locked to `replicas: 1` — SQLite does not support concurrent
writers and the background worker runs inside the same process.

---

## Project Structure

```
api/
  main.py            # FastAPI app + lifespan (DB init, worker startup, clean shutdown)
  config.py          # Settings (env vars via pydantic-settings)
  database.py        # Async SQLAlchemy engine + session
  version.py         # Single source of truth for the version string
  media_types.py     # Content-type → file extension maps
  models/            # ORM models: show, episode, episode_version
  routers/
    admin.py         # Show/episode CRUD, OPML import/export, worker suspend
    feed.py          # Public catalog + per-show RSS feeds (local URLs only)
  schemas/           # Pydantic request/response schemas
  services/
    archive.py       # Folder-path helper (shared by all)
    feed_parser.py   # Fetch + parse upstream RSS feeds (streaming, size-limited)
    ingestion.py     # Deduplication + episode insertion (shared by admin + worker)
    worker.py        # Background download loop (covers → audio → refresh)
    hooks.py         # Optional webhook dispatch after each download
docker/              # Dockerfile + docker-compose.yml
kubernetes/          # Kubernetes manifests (ConfigMap, Secret, PVCs, Deployment, Service)
hooks.yaml.dist      # Webhook config template (copy to hooks.yaml)
```

---

## Objects

### Show

| Field | Description |
|-------|-------------|
| `slug` | URL-safe identifier derived from title (e.g. `le-code-a-change`) |
| `status` | `active` · `paused` · `abandoned` |
| `upstream_url` | Current upstream RSS URL (updated when feed redirects) |
| `archive_diff` | If `true`, replaced audio/cover files are renamed (`audio-change-N.mp3`) and kept on disk. Version history is always recorded in DB regardless of this flag — overridable per show via `PUT /admin/shows/{slug}` |
| `episode_count` | Total episodes ingested |
| `published_episode_count` | Episodes with local audio archived |

### Episode

| Field | Description |
|-------|-------------|
| `upstream_guid` | Verbatim GUID from the upstream feed |
| `item_number` | Per-show archive sequence number (oldest = 1, monotonically increasing) — maps to the `item-{N}` folder on disk |
| `episode_version` | Incremented each time upstream metadata changes for an already-archived episode |
| `status` | `pending` · `downloading` · `published` · `failed` |
| `pub_date` | Publication date from the feed — stored in the DB to filter out old episodes on refresh |
| `enclosure_url_upstream` | Original audio URL used to download the file |
| `enclosure_url_local` | Local URL served to AntennaPod, built from `audio_path` |
| `title`, `description`, `duration`, … | Held in `pending_metadata` until archived; then stored as DB columns and in the `episode_versions` snapshot |
