#!/usr/bin/env python3
"""Drive the New person flow through pad_server, without an arm or a camera.

The capture flow is a small state machine and every one of its transitions
happens somewhere different: a visitor's tablet, the runner on the Pi, and
headshot.py. That is exactly the shape of thing that works when each piece is
tested alone and deadlocks when they meet, so this runs the real server in
process and walks the whole path over HTTP.

Writes its jobs to a temp directory, so the real queue is untouched.

    python test_capture.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASSWORD = "test-password"
os.environ["SETUP_PASSWORD"] = PASSWORD
import pad_server as S

JOBS = tempfile.mkdtemp(prefix="pad-test-jobs-")
S.JOBS_DIR = JOBS
S.LAYOUT_PATH = os.path.join(JOBS, "layout.json")
S.load_jobs()

server = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
BASE = "http://127.0.0.1:{}".format(server.server_address[1])
threading.Thread(target=server.serve_forever, daemon=True).start()

fails = []
DABS = [{"row": 0, "col": 0, "pigment": "carbon"},
        {"row": 4, "col": 7, "pigment": "ochre"}]
THUMB = S.THUMB_PREFIX + "aGVsbG8="


def call(path, body=None, password=None, method=None):
    """Returns (status, parsed body). A 4xx is data here, not an exception."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path, data=data, method=method or ("POST" if data is not None else "GET"),
        headers={"Content-Type": "application/json"})
    if data is not None and not body:
        request.data = b"{}"
    if password is not None:
        import base64
        token = base64.b64encode(("studio:" + password).encode()).decode()
        request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:80].decode("utf-8", "replace")}


def check(name, got, want):
    ok = got == want
    print("  {:<46} {}".format(name, "ok" if ok else "GOT {!r}, WANTED {!r}".format(got, want)))
    if not ok:
        fails.append(name)


def state():
    return call("/api/capture")[1]["state"]


print("pad_server on {}, jobs in a temp dir\n".format(BASE))

print("the happy path")
check("starts idle", state(), "idle")
check("request is accepted", call("/api/capture/request", {})[0], 200)
check("and moves to requested", state(), "requested")
check("headshot says it is capturing",
      call("/api/capture/state", {"state": "capturing"}, PASSWORD)[0], 200)
check("state is capturing", state(), "capturing")
code, body = call("/api/capture/propose", {"dabs": DABS, "thumb": THUMB}, PASSWORD)
check("a render is proposed", code, 200)
check("the pad is told how many dabs", body.get("dab_count"), len(DABS))
check("and gets the thumbnail to show", body.get("thumb"), THUMB)
before = len(call("/api/jobs")[1]["jobs"])
code, body = call("/api/capture/decide", {"decision": "paint"})
check("accepting queues it", code, 201)
check("the queue grew by one", len(call("/api/jobs")[1]["jobs"]), before + 1)
check("and the flow is idle again", state(), "idle")

print("\nthe job it made is an ordinary job")
job = call("/api/jobs/{}".format(body["id"]))[1]
check("same dabs", job["dabs"], DABS)
check("two copies, like any other", len(job["copies"]), 2)
check("carries the thumbnail", job["thumb"], THUMB)
check("written to disk", os.path.exists(os.path.join(JOBS, "{}.json".format(job["id"]))), True)

print("\nsaying no keeps nothing")
call("/api/capture/request", {})
call("/api/capture/propose", {"dabs": DABS, "thumb": THUMB}, PASSWORD)
count = len(call("/api/jobs")[1]["jobs"])
check("binning it goes back to idle",
      call("/api/capture/decide", {"decision": "bin"})[1]["capture"]["state"], "idle")
check("nothing was queued", len(call("/api/jobs")[1]["jobs"]), count)
check("and the dabs are gone", S._capture_dabs, [])
check("so accepting now is refused",
      call("/api/capture/decide", {"decision": "paint"})[0], 409)

print("\nanother go is another request")
call("/api/capture/request", {})
call("/api/capture/propose", {"dabs": DABS, "thumb": THUMB}, PASSWORD)
check("it goes round again",
      call("/api/capture/decide", {"decision": "again"})[1]["capture"]["state"], "requested")
check("with no render left over", S._capture_dabs, [])
call("/api/capture/decide", {"decision": "bin"})

print("\none person at a time")
call("/api/capture/request", {})
check("a second press is refused", call("/api/capture/request", {})[0], 409)
call("/api/capture/state", {"state": "capturing"}, PASSWORD)
check("and while the arm is working too", call("/api/capture/request", {})[0], 409)
call("/api/capture/state", {"state": "failed", "reason": "no face found"}, PASSWORD)
check("a failure says why", call("/api/capture")[1]["reason"], "no face found")
check("and does not block the next person", call("/api/capture/request", {})[0], 200)
call("/api/capture/decide", {"decision": "bin"})

print("\nthe Funnel URL is public, so the arm's own steps are not")
check("proposing without the password", call("/api/capture/propose", {"dabs": DABS})[0], 401)
check("saying capturing without it",
      call("/api/capture/state", {"state": "capturing"})[0], 401)
check("with the wrong password",
      call("/api/capture/propose", {"dabs": DABS}, "wrong")[0], 401)

print("\nbad input is refused rather than believed")
call("/api/capture/request", {})
check("a dab off the grid",
      call("/api/capture/propose", {"dabs": [{"row": 99, "col": 0, "pigment": "carbon"}]},
           PASSWORD)[0], 400)
check("a pigment with no pot",
      call("/api/capture/propose", {"dabs": [{"row": 0, "col": 0, "pigment": "teal"}]},
           PASSWORD)[0], 400)
check("an empty render", call("/api/capture/propose", {"dabs": []}, PASSWORD)[0], 400)
check("a decision nobody offered",
      call("/api/capture/decide", {"decision": "maybe"})[0], 400)
check("an unknown step", call("/api/capture/sideways", {})[0], 404)
call("/api/capture/decide", {"decision": "bin"})

print("\nthe taught portrait pose survives the setup page")
POSE = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
lay = call("/api/layout")[1]
lay["portrait"] = {"pose": POSE, "target": [0.5, 0.42]}
check("teaching it saves it",
      call("/api/layout", lay, PASSWORD, method="PUT")[1]["portrait"]["pose"], POSE)
# The setup page knows nothing about the portrait pose, so it sends a layout
# without one. Dropping it there would silently un-teach the arm every time
# somebody dragged a sheet, and the next visitor would be told to go and find
# whoever can teach it.
from_setup = {k: lay[k] for k in ("sheets", "pots", "sector", "nogo")}
check("and a save from the setup page keeps it",
      call("/api/layout", from_setup, PASSWORD, method="PUT")[1]["portrait"]["pose"], POSE)
check("six angles or nothing",
      call("/api/layout", dict(lay, portrait={"pose": [1, 2, 3]}), PASSWORD,
           method="PUT")[0], 400)

print("\na request nobody serves ages out")
# Negative, not zero: the Windows clock is coarse enough that "since" and "now"
# can read identical, and then a zero TTL has not strictly elapsed.
S.CAPTURE_TTL = -1.0
call("/api/capture/request", {})
check("so the button is not wedged for the day", state(), "idle")
S.CAPTURE_TTL = 300.0

server.shutdown()
shutil.rmtree(JOBS, ignore_errors=True)
print("\n" + ("FAILURES: " + ", ".join(fails) if fails else "all checks passed"))
sys.exit(1 if fails else 0)
