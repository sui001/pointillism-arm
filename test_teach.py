#!/usr/bin/env python3
"""Round-trip teach.py's frame maths against paint_sim's own place().

Generate the corners a known sheet would present, feed them to the solver, and
check it hands back the sheet you started with. A sign error here would paint a
perfectly valid plan in the wrong place, silently, so this is the test that
matters.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import teach

COLS, ROWS, PITCH = teach.COLS, teach.ROWS, teach.PITCH
U, V = teach.U, teach.V


def place(item, u, v):
    """Copied verbatim from paint_sim.py. If this drifts, the test is worthless."""
    t = math.radians(item["rot"])
    return (item["x"] + u * math.cos(t) - v * math.sin(t),
            item["y"] + u * math.sin(t) + v * math.cos(t))


def dab_point(sheet, row, col):
    return place(sheet, ((ROWS - 1) / 2.0 - row) * PITCH,
                 ((COLS - 1) / 2.0 - col) * PITCH)


def corners_of(sheet, z=0.16):
    """What the operator would teach, at the three grid cells teach.py asks for."""
    a = dab_point(sheet, 0, 0) + (z,)
    b = dab_point(sheet, 0, COLS - 1) + (z,)
    c = dab_point(sheet, ROWS - 1, 0) + (z,)
    return a, b, c


fails = []


def check(name, got, want, tol, unit=""):
    ok = abs(got - want) <= tol
    if not ok:
        fails.append(name)
    print("  {:<34} {:>10.4f} want {:>9.4f} {:<3} [{}]".format(
        name, got, want, unit, "ok" if ok else "FAIL"))


print("=== round trip: known sheet -> corners -> solver -> sheet ===")
for sheet in ({"x": 0.43, "y": 0.13, "rot": 0},
              {"x": 0.43, "y": -0.12, "rot": 0},
              {"x": 0.50, "y": 0.21, "rot": 0},
              {"x": 0.39, "y": -0.115, "rot": 90},
              {"x": 0.46, "y": 0.02, "rot": 90}):
    a, b, c = corners_of(sheet)
    centre, deg, m = teach.solve_sheet(a, b, c)
    print("\nsheet x={x} y={y} rot={rot}".format(**sheet))
    check("centre x", centre[0], sheet["x"], 1e-9)
    check("centre y", centre[1], sheet["y"], 1e-9)
    snapped, off = teach.nearest_right_angle(deg)
    check("rot recovered", snapped, sheet["rot"], 1e-9, "deg")
    check("off square", off, 0.0, 1e-9, "deg")
    check("side error along", m["along_mm"], 0.0, 1e-6, "mm")
    check("side error across", m["across_mm"], 0.0, 1e-6, "mm")
    check("skew", m["skew_deg"], 0.0, 1e-6, "deg")

print("\n=== a deliberately skewed sheet should be detected, not smoothed away ===")
sheet = {"x": 0.43, "y": 0.13, "rot": 0}
a, b, c = corners_of(sheet)
skewed = (b[0] + 0.004, b[1], b[2])          # nudge one corner 4 mm out
centre, deg, m = teach.solve_sheet(a, skewed, c)
print("  skew reported: {:+.3f} deg (nudged one corner 4 mm)".format(m["skew_deg"]))
if abs(m["skew_deg"]) < 0.5:
    fails.append("skew not detected")
    print("  [FAIL] a 4 mm nudge should show up as skew")
else:
    print("  [ok] detected")

print("\n=== tilt: lift one corner 3 mm, expect a small angle and a 4th corner ===")
a, b, c = corners_of(sheet)
tilt, far = teach.tilt_of(a, b, (c[0], c[1], c[2] + 0.003))
expected = math.degrees(math.atan2(0.003, 2 * U))
print("  tilt {:.3f} deg, expected about {:.3f}".format(tilt, expected))
if abs(tilt - expected) > 0.05:
    fails.append("tilt")
    print("  [FAIL]")
else:
    print("  [ok]")
print("  implied 4th corner z = {:+.4f} (want {:+.4f})".format(far[2], 0.16 + 0.003))
if abs(far[2] - 0.163) > 1e-9:
    fails.append("4th corner z")
    print("  [FAIL]")

print("\n=== pots: known block -> two pot centres -> solver ===")
for pots in ({"x": 0.425, "y": -0.325, "rot": 90}, {"x": 0.13, "y": -0.5, "rot": 90},
             {"x": 0.30, "y": 0.40, "rot": 0}):
    first = place(pots, 0.0, (0 - 2) * teach.SLOT_PITCH) + (0.16,)
    last = place(pots, 0.0, (4 - 2) * teach.SLOT_PITCH) + (0.16,)
    centre, deg, m = teach.solve_pots(first, last)
    print("\npots x={x} y={y} rot={rot}".format(**pots))
    check("centre x", centre[0], pots["x"], 1e-9)
    check("centre y", centre[1], pots["y"], 1e-9)
    snapped, off = teach.nearest_right_angle(deg)
    check("rot recovered", snapped, pots["rot"], 1e-9, "deg")
    check("pitch error", m["pitch_mm"], 0.0, 1e-6, "mm")

print("\n=== reach band check should reject what paint_sim rejected ===")
sector = {"from": -100.0, "to": 50.0}
bad = teach.corner_problems({"inner": (0.2465, -0.0135)}, sector)
print("  the 0.39 keepsake corner (0.247 m):", bad or "NOT FLAGGED")
if not bad:
    fails.append("reach check")
good = teach.corner_problems({"inner": (0.2765, -0.0135)}, sector)
print("  the 0.43 keepsake corner (0.277 m):", good or "accepted, correct")
if good:
    fails.append("false reach rejection")

print("\n" + ("FAILURES: " + ", ".join(fails) if fails else "all checks passed"))
sys.exit(1 if fails else 0)
