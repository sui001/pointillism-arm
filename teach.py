#!/usr/bin/env python3
"""Teach the rig where the paper and the pots actually are, by putting the arm there.

The setup page records where you believe a sheet is. The arm then finds out the
truth, late: on 2026-09-16 a sheet placed somewhere sensible looking put four
dabs 3 mm too close to the base, and the whole job was refused. Before that a run
died two minutes in over a position the plan had passed.

Teaching inverts that. A corner read off the arm's own joints is reachable by
construction, because reading it meant having the arm there. That kills the whole
class of "the plan validated and the arm refused anyway".

This script commands NO MOTION, ever. You move the arm, by hand. Put it in
admittance mode with the wrist button so it goes compliant, push the brush tip
onto a corner, and right click. That is the safest arrangement available, because
the only part of this where a person stands inside the arm's reach is the part
where the arm is limp rather than driven.

    right click   capture this corner
    middle click  scrap the last one and ask again

Three points per sheet, named as they appear on the visitor's pad:

    top left      row 0, column 0            becomes the origin
    top right     row 0, last column         gives rotation and a scale check
    bottom left   last row, column 0         gives height, tilt and squareness

Two would be enough for what layout.json can store. The third is worth ten
seconds because it measures three things nothing else here knows: paper height,
which is a placeholder until a gripper holds a brush; tilt, where one degree
across a 294 mm sheet is 5 mm, most of a dab; and whether your three points form
the rectangle they should, which catches a mis-click during setup rather than
during a show.

The pots want two: the first and last pot centre.

What it can and cannot save: layout.json holds x, y and a rot of 0 or 90, and has
no z at all. So position is written, and rotation only if it is close enough to
square to be stored. Measured height, tilt and any rotation in between get
reported for you to act on with your hands. At a 7 mm pitch you want the paper
actually square, not square-ish and compensated for.

    KINOVA_TEACH=display ~/kinova-py310/bin/python ~/kinova/teach.py
    KINOVA_TEACH=display KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/teach.py

KINOVA_TEACH     display, keepsake or pots
KINOVA_CONFIRM   yes to write layout.json. Without it you get the numbers and
                 nothing is saved, which is the right way to try this once.
KINOVA_SQUARE    degrees of rotation still counted as square, default 1.5
KINOVA_PAD_URL   default http://127.0.0.1:8010
"""
import json
import math
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import notify

# The kortex import and the arm session live in main(), not here, so the frame
# arithmetic below can be imported and tested on a machine with no arm and no
# SDK. That geometry is the one part of this whose mistakes would be silent: a
# sign error paints a perfectly good plan in the wrong place.

# Must match the pad's grid, which is what actually gets painted.
COLS, ROWS, PITCH = 30, 42, 0.007
SLOT_PITCH, SLOTS = 0.06, 5
MIN_REACH, MAX_REACH = 0.25, 0.80      # the same band paint_sim enforces
U = (ROWS - 1) / 2.0 * PITCH           # half the sheet along the arm's out-and-back axis
V = (COLS - 1) / 2.0 * PITCH           # half of it across


def solve_sheet(a, b, c):
    """Turn three taught corners into the frame paint_sim's place() expects.

    A sits at local (+U, +V), B at (+U, -V), C at (-U, +V), because row 0 is the
    far edge and column 0 is the far side. So A minus C runs along +u and A minus
    B along +v, and place() maps u to (cos t, sin t) and v to (-sin t, cos t),
    which makes the rotation just the bearing of the u axis.

    Returns centre, degrees, and the measurements worth complaining about.
    """
    du = ((a[0] - c[0]) / (2 * U), (a[1] - c[1]) / (2 * U))
    dv = ((a[0] - b[0]) / (2 * V), (a[1] - b[1]) / (2 * V))
    lu, lv = math.hypot(*du), math.hypot(*dv)
    if lu < 1e-6 or lv < 1e-6:
        raise ValueError("those corners are on top of each other")
    uax, vax = (du[0] / lu, du[1] / lu), (dv[0] / lv, dv[1] / lv)
    centre = (a[0] - uax[0] * U - vax[0] * V, a[1] - uax[1] * U - vax[1] * V)
    deg = math.degrees(math.atan2(uax[1], uax[0]))
    skew = math.degrees(math.acos(max(-1.0, min(1.0, uax[0] * vax[0] + uax[1] * vax[1]))))
    return centre, deg, {
        "along_mm": (lu - 1.0) * 2 * U * 1000,
        "across_mm": (lv - 1.0) * 2 * V * 1000,
        "skew_deg": skew - 90.0,
    }


def solve_pots(a, b):
    """First and last pot centre into a frame. Same idea, one axis."""
    reach = (SLOTS - 1) * SLOT_PITCH
    across = (b[0] - a[0], b[1] - a[1])
    measured = math.hypot(*across)
    if measured < 1e-4:
        raise ValueError("those two points are the same")
    vax = (across[0] / measured, across[1] / measured)
    centre = (a[0] + vax[0] * reach / 2.0, a[1] + vax[1] * reach / 2.0)
    deg = math.degrees(math.atan2(-vax[0], vax[1]))
    return centre, deg, {"pitch_mm": (measured - reach) * 1000, "measured_mm": measured * 1000}


def tilt_of(a, b, c):
    """Degrees off level, and the fourth corner the other three imply."""
    e1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    e2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    n = (e1[1] * e2[2] - e1[2] * e2[1],
         e1[2] * e2[0] - e1[0] * e2[2],
         e1[0] * e2[1] - e1[1] * e2[0])
    ln = math.hypot(*n)
    far = (a[0] + e1[0] + e2[0], a[1] + e1[1] + e2[1], a[2] + e1[2] + e2[2])
    deg = math.degrees(math.acos(min(1.0, abs(n[2]) / ln))) if ln > 1e-9 else None
    return deg, far


USER = os.environ.get("KINOVA_USER")
PASS = os.environ.get("KINOVA_PASS")
TARGET = os.environ.get("KINOVA_TEACH", "").strip().lower()
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
SQUARE_TOL = float(os.environ.get("KINOVA_SQUARE", "1.5"))
PAD = os.environ.get("KINOVA_PAD_URL", "http://127.0.0.1:8010")


def fetch(path):
    with urllib.request.urlopen(PAD + path, timeout=5) as r:
        return json.load(r)


def studio_password():
    value = os.environ.get("SETUP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open(os.path.join(HERE, "setup_password.txt")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def save_layout(layout):
    import base64
    password = studio_password()
    if not password:
        return "no studio password on this Pi, so nothing was saved"
    token = base64.b64encode(("studio:" + password).encode()).decode()
    request = urllib.request.Request(
        PAD + "/api/layout", data=json.dumps(layout).encode(), method="PUT",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            r.read()
        return None
    except Exception as e:
        return str(e)


def bearing(x, y):
    return math.degrees(math.atan2(y, x))


def in_sweep(deg, sector):
    width = (sector["to"] - sector["from"]) % 360.0 or 360.0
    return ((deg - sector["from"]) % 360.0) <= width


def corner_problems(corners, sector):
    """The same reach band and sweep paint_sim enforces, applied before saving.

    Teaching cannot produce an unreachable point, since reading it meant having
    the arm there. It can still produce a frame whose implied fourth corner, or
    whose dabs, fall outside what paint_sim will accept, and saving that would
    re-import the exact bug teaching exists to remove.
    """
    bad = []
    for label in sorted(corners):
        x, y = corners[label]
        r = math.hypot(x, y)
        if not MIN_REACH <= r <= MAX_REACH:
            bad.append("{}: reach {:.3f} outside {:.2f}-{:.2f}".format(
                label, r, MIN_REACH, MAX_REACH))
        elif not in_sweep(bearing(x, y), sector):
            bad.append("{}: bearing {:.0f} deg is outside the working sweep".format(
                label, bearing(x, y)))
    return bad


def nearest_right_angle(deg):
    """The storable rot this rotation is closest to, and how far off it is."""
    best = min((0, 90, 180, 270, 360), key=lambda k: abs(deg - k))
    return best % 360, deg - best


def main():
    import kenv
    kenv.load()
    from kortex_api.TCPTransport import TCPTransport
    from kortex_api.RouterClient import RouterClient
    from kortex_api.SessionManager import SessionManager
    from kortex_api.autogen.client_stubs.BaseClientRpc import BaseClient
    from kortex_api.autogen.client_stubs.BaseCyclicClientRpc import BaseCyclicClient
    from kortex_api.autogen.messages import Base_pb2, Session_pb2

    user = os.environ.get("KINOVA_USER")
    password = os.environ.get("KINOVA_PASS")
    if not user or not password:
        sys.exit("Set KINOVA_USER and KINOVA_PASS, or fill /etc/kinova.env.")
    if TARGET not in ("display", "keepsake", "pots"):
        sys.exit("Set KINOVA_TEACH to display, keepsake or pots.")

    transport = TCPTransport()
    router = RouterClient(transport, RouterClient.basicErrorCallback)
    transport.connect(os.environ.get("KINOVA_IP", "192.168.1.10"), 10000)
    info = Session_pb2.CreateSessionInfo()
    info.username = user
    info.password = password
    info.session_inactivity_timeout = 60000
    info.connection_inactivity_timeout = 2000
    session = SessionManager(router)
    session.CreateSession(info)
    base = BaseClient(router)
    cyclic = BaseCyclicClient(router)

    def capture(prompt):
        """Ask for one point and read where the arm is. Commands nothing."""
        print("\n  {}".format(prompt))
        print("  right click to capture, middle click to scrap it and ask again")
        notify.beep(times=2, on=0.08, gap=0.08)
        if notify.wait_for_button(("right", "middle")) == "middle":
            print("  scrapped.")
            return None
        fb = cyclic.RefreshFeedback()
        point = (fb.base.tool_pose_x, fb.base.tool_pose_y, fb.base.tool_pose_z)
        print("  captured x={:+.4f} y={:+.4f} z={:+.4f}  ({:.3f} m out)".format(
            point[0], point[1], point[2], math.hypot(point[0], point[1])))
        return point

    def ask(prompt):
        while True:
            point = capture(prompt)
            if point is not None:
                return point

    try:
        # Unlike every other script here, MANUALLY_CONTROLLED is not a problem: it
        # is the whole point, since admittance mode is how you move the arm by
        # hand. What must not happen is teaching over the top of a painting.
        state = base.GetArmState().active_state
        if state == Base_pb2.ARMSTATE_SERVOING_PLAYING_SEQUENCE:
            raise SystemExit(
                "Refusing: the arm is mid sequence, so something is painting.\n"
                "Let it finish, or stop the queue runner first.")
        try:
            run = fetch("/api/run")
            if run.get("state") == "running":
                raise SystemExit(
                    "Refusing: the pad says job #{} is painting. Let it finish first."
                    .format(run.get("job")))
        except SystemExit:
            raise
        except Exception:
            pass            # no pad reachable is not a reason to refuse a teach

        print("arm state  : {}".format(Base_pb2.ArmState.Name(state)))
        if state == Base_pb2.ARMSTATE_SERVOING_READY:
            print("             press the wrist button to go compliant before pushing it.")
        else:
            print("             which usually means it is compliant and ready to push.")
        print("teaching   : {}".format(TARGET))
        print("This commands no motion at all. You move the arm, it only reads.")

        layout = fetch("/api/layout")
        sector = layout["sector"]
        notes = []

        if TARGET == "pots":
            a = ask("brush into the FIRST pot, at one end of the block")
            b = ask("now the LAST pot, at the other end")
            centre, deg, m = solve_pots(a, b)
            notes.append("pot spacing measured {:.1f} mm, out by {:+.1f} mm".format(
                m["measured_mm"], m["pitch_mm"]))
            zs = [a[2], b[2]]
            corners = {"first pot": a[:2], "last pot": b[:2]}
        else:
            a = ask("brush on the TOP LEFT corner of the drawing (row 0, column 0)")
            b = ask("now the TOP RIGHT corner (row 0, last column)")
            c = ask("now the BOTTOM LEFT corner (last row, column 0)")
            centre, deg, m = solve_sheet(a, b, c)
            notes.append("sides out by {:+.1f} mm along and {:+.1f} mm across".format(
                m["along_mm"], m["across_mm"]))
            notes.append("corners {:+.2f} deg off square".format(m["skew_deg"]))
            tilt, far = tilt_of(a, b, c)
            if tilt is not None:
                notes.append("paper tilts {:.2f} deg from level".format(tilt))
            zs = [a[2], b[2], c[2], far[2]]
            corners = {"top left": a[:2], "top right": b[:2],
                       "bottom left": c[:2], "bottom right": far[:2]}

        print("\n" + "=" * 68)
        print("measured {}".format(TARGET))
        print("=" * 68)
        print("centre     : x={:+.4f} y={:+.4f}".format(centre[0], centre[1]))
        print("rotation   : {:+.2f} deg".format(deg))
        print("height     : {:+.4f} m mean, {:.1f} mm corner to corner".format(
            sum(zs) / len(zs), (max(zs) - min(zs)) * 1000))
        for note in notes:
            print("             {}".format(note))

        print("\ncorners the arm would have to paint:")
        for label in sorted(corners):
            x, y = corners[label]
            print("  {:<13} x={:+.4f} y={:+.4f}   {:.3f} m out".format(
                label, x, y, math.hypot(x, y)))

        bad = corner_problems(corners, sector)
        if bad:
            print("\nOutside what paint_sim will accept:")
            for line in bad:
                print("  {}".format(line))
            raise SystemExit(
                "\nRefusing to save a layout the arm cannot paint. Move it, teach again.")

        # layout.json stores rot as 0 or 90 and nothing in between, so a sheet that
        # is not square cannot be described. Say so rather than rounding it away:
        # one degree across this sheet is 5 mm at the far corner, most of a dab.
        snapped, off = nearest_right_angle(deg)
        if snapped not in (0, 90):
            raise SystemExit(
                "\nMeasured rotation {:+.2f} deg is nearest {} deg, and layout.json only\n"
                "holds 0 or 90. Turn it round and teach again.".format(deg, snapped))
        if abs(off) > SQUARE_TOL:
            raise SystemExit(
                "\nRotation is {:+.2f} deg off square, more than the {:.1f} deg this will\n"
                "accept, which is {:.0f} mm across the sheet. Square the paper by hand and\n"
                "teach again, rather than saving a frame that cannot say it is skewed."
                .format(off, SQUARE_TOL, abs(math.radians(off)) * 2 * U * 1000))

        print("\nwould save : x={:+.4f} y={:+.4f} rot={}".format(
            centre[0], centre[1], snapped))
        print("not saved  : height and tilt, since layout.json has nowhere for them.")
        print("             Write them down if you are setting real dab heights.")

        if not ARMED:
            print("\nDRY RUN. Nothing saved. Re-run with KINOVA_CONFIRM=yes to keep it.")
            raise SystemExit(0)

        place = {"x": round(centre[0], 4), "y": round(centre[1], 4), "rot": snapped}
        if TARGET == "pots":
            layout["pots"] = place
        else:
            layout["sheets"][TARGET] = place
        problem = save_layout(layout)
        if problem:
            raise SystemExit("Could not save: {}".format(problem))
        print("\nSaved. The setup page will now show it where you taught it.")
        notify.beep(times=3, on=0.12, gap=0.1)
    finally:
        try:
            session.CloseSession()
            router.SetActivationStatus(False)
        except Exception:
            pass
        transport.disconnect()
        print("session closed cleanly.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Nothing was saved.")
