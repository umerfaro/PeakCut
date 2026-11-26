"""Music defaults and changes to old exports, using real FFmpeg audio and video streams."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, ".")
with tempfile.TemporaryDirectory(prefix="pc-test-music-") as tmp:
    os.environ.update(PEAKCUT_DATA=tmp, PEAKCUT_ACCOUNTS="0", DATABASE_URL="", PEAKCUT_UPDATE_CHECK="0")
    from studio import auth, ctx, db, jobs, media, music, music_library, render, server, templates

    db.init()
    owner = auth.local_user()
    base = "http://127.0.0.1:8766"
    client = TestClient(server.app, base_url=base)
    origin = {"Origin": base}

    def check(name, ok):
        assert ok, name
        print("PASS", name)

    def streams(path):
        return media.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0",
                          "-c", "copy", "-f", "streamhash", "-"])

    catalog = client.get("/api/music").json()
    check("there are searchable original, licensed instrumentals",
          len(catalog) >= 6 and all(t["license"] == "CC0 1.0" and t["mood"] and t["genre"] for t in catalog))
    track = client.get("/api/music/neon-run/audio", headers={"Range": "bytes=0-63"})
    check("the selected track can be streamed", track.status_code == 206 and track.content.startswith(b"RIFF"))
    check("unknown track IDs are refused", client.get("/api/music/unknown/audio").status_code == 404)
    library = [t for t in catalog if t["artist"] == "Kaino"]
    check("the full creator catalog includes its genre and license information",
          len(library) == 492 and len({t["genre"] for t in library}) == 57
          and all(t["source"].startswith("https://kaino.dev/library/music") for t in library))
    keynata = [t for t in catalog if t["artist"] == "Keynata Commons"]
    check("Keynata adds a second 203-track CC0 catalog with karaoke backing",
          len(catalog) == 703 and len(keynata) == 203
          and all(t["license"] == "CC0 1.0" for t in keynata)
          and any(t["genre"] == "Karaoke" and "Backing" in t["title"] for t in keynata))
    remote_id = library[0]["id"]
    slug = music_library.TRACKS[remote_id]["slug"]
    source_mp3 = Path(tmp) / "test.mp3"
    media.run(["ffmpeg", "-y", "-v", "error", "-i", str(music.track_file("side-quest")), str(source_mp3)])
    requests = []

    def deliver(request):
        requests.append(request)
        if request.url.host == "kaino.dev":
            target = f"https://{music_library.STORAGE_HOST}/kaino-library/music/{slug}.mp3?signature=test"
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=source_mp3.read_bytes())

    upstream = httpx.Client(transport=httpx.MockTransport(deliver))
    with patch.object(music_library.httpx, "Client", return_value=upstream):
        response = client.get(f"/api/music/{remote_id}/audio", headers={"Range": "bytes=0-63"})
    check("catalog audio downloads through the approved host once and supports seeking",
          response.status_code == 206 and len(requests) == 2 and music.track_file(remote_id).is_file())
    check("cached audio remains playable without the upstream client", client.get(f"/api/music/{remote_id}/audio").is_success)

    def redirect_elsewhere(request):
        assert request.url.host == "kaino.dev"
        return httpx.Response(302, headers={"Location": "https://example.org/unexpected.mp3"})

    upstream = httpx.Client(transport=httpx.MockTransport(redirect_elsewhere))
    with patch.object(music_library.httpx, "Client", return_value=upstream):
        check("unexpected download destinations are refused",
              client.get(f"/api/music/{library[1]['id']}/audio").status_code == 502)

    neon = music.clean({"track": "neon-run", "volume": 18, "original": 90, "duck": True})
    fresh = {
        "license": {"generated_content": "CC0-1.0", "url": music_library.LICENSE},
        "tracks": [
            {"track_id": "rock_standard_987654", "category": "rock_standard", "files": ["rock_standard_987654.mp3"],
             "bpm": 110, "duration_seconds": 26.6, "screening_passed": True},
            {"track_id": "../bad", "category": "rock_standard", "files": ["../bad.mp3"],
             "bpm": 110, "duration_seconds": 26.6, "screening_passed": True},
        ],
    }

    def keynata_index(request):
        assert str(request.url) == music_library.KEYNATA + "release/index.json"
        return httpx.Response(200, json=fresh)

    with httpx.Client(transport=httpx.MockTransport(keynata_index)) as upstream:
        with patch.object(music_library.httpx, "stream", side_effect=upstream.stream):
            refreshed = client.post("/api/music/refresh", headers=origin)
            check("the public JSON API refresh adds eligible tracks and ignores invalid file paths",
                  refreshed.is_success and len(refreshed.json()) == 704 and len(music_library._read_keynata()) == 204)
            fresh["license"]["generated_content"] = "CC-BY-NC-4.0"
            denied = client.post("/api/music/refresh", headers=origin)
            check("non-CC0 catalog data leaves the previously available library intact",
                  denied.status_code == 502 and len(client.get("/api/music").json()) == 704)

    def keynata_mp3(request):
        assert str(request.url) == music_library.KEYNATA + "release/tracks/rock_standard_987654/rock_standard_987654.mp3"
        return httpx.Response(200, headers={"Content-Type": "audio/mp3"}, content=source_mp3.read_bytes())

    keynata_id = "keynata-rock_standard_987654"
    upstream = httpx.Client(transport=httpx.MockTransport(keynata_mp3))
    with patch.object(music_library.httpx, "Client", return_value=upstream):
        response = client.get(f"/api/music/{keynata_id}/audio", headers={"Range": "bytes=0-63"})
    check("Keynata MP3s are cached and served with range support", response.status_code == 206
          and music.track_file(keynata_id).read_bytes() == source_mp3.read_bytes())
    with ctx.as_user(owner.id):
        style = templates.add({"name": "Gaming music", "look": {"music": neon}})
        _, look = templates.pick(style["id"])
        check("styles persist track, volume, original audio level, and ducking", look["music"] == neon)
        job = jobs.create({"type": "upload", "name": "Old clip.mp4"}, {}, enqueue=False)
        job["status"] = "done"
        folder = Path(job["dir"]) / "clips"
        folder.mkdir(exist_ok=True)
        video = folder / "clip01.mp4"
        media.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=96x160:r=24:d=4",
                   "-f", "lavfi", "-i", "sine=f=400:d=4", "-c:v", "libx264", "-c:a", "aac", str(video)])
        clip = {"id": "clip01", "start": 5, "end": 9, "status": "done", "version": 1,
                "file": video.name, "posts": {"existing": {"status": "posted"}}}
        job["clips"] = [clip]
        jobs.save(job)
        before = streams(video)
        path = f"/api/jobs/{job['id']}/clips/{clip['id']}/music"
        preview = client.post(path + "/preview", json={"music": neon, "version": 1}, headers=origin)
        (folder / "preview.mp4").write_bytes(preview.content)
        check("the preview mixes against the clip without changing the export", preview.is_success
              and streams(folder / "preview.mp4") != before and streams(video) == before)
        check("arbitrary files and invalid audio levels are rejected",
              client.post(path, json={"music": {"track": "../secret"}}, headers=origin).status_code == 400
              and client.post(path, json={"music": {"volume": 101}}, headers=origin).status_code == 400)

        def update(settings, version):
            with patch.object(jobs._q, "put") as queued:
                response = client.post(path, json={"music": settings, "version": version}, headers=origin)
                check("music-only update uses the existing clip queue", response.is_success
                      and queued.call_args.args[0] == ("clip", job["id"], clip["id"]))
                check("concurrent updates are rejected",
                      client.post(path, json={"music": settings, "version": version}, headers=origin).status_code == 400)
            jobs._update_music(job, clip)
            assert clip["status"] == "done" and clip["music_error"] is None

        update(neon, 1)
        mixed = streams(video).splitlines()
        original = before.splitlines()
        clean_file = folder / "clip01.clean.m4a"
        clean = clean_file.read_bytes()
        check("existing clips can add music with the exact same encoded video and no source file",
              mixed[0] == original[0] and mixed[1] != original[1] and not job.get("source_path"))
        update({"track": keynata_id, "volume": 38, "duck": False}, 2)
        check("replacing music keeps the original audio and preserves posting history",
              clean_file.read_bytes() == clean and streams(video) != "\n".join(mixed) + "\n"
              and clip["posts"]["existing"]["status"] == "posted")
        update({"track": ""}, 3)
        check("removing music restores the original video and audio packets", streams(video) == before)
        check("stale clip versions are refused", client.post(path, json={"music": neon, "version": 1},
                                                            headers=origin).status_code == 400)

        with patch.object(render, "W", 108), patch.object(render, "H", 192):
            rendered = render.render_clip(video, folder, "styled", {"start": 0, "end": 3, "look": look}, [], 96, 160)
        check("new styled clips get music and save a clean audio track",
              (folder / "styled.clean.m4a").is_file() and (folder / rendered["file"]).is_file())
print("ALL PASSED")
