# Teaching the rig

A plan, not a feature yet. Written 2026-09-16 while job #3 painted, so the reasoning
behind it is still fresh when someone picks it up.

## Why

The setup page records where you *believe* the paper is. The arm then discovers the
truth, late and expensively. On 2026-09-16 that cost a whole afternoon:

- Sheets placed somewhere sensible looking put four dabs 3 mm too close to the base,
  and `paint_sim` refused the entire job.
- Before that, a run died 117 seconds in because a dab needed more joint speed than
  the arm allows from the configuration it happened to be in.

Teaching inverts it. If a corner comes from the arm's own forward kinematics, then
**reachability is proven by construction**: you cannot teach a point the arm cannot
get to, because teaching it means putting it there. That removes the whole class of
"the plan validated and the arm refused anyway".

It does not fix everything. A corner being reachable does not prove every dab between
two corners is comfortable: the two sheets suit different arm configurations, and a few
dabs after swapping back the arm can still run out of joint, which is what
`KINOVA_RECOVERIES` exists to survive. But it removes the dumbest half.

The README already gestures at this: "Paper and pots get registered by a jig taught
once by hand, rather than by camera, because a 7 mm dab pitch needs tighter precision
than a wrist camera gives." This is that jig, made part of the software.

The right word is **teaching**, or frame registration. Not homing: homing is the arm
finding its own zero.

## What gets taught

Three points per sheet, in this order, named from the visitor's side of the table:

1. **top left** becomes the origin
2. **top right** gives rotation, and with the known sheet width, a scale check
3. **bottom left** gives height and tilt, and squares the frame

Two points would be enough for `x`, `y` and `rot`, which is all `layout.json` holds
today. The third is worth the extra ten seconds because it gives three things the rig
currently guesses at:

- **Height.** `MIN_Z` is 0.12, hover 0.20, dip 0.04, and the heights are placeholders
  with nothing meant to touch. The moment a gripper holds a brush, real paper height
  stops being optional. Teaching hands it over for free.
- **Tilt.** The desk is not level. Across a 294 mm sheet, one degree of tilt is 5 mm
  corner to corner, which is most of a dab.
- **A sanity check.** If the three points do not form the A4 rectangle they should,
  someone mis-clicked or the paper is not where they think. Far better to hear that
  during setup than during a show.

Teach the pots the same way: one point per pot centre, or two at the ends of the
block plus the known pitch.

## How it should feel

Hand guiding, not driving. The arm goes compliant, a person pushes the brush tip onto
the corner, and a click captures where it actually is.

That is safer than commanding the arm to a guess and nudging it, because the arm is
compliant during the only part of this where a person is deliberately inside its
reach. It is also faster than typing coordinates.

On the Gen3 this is admittance mode, reached from the wrist button. It is the same
`ARMSTATE_SERVOING_MANUALLY_CONTROLLED` that confused us for an hour on 2026-09-16
when it turned out to be the harmless transient after any session closes. Read
`jog_joint.py` before writing any of this: it already knows how to wait that out and
how to refuse to take the arm off a person who is actually driving it.

Flow per sheet:

1. Page says which corner it wants, and which sheet, in words a stranger can follow.
2. Operator presses the wrist button, guides the brush tip to that corner.
3. **Right click** captures it. **Middle click** scraps the last point and re-asks.
4. After three points the page shows the derived `x`, `y`, `rot`, `z` and tilt, plus
   the squareness error, and asks for a yes before writing `layout.json`.

Nothing is written until the whole sheet is taught and agreed. A half-taught sheet
must leave the old layout alone.

## Pieces to build

| Piece | Work |
|---|---|
| `notify.py` | `BTN_RIGHT` is 0x111 and `BTN_MIDDLE` 0x112, beside the `BTN_LEFT` 0x110 it already watches. Generalise `wait_for_click` to take which button, keep the drain so a click made before the machine asked cannot answer for you |
| `teach.py` | Owns the arm. Waits for the arm to be free, reads the tool pose on each capture, solves the frame from three points, reports squareness. Refuses to write a frame whose corners fall outside the reach band, because that would re-import the bug this is meant to kill |
| `pad_server.py` | A teach session: start, capture, cancel, commit. Behind the studio password like the rest of setup. State in memory is fine, it dies with the session by design |
| `setup.html` | A teach panel per sheet: which corner is wanted, what has been captured, the derived frame, a commit button. The envelope shading added on 2026-09-16 already shows whether the result lands somewhere legal |

## Safety, which is the whole risk here

This is the only mode where someone is deliberately within reach of a live arm, so
it deserves more care than anything else in the repo.

- The arm must be **compliant, not commanded**, whenever a person is at the paper.
  If `teach.py` ever moves the arm under its own power during a teach, that is a
  design error.
- Capture must be **passive**: read the pose, command nothing.
- Never take the arm from a human. `jog_joint.py`'s wait-then-refuse is the pattern.
- Teaching must be impossible while a job is painting. Check the queue runner is idle
  first, the way `soft_limits.py` checks before writing limits.
- A taught frame gets validated against the reach band and the sweep **before** it is
  written, and the operator is told which corner failed and by how much.

## What would make this worth doing twice

If the configuration problem is still biting after teaching, the next thing worth
measuring is which parts of a taught sheet the arm can paint *in sequence*, rather
than one point at a time from Home. That is a different experiment and needs parking
between samples, or the previous bad configuration poisons the next reading. The scan
in this repo's history got that wrong twice before it was caught.
