"""Podcast split screen: who is on screen, shot by shot, and the ffmpeg graph that draws it.

The clip's own frames are read once. Camera cuts split it into shots; in each shot the faces seen are grouped by
place into people. Two people: split screen, one above the other (the one on the left on top, so they never swap).
One person: full screen, centred on them. Nobody, or three and more: the whole frame over a blurred fill, so nobody
is ever cut off. A person who turns away for a moment is not "gone": a change of people inside one shot only counts
when it lasts.

Runs on the processor with OpenCV's face finder (studio/facecam.py): free, no service, no AI.

A plan is {"fps", "frames", "w", "h", "segments": [{"a", "b", "mode": "split"|"full"|"fit", "boxes": [...]}]}: frame
numbers a..b (b not included) at the clips' frame rate, and for each person the part of the source frame shown."""
import subprocess

from . import config
from .facecam import YUNET

W, H = 1080, 1920
HALF = H // 2
AW = 480              # the width the frames are looked at
EVERY = 5             # faces are looked for in every 5th frame (cuts are checked in every frame)
FACE_MIN = 0.035      # a face narrower than this share of the frame is too far away to be one of the speakers
BG = 0.5              # a face less than half the size of the biggest one is someone in the background
SPEAKER = 0.05        # a face narrower than this share of the frame is far off (a wide shot): not zoomed in on
SEEN = 0.3            # a person must be seen in this share of a stretch's looks to count
HOLD = 2.0            # seconds a face may be missed (turned head, hand) and still be there
SHORT = 1.0           # seconds: a stretch shorter than this inside a shot joins its neighbour
MIN_ASPECT = 1.2      # a vertical or square video has nothing to split
MAX_SEGMENTS = 60     # a very choppy video (a montage): the shortest stretches join their neighbours past this


def _even(v):
    return max(2, int(v) // 2 * 2)


def _read(src, start, dur, prefilter, aw, ah, fps, nice):
    """The clip's frames, small, as the render will see them (same seek, same filters, same frame numbers)."""
    import numpy as np
    from .media import NO_WINDOW
    cmd = [*nice, "ffmpeg", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src), "-an",
           "-vf", f"{prefilter}fps={fps},scale={aw}:{ah}:flags=fast_bilinear", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    size = aw * ah * 3
    try:
        while True:
            raw = p.stdout.read(size)
            if len(raw) < size:
                break
            yield np.frombuffer(raw, np.uint8).reshape(ah, aw, 3)
    finally:
        p.stdout.close()
        if p.poll() is None:
            p.kill()
        p.wait()


def _finder(aw, ah):
    """A function giving a frame's faces as (centre x, centre y, width, height) in its own pixels."""
    import cv2
    det = cv2.FaceDetectorYN.create(str(YUNET), "", (aw, ah), 0.7, 0.3, 50)

    def faces(img):
        _, fs = det.detect(img)
        return [(float(f[0] + f[2] / 2), float(f[1] + f[3] / 2), float(f[2]), float(f[3]))
                for f in (fs if fs is not None else []) if f[2] >= aw * FACE_MIN]
    return faces


def cuts(diffs) -> list:
    """The frames where the camera changes: a jump from the frame before that is large, and far larger than the
    movement around it. diffs[i] is how much frame i differs from frame i - 1 (diffs[0] is 0)."""
    out = []
    for i in range(1, len(diffs)):
        near = sorted(diffs[max(1, i - 12):i] + diffs[i + 1:i + 13])
        usual = near[len(near) // 2] if near else 0.0
        if diffs[i] > 14 and diffs[i] > 3 * usual + 6 and (not out or i - out[-1] >= 4):
            out.append(i)
    return out


def _tracks(looks, aw):
    """The people in one shot: faces grouped by where they are. looks: [(frame number, faces, patches)]."""
    tracks = []
    for n, (_, faces, patches) in enumerate(looks):
        big = max((f[2] for f in faces), default=0)
        taken = set()
        for face, patch in sorted(zip(faces, patches), key=lambda x: -x[0][2]):
            cx, cy, w, h = face
            if w < big * BG:
                continue
            best = None
            for k, t in enumerate(tracks):
                d = abs(t["cx"] - cx) + 0.5 * abs(t["cy"] - cy)
                if k not in taken and d < max(aw * 0.1, t["w"] * 1.2) and (best is None or d < best[0]):
                    best = (d, k)
            if best is None:
                tracks.append({"cx": cx, "cy": cy, "w": w, "seen": {}, "patches": []})
                best = (0, len(tracks) - 1)
            t = tracks[best[1]]
            taken.add(best[1])
            t["seen"][n] = face
            t["patches"].append(patch)
            k = len(t["seen"])
            t["cx"] += (cx - t["cx"]) / k
            t["cy"] += (cy - t["cy"]) / k
            t["w"] += (w - t["w"]) / k
    return tracks


def _alive(track) -> bool:
    """A person moves a little all the time; a face on a poster or a screen in the background is the same picture."""
    import numpy as np
    if len(track["patches"]) < 5:
        return True
    s = np.stack(track["patches"]).astype(np.float32)
    return float(np.abs(s - s.mean(axis=0)).mean()) >= 1.2


def _presence(track, count, hold, short):
    """Where in the shot (look by look) this person is there: short misses are filled, flickers dropped."""
    on = [n in track["seen"] for n in range(count)]
    if sum(on) < max(1, count * 0.08):
        return [False] * count

    def runs(value):
        i = 0
        while i < count:
            j = i
            while j < count and on[j] == on[i]:
                j += 1
            if on[i] == value:
                yield i, j
            i = j
    for i, j in list(runs(False)):
        if j - i < hold:
            on[i:j] = [True] * (j - i)
    for i, j in list(runs(True)):
        if j - i < short and count >= short:
            on[i:j] = [False] * (j - i)
    return on


def _median(v):
    v = sorted(v)
    return v[len(v) // 2]


def _shot(looks, a, b, fps, aw):
    """One camera shot as stretches: [(first frame, end frame, [faces of the people there, left to right])]."""
    if not looks:
        return [(a, b, [])]
    count = len(looks)
    per_s = fps / EVERY
    tracks = [t for t in _tracks(looks, aw)]
    alive = [t for t in tracks if _alive(t)]
    tracks = alive or tracks
    if count < SHORT * per_s * 1.5:      # a very short shot: whoever is seen in enough of it
        on = [[len(t["seen"]) >= max(1, count * SEEN)] * count for t in tracks]
    else:
        on = [_presence(t, count, int(HOLD * per_s), int(SHORT * per_s)) for t in tracks]
    sets = [tuple(k for k in range(len(tracks)) if on[k][n]) for n in range(count)]
    # stretches of the same people; one shorter than SHORT joins the longer one beside it
    stretches = []
    for n, s in enumerate(sets):
        if stretches and stretches[-1][2] == s:
            stretches[-1][1] = n + 1
        else:
            stretches.append([n, n + 1, s])
    while len(stretches) > 1:
        i = min(range(len(stretches)), key=lambda k: stretches[k][1] - stretches[k][0])
        if stretches[i][1] - stretches[i][0] >= SHORT * per_s:
            break
        side = [k for k in (i - 1, i + 1) if 0 <= k < len(stretches)]
        k = max(side, key=lambda k: stretches[k][1] - stretches[k][0])
        lo, hi = sorted((i, k))
        stretches[lo:hi + 1] = [[stretches[lo][0], stretches[hi][1], stretches[k][2]]]
    out = []
    for i, (n0, n1, who) in enumerate(stretches):
        first = a if i == 0 else looks[n0][0]
        end = b if i == len(stretches) - 1 else looks[stretches[i + 1][0]][0]
        people = []
        for k in who:
            got = [tracks[k]["seen"][n] for n in range(n0, n1) if n in tracks[k]["seen"]] or list(tracks[k]["seen"].values())
            people.append(tuple(_median([f[j] for f in got]) for j in range(4)))
        out.append((first, end, sorted(people)))
    return out


def full_box(face, src_w, src_h):
    """The 9:16 part of the frame shown for one person: as tall as the video, centred on them."""
    cw = _even(min(src_w, src_h * W / H))
    x = _even(max(0, min(src_w - cw, face[0] - cw / 2))) if face[0] > cw / 2 else 0
    return {"x": x, "y": 0, "w": cw, "h": _even(src_h)}


def half_box(face, other, src_w, src_h):
    """The 9:8 part of the frame shown in one half: head and shoulders of one person, keeping the other one out."""
    cx, cy, _fw, fh = face
    aspect = W / HALF
    gap = abs(other[0] - cx)
    cw = max(fh * 3.6, src_h * 0.45) * aspect
    cw = max(min(cw, gap * 1.04), src_w * 0.25)       # no wider than the space between the two faces
    cw = min(cw, src_w, src_h * aspect)
    ch = cw / aspect
    x = cx - cw / 2
    mid = (cx + other[0]) / 2                    # not past the middle between the two (a little is fine)
    x = min(x, mid + cw * 0.12 - cw) if cx < other[0] else max(x, mid - cw * 0.12)
    x = max(cx - cw * 0.72, min(x, cx - cw * 0.28))     # but their own face stays well inside
    x = max(0.0, min(src_w - cw, x))
    y = max(0.0, min(src_h - ch, cy - ch * 0.52))     # a little low in the half: room for the heading above
    return {"x": _even(x) if x >= 1 else 0, "y": _even(y) if y >= 1 else 0, "w": _even(cw), "h": _even(ch)}


def layout_of(people, src_w, src_h) -> tuple:
    """(mode, boxes) for the people on screen, their faces in source pixels, left to right."""
    people = [f for f in people if f[2] >= src_w * SPEAKER]
    if len(people) == 1:
        return "full", [full_box(people[0], src_w, src_h)]
    if len(people) == 2:
        a, b = people
        return "split", [half_box(a, b, src_w, src_h), half_box(b, a, src_w, src_h)]
    return "fit", []


def build(diffs, looks, src_w, src_h, aw, fps) -> dict:
    """The plan from what was seen: diffs per frame, looks [(frame, faces, patches)] in the small frames."""
    total = len(diffs)
    k = src_w / aw
    edges = [0, *cuts(diffs), total]
    segs = []
    for a, b in zip(edges, edges[1:]):
        for first, end, people in _shot([x for x in looks if a <= x[0] < b], a, b, fps, aw):
            mode, boxes = layout_of([tuple(v * k for v in f) for f in people], src_w, src_h)
            last = segs[-1] if segs else None
            same = last and last["mode"] == mode and len(last["boxes"]) == len(boxes) and all(
                abs(p["x"] - q["x"]) < src_w * 0.05 and abs(p["y"] - q["y"]) < src_h * 0.07
                and abs(p["w"] - q["w"]) < src_w * 0.06 for p, q in zip(last["boxes"], boxes))
            if same or (last and end - first < fps * 0.4):     # nothing changes, or a flash too short to follow
                last["b"] = end
            else:
                segs.append({"a": first, "b": end, "mode": mode, "boxes": boxes})
    while len(segs) > MAX_SEGMENTS:
        i = min(range(len(segs)), key=lambda k: segs[k]["b"] - segs[k]["a"])
        if i:
            segs[i - 1]["b"] = segs[i]["b"]
        else:
            segs[1]["a"] = segs[0]["a"]
        del segs[i]
    return {"fps": fps, "frames": total, "w": src_w, "h": src_h, "segments": segs}


def plan(src, start, end, src_w, src_h, prefilter="", nice=()) -> dict:
    """Look at the clip start..end of `src` and plan its layout. `prefilter`: a crop applied first (the part of the
    video a style shows), src_w x src_h being the size after it. None when there's nothing to plan (a vertical or
    square video, no face finder, no frames)."""
    import cv2
    if src_w / max(1, src_h) < MIN_ASPECT or not YUNET.is_file() or not hasattr(cv2, "FaceDetectorYN"):
        return None
    fps = config.OUT_FPS
    aw = min(AW, _even(src_w))
    ah = _even(round(aw * src_h / src_w))
    faces = _finder(aw, ah)
    diffs, looks, prev = [], [], None
    for n, img in enumerate(_read(src, start, end - start, prefilter, aw, ah, fps, list(nice))):
        small = cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (64, 36), interpolation=cv2.INTER_AREA)
        diffs.append(0.0 if prev is None else float(cv2.absdiff(small, prev).mean()))
        prev = small
        jumped = diffs[-1] > 14                  # perhaps a new shot: look at its very first frame too
        if n % EVERY == 0 or jumped:
            found = faces(img)
            patches = []
            for cx, cy, w, h in found:
                x0, y0 = int(max(0, cx - w / 2)), int(max(0, cy - h / 2))
                part = img[y0:int(cy + h / 2) + 1, x0:int(cx + w / 2) + 1]
                patches.append(cv2.resize(cv2.cvtColor(part, cv2.COLOR_BGR2GRAY), (24, 24)))
            looks.append((n, found, patches))
    if not diffs:
        return None
    return build(diffs, looks, src_w, src_h, aw, fps)


def graph(p, prefilter="") -> str:
    """The ffmpeg filter that draws a plan: from [0:v] to [v0], ready for the captions. Each stretch is cut out of
    the video by its frame numbers, laid out, and the stretches are joined again, so the sound stays in step."""
    fps, segs, n = p["fps"], p["segments"], len(p["segments"])

    def crop(b):
        return f"crop={b['w']}:{b['h']}:{b['x']}:{b['y']}"
    out = [f"[0:v]{prefilter}fps={fps},format=yuv420p" + (f",split={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else "[s0]")]
    for i, s in enumerate(segs):
        cut = f"[s{i}]trim=start_frame={s['a']}" + (f":end_frame={s['b']}" if i < n - 1 else "") + ",setpts=PTS-STARTPTS"
        if s["mode"] == "full":
            out.append(f"{cut},{crop(s['boxes'][0])},scale={W}:{H},setsar=1[p{i}]")
        elif s["mode"] == "split":
            top, low = s["boxes"]
            out.append(f"{cut},split=2[t{i}][u{i}]")
            out.append(f"[t{i}]{crop(top)},scale={W}:{HALF},setsar=1[ta{i}]")
            out.append(f"[u{i}]{crop(low)},scale={W}:{HALF},setsar=1[ua{i}]")
            out.append(f"[ta{i}][ua{i}]vstack,drawbox=x=0:y={HALF - 3}:w={W}:h=6:color=black:t=fill[p{i}]")
        else:
            fg = min(H, _even(round(W * p["h"] / p["w"])))
            out.append(f"{cut},split=2[a{i}][b{i}]")
            out.append(f"[a{i}]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
                       f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg{i}]")
            out.append(f"[b{i}]scale={W}:{fg}[fg{i}]")
            out.append(f"[bg{i}][fg{i}]overlay=0:(H-h)/2,setsar=1[p{i}]")
    if n > 1:
        out.append("".join(f"[p{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v0]")
        return ";".join(out) + ";"
    return ";".join(out).replace("[p0]", "[v0]") + ";"


# where the captions sit in each layout: on the line between the two halves, or low in the frame
CAP_Y = {"split": HALF, "full": 1450, "fit": 1444}


def cap_places(p) -> list:
    """[(from second, to second, caption height)] for the captions, which move with the layout."""
    return [(s["a"] / p["fps"], s["b"] / p["fps"], CAP_Y[s["mode"]]) for s in p["segments"]]
