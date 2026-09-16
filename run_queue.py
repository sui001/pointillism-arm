#!/usr/bin/env python3
"""Paint the whole queue, oldest first, with a person in the loop between jobs.

For each job: run paint_sim.py for it, mark it done so it leaves the queue,
beep, and wait for a left click before starting the next. That pause is when
someone takes the two finished sheets off and puts blank paper down.

Nothing here drives the arm. It runs paint_sim.py, which owns every movement
and every refusal, so there is exactly one place the arm is commanded from.

Run it with the same python that can import kortex_api, since that is what it
hands to paint_sim:

    # plan every queued job, move nothing:
    ~/kinova-py310/bin/python ~/kinova/run_queue.py
    # paint them:
    KINOVA_CONFIRM=yes ~/kinova-py310/bin/python ~/kinova/run_queue.py

KINOVA_ATTEMPTS   tries per job before giving up, default 3. The arm reports
                  itself busy for a few seconds after any session closes, so
                  the first try often bounces.
KINOVA_PAD_URL    default http://127.0.0.1:8010
Everything paint_sim.py reads (KINOVA_MAX_DABS, KINOVA_SPEED and the rest) is
passed straight through.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.stdout.reconfigure(line_buffering=True)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import notify

PAD = os.environ.get("KINOVA_PAD_URL", "http://127.0.0.1:8010")
ATTEMPTS = int(os.environ.get("KINOVA_ATTEMPTS", "3"))
ARMED = os.environ.get("KINOVA_CONFIRM") == "yes"
SIM = os.path.join(HERE, "paint_sim.py")
RETRY_PAUSE = 12
IDLE_POLL = 5
BUSY_EXIT = 75          # paint_sim's "the arm is busy", and it must match there
NOT_HOME_EXIT = 76      # paint_sim's "park me first", likewise
PARK = os.path.join(HERE, "goto_pose.py")


def studio_password():
    value = os.environ.get("SETUP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open(os.path.join(HERE, "setup_password.txt")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def fetch(path):
    with urllib.request.urlopen(PAD + path, timeout=5) as r:
        return json.load(r)


def mark_done(job_id):
    password = studio_password()
    if not password:
        print("  no studio password on this Pi, so the job stays in the queue")
        return False
    token = base64.b64encode(("studio:" + password).encode()).decode()
    request = urllib.request.Request(
        "{}/api/jobs/{}/done".format(PAD, job_id), data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token})
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return json.load(r)
    except Exception as e:
        print("  could not mark job {} done: {}".format(job_id, e))
        return False


def paint(job_id):
    """Hand one job to paint_sim and give back its exit code.

    A busy arm is the one failure worth another go. Everything else stands until
    something changes, so it comes straight back rather than being tried twice
    more in silence: from the room that looked exactly like a dead mouse, when
    the clicks had been read correctly all along.
    """
    env = os.environ.copy()
    env["KINOVA_JOB"] = str(job_id)
    code = 1
    for attempt in range(1, ATTEMPTS + 1):
        if attempt > 1:
            print("\n  attempt {} of {}".format(attempt, ATTEMPTS))
        code = subprocess.run([sys.executable, SIM], env=env).returncode
        if code != BUSY_EXIT:
            return code
        if attempt < ATTEMPTS:
            print("  the arm was still busy, another go in {}s".format(RETRY_PAUSE))
            time.sleep(RETRY_PAUSE)
    return code


def park():
    """Send the arm to Home, the way the failure message used to tell you to."""
    env = os.environ.copy()
    env["KINOVA_TARGET"] = "Home"
    env["KINOVA_CONFIRM"] = "yes"
    return subprocess.run([sys.executable, PARK], env=env).returncode == 0


def main():
    if not ARMED:
        jobs = fetch("/api/jobs")["jobs"]
        if not jobs:
            print("The queue is empty.")
            return
        print("DRY RUN over {} job(s). Nothing moves and nothing leaves the queue.\n".format(len(jobs)))
        for job in jobs:
            print("=" * 68)
            print("job #{}, {} dabs".format(job["id"], job["dab_count"]))
            print("=" * 68)
            paint(job["id"])
        print("\nDry run over. Re-run with KINOVA_CONFIRM=yes to actually paint.")
        return

    print("Painting the queue. Ctrl-C to stop between jobs.\n")
    painted = 0
    while True:
        try:
            jobs = fetch("/api/jobs")["jobs"]
        except Exception as e:
            print("cannot reach the pad ({}), waiting".format(e))
            time.sleep(IDLE_POLL)
            continue

        if not jobs:
            if painted:
                print("\nQueue empty after {} painting(s). Waiting for the next submission.".format(painted))
                painted = 0
            time.sleep(IDLE_POLL)
            continue

        job = jobs[0]
        print("=" * 68)
        print("job #{}, {} dabs, {} left in the queue".format(
            job["id"], job["dab_count"], len(jobs)))
        print("=" * 68)
        print("Put blank paper down, then left click the mouse to start.")
        notify.beep()
        notify.wait_for_click()
        print("Off we go.\n")

        code = paint(job["id"])

        if code == NOT_HOME_EXIT:
            # Much the commonest way a session goes wrong: a job before this one
            # stopped somewhere, so this one will not start, and the fix was a
            # command typed into a terminal nobody has open during a show.
            # Parking is a large move, so it waits for the same click every
            # other movement here waits for.
            print("\nThe arm is not parked at Home, so job #{} cannot start."
                  .format(job["id"]))
            print("Stand clear, then click to park it at Home and begin.")
            notify.beep()
            notify.wait_for_click()
            if park():
                print("Parked. Off we go.\n")
                code = paint(job["id"])
            else:
                print("\nParking did not finish. Something else may be holding the")
                print("arm: the Kortex web app open in a browser, a gamepad, or")
                print("admittance mode from the wrist button.")

        if code != 0:
            # Stay put rather than exit. As a service, exiting just means a
            # restart loop; waiting means a person can fix the arm and click.
            print("\nJob #{} did not finish. It stays in the queue.".format(job["id"]))
            print("The reason is above. Click to try it again.")
            notify.beep()
            notify.wait_for_click()
            continue

        mark_done(job["id"])
        painted += 1
        print("\nJob #{} painted. Take both sheets off.\n".format(job["id"]))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
