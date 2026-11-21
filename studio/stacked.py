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
            # A streamer standing back from the camera has a smaller face than usual.
            # Game characters beside the webcam are larger and do not stay put.
            if face["confidence"] >= .55 and .015 < face["width"] < .24 and (cx < .28 or cx > .72):
                candidates.append((i, face, cx, cy))
    if not candidates:
        return None
    # Keep the window tight. A jump stays inside the webcam; a character in the
    # game just outside it must not be counted as the same face.
    clusters = [[row for row in candidates if abs(row[2] - cx) < .055 and abs(row[3] - cy) < .10]
                for _, _, cx, cy in candidates]

    def rank(rows):
        seen = len({r[0] for r in rows})
        spread = (max(r[2] for r in rows) - min(r[2] for r in rows)
                  + max(r[3] for r in rows) - min(r[3] for r in rows))
        # Still and small: the webcam overlay, not a close-up in the game.
        return seen, -spread, -median(r[1]["width"] for r in rows)

    cluster = max(clusters, key=rank)
    if len({r[0] for r in cluster}) < max(2, len(frames) * .45):
        return None
    # A second person sitting in the same webcam is a separate face. Pull them
    # in so the crop covers the picture, not only whoever the detector prefers.
    cx0, cy0 = median(r[2] for r in cluster), median(r[3] for r in cluster)
    faces = []
    seen_ids = set()
    for rows in clusters:
        if len({r[0] for r in rows}) < 3 or median(r[1]["width"] for r in rows) > .09:
            continue
        if abs(median(r[2] for r in rows) - cx0) > .12 or abs(median(r[3] for r in rows) - cy0) > .12:
            continue
        for row in rows:
            mark = (row[0], round(row[2], 3), round(row[3], 3))
            if mark not in seen_ids:
                seen_ids.add(mark)
                faces.append(row[1])
    if not faces:
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

    # Score complete rectangles, not four unrelated strongest edges. A narrow
    # viewer counter can have a stronger edge than the actual camera boundary.
    # Requiring support across the whole panel excludes that counter and avoids
    # expanding the crop into gameplay when an interior room edge is stronger.
    dx, dy = edges(frames, 2), edges(frames, 1)

    def peaks(profile, low, high, soft=False):
        # Soft peaks keep weaker vertical camera edges that a busy game or menu
        # would otherwise bury under a 7-threshold.
        low, high = max(1, int(low)), min(len(profile) - 1, int(high))
        floor = 3 if soft else 7
        found = [i + 1 for i in range(low, high)
                 if profile[i] >= floor and profile[i] >= profile[i - 1]
                 and profile[i] > profile[i + 1]]
        return sorted(found, key=lambda v: profile[v - 1], reverse=True)[:80 if soft else 12]

    band_top, band_bottom = max(0, int(cy - fh * .5)), min(height, int(cy + fh * .5) + 1)
    vertical = np.percentile(dx[band_top:band_bottom], 35, axis=0)
    lefts = peaks(vertical, cx - width * .4, cx - fw * .3, soft=True)
    rights = peaks(vertical, cx + fw * .3, cx + width * .4, soft=True)
    if cx < width * .3:
        lefts.append(0)
    if cx > width * .7:
        rights.append(width)
    best = None
    for left in lefts:
        for right in rights:
            if not fw * .8 <= right - left <= width * .45:
                continue
            inset = min(4, (right - left) // 8)
            horizontal = np.percentile(dy[:, left + inset:right - inset], 35, axis=1)
            # Search a little closer to the face so a standing or moving person
            # still finds the real panel top and bottom.
            tops = peaks(horizontal, cy - min(height * .43, fh * 2), cy - fh * .2)
            bottoms = peaks(horizontal, cy + fh * .2, cy + min(height * .43, fh * 2))
            if cy < height * .3:
                tops.append(0)
            if cy > height * .7:
                bottoms.append(height)
            for top in tops:
                for bottom in bottoms:
                    rw, rh = right - left, bottom - top
                    if not fh * .55 <= rh <= height * .65 or not .65 <= rw / rh <= 2.8:
                        continue
                    # A boundary must persist along most of each side, including
                    # through changes in the game and the person's expression.
                    scores = [horizontal[v - 1] for v in (top, bottom) if v not in (0, height)]
                    scores += [np.percentile(dx[top + inset:bottom - inset, v - 1], 35)
                               for v in (left, right) if v not in (0, width)]
                    # Both ends of each border must agree too. A paused game's
                    # menu can create a larger rectangle that overlaps the real
                    # webcam on only part of its bottom edge.
                    borders = [dy[v - 1, left + inset:right - inset]
                               for v in (top, bottom) if v not in (0, height)]
                    supported = all(min(np.percentile(part, 35) for part in np.array_split(line, 3)) >= 5
                                    for line in borders)
                    for side in (left, right):
                        if side not in (0, width):
                            line = dx[top + 1:bottom - 1, side - 1]
                            tip = max(3, len(line) // 10)
                            supported &= min(np.median(line[:tip]), np.median(line[-tip:])) >= 3
                    if not supported or len(scores) < 2 or min(scores) < 7:
                        continue
                    strength = float(np.mean(np.minimum(scores, 30)))
                    score = strength * (min(scores) / max(7, strength)) ** .25
                    # Prefer the full picture over a smaller rectangle formed by
                    # furniture inside it when their boundary support is similar.
                    score *= (rw * rh / (width * height)) ** .08
                    if best is None or score > best[0]:
                        best = score, (left / width, top / height, rw / width, rh / height)
    if best is None:
        return None
    # Keep the face crop inside the panel: a strong inner edge can shrink the
    # measured box onto the face alone, which cuts off hair and shoulders.
    mx, my, mw, mh = best[1]
    px, py, pw, ph = portrait
    left, top = min(mx, px), min(my, py)
    right, bottom = max(mx + mw, px + pw), max(my + mh, py + ph)
    return left, top, right - left, bottom - top


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
                        measured = webcam_bounds(folder, portrait)
                        if measured:
                            return measured
                        # A dark game behind a small camera often has no edge the
                        # border search will trust. The faces still outline it.
                        if portrait[2] < .25 and portrait[3] < .30:
                            return portrait
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
