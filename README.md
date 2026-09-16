# Pointillism Arm

A drawing pad for gallery visitors, and the arm side that turns what they draw into
paint on paper.

Someone drags a finger across a 30 x 42 grid, which is a 7 mm dab pitch on A4. They
submit, it joins a print queue, and a robot arm paints it twice: one sheet stays on
the wall, one goes home with them.

Part of the Cybernetic Garden, a PhD build of machine creatures that share a space
and notice each other. The arm is a Kinova Gen3 (the 6 degree of freedom variant).

## Try the pad

**[sui001.github.io/pointillism-arm](https://sui001.github.io/pointillism-arm/)**

There is no arm behind that page, so it runs in demo mode: draw, submit, watch the
queue fill up, all of it staying in your own browser. Nothing is sent anywhere.

## Why dabs and not strokes

A continuous line needs steady contact force through the wrist for the whole
stroke, and getting that right is its own project. A dab is one press and one lift,
which a robot repeats reliably. Pointillism is what you get when you accept that
constraint and lean into it, so the constraint became the aesthetic.

Five pigments, no mixing. Colours sit next to each other and the viewer's eye does
the blending.

## What is here

| File | What it does |
|---|---|
| `pad.html` | The visitor's pad: grid, palette, print queue with per job and total paint times |
| `setup.html` | Bird's eye plan of the rig. Drag the sheets and pot block onto where they really are, set the sweep the arm may work in, add no-go boxes |
| `pad_server.py` | Serves both pages, holds the queue on disk, stores the layout. Standard library only |
| `paint_sim.py` | Walks a queued job through every arm movement, with no paint and no contact |
| `kenv.py` | Reads arm credentials from `/etc/kinova.env` so they stay off the command line |

Nothing about the rig is hardcoded. The sheets, the pots, the working sweep and the
no-go boxes all live in `layout.json`, written by the setup page, so the whole thing
can be set up anywhere.

## How the painting works

Each colour has its own brush standing in its own pot, so collecting the brush and
loading it with paint are the same movement. The arm lifts a brush out of its pot,
puts every dab of that colour onto both sheets, returns to the pot to reload as it
goes, stands the brush back in the pot, and moves to the next colour. Both copies
get each dab before it moves on, so the two sheets finish together.

## Safety

The arm is a real machine on a desk, so the sim refuses rather than guesses:

- Every position and every straight line between positions is checked against the
  working sweep, the no-go boxes and the reach band.
- A straight move that would cut inside the base gets a via point added, so the arm
  swings round its own column instead of past it.
- It will not move unless the arm is idle and parked at Home first.
- Without `KINOVA_CONFIRM=yes` it prints the plan and moves nothing.

## Running it against a real arm

Needs a host on the arm's network with `kortex_api` 2.7.0 and `protobuf` 3.20.0
(the Kortex 2.x wheels, not 3.x). Then:

```sh
python3 pad_server.py                 # serves the pad on 127.0.0.1:8010
KINOVA_JOB=latest python3 paint_sim.py   # prints the plan, moves nothing
```

## Status

Working: the pad, the queue, the setup page, and the sim walking a real arm through
every movement of a real submission.

Not done yet: the gripper is away being repaired, so nothing actually holds a brush.
The heights are placeholders and the arm stays well clear of the desk. The pauses
where gripping would happen are still spent, so the timings mean something. Paper and
pots get registered by a jig taught once by hand, rather than by camera, because a
7 mm dab pitch needs tighter precision than a wrist camera gives.
