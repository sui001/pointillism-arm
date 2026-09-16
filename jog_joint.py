#!/usr/bin/env python3
"""Move ONE joint to a target angle, slowly, leaving every other joint alone.

For recovering from an unknown pose. A whole-pose move interpolates every
joint at once and the swept path is not predictable from the endpoints, which
is dangerous when the arm is near a surface. One joint at a time is.

    export KINOVA_USER=... KINOVA_PASS=...
    # look, do not move:
    KINOVA_JOINT=1 KINOVA_ANGLE=0 ~/kinova-py310/bin/python ~/kinova/jog_joint.py
    # move:
    KINOVA_JOINT=1 KINOVA_ANGLE=0 KINOVA_CONFIRM=yes \
        ~/kinova-py310/bin/python ~/kinova/jog_joint.py

KINOVA_JOINT  which joint, 0-5
KINOVA_ANGLE  target in degrees
KINOVA_SPEED  deg/s, default 4, capped at 8
"""
import os
import signal
import sys
import threading
import time

from kortex_api.TCPTransport import TCPTransport
from kortex_api.RouterClient import RouterClient
from kortex_api.SessionManager import SessionManager
from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
from kortex_api.autogen.client_stubs.BaseCyclicClientRpc import BaseCyclicClient
from kortex_api.autogen.messages import Base_pb2, Session_pb2

# credentials come from /etc/kinova.env unless already in the environment
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kenv
kenv.load()

USER = os.environ.get("KINOVA_USER")
PASS = os.environ.get("KINOVA_PASS")
IP = os.environ.get("KINOVA_IP", "192.168.1.10")
SPEED = float(os.environ.get("KINOVA_SPEED", "4"))
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"

if not USER or not PASS:
    sys.exit("Set KINOVA_USER and KINOVA_PASS in the environment first.")
try:
    J = int(os.environ["KINOVA_JOINT"])
    TARGET = float(os.environ["KINOVA_ANGLE"])
except (KeyError, ValueError):
    sys.exit("Set KINOVA_JOINT (0-5) and KINOVA_ANGLE (degrees).")
if not (0 <= J <= 5):
    sys.exit("KINOVA_JOINT must be 0-5.")
if not (0 < SPEED <= 8):
    sys.exit("Refusing: keep KINOVA_SPEED at or under 8 deg/s for recovery work.")


def norm(a):
    return ((a + 180.0) % 360.0) - 180.0


transport = TCPTransport()
router = RouterClient(transport, RouterClient.basicErrorCallback)
transport.connect(IP, 10000)
info = Session_pb2.CreateSessionInfo()
info.username = USER
info.password = PASS
info.session_inactivity_timeout = 60000
info.connection_inactivity_timeout = 2000
session = SessionManager(router)
session.CreateSession(info)
base = BaseClient(router)
cyclic = BaseCyclicClient(router)

stop = threading.Event()


def halt():
    try:
        base.Stop()
    except Exception:
        pass


def _bail(signum, _frame):
    raise SystemExit("signal {}".format(signum))


for _s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    try:
        signal.signal(_s, _bail)
    except Exception:
        pass


def angles():
    return [a.position for a in cyclic.RefreshFeedback().actuators]


try:
    # Another script closing its session leaves the arm reporting
    # MANUALLY_CONTROLLED for a few seconds. Wait rather than fail, but never
    # take the arm from a human who is actually driving it.
    _deadline = time.time() + float(os.environ.get("KINOVA_WAIT_READY", "20"))
    st = base.GetArmState().active_state
    while st != Base_pb2.ARMSTATE_SERVOING_READY and time.time() < _deadline:
        print("  waiting for the arm to be free: {}".format(
            Base_pb2.ArmState.Name(st)))
        time.sleep(2)
        st = base.GetArmState().active_state
    if st != Base_pb2.ARMSTATE_SERVOING_READY:
        print("Something else holds control of the arm: the Kortex web app")
        print("open in a browser, a gamepad plugged into the base, or")
        print("admittance mode from the wrist button.")
        raise SystemExit("arm is {}, not SERVOING_READY.".format(
            Base_pb2.ArmState.Name(st)))
    fb = cyclic.RefreshFeedback()
    if fb.base.fault_bank_a or fb.base.fault_bank_b:
        raise SystemExit("base reports active faults. Clear them first.")

    cur = angles()
    travel = norm(TARGET - cur[J])
    print("joint     : {}".format(J))
    print("from      : {:+.2f} deg".format(cur[J]))
    print("to        : {:+.2f} deg".format(TARGET))
    print("travel    : {:+.2f} deg, about {:.0f}s at {:.1f} deg/s".format(
        travel, abs(travel) / SPEED, SPEED))
    print("all other joints are held where they are.")
    print("tool now  : x={:+.3f} y={:+.3f} z={:+.3f} m".format(
        fb.base.tool_pose_x, fb.base.tool_pose_y, fb.base.tool_pose_z))

    if abs(travel) < 0.3:
        print("")
        print("already there, nothing to do.")
        raise SystemExit(0)

    if not ARMED:
        print("")
        print("DRY RUN. No motion. Add KINOVA_CONFIRM=yes to move.")
        raise SystemExit(0)

    print("")
    print("MOVING. Hand on the E-stop.")

    targets = list(cur)
    targets[J] = TARGET

    act = Base_pb2.Action()
    act.name = "jog joint {}".format(J)
    reach = act.reach_joint_angles
    reach.constraint.type = Base_pb2.JOINT_CONSTRAINT_SPEED
    reach.constraint.value = SPEED
    for i, t in enumerate(targets):
        ja = reach.joint_angles.joint_angles.add()
        ja.joint_identifier = i
        ja.value = t

    done = threading.Event()
    res = {}

    def on_event(n):
        if n.action_event in (Base_pb2.ACTION_END, Base_pb2.ACTION_ABORT):
            res["e"] = Base_pb2.ActionEvent.Name(n.action_event)
            done.set()

    h = base.OnNotificationActionTopic(on_event, Base_pb2.NotificationOptions())

    def watch():
        while not stop.wait(1.0):
            try:
                f = cyclic.RefreshFeedback()
            except Exception:
                continue
            bad = f.base.fault_bank_a or f.base.fault_bank_b
            print("    j{} {:+8.2f} deg   tool z={:+.3f}{}".format(
                J, f.actuators[J].position, f.base.tool_pose_z,
                "   <-- FAULT" if bad else ""))

    threading.Thread(target=watch, daemon=True).start()
    base.ExecuteAction(act)
    ok = done.wait(180)
    stop.set()
    time.sleep(1.1)
    base.Unsubscribe(h)

    if not ok:
        halt()
        raise SystemExit("timed out, sent Stop()")

    fb = cyclic.RefreshFeedback()
    print("")
    print("result    : {}".format(res.get("e")))
    print("joint {}   : {:+.2f} deg".format(J, fb.actuators[J].position))
    print("tool      : x={:+.3f} y={:+.3f} z={:+.3f} m".format(
        fb.base.tool_pose_x, fb.base.tool_pose_y, fb.base.tool_pose_z))
finally:
    stop.set()
    halt()
    try:
        session.CloseSession()
        router.SetActivationStatus(False)
    except Exception:
        pass
    transport.disconnect()
    print("session closed cleanly.")
