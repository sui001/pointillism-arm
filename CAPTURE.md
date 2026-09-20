# Capture: the arm takes the photograph

Written 2026-09-20, away from the arm, so that the parts that can be built and
tested on a laptop are separated from the ones that need somebody standing next
to the machine. Steps 1 to 6 are now built, and "Where this got to" says what
came out different from the plan.

## What it should feel like

A visitor presses **New person** on the pad. The arm turns to the spot where the
light is good and the wall behind is blank, finds their face, frames it the same way
it frames everybody, takes one frame, and shows them the render as dabs. They say
yes and it joins the queue. Nothing else to click, no phone, no file, no laptop.

Two clicks in total: **New person**, and **Paint it**.

## The three rules this has to live inside

These are already true of the rig and none of them get bent for this.

1. **One place commands the arm.** Today that is `paint_sim.py`, launched by
   `run_queue.py`. Capture does not get its own private session on the side.
2. **Nothing moves until somebody at the machine has clicked.** The arm now moves
   while a person stands in front of it, so the click matters more here, not less.
3. **The photograph is never stored.** Capture, render to 1260 cells, discard. The
   painting is the artefact, the snapshot is not. Nothing is written to disk at any
   point, including a temp file.

## How it hangs together

```
pad.html  -- New person -->  pad_server      capture request, one at a time
                                 |
run_queue.py polls, beeps, waits for the click, then:
                                 |
                          headshot.py  (owns the arm, the way paint_sim does)
                                 |
        +------------------------+------------------------+
        |                        |                        |
   go to taught          frame the face            one clean frame
   portrait pose         joints 0 and 4            after motion stops
   (joint angles)        PD, like track.py         full res /snapshot
                                 |
                          portrait.render()        in memory, no file
                                 |
                      POST /api/capture            dabs + thumb, pending
                                 |
pad.html shows the render:  Paint it / Another go / No thanks
                                 |
                       Paint it becomes a normal job in the queue
```

The camera is not read directly. `stream.py` is the sole RTSP client and everything
else asks it over HTTP, so `headshot.py` pulls `/snapshot` and `/detections` the way
`track.py` does. That constraint is not negotiable: a second RTSP client kills the
video for everybody and can take the vision module down with it.

## Framing: half joints, half crop

"Moves the arm around until it takes the same sort of pic every time" splits into two
problems with two different answers, and the split is the whole trick.

**Where the face sits in the frame** is the arm's job. Pan on joint 0, tilt on joint
4, the same PD controller `track.py` already uses, with the error being the face
centre against a fixed target at (0.50, 0.42) of the frame. Slower and tighter than
tracking: an 8 deg/s ceiling, a hard joint window of about 25 degrees of yaw and 15
of pitch either side of the taught pose, and a timeout that gives up and returns to
the taught pose rather than hunting at somebody.

**How big the face is** is not the arm's job. Driving the arm in and out to normalise
face size is a Cartesian move near a person's head, for a result a crop gives for
free. So: crop a fixed multiple of the measured face height, 2.6 times as
`crop_portrait` already does, with the eyes at 38 percent down, and resample to the
grid's proportions. Tall visitor, short visitor, one step closer, same framing.

Two phases, because the two detectors have different strengths:

- **Coarse**, on `/detections`: YOLOX person boxes at about 3 Hz, already served,
  reliable on anybody upright. Gets a human into the middle of the frame.
- **Fine**, on `/snapshot` with the Haar cascade from `portrait.py`: only this gives
  the face box the crop needs. Haar is fussy about glasses, head angle and flat
  light, so it gets a patience budget rather than one attempt, while the coarse
  phase holds the person steady in frame.

Converged means the error is inside tolerance for three consecutive frames and the
joint speeds are zero. Then stop, wait half a second for the wrist to settle, and
**take a fresh full resolution frame**. The frames used for framing are throwaway.
A frame grabbed mid-correction is motion blurred at exactly the scale the dither
cares about.

## Consent, twice

The pad shows the render before anything is queued, which gives the visitor consent
over the output as well as the input. **Another go** re-runs the same capture path,
which is the same code and costs nothing to support. **No thanks** discards the lot.

Nothing is kept on either rejection, and there is nothing to keep, because the image
never left memory.

## What gets written

New:

| file | what |
|---|---|
| `headshot.py` | the whole capture: pose, frame, snap, render, propose. Owns the arm, dry run by default, like everything else here |
| `armlock.py` | an flock on one file, taken by anything that moves the arm |

Changed:

| file | what |
|---|---|
| `portrait.py` | vendored in from `pointillism-portrait`, and split so `render(bgr)` hands back dabs and a thumb. The CLI becomes a wrapper over it |
| `pad_server.py` | `/api/capture`: request, state, propose, decide. Held in memory, never written down |
| `pad.html` | the New person button, and the full screen preview with its three buttons |
| `run_queue.py` | serve a waiting person before the queue, with the same beep and click a painting gets |
| `teach.py` | `KINOVA_TEACH=portrait`, which records the pose to look from |

Exit codes stay a contract. 75 busy and 76 not at Home carry over unchanged, 77
means no face was found and it gave up, and 78 means the portrait pose was never
taught. The runner reports the last two rather than retrying them.

### armlock.py, and why now

There are about to be three things that move the arm: `paint_sim.py`, `headshot.py`
and `track.py`. Two sessions at once is a class of bug that presents as the arm
refusing everything for no reason, which is exactly the forty minute afternoon this
repo already has a section about. A shared `flock` on `/run/kinova.arm.lock`, taken
by every script that moves and refused loudly, is about forty lines and removes it.

## Teaching the portrait pose

The second thing that needs Sui at the arm. Same shape as teaching the paper: hand
guide, capture, command no motion.

Press the wrist button, push the arm until it looks at where a person will stand,
with a blank wall behind them and the light on their face rather than behind it.
Watch `stream.py`'s own live page while doing it, which is what that page is for.
Click to capture. It stores **joint angles**, not a Cartesian pose, so the pose is
reachable by construction, for the same reason teaching the paper corners is.

```
layout.portrait = {
  "pose":      [six joint angles],
  "target":    [0.50, 0.42],      where the face goes in the frame
  "face_frac": 0.18,              how big it should read, as a sanity check
  "taught_at": timestamp
}
```

A mark on the floor where people stand is worth more than any amount of code here.

## Where this got to

Steps 1 to 6 are built and tested on a laptop, with no arm and no camera. What
is left is steps 7 to 9, which all need somebody standing at the machine.

Four things came out different from the plan above, and the plan is wrong
rather than the code:

- **Jobs did not need a `kind` field.** A capture request is not a queue entry:
  it is a separate piece of state the runner checks first. That is better than
  queueing it, because a painting is forty minutes and nobody waits that long
  to be photographed, and it left the job schema alone.
- **`KINOVA_FAKE_CAM` became a pretend pan-tilt head**, not a fixed still. It
  asks the arm where it is pointing and returns the part of the still a camera
  at that bearing would see, and the dry-run arm integrates the speeds it is
  sent. So the framing loop really is tested, gains and all. What it cannot
  know is which way joint 0 physically turns, so `KINOVA_YAW_SIGN` and
  `KINOVA_PITCH_SIGN` are knobs and are the first thing to check at the arm.
- **The two detectors swapping mid-loop was a bug**, found by that simulator.
  The coarse and fine phases aim at different points on the same person, so
  differencing across a swap gave a derivative term measuring the swap rather
  than the person, and it arrived as a kick. A phase change now breaks the
  chain.
- **Standing too close breaks the framing promise** and nothing had noticed.
  Under about a metre the frame cannot hold 2.6 face heights, and the old code
  silently cropped tighter, so that one portrait is framed differently from
  every other one on the wall. It is now reported rather than corrected: the
  fix is a step backwards and only a person can take it.

`armlock.py` is the one piece with no test behind it. Windows has no `flock`,
so on this laptop it degrades to not locking and says so. It needs ten seconds
on the Pi: run `paint_sim.py` and `headshot.py` at once and check the second
one refuses and names the first.

## What can be built and tested without the arm

Most of it, if `headshot.py` gets a `KINOVA_FAKE_CAM=some.jpg` mode that stands in
for `/snapshot` and reports the framing loop as already converged. Then the whole
path runs on a laptop with no arm and no camera: button, request, render, preview,
accept, queued job, and `paint_sim` dry running the result.

Order, each step testable before the next:

1. done. `portrait.py` vendored and split so `render()` is importable.
   `test_portrait.py` compares it against the old inline pipeline.
2. done. `pad_server.py` capture endpoints. `test_capture.py` walks the whole
   state machine over HTTP against the real server.
3. done. `pad.html` button and preview overlay, and the two demo copies.
4. done. `headshot.py` with `KINOVA_FAKE_CAM`. `test_framing.py` makes the
   framing loop chase a face on a laptop.
5. done in this repo. `armlock.py`, taken by `paint_sim.py` and `headshot.py`.
   `track.py` lives in genwatch and still has to take it.
6. done. `run_queue.py` dispatch, with `test_runqueue.py` driving the real
   runner and the real headshot against a still.

Then at the arm:

7. `teach.py` portrait mode, and teach the pose.
8. Tune the framing loop on real people: gains, tolerances, patience budget.
9. Re-tune `KINOVA_GAIN` and `KINOVA_CHROMA` on wrist camera frames. The current
   defaults were found on a phone photo, and a different sensor under gallery light
   is a different tonal range. `stages.py` sweeps both, so this is one run and a look.

## Things I expect to be wrong

- **Haar will miss people.** Glasses, a bit of head turn, flat overhead light. The
  patience budget covers some of it. If it misses often enough to be annoying the
  answer is a better face detector, not more tuning: OpenCV's DNN face module or
  MediaPipe, both fine on a Pi 5 at 2 Hz.
- **The wrist camera's white balance under gallery light.** Skin coming out blue is
  the failure `portrait.py` already documents, and the fix is `KINOVA_CHROMA` up
  rather than down. Expect to need it.
- **The framing loop will look like hunting** to somebody being pointed at. A slow
  ceiling, a deadband and giving up cleanly matter more than converging perfectly.
- **Three repos, one `~/kinova`.** `pointillism-arm`, `genwatch/kinova` and
  `pointillism-portrait` all deploy into the same directory on the Pi, and the shared
  files have already drifted: `paint_sim.py`, `run_queue.py` and `goto_pose.py` all
  differ between the first two. Vendoring `portrait.py` in makes that worse before it
  gets better. Worth deciding which repo owns the Pi.
