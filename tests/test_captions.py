"""Check caption preferences and the subtitle events sent to the encoder."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, ".")
WORDS = [{"w": "spoken", "s": 1.0, "e": 1.5}]
FIXED = [{"w": "polished", "s": 1.0, "e": 1.5}]


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f"PASS {name}")


def has_dialogue(ass, style, text=""):
    return any(line.startswith("Dialogue: ") and f",{style}," in line and text in line
               for line in ass.splitlines())


def rejects_caption_value(call):
    try:
        call()
    except HTTPException as error:
        return error.status_code == 400 and "captions" in str(error.detail).lower()
    return False


def caption_text(line):
    """The words of one subtitle event, after the ASS tags."""
    return line.split("}", 1)[-1].strip()


def style_marks(render):
    """Each caption style leaves its own mark in the subtitle file, and Bounce still pops."""
    words = [{"w": "one", "s": 0.2, "e": 0.7}, {"w": "two", "s": 0.7, "e": 1.2}, {"w": "three", "s": 1.2, "e": 1.7}]

    def caps(style, highlight=None):
        return render.build_ass(words, 0, 2.0, "", style, highlight or [], 300, 1380)

    def spoken(ass):
        return [line for line in ass.splitlines() if line.startswith("Dialogue:") and ",Cap," in line]

    check("every style id fits the length the editor keeps", all(len(name) <= 20 for name in render.STYLES))
    check("bounce, hype and clean are still offered", {"bounce", "hype", "clean"} <= set(render.STYLES))
    bounce = caps("bounce")
    check("bounce still pops the spoken word", "\\fscx94" in bounce and "\\t(0,90,\\fscx" in bounce)
    for name in render.STYLES:
        check(f"{name} writes a caption", any(caption_text(line) for line in spoken(caps(name))))
    one = spoken(caps("word"))
    check("one word puts a single word on each caption line", one and all(len(caption_text(line).split()) == 1 for line in one))
    check("karaoke does not grow the spoken word", "\\fscx" not in caps("sing"))
    check("minimal does not grow the spoken word", "\\fscx" not in caps("minimal"))
    check("glow lays a soft colour behind the word", "\\blur" in caps("glow") and "\\blur" in caps("neon") and "\\blur" in caps("softblur"))
    check("word rise moves the spoken word up into the line", "\\move(" in caps("rise"))
    check("type uncovers the spoken word from the left", "\\clip(" in caps("type") and "\\t(" in caps("type"))
    key = caps("keyword", ["two"])
    accent = render.STYLES["keyword"]["active"]
    two = [line for line in spoken(key) if caption_text(line) == "TWO"]
    one = [line for line in spoken(key) if caption_text(line) == "ONE"]
    check("keyword colours only the highlighted word", two and all(f"&H{accent}&" in line for line in two)
          and one and all(f"&H{accent}&" not in line for line in one))
    check("an unknown style falls back to the bounce pop", "\\fscx94" in caps("no-such-style"))


def main(data):
    os.environ.update(PEAKCUT_DATA=data, DATABASE_URL="", PEAKCUT_ACCOUNTS="0")
    from studio import auth, brain, creators, ctx, db, jobs, render, roman, server
    style_marks(render)

    db.init()
    try:
        user = auth.local_user().id
        source = {"type": "file", "name": "Caption test"}
        clip = {"id": "clip1", "start": 0.0, "end": 4.0, "status": "done", "hook": "KEEP THE HOOK",
                "title": "A clip", "description": "", "hashtags": [], "score": 95}

        with ctx.as_user(user):
            creator = creators.create({"name": "Caption test creator", "captions": False})
            inherited = jobs.create(source, {}, enqueue=False, creator=creator["id"])
            live_job = jobs.create(source, {}, enqueue=False, creator=creator["id"], kind="live")
            override = jobs.create(source, {"captions": True}, enqueue=False, creator=creator["id"])
            check("videos and live jobs inherit the creator's caption preference",
                  inherited["options"]["captions"] is False and live_job["options"]["captions"] is False)
            check("a job can override its creator's caption preference", override["options"]["captions"] is True)

            job = jobs.create(source, {}, enqueue=False)
            check("the default is to show captions", job["options"]["captions"] is True)
            job["options"].pop("captions")
            job.update(status="done", source_path=str(Path(data) / "source.mp4"), width=1920, height=1080,
                       duration=4.0, clips=[clip])
            jobs.save(job)
            (Path(data) / "source.mp4").write_bytes(b"x")     # the whole video is still kept, so the clip can be made again

        with (ctx.as_user(user), patch.object(render, "_render_in", return_value={"file": "clip1.mp4"}) as encode,
              patch.object(brain, "english_captions", return_value=FIXED) as prepare,
              patch.object(roman, "words", wraps=roman.words) as romanize,
              patch.object(jobs._q, "put") as queue):
            jobs.render_one(job, clip, WORDS)
            ass = next(x for x in encode.call_args.args if isinstance(x, str) and "[Events]" in x)
            check("an older saved clip still renders spoken captions",
                  clip["status"] == "done" and has_dialogue(ass, "Cap", "POLISHED"))
            check("caption preparation is cached for the clip", prepare.call_count == 1 and clip["fixed_words"] == FIXED)

            for value in ("false", 0, None):
                check(f"the clip editor and creator settings reject {value!r} as a caption preference",
                      rejects_caption_value(lambda: server.rerender(job["id"], clip["id"], {"captions": value}))
                      and rejects_caption_value(lambda: server.new_creator({"name": "Invalid", "captions": value})))
            check("invalid caption changes do not queue a render", not queue.called)

            prepare.reset_mock()
            romanize.reset_mock()
            response = server.rerender(job["id"], clip["id"], {"captions": False})
            check("the clip editor can turn captions off", response["clips"][0]["captions"] is False)
            check("turning captions off queues the clip for rendering", queue.call_args.args[0] == ("clip", job["id"], clip["id"]))
            jobs.render_one(job, clip, WORDS)
            ass = next(x for x in encode.call_args.args if isinstance(x, str) and "[Events]" in x)
            check("turning off captions keeps the hook and skips caption processing",
                  clip["status"] == "done" and not has_dialogue(ass, "Cap") and has_dialogue(ass, "Hook", "KEEP THE HOOK")
                  and not prepare.called and not romanize.called)
            saved = json.loads((Path(job["dir"]) / "job.json").read_text(encoding="utf-8"))["clips"][0]
            check("the saved clip retains both the preference and the cached words",
                  saved["captions"] is False and saved["fixed_words"] == FIXED)

            response = server.rerender(job["id"], clip["id"], {"captions": True})
            check("the clip editor can turn captions back on", response["clips"][0]["captions"] is True)
            jobs.render_one(job, clip, WORDS)
            check("turning captions back on uses the cached words",
                  clip["status"] == "done" and has_dialogue(next(x for x in encode.call_args.args if isinstance(x, str) and "[Events]" in x), "Cap", "POLISHED")
                  and not prepare.called)

            render.render_clip(Path(data) / "source.mp4", Path(data) / "clips", "direct",
                               dict(clip, captions=False), WORDS, 1920, 1080)
            ass = next(x for x in encode.call_args.args if isinstance(x, str) and "[Events]" in x)
            check("the renderer honors captions off even when it receives spoken words",
                  not has_dialogue(ass, "Cap") and has_dialogue(ass, "Hook", "KEEP THE HOOK"))
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="pc-captions-") as data:
        main(data)
