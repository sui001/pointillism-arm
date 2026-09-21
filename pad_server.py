#!/usr/bin/env python3
"""Serve the pointillism pad, its print queue, and the studio layout.

Binds to 127.0.0.1 by default, NOT 0.0.0.0: reachable only from this Pi
itself, and from `tailscale funnel`, which proxies the public URL to
127.0.0.1 locally. It is deliberately NOT on the campus network directly.

Only the files in PAGES are served. The rest of ~/kinova is not public.

Anyone with the Funnel URL can submit, and submitted dabs later become arm
positions, so everything is validated here and the grid is fixed server-side.
Nothing moves the arm automatically: paint_sim.py is run by hand.

Jobs are written to ~/kinova/jobs/<id>.json and reloaded on start, so a
restart no longer empties the queue. The layout lives in ~/kinova/layout.json.

The setup page and saving a layout need a password, since the layout becomes
arm positions and the pad is on a public URL. It is read from SETUP_PASSWORD or
from setup_password.txt beside this file, and neither is ever committed. With no
password set, the setup page is refused outright rather than left open.

    GET  /api/jobs        queue summary, oldest (next to paint) first, done ones gone
    POST /api/jobs        submit {dabs, thumb}; queued as 2 copies
    GET  /api/jobs/<id>   full job, for paint_sim.py
    POST /api/jobs/<id>/done   painted and collected, drop it from the queue
    GET  /api/layout      where the sheets, pots, brushes and no-go zones are
    PUT  /api/layout      save that, from setup.html
    GET  /api/capture     how far the New person flow has got
    POST /api/capture/request   a visitor asking to be photographed
    POST /api/capture/state     headshot.py saying what it is doing
    POST /api/capture/propose   headshot.py offering a render to accept
    POST /api/capture/decide    the visitor saying paint it, again, or bin it

The capture state is held in memory and never written to disk, unlike the
queue. A render of somebody's face that nobody accepted should not outlive
the moment, and the accepted ones become ordinary jobs anyway.

Base frame, metres: +x is the front of the arm (away from the base panel),
+y is the arm's left, so the right side is negative y. rot is 0 or 90 deg.

    BIND=0.0.0.0 PORT=8010 ~/kinova-py310/bin/python ~/kinova/pad_server.py
"""
import base64
import glob
import hmac
import json
import math
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8010"))
DIR = os.path.dirname(os.path.abspath(__file__))
BIND = os.environ.get("BIND") or "127.0.0.1"
JOBS_DIR = os.path.join(DIR, "jobs")
LAYOUT_PATH = os.path.join(DIR, "layout.json")
PASSWORD_PATH = os.path.join(DIR, "setup_password.txt")
# A whitelist, not a document root: the rest of ~/kinova is not public, and the
# Funnel URL means "not public" has to mean it. A new page or asset that is not
# listed here is a 404, which is the right way round.
PAGES = ("/pad.html", "/portrait.html", "/setup.html", "/display.html",
         "/pad.css", "/queue.js")
GUARDED = "/setup.html"
MAX_POINTS = 4000

GRID = {"cols": 30, "rows": 42, "pitch_mm": 7}
PIGMENTS = ("carbon", "green", "ochre", "ultramarine", "venetian")
COPIES = [{"slot": "display"}, {"slot": "keepsake"}]
THUMB_PREFIX = "data:image/png;base64,"
MAX_BODY = 512 * 1024
MAX_THUMB = 200 * 1024
MAX_JOBS = 50
THUMBS_SHOWN = 5
MAX_ZONES = 12

# Sui's stated setup: pots to the right of the arm, two portrait sheets in
# front about a page width apart, base panel is the back.
DEFAULT_LAYOUT = {
    "sheets": {
        "display": {"x": 0.50, "y": 0.21, "rot": 0},
        "keepsake": {"x": 0.50, "y": -0.21, "rot": 0},
    },
    "pots": {"x": 0.34, "y": -0.40, "rot": 90},
    "sector": {"from": -150.0, "to": 75.0},
    "nogo": [],
}

_lock = threading.Lock()
_jobs = []
_next_id = 1
# what paint_sim is doing right now, for display.html. Runtime only.
_run = {"state": "idle", "job": None, "total": 0, "index": 0,
        "points": [], "labels": [], "label": "", "at": None,
        "started": 0.0, "updated": 0.0}

# Where the New person flow has got to. Runtime only, deliberately: see the
# module docstring. The dabs live here between headshot.py rendering them and
# the visitor deciding, and go no further if the answer is no.
#
#   idle       nobody is being photographed
#   requested  somebody pressed the button; run_queue.py will pick it up
#   capturing  headshot.py has the arm and is framing a face
#   proposed   there is a render on screen waiting for a yes or a no
#   failed     it gave up, and `reason` says why
CAPTURE_IDLE = {"state": "idle", "reason": "", "thumb": "", "dab_count": 0,
                "since": 0.0}
_capture = dict(CAPTURE_IDLE)
_capture_dabs = []
# A request nobody serves, or a render nobody answers, must not wedge the button
# for the rest of the day. Five minutes is far longer than either step takes and
# short enough that the next visitor is not locked out.
CAPTURE_TTL = float(os.environ.get("CAPTURE_TTL", "300"))
CAPTURE_STATES = ("requested", "capturing", "proposed", "failed")


def clean_dabs(raw):
    if not isinstance(raw, list) or not raw or len(raw) > GRID["cols"] * GRID["rows"]:
        raise ValueError("dabs must be a non-empty list")
    out, seen = [], set()
    for d in raw:
        col, row, pig = int(d["col"]), int(d["row"]), d["pigment"]
        if not (0 <= col < GRID["cols"] and 0 <= row < GRID["rows"]):
            raise ValueError("dab off the grid")
        if pig not in PIGMENTS:
            raise ValueError("unknown pigment")
        if (row, col) not in seen:
            seen.add((row, col))
            out.append({"col": col, "row": row, "pigment": pig})
    return out


def clean_thumb(raw):
    if isinstance(raw, str) and raw.startswith(THUMB_PREFIX) and len(raw) <= MAX_THUMB:
        return raw
    return ""


def num(value, lo, hi):
    v = float(value)
    if math.isnan(v) or not (lo <= v <= hi):
        raise ValueError("value {} outside {} to {}".format(value, lo, hi))
    return round(v, 4)


def rot(value):
    v = int(value)
    if v not in (0, 90):
        raise ValueError("rot must be 0 or 90")
    return v


def placed(item):
    return {"x": num(item["x"], -1.0, 1.0), "y": num(item["y"], -1.0, 1.0), "rot": rot(item["rot"])}


def clean_portrait(data):
    """The pose the arm looks from for a headshot, taught by hand in teach.py.

    Joint angles rather than a Cartesian pose, because a pose read off the
    arm's own joints is reachable by construction: teaching it meant having the
    arm there. Same argument as the paper corners.
    """
    if not data:
        return None
    pose = data.get("pose")
    if not isinstance(pose, list) or len(pose) != 6:
        raise ValueError("a portrait pose is six joint angles")
    target = data.get("target") or [0.50, 0.42]
    return {
        "pose": [num(v, -360.0, 360.0) for v in pose],
        "target": [num(target[0], 0.05, 0.95), num(target[1], 0.05, 0.95)],
        "taught_at": float(data.get("taught_at") or time.time()),
    }


def clean_layout(data, keep=None):
    """Validate a layout. `keep` is the stored one, for fields the sender omits.

    The setup page knows nothing about the portrait pose, so a layout saved
    from it arrives without one. Dropping it there would silently un-teach the
    arm every time somebody drags a sheet, and the next visitor would be told
    to go and find someone who can teach it.
    """
    out = {"sheets": {}, "nogo": []}
    portrait = data.get("portrait") or (keep or {}).get("portrait")
    portrait = clean_portrait(portrait)
    if portrait:
        out["portrait"] = portrait
    for name in ("display", "keepsake"):
        out["sheets"][name] = placed(data["sheets"][name])
    out["pots"] = placed(data["pots"])
    sector = data.get("sector") or DEFAULT_LAYOUT["sector"]
    start, end = num(sector["from"], -360, 360), num(sector["to"], -360, 360)
    if (end - start) % 360 < 1.0:
        raise ValueError("the working sector must be wider than 1 degree")
    out["sector"] = {"from": start, "to": end}
    for zone in list(data.get("nogo", []))[:MAX_ZONES]:
        out["nogo"].append({
            "x": num(zone["x"], -1.0, 1.0),
            "y": num(zone["y"], -1.0, 1.0),
            "sx": num(zone["sx"], 0.02, 1.6),
            "sy": num(zone["sy"], 0.02, 1.6),
        })
    return out


def read_layout():
    try:
        with open(LAYOUT_PATH) as fh:
            return clean_layout(json.load(fh))
    except Exception:
        return json.loads(json.dumps(DEFAULT_LAYOUT))


def load_jobs():
    global _next_id
    os.makedirs(JOBS_DIR, exist_ok=True)
    found = []
    for path in glob.glob(os.path.join(JOBS_DIR, "*.json")):
        try:
            with open(path) as fh:
                found.append(json.load(fh))
        except Exception:
            print("skipping unreadable job file {}".format(path), flush=True)
    found.sort(key=lambda j: j["id"])
    _jobs.extend(found)
    _next_id = (found[-1]["id"] + 1) if found else 1


def save_job(job):
    tmp = os.path.join(JOBS_DIR, "{}.json.tmp".format(job["id"]))
    with open(tmp, "w") as fh:
        json.dump(job, fh)
    os.replace(tmp, os.path.join(JOBS_DIR, "{}.json".format(job["id"])))


class Full(Exception):
    """The queue is full. Its own type because it is a 429, not a 400."""


def queue_job(dabs, thumb):
    """Put a validated set of dabs in the queue. Call with _lock held.

    Both ways in end up here: a visitor drawing on the pad, and a visitor
    accepting a render of their own face. They are the same job by the time the
    arm sees them, and that is the point of the capture flow ending in one.
    """
    global _next_id
    if sum(1 for j in _jobs if not j.get("done")) >= MAX_JOBS:
        raise Full("the queue is full")
    job = {
        "id": _next_id,
        "submitted_at": time.time(),
        "grid": dict(GRID),
        "dabs": dabs,
        "dab_count": len(dabs),
        "thumb": clean_thumb(thumb),
        "copies": [dict(c) for c in COPIES],
    }
    _next_id += 1
    _jobs.append(job)
    try:
        save_job(job)
    except OSError as e:
        print("could not save job {}: {}".format(job["id"], e), flush=True)
    return job, len(_jobs)


def capture_now():
    """The capture state, with a stale one aged out. Call with _lock held.

    Expiry is applied on read rather than by a timer thread: there is nothing to
    do about a stale state until somebody asks, and a timer would be one more
    thing to get wrong around a restart.
    """
    global _capture, _capture_dabs
    if (_capture["state"] in CAPTURE_STATES
            and time.time() - _capture["since"] > CAPTURE_TTL):
        _capture = dict(CAPTURE_IDLE)
        _capture_dabs = []
    return _capture


def capture_set(state, reason="", thumb="", dab_count=0):
    """Move the capture flow to a state. Call with _lock held."""
    global _capture
    _capture = {"state": state, "reason": str(reason)[:200], "thumb": thumb,
                "dab_count": int(dab_count), "since": time.time()}
    return _capture


def setup_password():
    """From SETUP_PASSWORD, else setup_password.txt beside this file. Never committed."""
    value = os.environ.get("SETUP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open(PASSWORD_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def summary(job, with_thumb=True):
    out = {k: job[k] for k in ("id", "dab_count", "submitted_at")}
    out["thumb"] = job["thumb"] if with_thumb else ""
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorised(self):
        """Basic auth on the setup page and on saving a layout. No password, no entry."""
        wanted = setup_password()
        if not wanted:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        given = decoded.split(":", 1)[1] if ":" in decoded else ""
        return hmac.compare_digest(given, wanted)

    def demand_password(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Studio setup"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("body missing or too large")
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path == "/api/jobs":
            with _lock:
                waiting = [j for j in _jobs if not j.get("done")]
                jobs = [summary(j, i < THUMBS_SHOWN) for i, j in enumerate(waiting)]
            return self.send_json(200, {"jobs": jobs, "total": len(jobs)})
        if self.path == "/api/layout":
            with _lock:
                return self.send_json(200, read_layout())
        if self.path == "/api/run":
            with _lock:
                return self.send_json(200, dict(_run))
        if self.path == "/api/capture":
            with _lock:
                return self.send_json(200, dict(capture_now()))
        if self.path.startswith("/api/jobs/"):
            try:
                jid = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                return self.send_json(400, {"error": "job id must be a number"})
            with _lock:
                job = next((j for j in _jobs if j["id"] == jid), None)
            if job is None:
                return self.send_json(404, {"error": "no job {}".format(jid)})
            return self.send_json(200, job)
        if self.path == "/":
            self.path = "/pad.html"
        page = self.path.split("?")[0]
        if page not in PAGES:
            return self.send_json(404, {"error": "not found"})
        if page == GUARDED and not self.authorised():
            return self.demand_password()
        super().do_GET()

    def do_PUT(self):
        if self.path != "/api/layout":
            return self.send_json(404, {"error": "unknown endpoint"})
        if not self.authorised():
            return self.demand_password()
        with _lock:
            stored = read_layout()
        try:
            layout = clean_layout(self.read_body(), stored)
        except Exception as e:
            return self.send_json(400, {"error": str(e) or "bad layout"})
        with _lock:
            tmp = LAYOUT_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(layout, fh, indent=2)
            os.replace(tmp, LAYOUT_PATH)
        self.send_json(200, layout)

    def post_run(self):
        """paint_sim reporting what it is about to do, and then where it is."""
        if not self.authorised():
            return self.demand_password()
        try:
            data = self.read_body()
        except Exception:
            return self.send_json(400, {"error": "bad body"})
        event = data.get("event")
        now = time.time()
        with _lock:
            if event == "plan":
                _run.update({
                    "state": "running",
                    "job": data.get("job"),
                    "total": int(data.get("total") or 0),
                    "points": (data.get("points") or [])[:MAX_POINTS],
                    "labels": (data.get("labels") or [])[:MAX_POINTS],
                    "index": 0, "label": "", "at": None,
                    "started": now, "updated": now,
                })
            elif event == "progress":
                _run.update({
                    "index": int(data.get("index") or 0),
                    "label": str(data.get("label") or "")[:120],
                    "at": [data.get("x"), data.get("y"), data.get("z")],
                    "updated": now,
                })
            else:
                _run.update({"state": str(event or "done")[:20], "updated": now})
        return self.send_json(200, {"ok": True})

    def post_capture(self, step):
        """The New person flow. Four steps, two of them from the arm's own Pi.

        request and decide are the visitor's, so they are open the way the pad
        is. state and propose come from headshot.py and carry the studio
        password, because a proposal turns into arm positions and anyone with
        the Funnel URL can reach this.
        """
        global _capture_dabs
        # Read the body before refusing on the password. A handler that answers
        # and returns with bytes still in the socket gives the client a reset
        # instead of the 401 it was told about, which showed up here once as a
        # ConnectionAbortedError in the middle of a passing test.
        try:
            data = self.read_body()
        except Exception:
            data = {}       # request carries nothing, and a bad body fails below
        if step in ("state", "propose") and not self.authorised():
            return self.demand_password()

        with _lock:
            now = capture_now()

            if step == "request":
                # One person at a time. The button is not a queue: a second
                # press while somebody is being photographed would either
                # interrupt them or silently do nothing, and refusing says so.
                if now["state"] in ("requested", "capturing", "proposed"):
                    return self.send_json(409, {"error": "somebody is already being painted",
                                                "capture": dict(now)})
                _capture_dabs = []
                return self.send_json(200, dict(capture_set("requested")))

            if step == "state":
                state = data.get("state")
                if state not in ("capturing", "failed", "idle"):
                    return self.send_json(400, {"error": "unknown state"})
                if state == "idle":
                    _capture_dabs = []
                    return self.send_json(200, dict(capture_set("idle")))
                return self.send_json(200, dict(capture_set(state, data.get("reason", ""))))

            if step == "propose":
                try:
                    dabs = clean_dabs(data["dabs"])
                except (ValueError, KeyError, TypeError) as e:
                    return self.send_json(400, {"error": str(e) or "expected {dabs:[...], thumb}"})
                _capture_dabs = dabs
                return self.send_json(200, dict(capture_set(
                    "proposed", thumb=clean_thumb(data.get("thumb")),
                    dab_count=len(dabs))))

            # decide
            choice = data.get("decision")
            if choice not in ("paint", "again", "bin"):
                return self.send_json(400, {"error": "decision must be paint, again or bin"})
            if choice == "bin":
                _capture_dabs = []
                return self.send_json(200, {"capture": dict(capture_set("idle"))})
            if choice == "again":
                # The same path from the top, which is why there is no separate
                # retry: another go is another request.
                _capture_dabs = []
                return self.send_json(200, {"capture": dict(capture_set("requested"))})
            if now["state"] != "proposed" or not _capture_dabs:
                return self.send_json(409, {"error": "there is nothing to accept",
                                            "capture": dict(now)})
            try:
                job, position = queue_job(_capture_dabs, now["thumb"])
            except Full as e:
                return self.send_json(429, {"error": str(e)})
            _capture_dabs = []
            capture_set("idle")
            return self.send_json(201, {"id": job["id"], "position": position,
                                        "capture": dict(_capture)})

    def mark_done(self, raw):
        """Painted and collected. It leaves the queue but the file stays as the record."""
        if not self.authorised():
            return self.demand_password()
        try:
            jid = int(raw)
        except ValueError:
            return self.send_json(400, {"error": "job id must be a number"})
        with _lock:
            job = next((j for j in _jobs if j["id"] == jid), None)
            if job is None:
                return self.send_json(404, {"error": "no job {}".format(jid)})
            job["done"] = True
            job["done_at"] = time.time()
            try:
                save_job(job)
            except OSError as e:
                print("could not save job {}: {}".format(jid, e), flush=True)
            waiting = sum(1 for j in _jobs if not j.get("done"))
        return self.send_json(200, {"id": jid, "waiting": waiting})

    def do_POST(self):
        if self.path == "/api/run":
            return self.post_run()
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "capture"]:
            if parts[2] in ("request", "state", "propose", "decide"):
                return self.post_capture(parts[2])
            try:
                self.read_body()          # drain it, so the 404 is what arrives
            except Exception:
                pass
            return self.send_json(404, {"error": "unknown capture step"})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "done":
            return self.mark_done(parts[2])
        if self.path != "/api/jobs":
            return self.send_json(404, {"error": "unknown endpoint"})
        try:
            data = self.read_body()
            dabs = clean_dabs(data["dabs"])
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json(400, {"error": str(e) or "expected {dabs:[...], thumb}"})
        with _lock:
            try:
                job, position = queue_job(dabs, data.get("thumb"))
            except Full as e:
                return self.send_json(429, {"error": str(e)})
        self.send_json(201, {"id": job["id"], "position": position})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    load_jobs()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print("pointillism pad serving http://{}:{}/  ({} job(s) reloaded)".format(
        BIND, PORT, len(_jobs)), flush=True)
    server.serve_forever()
