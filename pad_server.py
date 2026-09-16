#!/usr/bin/env python3
"""Serve the pointillism pad, its print queue, and the studio layout.

Binds to 127.0.0.1 by default, NOT 0.0.0.0: reachable only from this Pi
itself, and from `tailscale funnel`, which proxies the public URL to
127.0.0.1 locally. It is deliberately NOT on the campus network directly.

Only pad.html and setup.html are served. The rest of ~/kinova is not public.

Anyone with the Funnel URL can submit, and submitted dabs later become arm
positions, so everything is validated here and the grid is fixed server-side.
Nothing moves the arm automatically: paint_sim.py is run by hand.

Jobs are written to ~/kinova/jobs/<id>.json and reloaded on start, so a
restart no longer empties the queue. The layout lives in ~/kinova/layout.json.

    GET  /api/jobs        queue summary, oldest (next to paint) first
    POST /api/jobs        submit {dabs, thumb}; queued as 2 copies
    GET  /api/jobs/<id>   full job, for paint_sim.py
    GET  /api/layout      where the sheets, pots, brushes and no-go zones are
    PUT  /api/layout      save that, from setup.html

Base frame, metres: +x is the front of the arm (away from the base panel),
+y is the arm's left, so the right side is negative y. rot is 0 or 90 deg.

    BIND=0.0.0.0 PORT=8010 ~/kinova-py310/bin/python ~/kinova/pad_server.py
"""
import glob
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
PAGES = ("/pad.html", "/setup.html")

GRID = {"cols": 30, "rows": 42, "pitch_mm": 7}
PIGMENTS = ("carbon", "titanium", "ochre", "ultramarine", "venetian")
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


def clean_layout(data):
    out = {"sheets": {}, "nogo": []}
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

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("body missing or too large")
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path == "/api/jobs":
            with _lock:
                jobs = [summary(j, i < THUMBS_SHOWN) for i, j in enumerate(_jobs)]
            return self.send_json(200, {"jobs": jobs, "total": len(jobs)})
        if self.path == "/api/layout":
            with _lock:
                return self.send_json(200, read_layout())
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
        if self.path.split("?")[0] not in PAGES:
            return self.send_json(404, {"error": "not found"})
        super().do_GET()

    def do_PUT(self):
        if self.path != "/api/layout":
            return self.send_json(404, {"error": "unknown endpoint"})
        try:
            layout = clean_layout(self.read_body())
        except Exception as e:
            return self.send_json(400, {"error": str(e) or "bad layout"})
        with _lock:
            tmp = LAYOUT_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(layout, fh, indent=2)
            os.replace(tmp, LAYOUT_PATH)
        self.send_json(200, layout)

    def do_POST(self):
        global _next_id
        if self.path != "/api/jobs":
            return self.send_json(404, {"error": "unknown endpoint"})
        try:
            data = self.read_body()
            dabs = clean_dabs(data["dabs"])
        except (ValueError, KeyError, TypeError) as e:
            return self.send_json(400, {"error": str(e) or "expected {dabs:[...], thumb}"})
        with _lock:
            if len(_jobs) >= MAX_JOBS:
                return self.send_json(429, {"error": "the queue is full"})
            job = {
                "id": _next_id,
                "submitted_at": time.time(),
                "grid": dict(GRID),
                "dabs": dabs,
                "dab_count": len(dabs),
                "thumb": clean_thumb(data.get("thumb")),
                "copies": [dict(c) for c in COPIES],
            }
            _next_id += 1
            _jobs.append(job)
            position = len(_jobs)
            try:
                save_job(job)
            except OSError as e:
                print("could not save job {}: {}".format(job["id"], e), flush=True)
        self.send_json(201, {"id": job["id"], "position": position})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    load_jobs()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print("pointillism pad serving http://{}:{}/  ({} job(s) reloaded)".format(
        BIND, PORT, len(_jobs)), flush=True)
    server.serve_forever()
