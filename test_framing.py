#!/usr/bin/env python3
"""Make the framing loop chase a face on a laptop, before it does it at anybody.

headshot.py's FakeCamera is a still plus a pretend pan-tilt head: it asks the
arm where it is pointing and returns the part of the still a camera at that
bearing would see, and NoArm integrates the speeds it is sent. So the loop
here is the real loop, with real gains, and an unstable controller shows up as
a test that never converges rather than as an arm hunting at somebody's face.

What it cannot test is which way joint 0 physically turns. That is
KINOVA_YAW_SIGN and KINOVA_PITCH_SIGN, and it is the first thing to check at
the machine.

Needs a photograph beside it as sample.jpg, which the repo does not carry.

    python test_framing.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("KINOVA_FAKE_CAM", os.path.join(HERE, "sample.jpg"))
import headshot as H

PHOTO = os.environ["KINOVA_FAKE_CAM"]
if not os.path.exists(PHOTO):
    sys.exit("Put a photograph at {}. See this file's docstring.".format(PHOTO))

fails = []


def check(name, ok, detail=""):
    print("  {:<44} {}".format(name, "ok" if ok else "FAILED " + detail))
    if not ok:
        fails.append(name)


def run(offset_yaw, offset_pitch, patience=25.0):
    """Point the pretend head away from the face and see if it finds its way back."""
    arm = H.NoArm([0.0] * 6)
    cam = H.FakeCamera(PHOTO, arm)
    arm.pose[H.YAW_J] += offset_yaw
    arm.pose[H.PITCH_J] += offset_pitch
    arm.t = time.time()
    started = time.time()
    face = H.frame_up(arm, cam, [0.0] * 6, H.TARGET, started + patience)
    return face, cam, arm, time.time() - started


print("framing loop, gains {} / {} deg/s per frame width, ceiling {} deg/s\n"
      .format(H.YAW_KP, H.PITCH_KP, H.MAX_SPEED))

print("it finds a face that is already in shot")
face, cam, arm, secs = run(0.0, 0.0)
check("converges", face is not None, "gave up after {:.0f}s".format(secs))
if face is not None:
    ex, ey = H.frame_error(face, cam.snapshot().shape, H.TARGET)
    check("and lands on the mark", abs(ex) < H.TOL and abs(ey) < H.TOL,
          "error {:+.3f}, {:+.3f}".format(ex, ey))

print("\nit walks one back in from the edge of the frame")
face, cam, arm, secs = run(-9.0, -5.0)
check("converges from off target", face is not None,
      "gave up after {:.0f}s".format(secs))
if face is not None:
    ex, ey = H.frame_error(face, cam.snapshot().shape, H.TARGET)
    check("lands on the mark", abs(ex) < H.TOL and abs(ey) < H.TOL,
          "error {:+.3f}, {:+.3f}".format(ex, ey))
    check("in a time a person will stand still for", secs < 20.0,
          "took {:.0f}s".format(secs))
    # A controller that got there by swinging past and coming back is one that
    # will do the same at a person, and the arm carries a brush.
    check("without leaving the taught window",
          abs(arm.pose[H.YAW_J]) <= H.YAW_WINDOW
          and abs(arm.pose[H.PITCH_J]) <= H.PITCH_WINDOW,
          "ended at {:+.1f}, {:+.1f}".format(arm.pose[H.YAW_J], arm.pose[H.PITCH_J]))

print("\nit gives up rather than hunting for ever")
arm = H.NoArm([0.0] * 6)
cam = H.FakeCamera(PHOTO, arm)
cam.people = lambda: []                       # nobody there, and no face either
cam.snapshot = lambda: __import__("numpy").zeros((480, 640, 3), dtype="uint8")
started = time.time()
face = H.frame_up(arm, cam, [0.0] * 6, H.TARGET, started + 2.0)
check("gives up on an empty room", face is None)
check("at about the time it was told to", 1.5 < time.time() - started < 4.0,
      "{:.1f}s".format(time.time() - started))
check("and leaves the arm where it started",
      abs(arm.pose[H.YAW_J]) < 0.001 and abs(arm.pose[H.PITCH_J]) < 0.001,
      "moved to {:+.2f}, {:+.2f}".format(arm.pose[H.YAW_J], arm.pose[H.PITCH_J]))

print("\nthe window holds even when the face never arrives")
arm = H.NoArm([0.0] * 6)
cam = H.FakeCamera(PHOTO, arm)
# A face pinned to the corner of the frame: the error never goes away, so the
# loop drives until the window stops it. This is the case that would otherwise
# walk the arm round to point at a wall.
cam.people = lambda: [{"cls": "person", "conf": 0.9, "x": 0, "y": 0,
                       "w": 40, "h": 200}]
cam.snapshot = lambda: __import__("numpy").zeros((480, 640, 3), dtype="uint8")
H.frame_up(arm, cam, [0.0] * 6, H.TARGET, time.time() + 4.0)
check("yaw stops at the window",
      abs(arm.pose[H.YAW_J]) <= H.YAW_WINDOW + H.MAX_SPEED * 0.2,
      "reached {:+.1f}".format(arm.pose[H.YAW_J]))
check("pitch stops at the window",
      abs(arm.pose[H.PITCH_J]) <= H.PITCH_WINDOW + H.MAX_SPEED * 0.2,
      "reached {:+.1f}".format(arm.pose[H.PITCH_J]))

print("\n" + ("FAILURES: " + ", ".join(fails) if fails else "all checks passed"))
sys.exit(1 if fails else 0)
