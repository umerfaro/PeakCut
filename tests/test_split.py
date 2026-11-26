"""Podcast split screen: camera cuts, who is on screen in each shot, the part of the frame shown for each person,
captions that move with the layout, and the ffmpeg graph (run for real, keeping every frame so sound stays in step)."""
import os, subprocess, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-split-"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from studio import render, split, templates   # noqa: E402
fails = []


def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n + (f"  {x}" if x and not ok else "")); ok or fails.append(n)


# ---- cuts: a big jump stands out; steady movement doesn't
diffs = [0.0] + [2.0] * 29 + [40.0] + [2.0] * 29 + [25.0] + [2.0] * 29
check("camera cuts are found where the picture jumps", split.cuts(diffs) == [30, 60], split.cuts(diffs))
check("a lot of movement is not a cut", split.cuts([0.0] + [18.0, 20.0, 19.0, 17.0] * 20) == [])

# ---- who is on screen (faces in the 480 x 270 frames looks are made of)
import numpy as np   # noqa: E402
rng = np.random.default_rng(1)


def face(cx, w=44):
    return (cx, 100.0, float(w), float(w) * 1.3)


def looks(a, b, faces):
    """A look every 5th frame of a..b; faces: a function of the frame number."""
    out = []
    for n in range(a, b, split.EVERY):
        f = faces(n)
        out.append((n, f, [rng.integers(0, 255, (24, 24)).astype(np.uint8) for _ in f]))
    return out


two, one, none = lambda n: [face(130), face(350)], lambda n: [face(240, 70)], lambda n: []
L = looks(0, 120, two) + looks(120, 210, one) + looks(210, 285, none) + looks(285, 390, two)
d = [0.0] * 390
for c in (120, 210, 285):
    d[c] = 50.0
p = split.build(d, L, 1920, 1080, 480, 30)
got = [(s["a"], s["b"], s["mode"]) for s in p["segments"]]
check("two people split, one is full screen, nobody is the whole frame, shot by shot",
      got == [(0, 120, "split"), (120, 210, "full"), (210, 285, "fit"), (285, 390, "split")], got)
check("the stretches cover every frame", p["segments"][0]["a"] == 0 and p["segments"][-1]["b"] == 390 and p["frames"] == 390)

p = split.build([0.0] * 300, looks(0, 300, lambda n: [face(130), face(350)] if n % 20 < 10 or n < 60 else [face(130)]), 1920, 1080, 480, 30)
check("a person turning away for a moment is not gone: no flicking between layouts", [s["mode"] for s in p["segments"]] == ["split"],
      [(s["a"], s["mode"]) for s in p["segments"]])
p = split.build([0.0] * 300, looks(0, 300, lambda n: [face(130), face(350)] if n < 150 else [face(130)]), 1920, 1080, 480, 30)
check("someone leaving for good, with no cut, changes the layout", [s["mode"] for s in p["segments"]] == ["split", "full"]
      and 135 <= p["segments"][1]["a"] <= 175, [(s["a"], s["mode"]) for s in p["segments"]])
p = split.build([0.0] * 150, looks(0, 150, lambda n: [face(240, 70), face(440, 20)]), 1920, 1080, 480, 30)
check("a small face in the background is not a speaker", [s["mode"] for s in p["segments"]] == ["full"])
p = split.build([0.0] * 150, looks(0, 150, lambda n: [face(80), face(240), face(400)]), 1920, 1080, 480, 30)
check("three people: the whole frame, nobody cut off", [s["mode"] for s in p["segments"]] == ["fit"])
p = split.build([0.0] * 150, looks(0, 150, lambda n: [face(150, 20), face(330, 21)]), 1920, 1080, 480, 30)
check("a wide shot with far-off faces keeps the whole frame, nobody zoomed past", [s["mode"] for s in p["segments"]] == ["fit"])
still = np.full((24, 24), 90, np.uint8)
poster = [(n, [face(130), face(350)], [rng.integers(0, 255, (24, 24)).astype(np.uint8), still]) for n in range(0, 150, 5)]
p = split.build([0.0] * 150, poster, 1920, 1080, 480, 30)
check("a face on a poster (never moving) is not a person", [s["mode"] for s in p["segments"]] == ["full"])

# ---- a very choppy video stays a manageable number of stretches
d2 = [0.0] * 3000
many = []
for i, a_ in enumerate(range(0, 3000, 15)):
    d2[a_] = 50.0 if a_ else 0.0
    many += looks(a_, a_ + 15, two if i % 2 else one)
p = split.build(d2, many, 1920, 1080, 480, 30)
check("a montage is kept to 60 stretches, still covering every frame", len(p["segments"]) <= split.MAX_SEGMENTS
      and p["segments"][0]["a"] == 0 and p["segments"][-1]["b"] == 3000
      and all(x["b"] == y["a"] for x, y in zip(p["segments"], p["segments"][1:])), len(p["segments"]))

# ---- the parts of the frame shown
a, b = (500.0, 400.0, 170.0, 220.0), (1400.0, 400.0, 170.0, 220.0)
mode, (top, low) = split.layout_of([a, b], 1920, 1080)
inside = lambda box, f: box["x"] < f[0] < box["x"] + box["w"] and box["y"] < f[1] < box["y"] + box["h"]   # noqa: E731
check("each half holds its own person: the left one on top", mode == "split" and inside(top, a) and inside(low, b))
check("...and not the other one's face", top["x"] + top["w"] < b[0] - b[2] / 2 and low["x"] > a[0] + a[2] / 2, (top, low))
for f, o in (((90.0, 80.0, 100.0, 130.0), (1850.0, 1000.0, 100.0, 130.0)), (a, (760.0, 400.0, 170.0, 220.0))):
    box = split.half_box(f, o, 1920, 1080)
    check("a half stays inside the video, in the half's shape", box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["w"] <= 1920
          and box["y"] + box["h"] <= 1080 and abs(box["w"] / box["h"] - 1080 / 960) < 0.02, box)
box = split.full_box((1900.0, 400.0, 170.0, 220.0), 1920, 1080)
check("one person full screen: 9:16, centred on them, never past the edge", box["x"] + box["w"] <= 1920 and abs(box["w"] / box["h"] - 9 / 16) < 0.01, box)

# ---- a style: the podcast split has no streamer cam
look = templates.clean_look({"layout": "podcast", "cam": {"x": 0, "y": 0, "w": 100, "h": 40}})
check("a style keeps the podcast split, without a streamer cam", look["layout"] == "podcast" and look["cam"] is None)
check("it is offered as a layout and as a ready-made style", "podcast" in templates.LAYOUT_NAMES and "podcast" in render.LAYOUTS
      and any(t["look"]["layout"] == "podcast" for t in templates.BUILTIN))
check("the fixed left and right split stays as it was", templates.clean_look({"layout": "split"})["layout"] == "split")

# ---- captions go with the layout
places = split.cap_places(split.build(d, L, 1920, 1080, 480, 30))
words = [{"w": f"w{i}", "s": 0.1 + i * 0.4, "e": 0.4 + i * 0.4} for i in range(32)]
render.install_fonts()
ass = render.build_ass(words, 0.0, 13.0, "", look["cap"], [], 200, 960, show_hook=False, places=places)
import re   # noqa: E402


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


bad = []
for line in ass.splitlines():
    m = re.match(r"Dialogue: \d+,([\d:.]+),([\d:.]+),Cap,.*?\\pos\((-?\d+),(-?\d+)\)", line)
    if not m or "\\p1" in line:
        continue
    at, y = secs(m[1]), int(m[4])
    want = next(h for a_, b_, h in places if at < b_ - 0.001)
    if abs(y - want) > 60:
        bad.append((at, y, want))
check("each caption sits where its moment's layout puts it", not bad, bad[:4])
check("...on the line between the halves in the split, lower in full screen", {h for _, _, h in places} >= {960, 1450})
plain = render.build_ass(words, 0.0, 13.0, "", look["cap"], [], 200, 960, show_hook=False)
check("without a layout that changes, captions are as before", plain == render.build_ass(words, 0.0, 13.0, "", look["cap"], [], 200, 960, show_hook=False, places=None))

# ---- the graph itself, run by ffmpeg: every frame comes out, once, 1080x1920
plan = split.build(d, L, 640, 360, 480, 30)
out = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=13", "-filter_complex",
                      split.graph(plan).rstrip(";"), "-map", "[v0]", "-f", "framecrc", "-"], capture_output=True, text=True, timeout=600)
frames = [l for l in out.stdout.splitlines() if l and not l.startswith("#")]
check("ffmpeg accepts the graph", out.returncode == 0, out.stderr[-600:])
check("every frame comes out once, so the sound stays in step", len(frames) == 390, len(frames))
check("...at 1080x1920", "#dimensions 0: 1080x1920" in out.stdout)
one_seg = {"fps": 30, "frames": 60, "w": 640, "h": 360, "segments": [{"a": 0, "b": 60, "mode": "full", "boxes": [split.full_box((320, 180, 60, 80), 640, 360)]}]}
out = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=2", "-filter_complex",
                      split.graph(one_seg).rstrip(";"), "-map", "[v0]", "-f", "framecrc", "-"], capture_output=True, text=True, timeout=600)
check("a clip that never changes layout works too", out.returncode == 0 and len([l for l in out.stdout.splitlines() if l and not l.startswith("#")]) == 60, out.stderr[-400:])
check("a vertical video has nothing to split", split.plan("missing.mp4", 0, 5, 720, 1280) is None)

print(f"\n{len(fails)} FAILED: {fails}" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
