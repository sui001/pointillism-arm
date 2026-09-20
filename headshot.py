#!/usr/bin/env python3
"""Look at a person, frame them the same way every time, and offer the render.

The arm side of the pad's New person button. run_queue.py runs this the way it
runs paint_sim.py: one process, owning every movement it makes, exiting with a
code that says what happened.

What it does, in order:

  1. take the arm lock, check the arm is free and parked at Home
  2. angular move to the portrait pose, which was taught by hand
  3. coarse framing on the person boxes stream.py already serves, to get a
     human into the middle of the frame at all
  4. fine framing on a face box found here, until the face sits on its mark
  5. stop, let the wrist settle, and take one fresh full resolution frame
  6. render it to 1260 cells in memory and offer that to the pad
  7. park at Home

The photograph is never written down. It exists as a numpy array inside this
process for about a second, becomes 1260 cells, and goes. That is the whole
argument of the portrait side of this piece, and it is one line away from not
being true at any time, so there is no debug flag here that saves a frame.

Framing is half joints and half crop, which is the part worth understanding
before changing anything. Where the face sits in the frame is the arm's job:
pan on joint 0, tilt on joint 4. How big the face is is not. Driving the arm
in and out to normalise face size means Cartesian moves near somebody's head
for a result a crop gives free, so the crop does it, in portrait.py, at a fixed
multiple of the measured face height. Tall visitor, short visitor, one step
closer to the machine: same framing.

    # everything except the arm, from a still, no camera and no Kinova SDK:
    KINOVA_FAKE_CAM=sample.jpg python headshot.py
    # the real thing, dry run: says what it would do and moves nothing
    ~/kinova-py310/bin/python ~/kinova/headshot.py
    # the real thing
    KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/headshot.py

KINOVA_FAKE_CAM   a still to use instead of the camera, for testing off the arm
KINOVA_CONFIRM    yes to actually move
KINOVA_STREAM_URL default http://127.0.0.1:8000, stream.py
KINOVA_PAD_URL    default http://127.0.0.1:8010
KINOVA_PATIENCE   seconds to spend trying to frame somebody, default 45
KINOVA_FACE_TOL   how close to the mark counts as framed, in frame widths,
                  default 0.04
KINOVA_SETTLE     seconds to wait after stopping before the keeper frame,
                  default 0.6
Everything portrait.py reads (KINOVA_GAIN, KINOVA_CHROMA) is used by the render.

Exit codes, which run_queue.py reads:
  0   a render is waiting for the visitor to accept
  75  the arm was busy, worth trying again in a moment
  76  the arm was not parked at Home
  77  nobody could be framed, and it gave up
  78  the portrait pose has not been taught yet
"""
import base64
import json
import os
import sys
import time
import urllib.request

import cv2
import numpy as np

sys.stdout.reconfigure(line_buffering=True)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import armlock
import portrait

PAD = os.environ.get("KINOVA_PAD_URL", "http://127.0.0.1:8010")
STREAM = os.environ.get("KINOVA_STREAM_URL", "http://127.0.0.1:8000")
FAKE_CAM = os.environ.get("KINOVA_FAKE_CAM", "")
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
IP = os.environ.get("KINOVA_IP", "192.168.1.10")

BUSY_EXIT = 75              # paint_sim.py's codes, and they must stay the same
NOT_HOME_EXIT = 76
NO_FACE_EXIT = 77
NOT_TAUGHT_EXIT = 78

YAW_J = int(os.environ.get("KINOVA_YAW_JOINT", "0"))
PITCH_J = int(os.environ.get("KINOVA_PITCH_JOINT", "4"))
YAW_SIGN = float(os.environ.get("KINOVA_YAW_SIGN", "1"))
PITCH_SIGN = float(os.environ.get("KINOVA_PITCH_SIGN", "1"))

# Normalised error, so a gain means degrees per second per frame width and does
# not change when the camera resolution does. track.py works in pixels because
# it was written against one camera; this has to survive the vision module
# being swapped, which is a thing that has already been on the cards.
YAW_KP = float(os.environ.get("KINOVA_FRAME_GAIN", "26"))
YAW_KD = float(os.environ.get("KINOVA_FRAME_KD", "7"))
PITCH_KP = float(os.environ.get("KINOVA_FRAME_PITCH_GAIN", "20"))
PITCH_KD = float(os.environ.get("KINOVA_FRAME_PITCH_KD", "5"))
# Slower than tracking on purpose. This runs with somebody's face a metre away
# and the arm pointed at them, and a machine that eases onto its mark reads as
# looking at you where one that snaps reads as lunging.
MAX_SPEED = float(os.environ.get("KINOVA_FRAME_MAX_SPEED", "8"))
DEAD = float(os.environ.get("KINOVA_FRAME_DEADBAND", "0.012"))
# How far either joint may wander from the pose that was taught. The taught
# pose is the one known to have good light and a blank wall behind it, so this
# is the edge of the picture as much as it is a safety limit.
YAW_WINDOW = float(os.environ.get("KINOVA_YAW_WINDOW", "25"))
PITCH_WINDOW = float(os.environ.get("KINOVA_PITCH_WINDOW", "15"))

PATIENCE = float(os.environ.get("KINOVA_PATIENCE", "45"))
TOL = float(os.environ.get("KINOVA_FACE_TOL", "0.04"))
HITS = int(os.environ.get("KINOVA_FRAME_HITS", "3"))
SETTLE = float(os.environ.get("KINOVA_SETTLE", "0.6"))
RATE = 10.0                                    # command loop, Hz
DETECT_WIDTH = 640     # the cascade runs on this, not on a 1280 wide frame

TARGET = (0.50, 0.42)      # where a face belongs in frame, overridden by layout


# ---- talking to the pad -----------------------------------------------------

def studio_password():
    value = os.environ.get("SETUP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open(os.path.join(HERE, "setup_password.txt")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def tell_pad(step, payload):
    """Post to the capture flow. Returns the parsed reply, or None."""
    password = studio_password()
    if not password:
        print("  no studio password on this Pi, so the pad cannot be told")
        return None
    token = base64.b64encode(("studio:" + password).encode()).decode()
    request = urllib.request.Request(
        "{}/api/capture/{}".format(PAD, step), data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json",
                                "Authorization": "Basic " + token})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return json.load(r)
    except Exception as e:
        print("  could not tell the pad ({}): {}".format(step, e))
        return None


def give_up(reason, code=NO_FACE_EXIT):
    print("\n{}.".format(reason[0].upper() + reason[1:]))
    tell_pad("state", {"state": "failed", "reason": reason})
    return code


def layout():
    try:
        with urllib.request.urlopen(PAD + "/api/layout", timeout=5) as r:
            return json.load(r)
    except Exception as e:
        print("  could not read the layout ({}), using defaults".format(e))
        return {}


# ---- the camera -------------------------------------------------------------

class Camera(object):
    """stream.py, over HTTP. It is the only RTSP client and must stay that way.

    A second client kills the video for everybody and can take the vision
    module down with it, so nothing here opens the camera itself, ever.
    """

    def __init__(self, base):
        self.base = base

    def snapshot(self):
        with urllib.request.urlopen(self.base + "/snapshot", timeout=5) as r:
            raw = np.frombuffer(r.read(), dtype=np.uint8)
        img = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("the snapshot did not decode")
        return img

    def people(self):
        """Person boxes, or None if the detector is not answering."""
        try:
            with urllib.request.urlopen(self.base + "/detections", timeout=2) as r:
                data = json.loads(r.read().decode())
        except Exception:
            return None
        return [d for d in data.get("detections", []) if d.get("cls") == "person"]


class FakeCamera(object):
    """A still and a pretend pan-tilt head, so the framing loop can be tested.

    Not just a fixed picture: it asks the arm where it is pointing and returns
    the part of the still that a camera at that bearing would see. So the loop
    really does chase a face, the gains really are exercised, and an unstable
    PD shows up on a laptop instead of on somebody's face.

    What it cannot tell you is which way joint 0 turns. That is what
    KINOVA_YAW_SIGN and KINOVA_PITCH_SIGN are for, and getting them wrong makes
    the arm run to its window and stop rather than converge, which is the first
    thing to check at the machine.
    """

    def __init__(self, path, arm, fov=65.0):
        self.img = cv2.imread(path)
        if self.img is None:
            raise SystemExit("Could not read KINOVA_FAKE_CAM={}".format(path))
        self.arm = arm
        h, w = self.img.shape[:2]
        # A window rather than the whole still, so there is room to pan into.
        self.vw, self.vh = int(w * 0.6), int(h * 0.6)
        self.per_deg = self.vw / float(fov)
        self.home = list(arm.joints())
        # Found once, on the whole still, so that people() can still report
        # somebody standing there when the face itself has swung out of shot or
        # is cut in half by the edge. That is what the real detector does: a
        # person box survives a head turn that the cascade does not, which is
        # the entire reason the coarse phase exists.
        self.face_full = find_face(self.img)

    def window(self):
        j = self.arm.joints()
        h, w = self.img.shape[:2]
        dx = (j[YAW_J] - self.home[YAW_J]) * self.per_deg
        dy = (j[PITCH_J] - self.home[PITCH_J]) * self.per_deg
        return (int(clamp((w - self.vw) / 2.0 + dx, 0, w - self.vw)),
                int(clamp((h - self.vh) / 2.0 + dy, 0, h - self.vh)))

    def snapshot(self):
        left, top = self.window()
        return self.img[top:top + self.vh, left:left + self.vw].copy()

    def people(self):
        """The person box the face implies, in the current window's coordinates."""
        if self.face_full is None:
            return []
        left, top = self.window()
        fx, fy, fw, fh = self.face_full
        x, y, w, h = fx - fw - left, fy - top, fw * 3, fh * 4
        if x + w <= 0 or y + h <= 0 or x >= self.vw or y >= self.vh:
            return []                       # entirely out of shot
        return [{"cls": "person", "conf": 0.9, "x": int(x), "y": int(y),
                 "w": int(w), "h": int(h)}]


def find_face(img):
    """The biggest frontal face, found on a downscaled copy for speed.

    The cascade's cost goes with the pixel count, and a 1280 wide frame is four
    times the work of a 640 one for a box whose accuracy only has to be good to
    a few pixels: the crop around it is 2.6 face heights, so a couple of pixels
    of slop in the box is well under a millimetre on paper.
    """
    h, w = img.shape[:2]
    scale = min(1.0, DETECT_WIDTH / float(w))
    small = img if scale >= 1.0 else cv2.resize(
        img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    found = portrait.face_box(small)
    if found is None:
        return None
    return tuple(int(round(v / scale)) for v in found)


def frame_error(box, frame_shape, target):
    """Where a box sits against its mark, as a fraction of the frame width.

    Both axes are in frame *widths* so that one tolerance means the same thing
    in each, rather than being tighter vertically on a 16:9 sensor for no
    reason anybody chose.
    """
    h, w = frame_shape[:2]
    cx = box[0] + box[2] / 2.0
    cy = box[1] + box[3] / 2.0
    return ((cx - target[0] * w) / float(w),
            (cy - target[1] * h) / float(w))


def head_of(person, frame_shape):
    """Roughly where the head is inside a person box, for coarse framing.

    A person box is mostly torso, so aiming at its centre points the camera at
    somebody's chest and the face leaves the top of the frame. The crown sits
    at the top of the box and a head is about an eighth of a standing body, so
    this aims a little below the top edge.
    """
    return (person["x"], person["y"], person["w"], max(1, int(person["h"] * 0.22)))


def ease(err, dead):
    """Ease out of the deadband rather than stepping out of it.

    Straight from track.py, and for the same reason: a hard deadband makes the
    command jump from zero to its full value the moment the error crosses the
    edge, and that is what makes a tracking motion chatter.
    """
    if abs(err) <= dead:
        return 0.0
    return err - (dead if err > 0 else -dead)


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


# ---- the arm ----------------------------------------------------------------

class NoArm(object):
    """The arm in a dry run, and the pretend head FakeCamera looks through.

    It integrates the speeds it is sent rather than ignoring them, which is
    what makes a test of the framing loop a test of anything: a controller
    whose output goes nowhere converges beautifully every time.
    """

    def __init__(self, pose=None):
        self.pose = list(pose or [0.0] * 6)
        self.t = time.time()

    def joints(self):
        return list(self.pose)

    def at_home(self):
        return True

    def ready(self):
        return True

    def go_to_pose(self, angles, name):
        print("  would move to {}: {}".format(
            name, ", ".join("{:+.1f}".format(a) for a in angles)))
        self.pose = list(angles)
        return True

    def park(self):
        print("  would park at Home")
        return True

    def send_speeds(self, yaw, pitch):
        now = time.time()
        dt = clamp(now - self.t, 0.0, 0.5)
        self.t = now
        self.pose[YAW_J] += yaw * dt
        self.pose[PITCH_J] += pitch * dt

    def stop(self):
        pass

    def close(self):
        pass


class Arm(object):
    """The real one. Every kortex import lives in here.

    Deliberately: the SDK is only installed in the Kortex virtualenv on the Pi,
    and everything else in this file has to run on a laptop with a still and no
    arm. An import at the top of the module would make that impossible.
    """

    def __init__(self):
        import kenv
        kenv.load()
        user = os.environ.get("KINOVA_USER")
        password = os.environ.get("KINOVA_PASS")
        if not user or not password:
            raise SystemExit("Set KINOVA_USER and KINOVA_PASS, or fill /etc/kinova.env.")

        from kortex_api.TCPTransport import TCPTransport
        from kortex_api.RouterClient import RouterClient
        from kortex_api.SessionManager import SessionManager
        from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
        from kortex_api.autogen.client_stubs.BaseCyclicClientRpc import BaseCyclicClient
        from kortex_api.autogen.messages import Base_pb2, Session_pb2

        self.Base_pb2 = Base_pb2
        self.transport = TCPTransport()
        self.router = RouterClient(self.transport, lambda ex: None)
        self.transport.connect(IP, 10000)
        info = Session_pb2.CreateSessionInfo()
        info.username = user
        info.password = password
        info.session_inactivity_timeout = 60000
        info.connection_inactivity_timeout = 2000
        self.session = SessionManager(self.router)
        self.session.CreateSession(info)
        self.base = BaseClient(self.router)
        self.cyclic = BaseCyclicClient(self.router)

    def ready(self, wait=20.0):
        """SERVOING_READY, waiting out the transient that follows any session.

        The arm reports MANUALLY_CONTROLLED for a few seconds after any script
        closes its session, which is exactly when the next one starts. It is
        not the web app and not the wrist button, and sending somebody hunting
        for a browser tab over it wastes an afternoon.
        """
        B = self.Base_pb2
        deadline = time.time() + wait
        state = self.base.GetArmState().active_state
        while state != B.ARMSTATE_SERVOING_READY and time.time() < deadline:
            print("  waiting for the arm to be free: {}".format(
                B.ArmState.Name(state)))
            time.sleep(2)
            state = self.base.GetArmState().active_state
        return state == B.ARMSTATE_SERVOING_READY

    def faulted(self):
        fb = self.cyclic.RefreshFeedback()
        return bool(fb.base.fault_bank_a or fb.base.fault_bank_b)

    def joints(self):
        return [a.position for a in self.cyclic.RefreshFeedback().actuators]

    def home_angles(self):
        B = self.Base_pb2
        req = B.RequestedActionType()
        req.action_type = B.REACH_JOINT_ANGLES
        for a in self.base.ReadAllActions(req).action_list:
            if a.name.strip().lower() == "home":
                act = self.base.ReadAction(a.handle)
                return [ja.value for ja in act.reach_joint_angles.joint_angles.joint_angles]
        return None

    def at_home(self, tol=8.0):
        home = self.home_angles()
        if not home:
            return False
        return all(abs(((c - h + 180.0) % 360.0) - 180.0) <= tol
                   for c, h in zip(self.joints(), home))

    def go_to_pose(self, angles, name, speed=20.0):
        import threading
        B = self.Base_pb2
        done = threading.Event()
        result = {}

        def on_event(notification):
            ev = notification.action_event
            if ev in (B.ACTION_END, B.ACTION_ABORT):
                result["event"] = ev
                done.set()

        handle = self.base.OnNotificationActionTopic(
            on_event, B.NotificationOptions())
        try:
            act = B.Action()
            act.name = name
            reach = act.reach_joint_angles
            reach.constraint.type = B.JOINT_CONSTRAINT_SPEED
            reach.constraint.value = speed
            for i, value in enumerate(angles):
                ja = reach.joint_angles.joint_angles.add()
                ja.joint_identifier = i
                ja.value = value
            self.base.ExecuteAction(act)
            if not done.wait(180):
                self.base.Stop()
                return False
            return result.get("event") == B.ACTION_END
        finally:
            self.base.Unsubscribe(handle)

    def park(self):
        home = self.home_angles()
        return bool(home) and self.go_to_pose(home, "park at Home")

    def send_speeds(self, yaw, pitch):
        cmd = self.Base_pb2.JointSpeeds()
        for i in range(6):
            js = cmd.joint_speeds.add()
            js.joint_identifier = i
            js.value = float(yaw if i == YAW_J else (pitch if i == PITCH_J else 0.0))
        self.base.SendJointSpeedsCommand(cmd)

    def stop(self):
        try:
            self.base.Stop()
        except Exception:
            pass

    def close(self):
        try:
            self.session.CloseSession()
        except Exception:
            pass
        try:
            self.transport.disconnect()
        except Exception:
            pass


# ---- framing ----------------------------------------------------------------

def frame_up(arm, cam, pose, target, deadline):
    """Drive the face onto its mark, and give back the box once it is there.

    Two phases with two detectors, because neither is enough alone. The person
    boxes stream.py serves are reliable on anybody upright and arrive at about
    3 Hz, which is what gets a human into the frame at all. Only the cascade
    gives the face box the crop needs, and it is fussy about glasses, head
    angle and flat light, so it is asked repeatedly rather than once.

    Returns the face box, or None if it ran out of time.
    """
    hits = 0
    last = (0.0, 0.0)
    last_t = time.time()
    phase = "coarse"
    last_phase = None
    said = ""

    while time.time() < deadline:
        img = cam.snapshot()
        face = find_face(img)
        if face is not None:
            box, phase = face, "fine"
        else:
            people = cam.people()
            if not people:
                arm.send_speeds(0.0, 0.0)
                if said != "nobody":
                    print("  waiting for somebody to stand on the mark")
                    said = "nobody"
                last_phase = None
                time.sleep(1.0 / RATE)
                continue
            biggest = max(people, key=lambda d: d["w"] * d["h"])
            box, phase = head_of(biggest, img.shape), "coarse"

        ex, ey = frame_error(box, img.shape, target)
        now = time.time()
        dt = max(1e-3, now - last_t)
        if phase != last_phase:
            # The cascade drops the face for a frame now and then, and the two
            # phases aim at different points on the same person. Differencing
            # across that swap gives a derivative term that is measuring the
            # swap rather than the person, and it arrives as a kick.
            dx = dy = 0.0
        else:
            dx, dy = (ex - last[0]) / dt, (ey - last[1]) / dt
        last, last_t, last_phase = (ex, ey), now, phase

        if phase == "fine" and abs(ex) < TOL and abs(ey) < TOL:
            hits += 1
            arm.send_speeds(0.0, 0.0)
            if hits >= HITS:
                print("  framed: face {:.0%} of the frame, error {:+.3f}, {:+.3f}"
                      .format(box[2] / float(img.shape[1]), ex, ey))
                return box
            time.sleep(1.0 / RATE)
            continue
        hits = 0

        # The taught pose is where the light and the blank wall are, so leaving
        # its window is not just unsafe, it is out of shot. Clamp rather than
        # abort: the person may still walk back into frame.
        # Positive error means the face is right of, or below, its mark, and
        # the joint moves the same way to chase it. Which physical direction
        # that is depends on the arm, so the two signs are knobs and are the
        # first thing to check at the machine: wrong, and the arm runs to the
        # edge of its window and sits there.
        j = arm.joints()
        yaw_v = YAW_SIGN * MAX_SPEED * clamp(
            (YAW_KP * ease(ex, DEAD) + YAW_KD * dx) / MAX_SPEED, -1.0, 1.0)
        pitch_v = PITCH_SIGN * MAX_SPEED * clamp(
            (PITCH_KP * ease(ey, DEAD) + PITCH_KD * dy) / MAX_SPEED, -1.0, 1.0)
        if abs(j[YAW_J] - pose[YAW_J]) > YAW_WINDOW:
            yaw_v = min(0.0, yaw_v) if j[YAW_J] > pose[YAW_J] else max(0.0, yaw_v)
        if abs(j[PITCH_J] - pose[PITCH_J]) > PITCH_WINDOW:
            pitch_v = min(0.0, pitch_v) if j[PITCH_J] > pose[PITCH_J] else max(0.0, pitch_v)

        if said != phase:
            print("  {} framing".format(phase))
            said = phase
        arm.send_speeds(yaw_v, pitch_v)
        time.sleep(1.0 / RATE)

    arm.send_speeds(0.0, 0.0)
    return None


# ---- the run ----------------------------------------------------------------

def portrait_pose(lay):
    """The taught pose, or None. Teaching it is a job for somebody at the arm."""
    got = (lay or {}).get("portrait") or {}
    pose = got.get("pose")
    if not isinstance(pose, list) or len(pose) != 6:
        return None, TARGET
    target = got.get("target")
    if (isinstance(target, list) and len(target) == 2
            and all(0.05 <= float(v) <= 0.95 for v in target)):
        return [float(v) for v in pose], (float(target[0]), float(target[1]))
    return [float(v) for v in pose], TARGET


def main():
    fake = bool(FAKE_CAM)
    print("=== headshot: the arm takes a photograph ===")
    print("pad        : {}".format(PAD))
    print("camera     : {}".format("a still, {}".format(FAKE_CAM) if fake else STREAM))

    lay = layout()
    pose, target = portrait_pose(lay)
    if pose is None and not fake:
        print("\nThe portrait pose has not been taught yet. Somebody has to stand")
        print("at the arm and show it where to look, once:")
        print("  KINOVA_TEACH=portrait KINOVA_CONFIRM=yes \\")
        print("      ~/kinova-py310/bin/python ~/kinova/teach.py")
        return NOT_TAUGHT_EXIT
    if pose is None:
        pose = [0.0] * 6
        print("pose       : not taught, and not needed with a still")
    else:
        print("pose       : {}".format(", ".join("{:+.1f}".format(a) for a in pose)))
    print("face mark  : {:.0%} across, {:.0%} down, tolerance {:.0%}".format(
        target[0], target[1], TOL))
    print("patience   : {:.0f} s\n".format(PATIENCE))

    if fake:
        # No arm, so no lock, no session and no motion. Everything else runs,
        # including the framing loop, against a pretend pan-tilt head.
        arm, lock = NoArm(pose), None
        cam = FakeCamera(FAKE_CAM, arm)
    else:
        cam = Camera(STREAM)
        try:
            lock = armlock.take("headshot", wait=3.0)
        except armlock.Busy as e:
            print("Refusing: {}.".format(e))
            return BUSY_EXIT
        arm = Arm() if ARMED else NoArm(pose)

    try:
        if ARMED and not fake:
            if not arm.ready():
                print("Refusing: the arm is not SERVOING_READY.")
                return BUSY_EXIT
            if arm.faulted():
                print("Refusing: the base reports active faults. Clear them first.")
                return 1
            if not arm.at_home():
                print("Refusing: not parked at Home, so the move to the portrait")
                print("pose would sweep an unpredictable path.")
                return NOT_HOME_EXIT
        elif not ARMED and not fake:
            print("DRY RUN. Nothing moves. Re-run with KINOVA_CONFIRM=yes.\n")

        tell_pad("state", {"state": "capturing"})

        if not arm.go_to_pose(pose, "portrait pose"):
            return give_up("the arm would not go to the portrait pose")

        if not ARMED and not fake:
            print("\nDry run over. The framing loop needs the arm to move.")
            tell_pad("state", {"state": "idle"})
            return 0

        deadline = time.time() + PATIENCE
        face = frame_up(arm, cam, pose, target, deadline)
        arm.send_speeds(0.0, 0.0)
        arm.stop()
        if face is None:
            arm.go_to_pose(pose, "back to the portrait pose")
            return give_up("no face found in {:.0f} s".format(PATIENCE))

        # The keeper. Every frame the loop above looked at is thrown away: one
        # taken mid-correction is motion blurred at exactly the scale the
        # dither cares about, and the dither will happily turn blur into dabs
        # and give back something that is nobody.
        time.sleep(SETTLE)
        img = cam.snapshot()
        face = find_face(img) or face
        out = portrait.render(img, face=face)
        del img                       # and it is not written down anywhere

        if not out["dabs"]:
            return give_up("the render came out as bare paper")

        if out["too_close"]:
            # Not a failure: the render is still a portrait, and the visitor is
            # about to see it and can ask for another go. But it is framed
            # tighter than everybody else's, and on the wall that shows.
            print("\nNOTE: too close for the usual crop. This one is tighter")
            print("      than the rest. A step back would fix it.")

        print("\ndabs       : {} ({:.0%} of the sheet)".format(
            len(out["dabs"]), len(out["dabs"]) / 1260.0))
        for name in sorted(out["counts"], key=lambda k: -out["counts"][k]):
            print("  {:<12} {:>4}".format(name, out["counts"][name]))

        if tell_pad("propose", {"dabs": out["dabs"], "thumb": out["thumb"]}) is None:
            return give_up("the pad would not take the render", code=1)
        print("\nOffered to the pad. Waiting on the visitor to say yes.")
        return 0

    finally:
        try:
            arm.send_speeds(0.0, 0.0)
            arm.stop()
            if ARMED and not fake:
                arm.park()
        finally:
            arm.close()
            if lock is not None:
                lock.release()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
