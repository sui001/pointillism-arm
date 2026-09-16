#!/usr/bin/env python3
"""Dry-run a pointillism pad job on the arm: every move, no paint, no contact.

Pretends the gripper works. Each colour has its own brush standing in its own
pot, so collecting the brush and loading it are the same place: it lifts the
brush out of that colour's pot, dabs every dab of that colour onto both
sheets, returns to the pot to reload as it goes, and stands the brush back in
the pot before moving to the next colour. Each dab goes onto the display
sheet then the keepsake sheet, so the two copies finish together.

There is no gripper yet, so nothing grips. The pauses are still spent, so the
run takes about as long as the real thing would: KINOVA_GRIP_DWELL at each
pick up and put down, a shorter pause at each reload and each dab.

Where the sheets, the pots, the working sweep and the no-go boxes are comes
from the setup page (/setup.html), not from this file. Heights are still
placeholders: it travels 200 mm above the base plane and a dip stops 160 mm
above it, so nothing touches anything.

Start from the factory Home pose (not Zero: fully vertical is a singularity
and a bad place to begin Cartesian moves). It returns to where it started.

    # plan only, arm untouched apart from reading its state:
    KINOVA_JOB=latest ~/kinova-py310/bin/python ~/kinova/paint_sim.py
    # move:
    KINOVA_JOB=latest KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/paint_sim.py

KINOVA_JOB         job id from the pad queue, or 'latest'
KINOVA_MAX_DABS    dabs to visit, evenly sampled from the job, default 12
KINOVA_SPEED       m/s, default 0.08, capped at 0.15
KINOVA_HOVER       travel height in m above the base plane, default 0.20
KINOVA_DIP         dip depth in m, default 0.04
KINOVA_GRIP_DWELL  pretend gripper time in s per pick up or put down, default 1.2
KINOVA_PAD_URL     default http://127.0.0.1:8010
"""
import base64
import json
import math
import os
import sys
import threading
import time
import urllib.request

from kortex_api.TCPTransport import TCPTransport
from kortex_api.RouterClient import RouterClient
from kortex_api.SessionManager import SessionManager
from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
from kortex_api.autogen.client_stubs.BaseCyclicClientRpc import BaseCyclicClient
from kortex_api.autogen.messages import Base_pb2, Session_pb2

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kenv
kenv.load()

USER = os.environ.get("KINOVA_USER")
PASS = os.environ.get("KINOVA_PASS")
JOB = os.environ.get("KINOVA_JOB", "latest").strip()
MAX_DABS = int(os.environ.get("KINOVA_MAX_DABS", "12"))
SPEED = float(os.environ.get("KINOVA_SPEED", "0.08"))
HOVER_Z = float(os.environ.get("KINOVA_HOVER", "0.20"))
DIP = float(os.environ.get("KINOVA_DIP", "0.04"))
GRIP_DWELL = float(os.environ.get("KINOVA_GRIP_DWELL", "1.2"))
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
PAD = os.environ.get("KINOVA_PAD_URL", "http://127.0.0.1:8010")

PIGMENT_ORDER = ("carbon", "green", "ochre", "ultramarine", "venetian")
SLOT_PITCH = 0.06           # pot spacing, matches the setup page
DOWN = (180.0, 0.0, 90.0)   # tool pointing at the desk
DIP_EVERY = 8               # touches per load of paint
LOAD_DWELL = 0.6            # pause while the brush takes up paint
DAB_DWELL = 0.25            # pause while the brush touches the paper
MIN_Z = 0.12                # nothing goes below this, dips included
MIN_REACH, MAX_REACH = 0.25, 0.80
SAMPLES = 24                # points checked along each straight move
HOME_TOL = 5.0              # deg per joint
MOVE_TIMEOUT = 30

if not USER or not PASS:
    sys.exit("Set KINOVA_USER and KINOVA_PASS, or fill /etc/kinova.env.")
if not (0 < SPEED <= 0.15):
    sys.exit("Refusing: KINOVA_SPEED must be within 0-0.15 m/s.")
if MAX_DABS < 1:
    sys.exit("KINOVA_MAX_DABS must be at least 1.")
if HOVER_Z - DIP < MIN_Z:
    sys.exit("Refusing: a dip would go below {:.2f} m. Raise KINOVA_HOVER or lower KINOVA_DIP.".format(MIN_Z))


def fetch(path):
    with urllib.request.urlopen(PAD + path, timeout=5) as r:
        return json.load(r)


def studio_password():
    value = os.environ.get("SETUP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_password.txt")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def report(payload):
    """Tell display.html what we are doing. Never worth failing a run over."""
    if not ARMED:
        return
    password = studio_password()
    if not password:
        return
    token = base64.b64encode(("studio:" + password).encode()).decode()
    request = urllib.request.Request(
        PAD + "/api/run", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token})
    try:
        urllib.request.urlopen(request, timeout=2).read()
    except Exception:
        pass


def load_job():
    if JOB == "latest":
        jobs = fetch("/api/jobs")["jobs"]
        if not jobs:
            sys.exit("The pad queue is empty. Submit a drawing first.")
        jid = jobs[-1]["id"]
    else:
        jid = int(JOB)
    return fetch("/api/jobs/{}".format(jid))


def sample(items, k):
    if len(items) <= k:
        return list(items)
    if k == 1:
        return items[:1]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


def place(item, u, v):
    """u is along the item's front-back axis, v across it, both in metres."""
    t = math.radians(item["rot"])
    return (item["x"] + u * math.cos(t) - v * math.sin(t),
            item["y"] + u * math.sin(t) + v * math.cos(t))


def pot(layout, pigment):
    return place(layout["pots"], 0.0, (PIGMENT_ORDER.index(pigment) - 2) * SLOT_PITCH)


def plan(job, layout):
    pitch = job["grid"]["pitch_mm"] / 1000.0
    cols, rows = job["grid"]["cols"], job["grid"]["rows"]
    dabs = sorted((d for d in job["dabs"] if d["pigment"] in PIGMENT_ORDER),
                  key=lambda d: (PIGMENT_ORDER.index(d["pigment"]), d["row"], d["col"]))
    picked = sample(dabs, MAX_DABS)
    moves = []

    def touch(label, point, dwell):
        moves.append((label + " over", point[0], point[1], HOVER_Z, 0.0))
        moves.append((label + " down", point[0], point[1], HOVER_Z - DIP, dwell))
        moves.append((label + " lift", point[0], point[1], HOVER_Z, 0.0))

    loaded, since = None, 0
    for n, d in enumerate(picked, 1):
        pig = d["pigment"]
        if pig != loaded:
            if loaded is not None:
                touch("stand {} brush in its pot".format(loaded), pot(layout, loaded), GRIP_DWELL)
            touch("lift {} brush from its pot".format(pig), pot(layout, pig), GRIP_DWELL)
            loaded, since = pig, 0
        elif since >= DIP_EVERY:
            touch("reload " + pig, pot(layout, pig), LOAD_DWELL)
            since = 0
        for copy in job["copies"]:
            sheet = layout["sheets"][copy["slot"]]
            u = ((rows - 1) / 2.0 - d["row"]) * pitch
            v = ((cols - 1) / 2.0 - d["col"]) * pitch
            touch("dab {}/{} {} r{} c{}".format(n, len(picked), copy["slot"], d["row"], d["col"]),
                  place(sheet, u, v), DAB_DWELL)
            since += 1
    if loaded is not None:
        touch("stand {} brush in its pot".format(loaded), pot(layout, loaded), GRIP_DWELL)
    return picked, moves


def bearing(x, y):
    return math.degrees(math.atan2(y, x))


def in_sector(x, y, sector):
    width = ((sector["to"] - sector["from"]) % 360.0) or 360.0
    return ((bearing(x, y) - sector["from"]) % 360.0) <= width


def in_zone(x, y, zone):
    return (abs(x - zone["x"]) <= zone["sx"] / 2.0 and
            abs(y - zone["y"]) <= zone["sy"] / 2.0)


def segment_hits(p, q, zone):
    """Straight Cartesian move from p to q against an axis-aligned no-go box."""
    bounds = ((zone["x"] - zone["sx"] / 2.0, zone["x"] + zone["sx"] / 2.0, p[0], q[0]),
              (zone["y"] - zone["sy"] / 2.0, zone["y"] + zone["sy"] / 2.0, p[1], q[1]))
    t0, t1 = 0.0, 1.0
    for lo, hi, a, b in bounds:
        d = b - a
        if abs(d) < 1e-9:
            if a < lo or a > hi:
                return False
            continue
        s0, s1 = (lo - a) / d, (hi - a) / d
        if s0 > s1:
            s0, s1 = s1, s0
        t0, t1 = max(t0, s0), min(t1, s1)
        if t0 > t1:
            return False
    return True


def point_problems(label, x, y, z, layout):
    out = []
    radius = math.hypot(x, y)
    if z < MIN_Z:
        out.append("{}: z {:.3f} below {:.2f}".format(label, z, MIN_Z))
    if not MIN_REACH <= radius <= MAX_REACH:
        out.append("{}: reach {:.3f} outside {:.2f}-{:.2f}".format(label, radius, MIN_REACH, MAX_REACH))
    if not in_sector(x, y, layout["sector"]):
        out.append("{}: bearing {:.0f} deg is outside the working sweep".format(label, bearing(x, y)))
    for n, zone in enumerate(layout["nogo"], 1):
        if in_zone(x, y, zone):
            out.append("{}: sits inside no-go box {}".format(label, n))
    return out


def travel_problems(label, p, q, layout):
    """A straight move can leave the sweep or the reach band even when both ends are fine."""
    for i in range(1, SAMPLES):
        t = i / float(SAMPLES)
        x, y = p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t
        radius = math.hypot(x, y)
        if not MIN_REACH <= radius <= MAX_REACH:
            return ["travel into '{}' passes through reach {:.3f} m".format(label, radius)]
        if not in_sector(x, y, layout["sector"]):
            return ["travel into '{}' leaves the working sweep".format(label)]
    for n, zone in enumerate(layout["nogo"], 1):
        if segment_hits(p, q, zone):
            return ["travel into '{}' crosses no-go box {}".format(label, n)]
    return []


def via_point(p, q, layout):
    """A point out on the arc between p and q, to swing round the base instead of past it."""
    a = bearing(*p)
    delta = ((bearing(*q) - a + 180.0) % 360.0) - 180.0
    mid = math.radians(a + delta / 2.0)
    radius = min(MAX_REACH - 0.02, max(math.hypot(*p), math.hypot(*q), MIN_REACH + 0.10))
    return (radius * math.cos(mid), radius * math.sin(mid))


def route(moves, layout, start=None):
    """Insert a via point wherever a straight move would cut inside the base or leave the sweep."""
    out = []
    prev = (start[0], start[1]) if start else None
    for move in moves:
        point = (move[1], move[2])
        if prev is not None and travel_problems(move[0], prev, point, layout):
            via = via_point(prev, point, layout)
            if (not travel_problems(move[0], prev, via, layout)
                    and not travel_problems(move[0], via, point, layout)):
                out.append(("swing round to " + move[0], via[0], via[1], HOVER_Z, 0.0))
        out.append(move)
        prev = point
    return out


def check(moves, layout, start=None):
    bad = []
    for label, x, y, z, _ in moves:
        bad += point_problems(label, x, y, z, layout)
    prev = (start[0], start[1]) if start else None
    for label, x, y, z, _ in moves:
        if prev is not None:
            bad += travel_problems(label, prev, (x, y), layout)
        prev = (x, y)
    return bad


def estimate(moves, start):
    total, prev = 0.0, start
    for _, x, y, z, dwell in moves:
        total += math.dist(prev, (x, y, z)) / SPEED + 0.6 + dwell
        prev = (x, y, z)
    return total + math.dist(prev, start) / SPEED


job = load_job()
layout = fetch("/api/layout")
picked, moves = plan(job, layout)
colours = []
for d in picked:
    if d["pigment"] not in colours:
        colours.append(d["pigment"])

print("job       : #{} from the pad, {} dabs drawn".format(job["id"], job["dab_count"]))
print("visiting  : {} dabs in {} colour(s): {}".format(len(picked), len(colours), ", ".join(colours)))
print("copies    : {}".format(", ".join(c["slot"] for c in job["copies"])))
print("brushes   : one per colour, standing in its own pot")
print("gripper   : pretend, {:.1f}s per pick up and put down, {:.1f}s per reload, {:.2f}s per dab".format(
    GRIP_DWELL, LOAD_DWELL, DAB_DWELL))
print("moves     : {}   speed {:.2f} m/s   hover {:.0f} mm, down to {:.0f} mm".format(
    len(moves), SPEED, HOVER_Z * 1000, (HOVER_Z - DIP) * 1000))
print("layout    : sheets {} and {}, pots {}".format(
    layout["sheets"]["display"], layout["sheets"]["keepsake"], layout["pots"]))
print("limits    : sweep {:.0f} to {:.0f} deg, {} no-go box(es), reach {:.2f}-{:.2f} m".format(
    layout["sector"]["from"], layout["sector"]["to"], len(layout["nogo"]), MIN_REACH, MAX_REACH))
print("heights   : PLACEHOLDER. Nothing is meant to touch.\n")

transport = TCPTransport()
router = RouterClient(transport, RouterClient.basicErrorCallback)
transport.connect(os.environ.get("KINOVA_IP", "192.168.1.10"), 10000)

info = Session_pb2.CreateSessionInfo()
info.username = USER
info.password = PASS
info.session_inactivity_timeout = 60000
info.connection_inactivity_timeout = 2000
session = SessionManager(router)
session.CreateSession(info)

base = BaseClient(router)
cyclic = BaseCyclicClient(router)

done = threading.Event()
result = {}
handle = None
moving = False


def on_event(notification):
    ev = notification.action_event
    if ev in (Base_pb2.ACTION_END, Base_pb2.ACTION_ABORT):
        result["event"] = ev
        done.set()


def home_angles():
    req = Base_pb2.RequestedActionType()
    req.action_type = Base_pb2.REACH_JOINT_ANGLES
    for a in base.ReadAllActions(req).action_list:
        if a.name.strip().lower() == "home":
            act = base.ReadAction(a.handle)
            return [ja.value for ja in act.reach_joint_angles.joint_angles.joint_angles]
    return None


def angdiff(a, b):
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def reach_pose(x, y, z, theta, name):
    act = Base_pb2.Action()
    act.name = name
    pose = act.reach_pose
    pose.constraint.speed.translation = SPEED
    pose.constraint.speed.orientation = 30.0
    t = pose.target_pose
    t.x, t.y, t.z = x, y, z
    t.theta_x, t.theta_y, t.theta_z = theta
    done.clear()
    result.clear()
    base.ExecuteAction(act)
    if not done.wait(MOVE_TIMEOUT):
        base.Stop()
        raise SystemExit("Timed out on '{}'. Sent Stop(), check the arm.".format(name))
    if result.get("event") != Base_pb2.ACTION_END:
        raise SystemExit("Arm aborted '{}'. Stopped here, check the arm.".format(name))
    fb = cyclic.RefreshFeedback()
    if fb.base.fault_bank_a or fb.base.fault_bank_b:
        base.Stop()
        raise SystemExit("Fault reported after '{}'. Stopped.".format(name))


try:
    state = base.GetArmState().active_state
    if state != Base_pb2.ARMSTATE_SERVOING_READY:
        busy = ("Arm is {}, not SERVOING_READY. Something else is driving it, or a "
                "session just closed; wait a few seconds.".format(Base_pb2.ArmState.Name(state)))
        if ARMED:
            raise SystemExit(busy + " Aborting.")
        print("NOTE: " + busy + "\n")
    fb = cyclic.RefreshFeedback()
    if fb.base.fault_bank_a or fb.base.fault_bank_b:
        raise SystemExit("Base reports active faults. Clear them first.")

    home = home_angles()
    current = [a.position for a in fb.actuators]
    off = max(angdiff(c, h) for c, h in zip(current, home)) if home else 999.0
    b = fb.base
    start = (b.tool_pose_x, b.tool_pose_y, b.tool_pose_z)
    start_theta = (b.tool_pose_theta_x, b.tool_pose_theta_y, b.tool_pose_theta_z)

    moves = route(moves, layout, start)
    bad = check(moves, layout, start)
    if bad:
        for line in bad[:20]:
            print("  " + line)
        if len(bad) > 20:
            print("  ... and {} more".format(len(bad) - 20))
        raise SystemExit("Refusing: {} problem(s) with the plan.".format(len(bad)))
    swings = sum(1 for m in moves if m[0].startswith("swing round"))
    if swings:
        print("routing   : {} swing(s) added to keep clear of the base\n".format(swings))

    for i, (label, x, y, z, dwell) in enumerate(moves, 1):
        print("  {:>3}  {:<44} x={:+.3f} y={:+.3f} z={:+.3f}{}".format(
            i, label, x, y, z, "  wait {:.1f}s".format(dwell) if dwell else ""))
    print("\nestimated run : {:.0f} s".format(estimate(moves, start)))
    print("start pose    : {:.1f} deg from Home (limit {:.0f})".format(off, HOME_TOL))

    if off > HOME_TOL:
        how = ("Start from Home so the first Cartesian move is predictable:\n"
               "  KINOVA_TARGET=Home KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/goto_pose.py")
        if ARMED:
            raise SystemExit("Refusing: not at Home.\n" + how)
        print("NOTE: " + how)

    if not ARMED:
        print("\nDRY RUN. No motion commanded. Re-run with KINOVA_CONFIRM=yes,")
        print("the desk clear under the arm, and the E-stop in hand.")
        raise SystemExit(0)

    print("\nMOVING. Hand on the E-stop. Ctrl-C stops the arm.\n")
    report({"event": "plan", "job": job["id"], "total": len(moves),
            "points": [[m[1], m[2], m[3]] for m in moves],
            "labels": [m[0] for m in moves]})
    handle = base.OnNotificationActionTopic(on_event, Base_pb2.NotificationOptions())
    moving = True
    prev = start
    t0 = time.time()
    for i, (label, x, y, z, dwell) in enumerate(moves, 1):
        if math.dist(prev, (x, y, z)) >= 0.001:
            print("  [{:>6.1f}s] {:>3}/{}  {}".format(time.time() - t0, i, len(moves), label))
            report({"event": "progress", "index": i, "label": label, "x": x, "y": y, "z": z})
            reach_pose(x, y, z, DOWN, label)
            prev = (x, y, z)
        if dwell:
            time.sleep(dwell)
    print("  returning to the start pose")
    reach_pose(start[0], start[1], start[2], start_theta, "return to start")
    moving = False
    report({"event": "done"})
    print("\ndone: {} moves in {:.0f} s, both copies visited.".format(len(moves), time.time() - t0))
except KeyboardInterrupt:
    if moving:
        base.Stop()
        report({"event": "stopped"})
    print("\ninterrupted. Sent Stop().")
finally:
    if handle is not None:
        try:
            base.Unsubscribe(handle)
        except Exception:
            pass
    try:
        session.CloseSession()
        router.SetActivationStatus(False)
    except Exception:
        pass
    transport.disconnect()
    print("session closed cleanly.")
