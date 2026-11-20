"""Clip Studio's persistent-face framing for the facecam-over-gameplay layout."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image

from . import config, facecam


def face_crop(frames):
    """Find the same face on one side of the screen across the sampled frames."""
    candidates = []
    for i, frame in enumerate(frames):
        for face in frame["faces"]:
            cx = face["x"] + face["width"] / 2
            cy = face["y"] + face["height"] / 2
            if face["confidence"] >= .65 and .025 < face["width"] < .24 and (cx < .28 or cx > .72):
                candidates.append((i, face, cx, cy))
    if not candidates:
        return None
    clusters = ([row for row in candidates if abs(row[2] - cx) < .09 and abs(row[3] - cy) < .12]
                for _, _, cx, cy in candidates)
    cluster = max(clusters, key=lambda rows: (len({r[0] for r in rows}), median(r[1]["width"] for r in rows)))
    if len({r[0] for r in cluster}) < max(2, len(frames) * .45):
        return None
    faces = [r[1] for r in cluster]
    fw = median(f["width"] for f in faces)
    fh = median(f["height"] for f in faces)
    x = max(0, min(f["x"] for f in faces) - fw * .45)
    y = max(0, min(f["y"] for f in faces) - fh * .38)
    right = min(1, max(f["x"] + f["width"] for f in faces) + fw * .45)
    bottom = min(1, max(f["y"] + f["height"] for f in faces) + fh * .55)
    return x, y, right - x, bottom - y


def webcam_bounds(folder, portrait):
    """Measure persistent picture edges around the face on the sampled frames."""
    images = []
    for path in sorted(folder.glob("frame*.jpg")):
        with Image.open(path) as image:
            images.append(np.asarray(image.convert("RGB")))
    frames = np.stack(images)
    height, width = frames.shape[1:3]
    x, y, w, h = portrait
    cx, cy = (x + w / 2) * width, (y + h / 2) * height
    fw, fh = w * width, h * height

    def edges(strip, axis):
        # A camera boundary stays in place even as the person and game move.
        delta = np.abs(np.diff(strip.astype(np.int16), axis=axis)).mean(axis=3)
        return np.median(delta, axis=0)

    def bounds(profile, center, radius, extent):
        def side(low, high, default):
            a, b = max(0, int(low)), min(len(profile), int(high))
            if a >= b:
                return default
            index = a + int(np.argmax(profile[a:b]))
            return index + 1 if profile[index] >= 10 else default

        return (side(center - extent, center - radius, max(0, int(center - radius * 2))),
                side(center + radius, center + extent, min(len(profile) + 1, int(center + radius * 2))))

    # A camera touching the top corner has a reliable outer border close to
    # that corner. Measure a thin top strip before looking at interior details.
    if y < .05 and (cx < width * .24 or cx > width * .76):
        profile = np.percentile(edges(frames[:, :12], 2), 35, axis=0)
        a = max(1, int((x - .06) * width))
        b = min(width - 2, int(cx))
        candidates = [i for i in range(a, b) if profile[i] > 12
                      and profile[i] >= profile[i - 1] and profile[i] > profile[i + 1]]
        if cx < width * .24:
            a, b = int(cx), min(width - 2, int((x + w + .06) * width))
            candidates = [i for i in range(a, b) if profile[i] > 12
                          and profile[i] >= profile[i - 1] and profile[i] > profile[i + 1]]
        if candidates:
            left, right = (candidates[0] + 1, width) if cx > width / 2 else (0, candidates[-1] + 1)
            horizontal = np.percentile(edges(frames[:, :, left + 4:right - 4], 1), 35, axis=1)
            a, b = int(cy + fh * .20), min(height - 2, int(cy + fh * 1.6))
            borders = [i for i in range(max(1, a), b) if horizontal[i] > 12
                       and horizontal[i] >= horizontal[i - 1] and horizontal[i] > horizontal[i + 1]]
            bottom = borders[0] + 1 if borders else min(height, int((y + h) * height))
            return left / width, 0, (right - left) / width, bottom / height

    left = max(0, int(cx - fw * .4))
    right = min(width, int(cx + fw * .4) + 1)
    horizontal = edges(frames[:, :, left:right], 1)
    top, bottom = bounds(np.percentile(horizontal, 35, axis=1), cy, fh * .20, min(height * .43, fh * 1.6))

    vertical = edges(frames[:, top:bottom], 2)
    left, right = bounds(np.percentile(vertical, 35, axis=0), cx, fw * .30, width * .42)
    # An inset flush with the source edge has no border on that side. Follow
    # its perpendicular borders to the frame edge (also works for top/bottom).
    def extend(low, high, center, across, video):
        size = video.shape[2]
        positions = [edge - 1 for edge in across if 12 <= edge <= video.shape[1] - 12]
        if positions:
            for side in (0, size):
                if abs(center - side) > min(size * .30, (across[1] - across[0]) * 2):
                    continue
                strip = video[:, :, :12] if side == 0 else video[:, :, -12:]
                if all(np.median(edges(strip[:, i:i + 2], 1)) >= 12 for i in positions):
                    if side == 0:
                        low = 0
                    else:
                        high = size
        return low, high

    top, bottom = extend(top, bottom, cy, (left, right), frames.swapaxes(1, 2))
    left, right = extend(left, right, cx, (top, bottom), frames)
    if right <= left or bottom <= top:
        return portrait
    return left / width, top / height, (right - left) / width, (bottom - top) / height


def detect(src, start, end):
    """Return a normalized crop for this time range, including on a long replay."""
    with tempfile.TemporaryDirectory(prefix="pc-cam-") as directory:
        folder = Path(directory)
        pattern = str(folder / "frame%02d.jpg")
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(end - start),
                            "-i", str(src), "-an", "-vf", f"fps={8 / (end - start):.8f},scale=960:-2",
                            "-frames:v", "8", pattern], capture_output=True, check=True, timeout=90)
            if sys.platform == "darwin" and shutil.which("swift"):
                try:
                    result = subprocess.run(["swift", "-module-cache-path", str(config.TMP_DIR / "swift-modules"),
                                             str(Path(__file__).with_name("detect_faces.swift")), directory],
                                            capture_output=True, check=True, timeout=90)
                    portrait = face_crop(json.loads(result.stdout))
                    if portrait:
                        return webcam_bounds(folder, portrait)
                    return None
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass  # Fall back to PeakCut's local webcam-border detector.
            sample = folder / "sample.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-framerate", "4", "-i", pattern, "-c:v", "ffv1", str(sample)],
                           capture_output=True, check=True, timeout=30)
            box = facecam.detect(sample)
            if box and box["conf"] == "high":
                width, height = facecam._size(sample)
                if width and height and box["w"] < .45 * width:
                    return box["x"] / width, box["y"] / height, box["w"] / width, box["h"] / height
        except (OSError, subprocess.SubprocessError):
            pass
    return None
