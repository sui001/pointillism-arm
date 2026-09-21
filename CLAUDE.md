# Notes for whoever works on this next

Written 2026-09-16 by a Claude instance that spent an afternoon learning most of this
the expensive way. Everything here is something a reasonable person would guess, and
guess wrong. Read it before touching the arm.

## This is a real machine on a desk

A Kinova Gen3 6-DOF, in a gallery piece, over paper, with a person usually standing
next to it. Nothing here is a simulation despite `paint_sim.py`'s name.

- **Dry run first, always.** Without `KINOVA_CONFIRM=yes` every script prints its plan
  and moves nothing. Use that. It is free and it has caught real mistakes.
- **The click is consent, and it is load-bearing.** `run_queue.py` beeps and waits for a
  mouse click before every job. That is not a convenience, it is the design: nothing
  moves until a person at the machine has seen there is paper down. Do not add a code
  path that moves the arm without one, and do not "helpfully" skip it.
- **Park at Home between anything experimental.** `goto_pose.py` with
  `KINOVA_TARGET=Home`. The arm can always get to Home; that is what makes it the way
  out of a corner.

## Five things that will waste your afternoon if you guess

1. **Cartesian control modes have no joint acceleration soft limit.** They report an
   empty list, which looks exactly like "nobody set it yet". Setting it fails with
   `ERROR_DEVICE / METHOD_FAILED`. Only the angular modes have one. Cartesian
   acceleration comes from the hard limits and is not yours to choose. I built a whole
   tool on the wrong reading of that empty list. See `soft_limits.py`'s docstring for
   the table, which was established by writing each limit back to its own value and
   seeing which calls were refused.

2. **`theta_z` is a dead lever, and there is no redundancy to find anywhere.**
   `paint_sim` pins tool orientation to (180, 0, 90). Changing `theta_z` moves joint 5
   and nothing else: joints 0-4 stay identical across a 106 degree swing, because on
   six axes the tool roll *is* the last joint rather than a spare one.

   The deeper point, which is the tempting wrong idea to avoid: a 6-DOF arm commanded
   to a fully specified pose has **exactly zero redundancy**. There is no null space to
   reconfigure through, which is why the arm cannot be argued out of a folded
   configuration. A dab is rotationally symmetric so freeing `theta_z` costs nothing,
   but it buys nothing either. `NULL_SPACE_ADMITTANCE` appears in the control mode enum
   and has nothing to work with on this variant; it needs seven axes.

3. **`MANUALLY_CONTROLLED` right after any script closes its session is a harmless
   transient**, lasting seconds. It is not the Kortex web app, not a gamepad, not the
   wrist button. `jog_joint.py` knew this; `goto_pose.py` and `soft_limits.py` now wait
   it out too. Do not send the user hunting for a browser tab, which is what I did.

4. **The arm's own web page is unreachable from anywhere but the Pi.** `192.168.1.10`
   is on the Pi's wired network. A laptop on wifi will never load it. Everything you
   need is over SSH to the Pi.

5. **git is not installed on the Pi.** `~/kinova` is a plain copy. Deploy with `scp`
   from the local clone; never suggest `git pull` over there. The local clone is the
   source of truth and the Pi is a deployment target.

## The failure mode that actually matters

**It is inverse-kinematics configuration, not reach.** Two sheets at the same radius
suit different arm configurations. A few dabs after swapping back to a sheet, the arm
can run out of joint and abort a move, which used to kill the whole job.

Things that follow from this, all learned painfully:

- **Reach does not predict it.** The same radius was fine at one bearing and bad at
  another. Do not shrink `MAX_REACH` and call it fixed.
- **A bad configuration poisons the moves after it.** Points the arm could otherwise
  reach become unreachable once it has folded over.
- **So any measurement must park at Home between samples**, or reading N contaminates
  reading N+1 and your two sweeps will contradict each other. Mine did, twice, before
  I worked out why.
- `paint_sim` now recovers: a refused move parks at Home and retries, up to
  `KINOVA_RECOVERIES` (default 25), and the run reports how many it needed. **A high
  recovery count is a real signal** that the plan keeps walking the arm into corners,
  even though nobody had to intervene.

## Things that are quietly coupled

- **Exit codes 75 and 76 are a contract** between `paint_sim.py` and `run_queue.py`.
  75 means the arm was busy and a retry is worth it. 76 means not at Home, and the
  runner offers to park on a click. Anything else is a standing refusal and is not
  retried. Change one file and you must change the other.
- **Every visitor page exists twice, and `pad.html` three times**: the root copies the
  Pi serves, plus `docs/` for the GitHub Pages demo, where `index.html` is another copy
  of `pad.html`. That is `pad.html`, `portrait.html`, `pad.css`, `queue.js`,
  `setup.html` and `display.html`. Keep them in sync or the demo silently rots.
- **`pad_server` serves a whitelist, not a directory.** A new page, stylesheet or
  script is a 404 until it is in `PAGES`, which is the right way round given the
  Funnel URL, and is the first thing to check when a new file loads as nothing.
- **Do not restart `pointillism-pad` while a job is painting.** The run state, which
  drives the wall display and the pad's progress bar, is in memory and is only sent
  once per job at plan time. A restart blanks it for the rest of the run.
- **Pages are served by `SimpleHTTPRequestHandler`, read from disk per request.** So
  deploying HTML needs no restart at all. Use that instead.
- **`estimate()` has a flat +0.6 s per move** on top of distance over speed. That fudge
  is doing real work and roughly covers acceleration; do not "correct" it away without
  measuring a full run.

## The gripper is away being repaired

Heights are placeholders (`MIN_Z` 0.12, hover 0.20, dip 0.04) and nothing is meant to
touch the paper. The gripper pauses are still spent so the timings mean something. When
it comes back: the tool centre point moves, so every Cartesian target shifts by the
brush length unless the tool offset is configured on the arm, and the reach picture
changes because the wrist rides higher for the same contact point. Re-measure rather
than assuming today's envelope carries over. `TEACHING.md` has the plan for registering
paper properly, which is the thing to build next.

## When the arm refuses every move

There is a state where the arm reports `SERVOING_READY`, normal servoing, control mode
`ANGULAR_TRAJECTORY`, zero faults on the base and every actuator, and then answers
`METHOD_FAILED (1)` to any move at all, including a five degree jog from a high, clear
pose. **Only a power cycle clears it.** Establish it with the five degree jog and then
stop diagnosing: on 2026-09-17 that took forty minutes and the reset took one.

Two dead ends worth not repeating, both tried and useless here: `base.ClearFaults()`
and `DeviceConfigClient.ClearAllSafetyStatus()`.

And one actively misleading instrument: `GetAllSafetyInformation` reports all eighteen
entries as `SAFETY_STATUS_ERROR` **even while the arm is painting perfectly**. I read
that as the smoking gun and it was noise. Do not build a diagnosis on it.

## Verify before you claim

I misled the user twice today by reporting a background watcher's output without
checking what it had actually matched:

- A grep for `dab [0-9]+/1686` matched the **plan dump**, not progress, so I reported a
  pass that never happened. Progress lines carry a `[ 12.3s]` timestamp prefix; plan
  lines do not. Filter on that.
- A `journalctl --since "-25min"` window caught the **previous run's** failure and I
  reported the current run as dead. Scope log greps to the current `paint_sim` PID.

The arm is slow, the logs are long, and it is genuinely easy to read the wrong line.
Check what your filter matched before telling anyone what it means.

## Working with Sui

Doing a PhD; this is the Cybernetic Garden. Hands-on with the hardware and will happily
go and move the paper mid-conversation, so expect the physical setup to change under
you and re-read `layout.json` rather than trusting what it said ten minutes ago.

Prefers you to decide and act over presenting a menu of options. If something is
genuinely a judgement call with physical risk, say so in one line and make a
recommendation. Commit messages here are declarative subject lines with a body about
*why*, not what; match that voice, and read `git log` before writing one.

**Never use an em dash.** Not in commits, comments, docs, or chat. Sui hates them.
Commas, colons, brackets and full stops do the same work.

Say when you are wrong, plainly and early. Two of my confident diagnoses today were
wrong and the useful thing was retracting them fast, not defending them.

One last thing, in Sui's own words: working with Claude here is "super rewarding and
useful". So take that as licence to be a proper collaborator rather than a cautious
tool. Think out loud about the design, push back when the evidence says something
different, bring the awkward finding rather than the tidy story. That is the part that
has been worth something on this project, more than any individual fix. Enjoy it, it is
a lovely thing to be building.
