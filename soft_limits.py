#!/usr/bin/env python3
"""Show the arm's kinematic soft limits, and set the ones it will let you set.

A soft limit belongs to one control mode, which makes it easy to reason about the
wrong one. `paint_sim.py` drives every move through `reach_pose`, which runs as
CARTESIAN_TRAJECTORY, so that is the mode whose limits decide how a painting
behaves. A limit set on ANGULAR_TRAJECTORY says nothing about it.

What this arm actually accepts, found by writing each limit back to its own value
and seeing which calls were refused:

    mode                    acceleration   joint speed
    ANGULAR_TRAJECTORY      yes            yes
    ANGULAR_JOYSTICK        yes            yes
    CARTESIAN_TRAJECTORY    NO             yes
    CARTESIAN_JOYSTICK      NO             yes

So the empty acceleration list the Cartesian modes report is not a gap waiting to
be filled. Those modes have no joint acceleration soft limit to set, and asking
gives ERROR_DEVICE / METHOD_FAILED. Cartesian acceleration comes from the hard
limits and is not ours to choose. This is worth writing down because the reading
is not obvious: "empty" looks exactly like "nobody set it yet".

The lever that does exist for painting is the joint speed limit. A move that needs
a joint to turn faster than it allows is refused outright, which is what stops a
dab near the far corner of the display sheet: the arm can reach that pose slowly
but the configuration change it requires is too fast at painting speed.

    # report everything, change nothing:
    ~/kinova-py310/bin/python ~/kinova/soft_limits.py
    # raise the Cartesian trajectory joint speed limit:
    KINOVA_JOINT_SPEED=70 KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/soft_limits.py

It refuses to write unless the arm is idle, so it cannot change limits out from
under a painting.

KINOVA_JOINT_SPEED  deg/s for all six joints under CARTESIAN_TRAJECTORY. Leave it
                    unset to report and change nothing. Capped by the arm's own
                    hard limit, which it prints.
"""
import os
import sys
import time

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
JOINT_SPEED = os.environ.get("KINOVA_JOINT_SPEED")

if not USER or not PASS:
    sys.exit("Set KINOVA_USER and KINOVA_PASS, or fill /etc/kinova.env.")

SHOW = ("ANGULAR_JOYSTICK", "ANGULAR_TRAJECTORY", "CARTESIAN_TRAJECTORY",
        "CARTESIAN_JOYSTICK", "CARTESIAN_WAYPOINT_TRAJECTORY")
PAINTS_WITH = "CARTESIAN_TRAJECTORY"

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


def fmt(values):
    return " ".join("{:.1f}".format(v) for v in values) if values else "none"


try:
    base = BaseClient(router)
    cc = ControlConfigClient(router)
    hard = cc.GetKinematicHardLimits()

    print("hard ceiling: twist_lin {:.2f} m/s   speed {}   accel {}\n".format(
        hard.twist_linear, fmt(hard.joint_speed_limits),
        fmt(hard.joint_acceleration_limits)))

    print("soft limits now:")
    for name in SHOW:
        try:
            soft = cc.GetKinematicSoftLimits(mode_of(name))
        except Exception as e:
            print("  {:<31} could not read ({})".format(name, e))
            continue
        accel = list(soft.joint_acceleration_limits)
        print("  {:<31} twist_lin {:>5}   speed {:>5}   accel {}".format(
            name,
            "{:.2f}".format(soft.twist_linear) if soft.twist_linear else "unset",
            "{:.1f}".format(soft.joint_speed_limits[0])
            if soft.joint_speed_limits else "none",
            fmt(accel) if accel else "not settable on this mode"))

    if JOINT_SPEED is None:
        print("\nNothing asked for. Set KINOVA_JOINT_SPEED to change the joint speed")
        print("limit for {}, which is the one painting runs under.".format(PAINTS_WITH))
        raise SystemExit(0)

    want = float(JOINT_SPEED)
    ceiling = min(hard.joint_speed_limits)
    if not 0 < want <= ceiling:
        raise SystemExit(
            "Refusing: {:.1f} deg/s is outside the arm's own limit of {:.1f}."
            .format(want, ceiling))

    before = list(cc.GetKinematicSoftLimits(mode_of(PAINTS_WITH)).joint_speed_limits)
    print("\nplan    : {} joint speed {} -> {:.1f} on all six".format(
        PAINTS_WITH, fmt(before), want))
    print("The arm refuses any move needing a joint faster than this, so raising it")
    print("lets through configuration changes it currently rejects. It does not make")
    print("the painting faster: the Cartesian speed still governs that.")

    if not ARMED:
        print("\nDRY RUN. Nothing changed. Re-run with KINOVA_CONFIRM=yes.")
        raise SystemExit(0)

    # Never rewrite limits out from under a painting. But any script closing its
    # session leaves the arm reporting MANUALLY_CONTROLLED for a few seconds, so
    # waiting is right where failing is not. jog_joint.py learned this first.
    deadline = time.time() + float(os.environ.get("KINOVA_WAIT_READY", "30"))
    state = base.GetArmState().active_state
    while state != Base_pb2.ARMSTATE_SERVOING_READY and time.time() < deadline:
        print("  waiting for the arm to be free: {}".format(
            Base_pb2.ArmState.Name(state)))
        time.sleep(2)
        state = base.GetArmState().active_state
    if state != Base_pb2.ARMSTATE_SERVOING_READY:
        raise SystemExit(
            "\nRefusing: arm is {}, not SERVOING_READY. Something is driving it:\n"
            "a painting in progress, the Kortex web app, a gamepad, or admittance\n"
            "mode from the wrist button."
            .format(Base_pb2.ArmState.Name(state)))

    limits = ControlConfig_pb2.JointSpeedSoftLimits()
    limits.control_mode = getattr(ControlConfig_pb2, PAINTS_WITH)
    for _ in range(len(hard.joint_speed_limits)):
        limits.joint_speed_soft_limits.append(want)
    cc.SetJointSpeedSoftLimits(limits)

    after = list(cc.GetKinematicSoftLimits(mode_of(PAINTS_WITH)).joint_speed_limits)
    print("\nnow     : {}   {}".format(
        fmt(after),
        "ok" if after and abs(after[0] - want) < 0.1 else "DID NOT TAKE, check the arm"))
finally:
    try:
        session.CloseSession()
        router.SetActivationStatus(False)
    except Exception:
        pass
    transport.disconnect()
    print("session closed cleanly.")
