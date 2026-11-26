"""No working AI key, no clipping: videos and uploads are refused with what to do, nothing is picked from the sound,
and a model list only offers models the key can use. Runs against the code directly, with its own empty data folder."""
import os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-needkey-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_AI_KEY="",
                  PEAKCUT_AI_KEY_FILE="", PEAKCUT_AI_MODELS="", PEAKCUT_AI_PROVIDER="", PEAKCUT_UPDATE_CHECK="0")
sys.path.insert(0, ".")
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from studio import ai, brain, config, db as database, server

database.init()

fails = []


def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


c = TestClient(server.app, base_url="http://127.0.0.1:8765")
H = {"Origin": "http://127.0.0.1:8765"}

# ---- without a key nothing starts
r = c.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}, headers=H)
check("a link is refused without a key", (r.status_code, "AI key" in r.json().get("detail", "")), (409, True))
r = c.post("/api/upload/start", json={"name": "a.mp4", "size": 1000}, headers=H)
check("an upload is refused before the file is sent", r.status_code, 409)
check("no video was made", c.get("/api/jobs", headers=H).json(), [])

# ---- a key that stopped working is tried once more before anything starts
with patch.object(config, "ai_ready", return_value=True):
    ai.problem.update(text="Gemini refused the key.", at=1e12)
    with patch.object(ai, "test", side_effect=ai.AIError("HTTP 401 bad key")):
        try:
            brain.require_ai(); said = ""
        except ValueError as e:
            said = str(e)
    check("a failing key says so and starts nothing", "isn't working" in said, True)
    with patch.object(ai, "test", side_effect=lambda: ai.clear_problem()):
        brain.require_ai()
    check("once it answers again, work starts", ai.problem["text"], "")

# ---- nothing is picked from the sound
words = [{"w": f"word{i}.", "s": float(i), "e": i + 0.5} for i in range(60)]
loud = np.linspace(-40.0, -5.0, 61)
with patch.object(brain, "ask_ai", side_effect=RuntimeError("The AI did not answer")):
    try:
        brain.find_clips(words, loud, 60.0); got = "clips"
    except brain.NoAI:
        got = "refused"
check("an AI that never answers gives an error, not loud moments", got, "refused")
with patch.object(brain, "ask_ai", return_value=([], "m", 1)):
    clips, info = brain.find_clips(words, loud, 60.0, force=True)
check("clip anyway with nothing from the AI gives nothing", clips, [])
check("the loudness picker is gone", hasattr(brain, "fallback_by_loudness"), False)


# ---- only models the key can use are listed (Google also lists retired ones)
class R:
    def __init__(s, code, data=None):
        s.status_code, s._d, s.text = code, data or {}, ""

    def json(s):
        return s._d


listing = {"models": [{"name": "models/" + m, "supportedGenerationMethods": ["generateContent"]}
                      for m in ("gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.1-pro-preview")]}
answers = {"gemini-3.5-flash-lite": 200, "gemini-2.5-flash": 404, "gemini-3.8-flash": 503, "gemini-3.1-pro-preview": 429}
with patch.object(ai.httpx, "get", return_value=R(200, listing)), \
        patch.object(ai.httpx, "post", side_effect=lambda url, **k: R(answers[url.split("/")[-1].split(":")[0]])):
    found = ai.list_models("gemini", "", "k" * 30)
check("retired and paid-only models are left out, busy ones stay", found, ["gemini-3.5-flash-lite", "gemini-3.8-flash"])

# ---- a busy model (HTTP 503) is said in plain words and is not a broken key
busy = ai.AIError('HTTP 503 { "error": { "status": "UNAVAILABLE" } }')
check("a busy model is explained without the raw answer", ("very busy" in ai.explain(busy), "{" in ai.explain(busy)), (True, False))
with patch.object(ai, "ready", return_value=True), patch.object(ai, "models", return_value=["m1"]), \
        patch.object(ai, "call", side_effect=busy), patch("time.sleep"):
    got = ai.test()
check("Save and test treats it as busy, not failed", (got["ok"], got["busy"], ai.problem["text"]), (True, True, ""))

# ---- the saved key can be removed, and then nothing starts
c.put("/api/ai", json={"provider": "gemini", "models": "gemini-3.5-flash-lite", "key": "k" * 30}, headers=H)
check("a key was saved", c.get("/api/ai", headers=H).json()["has_key"], True)
r = c.delete("/api/ai/key", headers=H)
check("Remove forgets it and keeps the service", (r.status_code, r.json()["has_key"], r.json()["provider"]), (200, False, "gemini"))
check("without it no video starts", c.post("/api/upload/start", json={"name": "a.mp4", "size": 1000}, headers=H).status_code, 409)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
