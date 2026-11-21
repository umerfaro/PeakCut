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


# The stacked layout's top band is 1080 wide and 768 tall (40% of the 1920 frame).
# A camera taller than that band is scaled up and then cut off the top and the bottom,
# which takes the hair and the headphones. A camera that is already this wide, or wider,
# only loses a little of its sides, so it is left alone.
_PANEL_W, _PANEL_H = 1080, 768


def _fit_panel(box, src_w, src_h):
    """Shorten a camera that is taller than the top band, keeping the head in frame.

    A rounded webcam often has no straight edge the border search will trust, so the
    box around the face runs down into the game. Filling the band from that tall box
    cuts the head off. Shortening it to the band's own shape keeps the side the head
    is on and drops the game underneath."""
    x, y, w, h = box
    if src_w <= 0 or src_h <= 0 or w <= 0 or h <= 0:
        return None
    panel = _PANEL_W / _PANEL_H
    if (w / h) * (src_w / src_h) >= panel * 0.92:
        return x, y, w, h
    new_h = w * (src_w / src_h) / panel
    # The head is at the top of the box. A camera sitting just under the top of
    # the frame is still that kind of camera: keep its top, and drop the chat
    # and the game that the padding pulled in underneath. Centering that box
    # slides the window down onto the chat and cuts the hair.
    if y <= 0.08:
        y2 = y
    elif y + h >= 0.98:
        y2 = y + h - new_h
    else:
        y2 = y + (h - new_h) / 2
    y2 = max(0.0, min(y2, 1.0 - new_h))
    return x, y2, w, new_h


def trim_spill(folder, portrait):
    """Cut the gameplay a rounded webcam's face box pulled in beside it.

    The border search needs a straight edge on every side, so a rounded corner
    comes back with no border and the padding around the face runs into the
    game. That side of the camera is still one straight line that does not
    move. Cut the box there. A camera that already reaches the frame keeps
    that edge, so the headphones are not shaved off."""
    images = []
    for path in sorted(folder.glob("frame*.jpg")):
        with Image.open(path) as image:
            images.append(np.asarray(image.convert("RGB")))
    if len(images) < 2:
        return portrait
    frames = np.stack(images)
    height, width = frames.shape[1:3]
    x, y, w, h = portrait
    if w <= 0 or h <= 0:
        return portrait
    y0 = max(0, int(y * height))
    y1 = min(height, int((y + h * 0.72) * height))
    if y1 - y0 < 8:
        return portrait
    # One number per column: how hard the colour changes, and whether it stays
    # put while the person and the game move.
    delta = np.abs(np.diff(frames[:, y0:y1].astype(np.int16), axis=2)).mean(axis=3)
    profile = np.median(delta, axis=(0, 1))
    on_right = x + w / 2 > 0.5
    if on_right:
        lo = max(1, int(x * width))
        hi = min(len(profile) - 2, int((x + w * 0.45) * width))
    else:
        lo = max(1, int((x + w * 0.55) * width))
        hi = min(len(profile) - 2, int((x + w) * width))
    if hi - lo < 4:
        return portrait
    window = profile[lo:hi]
    peak_at = int(np.argmax(window)) + lo
    peak = float(profile[peak_at])
    # The camera edge is one strong line. Edges inside the room are weaker and
    # spread out, so a busy face does not count as a border.
    if peak < 14 or peak < float(np.median(window)) * 3:
        return portrait
    edge = (peak_at + 1) / width
    inset = (edge - x) / w if on_right else (x + w - edge) / w
    # Too close to the current side means there is nothing to cut. Too far in
    # means the line is the person, not the camera.
    if inset < 0.04 or inset > 0.36:
        return portrait
    if on_right:
        x, w = edge, x + w - edge
    else:
        w = edge - x
    if w < 0.08:
        return portrait
    # A corner camera that already reaches the picture should use that edge.
    if on_right and x + w >= 0.965:
        w = 1.0 - x
    elif not on_right and x <= 0.035:
        w += x
        x = 0.0
    return x, y, w, h


def camera_crop(measured, portrait, src_w, src_h):
    """The webcam to lift into the top band, or None when this is not one.

    `measured` is the rectangle the borders gave, or None when a rounded corner
    gave no border worth trusting. `portrait` is the box around the face that
    stayed in the same corner. Either one is fitted to the band before it is used."""
    box = _fit_panel(measured or portrait, src_w, src_h)
    if box is None:
        return None
    x, y, w, h = box
    if w > 0.45 or h > 0.65 or x < 0 or y < 0 or x + w > 1.02 or y + h > 1.02:
        return None
    return box


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
                        # The rectangle search keeps the face padding when it cannot
                        # trust a rounded corner. That padding is the game. The
                        # straight edge beside the face is the camera: use it.
                        trimmed = trim_spill(folder, portrait)
                        px, _, pw, _ = portrait
                        on_right = px + pw / 2 > 0.5
                        if measured is None:
                            portrait = trimmed
                        else:
                            mx, _, mw, _ = measured
                            if on_right:
                                same_side = abs(mx - px) < 0.015
                                moved = trimmed[0] > px + 0.02
                            else:
                                same_side = abs((mx + mw) - (px + pw)) < 0.015
                                moved = trimmed[0] + trimmed[2] < px + pw - 0.02
                            if same_side and moved:
                                measured = None
                                portrait = trimmed
                        sample_path = next(folder.glob("frame*.jpg"), None)
                        if sample_path is not None:
                            with Image.open(sample_path) as sample:
                                src_w, src_h = sample.size
                            # A rounded webcam has no edge the border search will trust.
                            # The face is still the camera: fit that box to the top band
                            # instead of leaving the tiny corner picture in the game.
                            found = camera_crop(measured, portrait, src_w, src_h)
                            if found:
                                return found
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass  # Fall back to PeakCut's local webcam-border detector.
            sample = folder / "sample.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-framerate", "4", "-i", pattern, "-c:v", "ffv1", str(sample)],
                           capture_output=True, check=True, timeout=30)
            box = facecam.detect(sample)
            if box and box["conf"] == "high":
                width, height = facecam._size(sample)
                if width and height and box["w"] < .45 * width:
                    found = box["x"] / width, box["y"] / height, box["w"] / width, box["h"] / height
                    return _fit_panel(found, width, height)
        except (OSError, subprocess.SubprocessError):
            pass
    return None
