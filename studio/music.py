"""Original instrumental tracks and the shared audio mix for previews and exports."""
import math
import os
import tempfile
import threading
import wave
from pathlib import Path

import numpy as np

from . import config, media, music_library

# Each composition has its own tempo, key, progression and arrangement.
# Synthesized here from oscillators and noise, with no samples or third-party recordings.
TRACKS = {
    "late-night": ("Late Night", "Chill", "Lo-fi", 82, 48, (0, 5, 3, 7), "soft"),
    "neon-run": ("Neon Run", "High energy", "Electronic", 128, 45, (0, 0, 3, 5), "drive"),
    "quiet-pursuit": ("Quiet Pursuit", "Tense", "Cinematic", 100, 43, (0, 1, 0, 3), "dark"),
    "side-quest": ("Side Quest", "Funny", "Chiptune", 115, 53, (0, 5, 7, 0), "chip"),
    "first-light": ("First Light", "Upbeat", "Electronic", 106, 50, (0, 7, 5, 2), "bright"),
    "deep-water": ("Deep Water", "Calm", "Ambient", 68, 46, (0, 3, 7, 5), "ambient"),
    "overtime": ("Overtime", "High energy", "Hip hop", 94, 42, (0, 3, 0, 5), "beat"),
    "behind-the-door": ("Behind the Door", "Suspense", "Cinematic", 76, 41, (0, 6, 3, 1), "dark"),
}
DEFAULT = {"track": "", "volume": 12, "original": 100, "duck": True}
RATE = 48000
_lock = threading.Lock()


def catalog():
    return [{"id": key, "title": title, "mood": mood, "genre": genre, "bpm": bpm,
             "seconds": round(32 * 60 / bpm), "artist": "PeakCut Originals", "license": "CC0 1.0",
             "license_url": "https://creativecommons.org/publicdomain/zero/1.0/"}
            for key, (title, mood, genre, bpm, *_arrangement) in TRACKS.items()] + music_library.catalog()


def has_track(track):
    return track in TRACKS or track in music_library.TRACKS


def clean(value):
    if value is None:
        return dict(DEFAULT)
    if not isinstance(value, dict):
        raise ValueError("Choose a track and audio levels.")
    track = value.get("track", "")
    if not isinstance(track, str) or (track and not has_track(track)):
        raise ValueError("That music track is unavailable.")
    duck = value.get("duck", DEFAULT["duck"])
    if not isinstance(duck, bool):
        raise ValueError("Automatic music lowering must be on or off.")
    settings = {"track": track, "duck": duck}
    for key in ("volume", "original"):
        try:
            percent = float(value.get(key, DEFAULT[key]))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Audio levels must be between 0% and 100%.") from None
        if not 0 <= percent <= 100:
            raise ValueError("Audio levels must be between 0% and 100%.")
        settings[key] = round(percent)
    return settings


def active(settings):
    return bool(settings.get("track") or settings.get("original", 100) != 100)


def track_file(track):
    if track in music_library.TRACKS:
        return music_library.file(track)
    if track not in TRACKS:
        raise ValueError("That music track is unavailable.")
    folder = config.DATA_DIR / "music"
    path = folder / f"{track}-v1.wav"
    with _lock:
        if not path.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=folder, prefix="track-") as tmp:
                draft = Path(tmp) / path.name
                _synthesize(track, draft)
                os.replace(draft, path)
    return path


def _synthesize(track, path):
    _, _, _, bpm, key, progression, style = TRACKS[track]
    beat = 60 / bpm
    samples = np.zeros((round(32 * beat * RATE), 2), dtype=np.float32)
    random = np.random.default_rng(sum(map(ord, track)))

    def place(at, sound, level, pan=0.0):
        start = round(at * beat * RATE)
        length = min(len(samples) - start, len(sound))
        samples[start:start + length] += (sound[:length, None] * np.array([1 - pan, 1 + pan])) * level

    def note(at, length, pitch, level, pan=0.0, tone="soft"):
        t = np.arange(round(length * beat * RATE)) / RATE
        angle = 2 * np.pi * 440 * 2 ** ((pitch - 69) / 12) * t
        sound = np.sin(angle)
        if tone == "chip":
            sound += .32 * np.sin(angle * 3) + .15 * np.sin(angle * 5)
        elif tone != "bass":
            sound += .36 * np.sin(angle * 2.002) + .12 * np.sin(angle * 3)
        attack = .13 if tone == "pad" else .012
        envelope = np.minimum(t / attack, 1) * np.minimum((t[-1] - t) / .075, 1)
        if tone not in ("pad", "bass"):
            envelope *= np.exp(-t * 2.1)
        place(at, sound * envelope, level, pan)

    for bar in range(8):
        root = key + progression[bar % 4]
        minor = style in ("dark", "drive", "beat") or bar % 4 == 2
        chord = (0, 3 if minor else 4, 7, 10 if minor else 11)
        for i, interval in enumerate(chord):
            note(bar * 4, 3.95, root + 12 + interval, .04, (i - 1.5) * .22, "pad")
        for step in (0, 2, 3.5) if style in ("drive", "beat", "chip") else (0, 2):
            note(bar * 4 + step, .46 if step == 3.5 else 1.4, root, .20, tone="bass")
        pattern = (0, 1.5, 2.5, 3) if style != "ambient" else (.5, 2.5)
        order = (2, 1, 3, 0) if bar % 2 == 0 else (0, 2, 1, 3)
        for i, at in enumerate(pattern):
            note(bar * 4 + at, .7, root + 24 + chord[order[i]], .11, (-1) ** i * .22, style)
        if style == "ambient":
            continue
        for step in range(8):
            t = np.arange(2600) / RATE
            noise = random.uniform(-1, 1, len(t))
            hat = np.diff(noise, prepend=0) * np.exp(-t * 85)
            place(bar * 4 + step / 2, hat, .045 if step % 2 else .025, .33)
            if step in (2, 6):
                place(bar * 4 + step / 2, noise * np.exp(-t * 32), .14, -.09)
        t = np.arange(9600) / RATE
        kick = np.sin(2 * np.pi * (45 * t + 55 * .032 * (1 - np.exp(-t / .032)))) * np.exp(-t * 22)
        for step in ((0, 1, 2, 3) if style == "drive" else (0, 2.5)):
            place(bar * 4 + step, kick, .36)

    edge = np.linspace(0, 1, 480)
    samples[:480] *= edge[:, None]
    samples[-480:] *= edge[::-1, None]
    rms = np.sqrt(np.mean(samples**2))
    gain = min(.19 / rms, .95 / np.max(np.abs(samples)))
    with wave.open(str(path), "wb") as out:
        out.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        out.writeframes((samples * gain * 32767).astype("<i2").tobytes())


def duration(path):
    length = float(media.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)], timeout=15).strip())
    if not math.isfinite(length) or length <= 0:
        raise ValueError("The clip has no playable duration.")
    return length


def extract_clean(video, out, length):
    stream = media.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", str(video)], timeout=15).strip()
    source = ["-i", str(video)] if stream else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    codec = ["-c:a", "copy"] if stream == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    media.run(["ffmpeg", "-y", "-v", "error", *source, "-map", "0:a:0", "-t", f"{length:.6f}",
               *codec, str(out)], timeout=60)


def mix(video, clean_audio, out, settings, *, preview=False):
    """Copy the video stream. Use the original audio plus a looped music track."""
    length = duration(video)
    span = min(length, 15) if preview else length
    track = settings["track"]
    base = ["ffmpeg", "-y", "-v", "error"]
    inputs = ["-i", str(video), "-i", str(clean_audio)]
    audio = "1:a:0"
    mapping = ["-map", "0:v:0", "-c:v", "copy"]
    if not active(settings):
        filters = ["-map", audio, "-c:a", "copy"]
    else:
        original = (f"[{audio}]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"apad,atrim=duration={span:.6f},volume={settings['original'] / 100}")
        if track:
            source = track_file(track)
            inputs += ["-stream_loop", "-1"]
            if source.suffix == ".mp3":
                inputs += ["-f", "mp3"]
            inputs += ["-i", str(source)]
            fade = min(.8, length / 2)
            song = (f"[2:a]volume={settings['volume'] / 100},"
                    f"afade=t=in:d={min(.25, fade)},afade=t=out:st={length - fade}:d={fade}")
            if settings["duck"] and settings["original"]:
                graph = (f"{original},asplit[original][voice];{song}[music];"
                         "[music][voice]sidechaincompress=threshold=0.025:ratio=6:attack=25:release=400[bed];")
            else:
                graph = f"{original}[original];{song}[bed];"
            graph += "[original][bed]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95:level=0:latency=1[mix]"
        else:
            graph = f"{original}[mix]"
        filters = ["-filter_complex", graph, "-map", "[mix]", "-c:a", "aac", "-b:a", "192k"]
    media.run([*base, *inputs, *mapping, *filters, "-t", f"{span:.6f}", "-movflags", "+faststart", str(out)], timeout=90)
