"""The creator-published Kaino CC0 instrumentals, with on-demand audio caching."""
import json
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config

SITE = "https://kaino.dev"
LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
STORAGE_HOST = "a3abcd2451736f6a3867db58e7c8ec05.r2.cloudflarestorage.com"
MAX_BYTES = 16 * 1024 * 1024
HEADERS = {"User-Agent": "PeakCut music library (https://github.com/haziqahmedqasmi/PeakCut)"}
TRACKS = {
    f"kaino-{row['slug']}": row
    for row in json.loads(Path(__file__).with_name("music_catalog.json").read_text(encoding="utf-8"))
}
_locks = {key: threading.Lock() for key in TRACKS}
_downloads = threading.BoundedSemaphore(3)


def catalog():
    return [
        {"id": key, "title": row["title"], "genre": row["genre"], "mood": row["mood"],
         "bpm": row["bpm"], "seconds": row["seconds"], "artist": "Kaino", "license": "CC0 1.0",
         "license_url": LICENSE, "source": f"{SITE}/library/music?page={row['page']}#asset-{row['slug']}"}
        for key, row in TRACKS.items()
    ]


def _download(slug, target):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", slug):
        raise ValueError("Invalid music catalog ID.")
    expires = time.monotonic() + 40
    try:
        with httpx.Client(timeout=15, headers=HEADERS, follow_redirects=False) as client:
            response = client.get(f"{SITE}/api/library/music/{slug}/download", params={"format": "mp3"})
            if not response.is_redirect:
                raise RuntimeError("The creator's download service did not provide this track.")
            location = response.headers.get("location", "")
            address = urlsplit(location)
            if (address.scheme != "https" or address.netloc != STORAGE_HOST
                    or address.path != f"/kaino-library/music/{slug}.mp3"):
                raise RuntimeError("The creator's download service returned an unexpected address.")
            with client.stream("GET", location) as audio:
                audio.raise_for_status()
                if audio.headers.get("content-type", "").split(";", 1)[0] != "audio/mpeg":
                    raise RuntimeError("The catalog did not return an MP3.")
                size = 0
                with target.open("wb") as file:
                    for block in audio.iter_bytes(chunk_size=65536):
                        size += len(block)
                        if size > MAX_BYTES or time.monotonic() > expires:
                            raise RuntimeError("The music download exceeded its size or time limit.")
                        if size == len(block) and not (
                            block.startswith(b"ID3") or len(block) >= 2 and block[0] == 0xff and block[1] & 0xe0 == 0xe0
                        ):
                            raise RuntimeError("The downloaded file is not an MP3.")
                        file.write(block)
                if size < 512:
                    raise RuntimeError("The catalog returned an empty track.")
    except (httpx.HTTPError, ValueError) as exc:
        # Signed provider URLs and raw response bodies do not belong in client errors.
        raise RuntimeError("PeakCut couldn't download this track from Kaino. Try again in a moment.") from exc


def file(track):
    row = TRACKS.get(track)
    if row is None:
        raise ValueError("That music track is unavailable.")
    folder = config.DATA_DIR / "music"
    path = folder / f"{track}.mp3"
    if path.is_file():
        return path
    with _locks[track]:
        if path.is_file():
            return path
        if not _downloads.acquire(timeout=3):
            raise RuntimeError("Other tracks are downloading. Try again in a moment.")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=folder, prefix="music-") as tmp:
                draft = Path(tmp) / "download.mp3"
                _download(row["slug"], draft)
                draft.replace(path)
        finally:
            _downloads.release()
    return path
