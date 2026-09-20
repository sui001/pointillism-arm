#!/usr/bin/env python3
"""Check that splitting portrait.py into a library did not change what it paints.

The split moved the whole pipeline out of `main()` and into `render()`, so the
thing worth proving is that identical pixels still give identical dabs. This
runs the new `render()` and a copy of the old inline pipeline over the same
frame and compares the two dab lists cell by cell.

It needs a photograph, which this repo deliberately does not carry: it is public
and the argument for the portrait side is that photographs are not kept. Put one
beside this file as sample.jpg, where .gitignore will leave it alone.

    python test_portrait.py            # uses ./sample.jpg
    KINOVA_PHOTO=some.jpg python test_portrait.py
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import portrait as P

PHOTO = os.environ.get("KINOVA_PHOTO", os.path.join(HERE, "sample.jpg"))
fails = []


def old_pipeline(img, find=True):
    """portrait.py's main() as it was before the split, inlined verbatim.

    If this drifts from the real code the test stops meaning anything, which is
    the same bargain test_teach.py makes with place().
    """
    img = P.crop_portrait(img) if find else P.crop_to_grid(img)
    small = cv2.resize(img, (P.COLS, P.ROWS), interpolation=cv2.INTER_AREA)
    palette = P.linear_to_oklab(P.srgb_to_linear(
        np.array([rgb for _, rgb in P.PIGMENTS], dtype=np.float64)))
    lab = P.curve(P.to_oklab(small), P.GAIN, palette[:, 0].min(), palette[:, 0].max())
    lab = P.vignette(lab, palette[-1], P.VIGNETTE)
    index = P.dither(lab, palette, P.CHROMA)
    dabs = []
    for r in range(P.ROWS):
        for c in range(P.COLS):
            name = P.PIGMENTS[index[r, c]][0]
            if name:
                dabs.append({"row": r, "col": c, "pigment": name})
    return dabs


img = cv2.imread(PHOTO)
if img is None:
    sys.exit("Could not read {}. See this file's docstring.".format(PHOTO))
print("photo      : {} ({}x{})".format(PHOTO, img.shape[1], img.shape[0]))

for find in (True, False):
    label = "cropped to the face" if find else "middle of the frame"
    want = old_pipeline(img, find)
    out = P.render(img, find=find)
    same = out["dabs"] == want
    print("  {:<20} {:>4} dabs, {}".format(
        label, len(out["dabs"]), "identical to the old pipeline" if same
        else "DIFFERS from the old pipeline"))
    if not same:
        fails.append(label)

out = P.render(img)
print("\nwhat it would paint")
for name in sorted(out["counts"], key=lambda k: -out["counts"][k]):
    n = out["counts"][name]
    print("  {:<12} {:>4}  {:>4.0%}".format(name, n, n / float(P.ROWS * P.COLS)))

# The pad takes the thumb straight into an <img src>, so a broken URI is a blank
# preview at exactly the moment a visitor is deciding whether to be painted.
if not out["thumb"].startswith("data:image/png;base64,"):
    print("\nthumb      : not a png data URI")
    fails.append("thumb")
else:
    print("\nthumb      : png data URI, {:.0f} kB".format(len(out["thumb"]) / 1024.0))

# Every dab has to be a cell the pad's own grid has, and a pigment the arm has a
# pot for. paint_sim believes both without checking.
names = {n for n, _ in P.PIGMENTS if n}
off_grid = [d for d in out["dabs"]
            if not (0 <= d["row"] < P.ROWS and 0 <= d["col"] < P.COLS)]
unknown = sorted({d["pigment"] for d in out["dabs"]} - names)
print("cells      : {} dabs, {} off the grid, {} unknown pigment(s)".format(
    len(out["dabs"]), len(off_grid), len(unknown)))
if off_grid:
    fails.append("off-grid dabs")
if unknown:
    print("  unknown: {}".format(", ".join(unknown)))
    fails.append("unknown pigments")

# A face that was found should be a plausible face, not a doorframe. The band is
# wide on purpose: sample.jpg is a selfie whose face is 59% of the frame width,
# which is legitimate and was the first thing this check wrongly failed. What it
# is actually for is the cascade latching onto a tiny background object, or onto
# the whole frame, both of which give a crop that is a smudge.
if out["face"] is None:
    print("face       : none found")
else:
    frac = out["face"][2] / float(img.shape[1])
    print("face       : {}x{} px, {:.0%} of the frame width".format(
        out["face"][2], out["face"][3], frac))
    if not 0.05 <= frac <= 0.9:
        fails.append("implausible face box")

print("\n" + ("FAILURES: " + ", ".join(fails) if fails else "all checks passed"))
sys.exit(1 if fails else 0)
