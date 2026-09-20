#!/usr/bin/env python3
"""Render every stage of the portrait pipeline into one self-contained HTML page.

Getting a face out of 1260 cells and six colours took several wrong turns, and
every one of them was invisible from the final picture alone: a render that is
"not quite a face" tells you nothing about which step spoiled it. Seeing the
stages side by side does. The two real bugs both became obvious the moment the
intermediate images were on screen next to each other.

    KINOVA_PHOTO=sample.jpg ~/kinova-py310/bin/python stages.py

Writes index.html with every image inlined, so it can be opened straight off
disk with no server and nothing to install. Also sweeps a few settings, because
the right gain is a matter of taste and taste needs a contact sheet.

KINOVA_PHOTO   the photograph. Required.
KINOVA_OUT     where to write the page, default index.html
KINOVA_GAIN    gain for the main render, default 1.8
KINOVA_CHROMA  chroma weight for the main render, default 0.6
KINOVA_VIGNETTE  vignette for the main render, default 0.5
"""
import base64
import html
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import portrait as P

PHOTO = os.environ.get("KINOVA_PHOTO", os.path.join(HERE, "sample.jpg"))
OUT = os.environ.get("KINOVA_OUT", os.path.join(HERE, "index.html"))
GAIN = float(os.environ.get("KINOVA_GAIN", "1.8"))
CHROMA = float(os.environ.get("KINOVA_CHROMA", "0.6"))
VIG = float(os.environ.get("KINOVA_VIGNETTE", "0.5"))

PALETTE = P.linear_to_oklab(P.srgb_to_linear(
    np.array([rgb for _, rgb in P.PIGMENTS], dtype=np.float64)))


def oklab_to_bgr(lab):
    """Back out of Oklab so an intermediate stage can be looked at."""
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    rgb = np.stack([
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    ], axis=-1)
    rgb = np.clip(rgb, 0.0, 1.0)
    srgb = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - 0.055)
    return (np.clip(srgb, 0, 1) * 255).astype(np.uint8)[..., ::-1]


def uri(img, width=None):
    if width:
        scale = width / float(img.shape[1])
        img = cv2.resize(img, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST)
    ok, buf = cv2.imencode(".png", img)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode() if ok else ""


def blocky(small, cell=12):
    return cv2.resize(small, (small.shape[1] * cell, small.shape[0] * cell),
                      interpolation=cv2.INTER_NEAREST)


def run(img, gain, chroma, vig):
    small = cv2.resize(P.crop_portrait(img), (P.COLS, P.ROWS),
                       interpolation=cv2.INTER_AREA)
    raw = P.to_oklab(small)
    curved = P.curve(raw, gain, PALETTE[:, 0].min(), PALETTE[:, 0].max())
    faded = P.vignette(curved, PALETTE[-1], vig)
    index = P.dither(faded, PALETTE, chroma)
    return small, raw, curved, faded, index


def counts_of(index):
    out = {}
    for r in range(P.ROWS):
        for c in range(P.COLS):
            name = P.PIGMENTS[index[r, c]][0] or "paper"
            out[name] = out.get(name, 0) + 1
    return out


def main():
    img = cv2.imread(PHOTO)
    if img is None:
        sys.exit("could not read {}".format(PHOTO))
    print("photo {} ({}x{})".format(PHOTO, img.shape[1], img.shape[0]))

    marked = img.copy()
    found = P.face_box(img)
    if found is not None:
        x, y, w, h = (int(v) for v in found)
        cv2.rectangle(marked, (x, y), (x + w, y + h), (0, 220, 255), max(2, img.shape[1] // 300))

    small, raw, curved, faded, index = run(img, GAIN, CHROMA, VIG)
    counts = counts_of(index)
    dabs = P.ROWS * P.COLS - counts.get("paper", 0)

    stages = [
        ("1. the photograph", uri(marked, 420),
         "Face found with a Haar cascade. Person detection was the wrong tool: it "
         "returns a whole torso, and the top of that is forehead and ceiling."),
        ("2. cropped to head and shoulders", uri(P.crop_portrait(img), 300),
         "In the grid's own 30:42 proportions, so nobody comes out stretched. "
         "Any looser and a face will not survive being 30 cells wide."),
        ("3. down to 30 x 42", uri(blocky(small), 300),
         "1260 cells. This is the target, and it is plainly a face, which is how "
         "we know later problems are the algorithm's fault and not the resolution's."),
        ("4. tonal range opened out", uri(blocky(oklab_to_bgr(curved)), 300),
         "Gain {:.1f} about the picture's own median. A face's range is narrow, "
         "about one step of this palette, so without this the features vanish."
         .format(GAIN)),
        ("5. background faded to paper", uri(blocky(oklab_to_bgr(faded)), 300),
         "Vignette {:.2f}. The room stops competing with the face, and every cell "
         "it drops is a dab the arm never has to paint.".format(VIG)),
        ("6. what the arm will paint", uri(P.preview(index), 300),
         "Floyd-Steinberg error diffusion, serpentine, in Oklab with chroma "
         "weighted {:.2f}. {} dabs, {:.0%} of the sheet.".format(
             CHROMA, dabs, dabs / float(P.ROWS * P.COLS))),
    ]

    sweep = []
    for g in (1.2, 1.8, 2.6):
        for ch in (0.3, 0.6, 1.2):
            _, _, _, _, idx = run(img, g, ch, VIG)
            n = P.ROWS * P.COLS - counts_of(idx).get("paper", 0)
            sweep.append(("gain {:.1f}, chroma {:.1f}".format(g, ch),
                          uri(P.preview(idx, 8), 150), "{} dabs".format(n)))

    swatches = "".join(
        '<div class="sw"><span style="background:#{:02x}{:02x}{:02x}"></span>'
        '<b>{}</b><i>L={:.3f}</i></div>'.format(
            rgb[0], rgb[1], rgb[2], name or "paper (no dab)", lab[0])
        for (name, rgb), lab in sorted(zip(P.PIGMENTS, PALETTE), key=lambda t: t[1][0]))

    tally = "".join(
        "<tr><td>{}</td><td>{}</td><td>{:.0%}</td></tr>".format(
            k, counts[k], counts[k] / float(P.ROWS * P.COLS))
        for k in sorted(counts, key=lambda k: -counts[k]))

    cards = "".join(
        '<figure><img src="{}" alt="{}"><figcaption><b>{}</b><span>{}</span>'
        '</figcaption></figure>'.format(u, html.escape(t), html.escape(t), html.escape(d))
        for t, u, d in stages)

    grid = "".join(
        '<figure class="s"><img src="{}" alt="{}"><figcaption><b>{}</b>'
        '<span>{}</span></figcaption></figure>'.format(u, html.escape(t),
                                                       html.escape(t), html.escape(d))
        for t, u, d in sweep)

    page = TEMPLATE.format(cards=cards, grid=grid, swatches=swatches, tally=tally,
                           dabs=dabs, minutes=dabs * 4 / 60.0,
                           photo=html.escape(os.path.basename(PHOTO)))
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(page)
    print("wrote {} ({:.0f} KB)".format(OUT, os.path.getsize(OUT) / 1024.0))
    print("{} dabs, about {:.0f} min a copy".format(dabs, dabs * 4 / 60.0))


TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pointillism portrait stages</title>
<style>
  :root {{
    --paper:#eef0ea; --panel:#e3e6dc; --ink:#1e2420; --dim:#333a31;
    --accent:#1b6e73; --line:rgba(30,36,32,.18);
  }}
  @media (prefers-color-scheme:dark) {{
    :root {{ --paper:#171a17; --panel:#20241f; --ink:#f2f4ec; --dim:#d3dbd1;
             --accent:#4fd1d9; --line:rgba(242,244,236,.18); }}
  }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; padding-inline:20px; padding-block:28px 60px; background:var(--paper);
         color:var(--ink); font:15px/1.55 system-ui,sans-serif;
         display:flex; justify-content:center }}
  .sheet {{ width:100%; max-width:1080px; display:flex; flex-direction:column; gap:26px }}
  .eyebrow {{ font:600 .72rem/1 ui-monospace,monospace; letter-spacing:.14em;
              text-transform:uppercase; color:var(--accent) }}
  h1 {{ font-size:clamp(1.7rem,1.4rem+1.4vw,2.5rem); margin:.3em 0 0; letter-spacing:-.01em }}
  h2 {{ font:600 .74rem/1 ui-monospace,monospace; letter-spacing:.1em;
        text-transform:uppercase; margin:0 0 4px }}
  p.dek {{ max-width:70ch; color:var(--dim); margin:.5em 0 0 }}
  .row {{ display:flex; flex-wrap:wrap; gap:18px }}
  figure {{ margin:0; background:var(--panel); border:1px solid var(--line);
            border-radius:8px; padding:12px; flex:1 1 300px; max-width:340px;
            display:flex; flex-direction:column; gap:10px }}
  figure.s {{ flex:0 0 160px; max-width:160px; padding:8px }}
  img {{ width:100%; height:auto; display:block; border-radius:4px;
         image-rendering:pixelated; background:#f4f2ea }}
  figcaption {{ display:flex; flex-direction:column; gap:4px }}
  figcaption b {{ font-size:.9rem }}
  figcaption span {{ color:var(--dim); font-size:.82rem }}
  .sw {{ display:flex; align-items:center; gap:10px; font-size:.86rem }}
  .sw span {{ width:26px; height:26px; border-radius:50%; border:1px solid var(--line) }}
  .sw i {{ color:var(--dim); font:.78rem/1 ui-monospace,monospace; font-style:normal }}
  .sw b {{ min-width:9em }}
  .cols {{ display:flex; flex-wrap:wrap; gap:28px }}
  .cols > div {{ flex:1 1 260px }}
  table {{ border-collapse:collapse; font-size:.86rem }}
  td {{ padding:2px 14px 2px 0; border-bottom:1px solid var(--line) }}
  td:nth-child(2), td:nth-child(3) {{ font:.82rem/1 ui-monospace,monospace; text-align:right }}
  .big {{ font:700 1.6rem/1 ui-monospace,monospace; color:var(--accent) }}
</style>
<div class="sheet">
  <header>
    <span class="eyebrow">Cybernetic Garden &middot; pointillism portrait</span>
    <h1>From a photograph to 1260 dabs</h1>
    <p class="dek">Every stage of turning <b>{photo}</b> into a job the arm can paint,
    in five pigments plus bare paper. Shown because a render that is not quite a face
    tells you nothing about which step spoiled it, and these do.</p>
  </header>

  <section>
    <h2>The pipeline</h2>
    <div class="row">{cards}</div>
  </section>

  <section class="cols">
    <div>
      <h2>The palette, by lightness</h2>
      {swatches}
      <p class="dek" style="font-size:.84rem">Paper is a colour you get for free.
      Carbon, ochre, venetian and white paper is close to a Zorn palette, the four
      colours portrait painters have used for a century.</p>
    </div>
    <div>
      <h2>This render</h2>
      <p class="big">{dabs} dabs</p>
      <p class="dek" style="font-size:.84rem">About {minutes:.0f} minutes a copy,
      two copies interleaved so they finish together.</p>
      <table>{tally}</table>
    </div>
  </section>

  <section>
    <h2>Gain and chroma, since the right values are a matter of taste</h2>
    <p class="dek">Gain opens the tonal range around the picture's own midpoint and is
    what makes features appear at all. Chroma decides how much being the wrong colour
    counts against being the wrong brightness: too low and skin goes blue, because a
    desaturated blue is a closer match to dull skin than a saturated red is.</p>
    <div class="row">{grid}</div>
  </section>
</div>
"""

if __name__ == "__main__":
    main()
