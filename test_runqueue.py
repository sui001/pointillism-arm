#!/usr/bin/env python3
"""Serve a New person request the way the runner will, with no arm in the room.

run_queue.py is where the three pieces meet: it polls the pad, waits for the
click, runs headshot.py as its own process, and reads the exit code. That is
the seam most likely to be wrong and the one hardest to see wrong, because on
the Pi it only shows up as nothing happening.

So this stands up the real pad_server, pretends the mouse was clicked, and
lets the real runner drive the real headshot.py against a still.

Needs a photograph beside it as sample.jpg, which the repo does not carry.

    python test_runqueue.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PHOTO = os.environ.get("KINOVA_FAKE_CAM", os.path.join(HERE, "sample.jpg"))
if not os.path.exists(PHOTO):
    sys.exit("Put a photograph at {}. See this file's docstring.".format(PHOTO))

PASSWORD = "test-password"
os.environ["SETUP_PASSWORD"] = PASSWORD
os.environ["KINOVA_FAKE_CAM"] = PHOTO
import pad_server as S

JOBS = tempfile.mkdtemp(prefix="pad-test-jobs-")
S.JOBS_DIR = JOBS
S.load_jobs()
server = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
BASE = "http://127.0.0.1:{}".format(server.server_address[1])
threading.Thread(target=server.serve_forever, daemon=True).start()

os.environ["KINOVA_PAD_URL"] = BASE
import run_queue as R
R.PAD = BASE

fails = []
clicks = [0]


def check(name, got, want):
    ok = got == want
    print("  {:<44} {}".format(name, "ok" if ok else "GOT {!r}, WANTED {!r}".format(got, want)))
    if not ok:
        fails.append(name)


# There is no mouse and no buzzer here. Everything else is the real thing.
R.notify.beep = lambda *a, **k: None
R.notify.wait_for_click = lambda *a, **k: clicks.__setitem__(0, clicks[0] + 1)


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.load(r)


def post(path, body):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as r:
        return json.load(r)


print("pad on {}, still {}\n".format(BASE, os.path.basename(PHOTO)))

print("a visitor asks, and the runner serves it")
post("/api/capture/request", {})
check("the pad is holding a request", get("/api/capture")["state"], "requested")
R.capture()
check("it waited for a click first", clicks[0], 1)
cap = get("/api/capture")
check("and there is a render to accept", cap["state"], "proposed")
check("with dabs in it", cap["dab_count"] > 0, True)
check("and a thumbnail for the pad to show",
      cap["thumb"].startswith(S.THUMB_PREFIX), True)

print("\nthe visitor accepts, and it becomes an ordinary job")
before = len(get("/api/jobs")["jobs"])
out = post("/api/capture/decide", {"decision": "paint"})
check("queued", len(get("/api/jobs")["jobs"]), before + 1)
check("the flow is idle again", get("/api/capture")["state"], "idle")
job = get("/api/jobs/{}".format(out["id"]))
check("the job has the render's dabs", job["dab_count"], cap["dab_count"])
check("and two copies, like any other", len(job["copies"]), 2)

print("\nwhen there is nobody to photograph")
# An empty room, not a missing file: this has to exercise headshot giving up
# after its patience runs out, which is the failure a gallery will actually
# see, rather than a typo in a path.
import cv2
import numpy as np
empty = os.path.join(JOBS, "empty-room.jpg")
cv2.imwrite(empty, np.full((480, 640, 3), 90, dtype=np.uint8))
post("/api/capture/request", {})
os.environ["KINOVA_PATIENCE"] = "2"
os.environ["KINOVA_FAKE_CAM"] = empty
R.capture()
cap = get("/api/capture")
check("it says so rather than hanging", cap["state"], "failed")
check("and the reason reaches the pad", bool(cap["reason"]), True)
print("  reason: {}".format(cap["reason"]))

server.shutdown()
shutil.rmtree(JOBS, ignore_errors=True)
print("\n" + ("FAILURES: " + ", ".join(fails) if fails else "all checks passed"))
sys.exit(1 if fails else 0)
