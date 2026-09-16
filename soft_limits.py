#!/usr/bin/env python3
"""Show the arm's kinematic soft limits, and set the ones a painting actually uses.

A soft limit belongs to one control mode, which makes it easy to get half done: a
limit set on ANGULAR_TRAJECTORY does nothing to a Cartesian move. `paint_sim.py`
drives every move through `reach_pose`, which is CARTESIAN_TRAJECTORY, so that is
the mode whose limits decide how a painting feels.

Why this exists: none of the Cartesian modes had an acceleration soft limit, so
they fell back to the hard ceiling near 298 deg/s on the big joints. Hops between
dabs never reach travel speed, so they never noticed. The long run across to the
far sheet does reach it, then sheds all of it in about an eighth of a second,
which looks and sounds like the arm being slapped to a halt. Giving those modes
the acceleration ANGULAR_TRAJECTORY already uses lets the same move ease into its
stop instead.

What this deliberately does NOT touch: the twist limits. CARTESIAN_TRAJECTORY has
no angular one, so it uses the hard ceiling, and `reach_pose` asks for 30 deg/s of
its own. The other Cartesian modes carry 20. Matching them would look tidier and
would slow the cross sheet move down, because that move turns the tool about 46
degrees and the orientation constraint is already close to binding. Tidiness is
not worth a slower painting, so they are left alone.

    # look, change nothing:
    ~/kinova-py310/bin/python ~/kinova/soft_limits.py
    # apply everything listed as a change:
    KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/soft_limits.py

It refuses to write unless the arm is idle, so it can never rewrite limits out
from under a painting. Re-running it when nothing differs is a no-op, so it is
safe to leave in a checklist.

KINOVA_ACCEL_BASE   deg/s for joints 0-2, default 51.57, which is 0.9 rad/s
KINOVA_ACCEL_WRIST  deg/s for joints 3-5, default 515.66, which is 9.0 rad/s
                    Both default to what ANGULAR_TRAJECTORY already carries, so
                    every trajectory mode agrees unless you say otherwise.
"""
import os
import sys

from kortex_api.TCPTransport import TCPTransport
from kortex_api.RouterClient import RouterClient
from kortex_api.SessionManager import SessionManager
from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
from kortex_api.autogen.client_stubs.ControlConfigClientRpc import ControlConfigClient
from kortex_api.autogen.messages import Base_pb2, ControlConfig_pb2, Session_pb2

# credentials come from /etc/kinova.env unless already in the environment
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kenv
kenv.load()

USER = os.environ.get("KINOVA_USER")
PASS = os.environ.get("KINOVA_PASS")
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
ACCEL_BASE = float(os.environ.get("KINOVA_ACCEL_BASE", "51.5661964416504"))
ACCEL_WRIST = float(os.environ.get("KINOVA_ACCEL_WRIST", "515.661987304688"))

if not USER or not PASS:
    sys.exit("Set KINOVA_USER and KINOVA_PASS, or fill /etc/kinova.env.")

# Every mode a Cartesian move can run under. CARTESIAN_TRAJECTORY is the one
# painting uses; the other two are here so that jogging the arm by hand and any
# future waypoint work stop the same way, rather than surprising whoever meets
# them next.
WANTED = ("CARTESIAN_TRAJECTORY", "CARTESIAN_JOYSTICK", "CARTESIAN_WAYPOINT_TRAJECTORY")
PAINTS_WITH = "CARTESIAN_TRAJECTORY"
SHOW = ("ANGULAR_JOYSTICK", "ANGULAR_TRAJECTORY") + WANTED

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


def mode_of(name):
    m = ControlConfig_pb2.ControlModeInformation()
    m.control_mode = getattr(ControlConfig_pb2, name)
    return m


def accel_of(cc, name):
    return list(cc.GetKinematicSoftLimits(mode_of(name)).joint_acceleration_limits)


def fmt(values):
    return " ".join("{:.1f}".format(v) for v in values) if values else "unset"


def stopping(accel_deg, speed=0.35, radius=0.54):
    """Roughly how long the arm takes to shed travel speed at a joint acceleration.

    Deliberately rough. The point is the ratio between two settings, not a promise
    about any particular move.
    """
    return speed / ((accel_deg * 3.14159265 / 180.0) * radius)


try:
    base = BaseClient(router)
    cc = ControlConfigClient(router)
    hard = cc.GetKinematicHardLimits()
    ceiling = list(hard.joint_acceleration_limits)
    want = [ACCEL_BASE] * 3 + [ACCEL_WRIST] * 3

    print("hard ceiling : twist_lin {:.2f} m/s   accel {}\n".format(
        hard.twist_linear, fmt(ceiling)))

    print("soft limits now:")
    for name in SHOW:
        try:
            soft = cc.GetKinematicSoftLimits(mode_of(name))
        except Exception as e:
            print("  {:<31} could not read ({})".format(name, e))
            continue
        accel = list(soft.joint_acceleration_limits)
        print("  {:<31} twist_lin {:>5}   accel {}".format(
            name,
            "{:.2f}".format(soft.twist_linear) if soft.twist_linear else "unset",
            fmt(accel) if accel else "unset, so {} applies".format(fmt(ceiling))))

    changes = []
    for name in WANTED:
        before = accel_of(cc, name)
        if [round(v, 1) for v in before] != [round(v, 1) for v in want]:
            changes.append((name, before))

    print("\nqueued changes: {}".format(len(changes)))
    for name, before in changes:
        print("  {:<31} {}  ->  {}".format(
            name, before and fmt(before) or "unset", fmt(want)))
    if not changes:
        print("  none, the arm already matches what this file asks for.")
        raise SystemExit(0)

    was = next((b[0] for n, b in changes if n == PAINTS_WITH and b), ceiling[0])
    print("\nFor {}, shedding 0.35 m/s goes from about {:.2f}s to {:.2f}s.".format(
        PAINTS_WITH, stopping(was), stopping(ACCEL_BASE)))
    print("That is the difference between stopping dead and easing in. It costs")
    print("roughly {:.1f}s on each long travel and nothing on the dab hops,".format(
        2 * (stopping(ACCEL_BASE) - stopping(was))))
    print("which never get fast enough to care.")

    if not ARMED:
        print("\nDRY RUN. Nothing changed. Re-run with KINOVA_CONFIRM=yes.")
        raise SystemExit(0)

    # Never rewrite limits out from under a painting. The arm is mid trajectory
    # for most of a run, and a config write is not worth finding out about the
    # hard way.
    state = base.GetArmState().active_state
    if state != Base_pb2.ARMSTATE_SERVOING_READY:
        raise SystemExit(
            "\nRefusing: arm is {}, not SERVOING_READY. Something is driving it,\n"
            "most likely a painting in progress. Let it finish, then run this."
            .format(Base_pb2.ArmState.Name(state)))

    print("")
    for name, _ in changes:
        limits = ControlConfig_pb2.JointAccelerationSoftLimits()
        limits.control_mode = getattr(ControlConfig_pb2, name)
        for value in want:
            limits.joint_acceleration_soft_limits.append(value)
        cc.SetJointAccelerationSoftLimits(limits)
        after = accel_of(cc, name)
        ok = [round(v, 1) for v in after] == [round(v, 1) for v in want]
        print("  {:<31} now {}   {}".format(
            name, fmt(after), "ok" if ok else "DOES NOT MATCH, check the arm"))
finally:
    try:
        session.CloseSession()
        router.SetActivationStatus(False)
    except Exception:
        pass
    transport.disconnect()
    print("session closed cleanly.")
