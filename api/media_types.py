# ---------------------------------------------------------------------------
# Media-type → file extension maps
# Used to determine the file extension when saving downloaded media based on
# the Content-Type header returned by the upstream server.
# ---------------------------------------------------------------------------

AUDIO_CONTENT_TYPES: dict[str, str] = {
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}

IMAGE_CONTENT_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
