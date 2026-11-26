"""Creator-published CC0 music catalogs, with on-demand MP3 caching."""
import json
import math
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
KEYNATA = "https://carf-coder.github.io/keynata-commons/"
_KEYNATA_INDEX = KEYNATA + "release/index.json"
_KEYNATA_GENRES = {
    "classical_duo": ("Classical Duo", "Classical"),
    "classical_piano": ("Classical Piano", "Classical"),
    "classical_quartet": ("Classical Quartet", "Classical"),
    "jpop_3piece": ("J-Pop Three-Piece", "J-Pop"),
    "karaoke": ("Karaoke Backing", "Karaoke"),
    "rock_intense": ("Rock Intense", "Rock"),
    "rock_standard": ("Rock Standard", "Rock"),
}
_keynata_lock = threading.Lock()


def _keynata_rows(data):
    license_info = data.get("license") if isinstance(data, dict) else None
    if (not isinstance(license_info, dict) or license_info.get("generated_content") != "CC0-1.0"
            or license_info.get("url") != LICENSE or not isinstance(data.get("tracks"), list)):
        raise ValueError("Keynata did not provide a CC0 music catalog.")
    rows = {}
    for row in data["tracks"][:2000]:
        if not isinstance(row, dict) or row.get("screening_passed") is not True:
            continue
        slug, category = row.get("track_id"), row.get("category")
        if (not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9_]{3,80}", slug)
                or not isinstance(category, str) or category not in _KEYNATA_GENRES):
            continue
        filename = slug + ("_karaoke.mp3" if category == "karaoke" else ".mp3")
        files = row.get("files")
        if not isinstance(files, list) or filename not in files:
            continue
        try:
            bpm = float(row["bpm"])
            seconds = float(row["duration_seconds"])
        except (TypeError, KeyError, ValueError, OverflowError):
            continue
        if not (math.isfinite(bpm) and 1 <= bpm <= 300 and 1 <= seconds <= 600):
            continue
        label, genre = _KEYNATA_GENRES[category]
        seed = slug.rsplit("_", 1)[-1]
        rows[f"keynata-{slug}"] = {
            "slug": slug, "file": filename, "category": category, "title": f"{label} No. {seed}", "mood": label,
            "genre": genre, "bpm": round(bpm), "seconds": round(seconds),
        }
    if not rows:
        raise ValueError("Keynata returned no supported CC0 tracks.")
    return rows


def _read_keynata():
    path = config.DATA_DIR / "music" / "keynata-index.json"
    bundled = Path(__file__).with_name("keynata_music_catalog.json")
    initial = _keynata_rows(json.loads(bundled.read_text(encoding="utf-8")))
    try:
        initial.update(_keynata_rows(json.loads(path.read_bytes())))
    except (OSError, ValueError):
        pass
    return initial


KEYNATA_TRACKS = _read_keynata()


def refresh_keynata():
    """Refresh from the publisher's JSON API. Preserve the bundled catalog when offline."""
    if not _keynata_lock.acquire(blocking=False):
        raise RuntimeError("The music catalog is already refreshing.")
    try:
        with httpx.stream("GET", _KEYNATA_INDEX, headers=HEADERS, timeout=10, follow_redirects=False) as response:
            response.raise_for_status()
            payload = bytearray()
            deadline = time.monotonic() + 15
            for chunk in response.iter_bytes(chunk_size=65536):
                payload.extend(chunk)
                if len(payload) > 2 * 1024 * 1024 or time.monotonic() > deadline:
                    raise ValueError("The catalog exceeded its size or time limit.")
        rows = _keynata_rows(json.loads(payload))
        # Keep IDs that existing styles and clips may have saved from earlier catalog refreshes.
        known = KEYNATA_TRACKS | rows
        snapshot = {"license": {"generated_content": "CC0-1.0", "url": LICENSE}, "tracks": [
            {"track_id": row["slug"], "category": row["category"], "files": [row["file"]],
             "screening_passed": True, "bpm": row["bpm"], "duration_seconds": row["seconds"]}
            for row in known.values()
        ]}
        folder = config.DATA_DIR / "music"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder, prefix="catalog-") as tmp:
            draft = Path(tmp) / "index.json"
            draft.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
            draft.replace(folder / "keynata-index.json")
        KEYNATA_TRACKS.update(rows)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise RuntimeError("Couldn’t refresh Keynata Commons. The existing music catalog is still available.") from exc
    finally:
        _keynata_lock.release()


_locks = {}
_file_lock = threading.Lock()
_downloads = threading.BoundedSemaphore(3)


def has_track(track):
    return track in TRACKS or track in KEYNATA_TRACKS


def catalog():
    return [
        {"id": key, "title": row["title"], "genre": row["genre"], "mood": row["mood"],
         "bpm": row["bpm"], "seconds": row["seconds"], "artist": "Kaino", "license": "CC0 1.0",
         "license_url": LICENSE, "source": f"{SITE}/library/music?page={row['page']}#asset-{row['slug']}"}
        for key, row in TRACKS.items()
    ] + [
        {"id": key, "title": row["title"], "genre": row["genre"], "mood": row["mood"],
         "bpm": row["bpm"], "seconds": row["seconds"], "artist": "Keynata Commons", "license": "CC0 1.0",
         "license_url": LICENSE, "source": KEYNATA + "#tracks", "estimated_length": True}
        for key, row in KEYNATA_TRACKS.copy().items()
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
            _stream_mp3(client, location, target, expires)
    except (httpx.HTTPError, ValueError) as exc:
        # Signed provider URLs and raw response bodies do not belong in client errors.
        raise RuntimeError("PeakCut couldn't download this track from Kaino. Try again in a moment.") from exc


def _stream_mp3(client, url, target, expires):
    with client.stream("GET", url) as audio:
        audio.raise_for_status()
        if audio.status_code != 200:
            raise RuntimeError("The catalog did not return the complete track.")
        if audio.headers.get("content-type", "").split(";", 1)[0].lower() not in ("audio/mpeg", "audio/mp3"):
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


def _download_keynata(row, target):
    url = f"{KEYNATA}release/tracks/{row['slug']}/{row['file']}"
    try:
        with httpx.Client(timeout=15, headers=HEADERS, follow_redirects=False) as client:
            _stream_mp3(client, url, target, time.monotonic() + 40)
    except httpx.HTTPError as exc:
        raise RuntimeError("PeakCut couldn’t download this track from Keynata Commons.") from exc


def file(track):
    keynata = track in KEYNATA_TRACKS
    row = KEYNATA_TRACKS.get(track) if keynata else TRACKS.get(track)
    if row is None:
        raise ValueError("That music track is unavailable.")
    folder = config.DATA_DIR / "music"
    path = folder / f"{track}.mp3"
    if path.is_file():
        return path
    with _file_lock:
        lock = _locks.setdefault(track, threading.Lock())
    with lock:
        if path.is_file():
            return path
        if not _downloads.acquire(timeout=3):
            raise RuntimeError("Other tracks are downloading. Try again in a moment.")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=folder, prefix="music-") as tmp:
                draft = Path(tmp) / "download.mp3"
                if keynata:
                    _download_keynata(row, draft)
                else:
                    _download(row["slug"], draft)
                draft.replace(path)
        finally:
            _downloads.release()
    return path
