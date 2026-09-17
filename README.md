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
| `setup.html` | Bird's eye plan of the rig. Drag the sheets and pot block onto where they really are, set the sweep the arm may work in, add no-go boxes. Behind a password |
| `display.html` | For a screen on the wall: the planned path, how much is done, and a live marker where the arm is |
| `pointillism-pad.service` | systemd unit, so the Pi serves the pad from boot |
| `pointillism-queue.service` | systemd unit for the runner, so the arm is ready to paint from boot |
| `pad_server.py` | Serves both pages, holds the queue on disk, stores the layout. Standard library only |
| `paint_sim.py` | Walks a queued job through every arm movement, with no paint and no contact |
| `run_queue.py` | Paints the whole queue, oldest first, pausing between jobs for someone to change the paper |
| `goto_pose.py` | Parks the arm at a factory pose, Home nearly always. What the queue tells you to run when it refuses to start |
| `jog_joint.py` | Moves one joint at a time, for recovering from a pose that a whole-pose move should not be trusted with |
| `soft_limits.py` | Shows the arm's kinematic soft limits and sets the ones a painting uses. Will not write while the arm is moving |
| `teach.py` | Records where the paper and pots actually are, by having you push the arm onto the corners. Commands no motion |
| `notify.py` | Beeps a GPIO buzzer when a painting is done, and waits for a mouse click to start the next |
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

When it does refuse, it is nearly always because the arm is not at Home. `goto_pose.py`
parks it:

```sh
KINOVA_TARGET=Home KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/goto_pose.py
```

That interpolates every joint at once, and the swept path is not predictable from the
two endpoints. When the arm is standing somewhere that makes it a bad bet, `jog_joint.py`
moves one joint at a time instead, which is predictable:

```sh
KINOVA_JOINT=3 KINOVA_ANGLE=0 KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/jog_joint.py
```

## Setting it up by teaching

Dragging rectangles on the setup page records where you *believe* the paper is, and
the arm finds out the truth later and at the worst moment. Teaching turns that round:
you push the arm onto the corners and it writes down where they actually were. A
corner read off the arm's own joints is reachable by definition, because reading it
meant having the arm there.

Press the wrist button so the arm goes compliant, then put the brush tip where it
belongs and click. It commands no motion at any point, which is what makes it the safe
way to work close to the machine.

```sh
# three corners of a sheet, nothing saved:
KINOVA_TEACH=display ~/kinova-py310/bin/python ~/kinova/teach.py
# same again, and keep it:
KINOVA_TEACH=display KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/teach.py
# the pot block wants the first and last pot:
KINOVA_TEACH=pots KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/teach.py
```

Right click captures a corner, middle click scraps it and asks again. The corners are
named as they appear on the visitor's pad: top left is row 0 column 0.

It measures more than it can save. `layout.json` holds position and a rotation of 0 or
90, and has no height at all, so paper height and tilt are reported for you rather than
stored, and a sheet more than a degree or so off square is refused instead of being
rounded to square. At a 7 mm dab pitch you want the paper actually straight, and one
degree across a sheet is 5 mm at the far corner.

`notify.py buttons` names each button you press, which is the way to check a mouse
reports all three before you rely on it.

## When a painting stops

A job stops rather than guesses, so a stop is normal and recoverable. The runner
stays up and waits for a click, and the queue keeps the job.

**See what happened.** Everything is in the journal, newest last:

```sh
journalctl -u pointillism-queue -n 50 --no-pager   # what just happened
journalctl -u pointillism-queue -f                 # watch it live
```

The last few lines say which move stopped it and why. Three things turn up:

| What it says | What it means | What to do |
|---|---|---|
| `not parked at Home` | The job before it stopped somewhere | Click. The runner parks it and starts |
| `Arm aborted '<move>'` with a `reason:` | The arm refused that position | Read the reason and the reach it prints |
| `not SERVOING_READY` | Something else holds the arm | See below |

**Something else holds the arm.** Usually nothing does. The arm reports
`MANUALLY_CONTROLLED` for a few seconds after *any* script closes its session, which
is exactly when you are most likely to run the next one, so the scripts here wait it
out rather than believing it. If it persists, then something really does hold the arm:
the Kortex web app, a gamepad in the base, or the wrist button having put it in
admittance mode. Close or unplug whichever it is. The Kortex page lives on the arm's
own wired network, so only the Pi can reach it, never a laptop on wifi.

**Park it by hand**, if you ever need to without the runner:

```sh
KINOVA_TARGET=Home KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/goto_pose.py
```

**Start again.** Nothing else is needed: the job is still queued, so put paper down
and click. If the arm is somewhere a whole-pose move should not be trusted with,
walk it back a joint at a time with `jog_joint.py` first.

### When it will not move at all

Work down this list and stop as soon as something works. Most stops never get past
the first rung.

1. **Click.** The runner offers to park at Home and does it.
2. **Hand guide, then click.** Wrist button for admittance, lift the tool up and back
   away from the cart, release, click. This also fixes the case where the arm is low
   and a large move would sweep through a protection zone.
3. **Park from a terminal.**
   `KINOVA_TARGET=Home KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/goto_pose.py`
4. **Power cycle the arm.**

**The test for rung 4** is a deliberately tiny move. Read the current angle of any
joint, ask for five degrees less, and watch what comes back:

```sh
KINOVA_JOINT=4 KINOVA_ANGLE=<current minus 5> KINOVA_CONFIRM=yes \
    ~/kinova-py310/bin/python ~/kinova/jog_joint.py
```

`ACTION_ABORT` on a five degree move means the arm is refusing *everything*, and no
amount of software will talk it round. On 2026-09-17 this state cost forty minutes
before the power cycle that fixed it in one. Things that look like they should help
and do not:

- `base.ClearFaults()` does nothing for it.
- `DeviceConfigClient.ClearAllSafetyStatus()` does nothing for it either.
- The safety information is not worth reading. `GetAllSafetyInformation` reports every
  entry as `SAFETY_STATUS_ERROR` even while the arm is painting perfectly, so it cannot
  distinguish a healthy arm from a stuck one. It looks like a smoking gun and is not.

What the arm reports in this state is the confusing part: `SERVOING_READY`, servoing
mode normal, control mode `ANGULAR_TRAJECTORY`, zero faults on the base and on every
actuator, and `METHOD_FAILED (1)` on any move it is asked to make.

## Running it

It runs on a Raspberry Pi 5 sitting on the same wired network as the arm. The Pi
is the only thing that talks to the arm, and it serves all three pages.

`pad_server.py` is standard library only, so the system Python runs it. Only
`paint_sim.py` needs the Kinova SDK, and that wants `kortex_api` 2.7.0 with
`protobuf` 3.20.0 exactly. Those are the Kortex 2.x wheels, not 3.x: 3.x dropped
the TCP transport that 2.x arm firmware speaks.

Once the Pi is on your network and can reach the arm:

```sh
git clone https://github.com/sui001/pointillism-arm.git ~/kinova
cd ~/kinova
printf 'your-password-here\n' > setup_password.txt   # gitignored, never committed
chmod 600 setup_password.txt

python3 pad_server.py                    # pad on 127.0.0.1:8010
KINOVA_JOB=latest python3 paint_sim.py   # prints the plan, moves nothing
```

The arm's address defaults to `192.168.1.10` and is set with `KINOVA_IP`.
Credentials come from `/etc/kinova.env` so they stay off the command line.

### Starting on its own

So that anyone can plug the Pi in, switch it on, and have the pad come up:

```sh
sudo cp pointillism-pad.service /etc/systemd/system/
sudo systemctl enable --now pointillism-pad
systemctl status pointillism-pad
```

The queue and the layout are files on the Pi, so both survive a restart.

### Getting to it from a tablet

The server binds to `127.0.0.1` deliberately, so it is not on the local network
until you choose how to publish it. Two ways:

- **Private, for devices you control:** put the Pi on a [Tailscale](https://tailscale.com)
  network and run `sudo tailscale funnel --bg 8010`. That gives an HTTPS address
  that works from any device, and it survives reboots.
- **Local network only:** set `BIND=0.0.0.0` in the service file and reach it at
  the Pi's own address on port 8010.

Either way the setup page stays behind the password. The pad and the display do
not, so put this somewhere you are happy for people to reach.

## Status

Working: the pad, the queue, the setup page, and the sim walking a real arm through
every movement of a real submission.

Not done yet: the gripper is away being repaired, so nothing actually holds a brush.
The heights are placeholders and the arm stays well clear of the desk. The pauses
where gripping would happen are still spent, so the timings mean something. Paper and
pots get registered by a jig taught once by hand, rather than by camera, because a
7 mm dab pitch needs tighter precision than a wrist camera gives.
