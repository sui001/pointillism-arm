#!/usr/bin/env python3
"""Turn a photograph into a pointillism job: 30 x 42 cells of five pigments.

Two ways in. `render(img)` takes a frame that is already in memory and hands
back the dabs and a thumbnail, which is what `headshot.py` uses so that a
visitor's photograph never reaches the disk. The command line below reads a file
and is a thin wrapper over the same call, so both go through identical code.

The pad's own words are that colours sit next to each other and the viewer's eye
does the blending. That is optical mixing, and optical mixing is what error
diffusion dithering exploits, so the algorithm here is not a trick bolted onto
the piece: it is the thing the piece already claims to do, done arithmetically.

Nearest-colour matching would posterise a face into flat blocks and look like
nothing. Diffusing the error into neighbouring cells instead lets six available
tones imply the ones in between, which is the whole reason a Seurat works.

Six, not five: an unpainted cell is white paper, and paper is a colour you have
for free. Together with carbon, ochre and venetian that is very close to a Zorn
palette, the black, yellow ochre, red and white that portrait painters have used
for a century. Ultramarine adds the cool shadow. It is a better palette for a
face than five arbitrary colours had any right to be.

    # render a photo and see what it looks like, queue nothing:
    KINOVA_PHOTO=/tmp/me.jpg ~/kinova-py310/bin/python ~/kinova/portrait.py
    # find the person in it and crop to them first:
    KINOVA_PHOTO=/tmp/me.jpg KINOVA_FIND=yes ~/kinova-py310/bin/python portrait.py
    # and put it in the queue to be painted:
    KINOVA_PHOTO=/tmp/me.jpg KINOVA_CONFIRM=yes ~/kinova-py310/bin/python portrait.py

KINOVA_PHOTO     image to read. Required.
KINOVA_FIND      yes to locate a person with YOLOX and crop to their head and
                 shoulders, rather than using the whole frame.
KINOVA_PREVIEW   where to write a picture of what the arm would paint,
                 default /tmp/portrait.png
KINOVA_GAIN      how hard to open the tonal range around the picture's own
                 midpoint, default 1.8. Above 1 adds contrast; 1.0 leaves the
                 photo alone. This is what makes eyes and mouth appear at all.
KINOVA_CHROMA    how much colour error counts against lightness error,
                 default 0.6. Lower keeps the face readable, higher keeps it
                 colourful. This is the knob worth playing with.
KINOVA_CONFIRM   yes to submit it to the pad queue.
KINOVA_PAD_URL   default http://127.0.0.1:8010
"""
import base64
import json
import math
import os
import sys
import urllib.request

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

COLS, ROWS = 30, 42
PHOTO = os.environ.get("KINOVA_PHOTO", "")
FIND = os.environ.get("KINOVA_FIND") == "yes"
PREVIEW = os.environ.get("KINOVA_PREVIEW", "/tmp/portrait.png")
GAIN = float(os.environ.get("KINOVA_GAIN", "1.8"))
CHROMA = float(os.environ.get("KINOVA_CHROMA", "0.6"))
VIGNETTE = float(os.environ.get("KINOVA_VIGNETTE", "0.8"))
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
PAD = os.environ.get("KINOVA_PAD_URL", "http://127.0.0.1:8010")

# Must match pad.html. Paper is last and means "leave this cell alone", which is
# why it has no pigment name: there is no white paint, only unpainted paper.
PIGMENTS = [
    ("carbon", (0x14, 0x11, 0x0f)),
    ("green", (0x5a, 0x8f, 0x3c)),
    ("ochre", (0xc9, 0x8a, 0x2e)),
    ("ultramarine", (0x2b, 0x4c, 0x8c)),
    ("venetian", (0xa2, 0x3b, 0x2e)),
    (None, (0xf4, 0xf2, 0xea)),
]


def srgb_to_linear(c):
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_oklab(rgb):
    """Ottosson's Oklab. Perceptually uniform, so nearest means nearest to an eye."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    return np.stack([
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    ], axis=-1)


def to_oklab(bgr):
    rgb = bgr[..., ::-1].astype(np.float64)
    return linear_to_oklab(srgb_to_linear(rgb))


def crop_to_grid(img):
    """Trim to the grid's own shape so nobody comes out stretched."""
    want = COLS / float(ROWS)
    h, w = img.shape[:2]
    have = w / float(h)
    if have > want:
        new = int(round(h * want))
        x = (w - new) // 2
        return img[:, x:x + new]
    new = int(round(w / want))
    y = max(0, (h - new) // 2)
    return img[y:y + new, :]


def face_box(img):
    """Find the biggest frontal face. Haar, because it ships with cv2.

    Person detection is the wrong tool: the first version took the top 45% of a
    YOLOX person box, which is fine for somebody standing across the room and
    useless for a selfie that already fills the frame, where it returns a
    forehead and a lot of ceiling. A portrait wants the face itself.
    """
    path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    grey = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    grey = cv2.equalizeHist(grey)
    faces = cv2.CascadeClassifier(path).detectMultiScale(
        grey, scaleFactor=1.1, minNeighbors=5,
        minSize=(max(40, img.shape[1] // 20), max(40, img.shape[0] // 20)))
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def crop_portrait(img, found=None):
    """Head and shoulders around a found face, in the grid's own proportions.

    A face has to survive being 30 cells wide. Head and shoulders just about
    manages it; anything looser turns into a smudge with a collar.

    `found` is a face box already located by the caller, which is how
    `headshot.py` avoids running the cascade twice: its framing loop has to find
    the face before it can decide it is framed, and that is the same box.
    Nothing is printed here, because this is now library code and the caller
    knows whether anybody is reading.
    """
    if found is None:
        found = face_box(img)
    if found is None:
        return crop_to_grid(img)
    fx, fy, fw, fh = (int(v) for v in found)
    cx = fx + fw / 2.0
    cy = fy + fh / 2.0
    height = fh * 2.6                      # chin-to-crown box out to shoulders
    width = height * (COLS / float(ROWS))
    top = cy - 0.38 * height               # eyes a bit above centre, as portraits go
    left = cx - width / 2.0
    h, w = img.shape[:2]
    scale = min(1.0, w / width, h / height)
    width, height = width * scale, height * scale
    left = min(max(0.0, left), w - width)
    top = min(max(0.0, top), h - height)
    return img[int(top):int(top + height), int(left):int(left + width)]


def vignette(lab, paper, amount):
    """Fade the corners to bare paper, so the room stops competing with the face.

    A portrait does not want a lab ceiling in it, and every background cell it
    drops is a dab the arm does not have to paint. Cheaper than segmentation and
    it looks deliberate: the oval is how portraits have been framed for
    centuries. If a proper cut-out is ever wanted, cv2.grabCut seeded from the
    face box is the next step up.
    """
    if amount <= 0:
        return lab
    rows, cols = lab.shape[:2]
    yy, xx = np.mgrid[0:rows, 0:cols]
    rx, ry = cols / 2.0, rows / 2.0
    r = np.sqrt(((xx - rx + 0.5) / (rx * 0.98)) ** 2 + ((yy - ry + 0.5) / (ry * 0.98)) ** 2)
    keep = np.clip((1.15 - r) / 0.35, 0.0, 1.0)
    keep = keep * keep * (3 - 2 * keep)            # smoothstep, no hard edge
    keep = 1.0 - (1.0 - keep) * amount
    return lab * keep[..., None] + paper * (1.0 - keep)[..., None]


def curve(lab, gain, floor, ceiling):
    """Open the tonal range out around the picture's own midpoint.

    Two wrong versions came before this one, and the reasons are worth keeping.

    The first normalised lightness onto 0..1. But the darkest pigment sits at
    L=0.18 and paper at 0.96, so everything pushed below 0.18 had nothing
    available but black, and a photo whose darkest tone was already lighter than
    the black paint came out as a solid slab.

    The second normalised onto the palette's real range instead, which fixed the
    slab and broke the colour: normalising drags the midtones to the middle of
    the range, and the middle of this palette is ultramarine and venetian. Skin
    belongs near ochre at 0.68, so a face full of warm midtones came out full of
    blue.

    A face's own tonal range is narrow, maybe 0.55 to 0.75, which is about one
    step of this palette, so features vanish. What is needed is more contrast
    *around where the face already sits*, not a remap of the whole range. Gain
    about the median does that: skin stays on ochre, shadows fall to venetian,
    the deepest features reach carbon, and highlights go to bare paper. Which is
    a Zorn palette doing what a Zorn palette is for.
    """
    L = lab[..., 0]
    mid = float(np.median(L))
    lab = lab.copy()
    lab[..., 0] = np.clip(mid + (L - mid) * gain, floor, ceiling)
    return lab


def dither(lab, palette, chroma):
    """Floyd-Steinberg, serpentine, in Oklab. Returns a palette index per cell.

    Serpentine because scanning one way every row walks the error sideways and
    leaves visible diagonal grain. Alternating cancels most of it.
    """
    weight = np.array([1.0, chroma, chroma])
    work = lab.astype(np.float64).copy()
    out = np.zeros(work.shape[:2], dtype=np.int32)
    rows, cols = out.shape
    for r in range(rows):
        order = range(cols) if r % 2 == 0 else range(cols - 1, -1, -1)
        ahead = 1 if r % 2 == 0 else -1
        for c in order:
            want = work[r, c]
            d = ((palette - want) * weight) ** 2
            pick = int(np.argmin(d.sum(axis=1)))
            out[r, c] = pick
            err = np.clip(want - palette[pick], -0.35, 0.35)
            for dr, dc, f in ((0, ahead, 7 / 16.0), (1, -ahead, 3 / 16.0),
                              (1, 0, 5 / 16.0), (1, ahead, 1 / 16.0)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < rows and 0 <= cc < cols:
                    work[rr, cc] += err * f
    return out


def preview(index, cell=16):
    """Draw what the arm would actually put down: round dabs on paper."""
    paper = PIGMENTS[-1][1]
    img = np.full((ROWS * cell, COLS * cell, 3), paper[::-1], dtype=np.uint8)
    for r in range(ROWS):
        for c in range(COLS):
            name, rgb = PIGMENTS[index[r, c]]
            if name is None:
                continue
            cv2.circle(img, (c * cell + cell // 2, r * cell + cell // 2),
                       int(cell * 0.42), tuple(int(v) for v in rgb[::-1]), -1,
                       lineType=cv2.LINE_AA)
    return img


def thumb_uri(index, cell=6):
    ok, buf = cv2.imencode(".png", preview(index, cell))
    if not ok:
        return ""
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()


def render(img, find=True, face=None, gain=GAIN, chroma=CHROMA,
           vignette_amount=VIGNETTE, thumb_cell=6):
    """One frame in, one job's worth of dabs out. No file is read or written.

    This is the whole pipeline and the only place it lives: crop, downsample to
    the grid, convert to Oklab, open the tonal range about the picture's own
    median, fade the background to paper, dither, and read off a pigment per
    cell. `main()` below is a file reader wrapped round this, and `headshot.py`
    hands it a frame straight from the camera that is never stored anywhere.

    `face` lets a caller that has already located a face pass the box in rather
    than paying for the cascade twice.

    Gives back a dict, because the caller wants different parts of it: the pad
    wants `dabs` and `thumb`, a person at a terminal wants `counts` and `face`,
    and `stages.py` wants `index`.
    """
    if face is None and find:
        face = face_box(img)
    cropped = crop_portrait(img, face) if find else crop_to_grid(img)

    small = cv2.resize(cropped, (COLS, ROWS), interpolation=cv2.INTER_AREA)
    palette = linear_to_oklab(srgb_to_linear(
        np.array([rgb for _, rgb in PIGMENTS], dtype=np.float64)))
    lab = curve(to_oklab(small), gain, palette[:, 0].min(), palette[:, 0].max())
    lab = vignette(lab, palette[-1], vignette_amount)
    index = dither(lab, palette, chroma)

    dabs = []
    counts = {}
    for r in range(ROWS):
        for c in range(COLS):
            name = PIGMENTS[index[r, c]][0]
            counts[name or "paper"] = counts.get(name or "paper", 0) + 1
            if name:
                dabs.append({"row": r, "col": c, "pigment": name})

    return {
        "dabs": dabs,
        "thumb": thumb_uri(index, thumb_cell),
        "index": index,
        "counts": counts,
        "face": None if face is None else tuple(int(v) for v in face),
        "cropped_shape": (cropped.shape[1], cropped.shape[0]),
    }


def submit(dabs, thumb):
    body = json.dumps({"dabs": dabs, "thumb": thumb}).encode()
    request = urllib.request.Request(
        PAD + "/api/jobs", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as r:
        return json.load(r)


def main():
    if not PHOTO:
        sys.exit("Set KINOVA_PHOTO to an image.")
    img = cv2.imread(PHOTO)
    if img is None:
        sys.exit("Could not read {}".format(PHOTO))
    print("photo      : {} ({}x{})".format(PHOTO, img.shape[1], img.shape[0]))

    out = render(img, find=FIND)
    dabs, counts, index = out["dabs"], out["counts"], out["index"]

    if FIND and out["face"] is None:
        print("face       : none found, so the middle of the frame was used")
    elif FIND:
        fw, fh = out["face"][2], out["face"][3]
        print("face       : {}x{} px, {:.0%} of the frame width".format(
            fw, fh, fw / float(img.shape[1])))
    print("cropped to : {}x{}, grid shape".format(*out["cropped_shape"]))

    print("\ncells      : {} total".format(ROWS * COLS))
    for name in sorted(counts, key=lambda k: -counts[k]):
        print("  {:<12} {:>4}  {:>4.0%}".format(name, counts[name],
                                                counts[name] / float(ROWS * COLS)))
    print("\ndabs       : {}  ({:.0%} of the sheet gets paint)".format(
        len(dabs), len(dabs) / float(ROWS * COLS)))
    print("painting   : about {:.0f} min per copy at 4 s a dab".format(
        len(dabs) * 4 / 60.0))

    cv2.imwrite(PREVIEW, preview(index))
    print("preview    : {}".format(PREVIEW))

    if not ARMED:
        print("\nNothing queued. Add KINOVA_CONFIRM=yes to send it to be painted.")
        return
    if not dabs:
        sys.exit("Nothing to paint: every cell came out as bare paper.")
    print("\nqueued as  : #{}".format(submit(dabs, out["thumb"]).get("id")))


if __name__ == "__main__":
    main()
