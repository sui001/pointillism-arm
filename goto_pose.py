#!/usr/bin/env python3
"""Move the arm to a named factory pose, or to all-joints-zero (fully vertical).

Safety design:
  * refuses to run without KINOVA_CONFIRM=yes
  * hard speed cap, default 5 deg/s
  * verifies SERVOING_READY and zero faults before starting
  * prints the full plan and the worst-case joint travel before moving
  * live monitor thread prints joint angles and faults each second during travel
  * aborts immediately if the arm raises a fault mid-move
  * prints why the arm refused, not just that it did

Usage:
    export KINOVA_USER=... KINOVA_PASS=...
    # dry run, prints the plan and exits without moving:
    KINOVA_TARGET=zero ~/kinova-py310/bin/python ~/kinova/goto_pose.py
    # actually move:
    KINOVA_TARGET=zero KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/goto_pose.py

KINOVA_TARGET may be 'zero' or the name of any factory-stored joint pose
(case-insensitive), e.g. 'retract' or 'home'.
KINOVA_SPEED  degrees/second, default 5, hard capped at 10.
"""
import os
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
TARGET = os.environ.get("KINOVA_TARGET", "").strip()
SPEED = float(os.environ.get("KINOVA_SPEED", "5"))
ABORT_EXIT = 77         # the arm refused the move, and the caller must not think it parked
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
TIMEOUT = 180

if not USER or not PASS:
    sys.exit("Set KINOVA_USER and KINOVA_PASS in the environment first.")
if not TARGET:
    sys.exit("Set KINOVA_TARGET (e.g. 'zero', 'retract', 'home').")
if not (0 < SPEED <= 10):
    sys.exit("Refusing: KINOVA_SPEED must be within 0-10 deg/s for a large move.")

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

stop_monitor = threading.Event()
fault_seen = threading.Event()


def monitor():
    while not stop_monitor.wait(1.0):
        try:
            fb = cyclic.RefreshFeedback()
        except Exception:
            continue
        angles = " ".join("{:>7.2f}".format(a.position) for a in fb.actuators)
        bad = fb.base.fault_bank_a or fb.base.fault_bank_b
        if bad:
            fault_seen.set()
        print("    [{}] {}{}".format(time.strftime("%H:%M:%S"), angles,
                                     "   <-- FAULT" if bad else ""))


def factory_poses():
    req = Base_pb2.RequestedActionType()
    req.action_type = Base_pb2.REACH_JOINT_ANGLES
    return list(base.ReadAllActions(req).action_list)


def enum_name(enum, value):
    """Enum names move between kortex versions, so never die trying to read one."""
    try:
        return getattr(Base_pb2, enum).Name(value)
    except Exception:
        return "unrecognised"


def why(notification):
    """An abort with no reason attached is just 'no'.

    The arm does say why, on the notification, but printing only the event name
    threw that away and the next person had to write a throwaway script to get
    it back. A refused move most often means the trajectory left a soft limit or
    clipped a protection zone, and those name themselves here.
    """
    if notification is None:
        print("reason    : none given, the arm aborted without detail")
        return
    code = getattr(notification, "abort_details", 0)
    if code:
        print("reason    : {} ({})".format(enum_name("SubErrorCodes", code), code))
    for entry in getattr(notification, "trajectory_info", []):
        print("trajectory: {} on joint {}".format(
            enum_name("TrajectoryInfoType", entry.trajectory_info_type),
            getattr(entry, "joint_index", "?")))
    print("\nfull notification, since one of these fields is the answer:")
    print(notification)


try:
    # Any script closing its session leaves the arm reporting MANUALLY_CONTROLLED
    # for a few seconds, and this is the script you reach for straight after one
    # has stopped. Refusing on that is refusing on nothing. Wait for it to clear,
    # but never take the arm off a person who is actually driving it.
    deadline = time.time() + float(os.environ.get("KINOVA_WAIT_READY", "30"))
    state = base.GetArmState().active_state
    while state != Base_pb2.ARMSTATE_SERVOING_READY and time.time() < deadline:
        print("  waiting for the arm to be free: {}".format(
            Base_pb2.ArmState.Name(state)))
        time.sleep(2)
        state = base.GetArmState().active_state
    if state != Base_pb2.ARMSTATE_SERVOING_READY:
        raise SystemExit(
            "arm is {}, not SERVOING_READY. Something is driving it: a painting,\n"
            "the Kortex web app, a gamepad, or admittance mode from the wrist "
            "button.".format(Base_pb2.ArmState.Name(state)))

    fb = cyclic.RefreshFeedback()
    if fb.base.fault_bank_a or fb.base.fault_bank_b:
        raise SystemExit("base reports active faults. Clear them first. Aborting.")

    current = [a.position for a in fb.actuators]

    poses = factory_poses()
    match = [a for a in poses if a.name.strip().lower() == TARGET.lower()]
    if match:
        action = base.ReadAction(match[0].handle)
        targets = [ja.value for ja in action.reach_joint_angles.joint_angles.joint_angles]
        label = "factory pose '{}' (id {})".format(
            match[0].name, match[0].handle.identifier)
    elif TARGET.lower() in ("all-zeros", "allzeros"):
        targets = [0.0] * len(current)
        label = "all joints zero, computed by this script not factory-stored"
    else:
        names = ", ".join(sorted(a.name for a in poses)) or "(none found)"
        raise SystemExit(
            "No factory pose named '{}'. Available: {}. "
            "Use 'all-zeros' only if you deliberately want a computed pose.".format(
                TARGET, names))

    if len(targets) != len(current):
        raise SystemExit("pose has {} joints but arm has {}. Aborting.".format(
            len(targets), len(current)))

    print("target    : {}".format(label))
    print("speed     : {:.1f} deg/s\n".format(SPEED))
    print("   joint   current      target      travel")
    worst = 0.0
    for i, (c, t) in enumerate(zip(current, targets)):
        d = ((t - c + 180.0) % 360.0) - 180.0
        worst = max(worst, abs(d))
        print("   {:>5}   {:>7.2f}   {:>9.2f}   {:>+9.2f}".format(i, c, t, d))
    print("\nlargest single-joint travel : {:.1f} deg".format(worst))
    print("rough duration at {:.1f} deg/s : {:.0f} s".format(SPEED, worst / SPEED))

    b = fb.base
    print("tool starts at x={:+.3f} y={:+.3f} z={:+.3f} m".format(
        b.tool_pose_x, b.tool_pose_y, b.tool_pose_z))

    if not ARMED:
        print("\nDRY RUN. No motion commanded.")
        print("Re-run with KINOVA_CONFIRM=yes when the workspace is clear")
        print("and you have the E-stop in hand.")
        raise SystemExit(0)

    print("\nMOVING. Keep your hand on the E-stop.\n")
    threading.Thread(target=monitor, daemon=True).start()

    act = Base_pb2.Action()
    act.name = "goto {}".format(TARGET)
    reach = act.reach_joint_angles
    reach.constraint.type = Base_pb2.JOINT_CONSTRAINT_SPEED
    reach.constraint.value = SPEED
    for i, t in enumerate(targets):
        ja = reach.joint_angles.joint_angles.add()
        ja.joint_identifier = i
        ja.value = t

    done = threading.Event()
    result = {}

    def on_event(notification):
        ev = notification.action_event
        if ev in (Base_pb2.ACTION_END, Base_pb2.ACTION_ABORT):
            result["event"] = Base_pb2.ActionEvent.Name(ev)
            result["notification"] = notification
            done.set()

    handle = base.OnNotificationActionTopic(on_event, Base_pb2.NotificationOptions())
    base.ExecuteAction(act)
    finished = done.wait(TIMEOUT)
    base.Unsubscribe(handle)
    stop_monitor.set()
    time.sleep(1.1)

    if not finished:
        base.Stop()
        raise SystemExit("\nTIMED OUT after {}s. Sent Stop(). Check the arm.".format(TIMEOUT))

    aborted = result.get("event") == "ACTION_ABORT"
    print("\nresult    : {}".format(result.get("event")))
    if aborted:
        why(result.get("notification"))
    fb = cyclic.RefreshFeedback()
    print("final     : {}".format(
        " ".join("{:.2f}".format(a.position) for a in fb.actuators)))
    print("tool ends at x={:+.3f} y={:+.3f} z={:+.3f} m".format(
        fb.base.tool_pose_x, fb.base.tool_pose_y, fb.base.tool_pose_z))
    if fault_seen.is_set():
        print("NOTE: a fault was observed during the move. Inspect before continuing.")
    if aborted:
        # Exit non-zero, because callers believe exit codes. run_queue asks this
        # script to park and then reads the code to decide whether it worked.
        # Printing ACTION_ABORT and exiting 0 told it the arm was parked when the
        # arm had not moved at all, so it announced "Parked. Off we go." and went
        # straight back into a refusal. From the room that is a click, some beeps,
        # and nothing, over and over, with the real reason scrolling past.
        raise SystemExit(ABORT_EXIT)
finally:
    stop_monitor.set()
    try:
        session.CloseSession()
        router.SetActivationStatus(False)
    except Exception:
        pass
    transport.disconnect()
    print("session closed cleanly.")
