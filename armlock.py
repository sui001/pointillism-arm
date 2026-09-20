#!/usr/bin/env python3
"""One lock, so only one thing ever drives the arm.

There are three scripts here that command motion now: paint_sim.py painting a
job, headshot.py framing a face, and track.py following somebody. Two sessions
at once does not fail cleanly. It presents as the arm refusing every move for
no reason at all, which this repo already has a long section about, and which
cost forty minutes on 2026-09-17 before a power cycle fixed it in one.

So each of them takes this lock first and says who it is. A refusal then names
the holder and its pid, which turns "the arm is being weird" into "paint_sim is
painting job 12, wait or stop it".

It is an flock on a file, which the kernel releases when the process exits,
however it exits. That matters more than it sounds: a lock file that has to be
cleaned up is a lock file that is stale after every Ctrl-C, and then somebody
deletes it by hand out of habit and the lock has stopped meaning anything.

    with armlock.hold("paint_sim job 12"):
        ...                        # the arm is ours

    # or, when the caller wants to report the refusal itself:
    lock = armlock.take("headshot")
    ...
    lock.release()

Windows has no flock and no arm, so there it degrades to no locking and says
so once. That is for running the tests on a laptop, nothing else.
"""
import os
import sys
import time

LOCK_PATH = os.environ.get("KINOVA_LOCK", "/run/kinova.arm.lock")

try:
    import fcntl
except ImportError:                                   # a laptop, not the Pi
    fcntl = None


class Busy(Exception):
    """Somebody else has the arm. `holder` is their line from the lock file."""

    def __init__(self, holder):
        self.holder = holder or "something that did not say who it was"
        super().__init__("the arm is held by {}".format(self.holder))


class Lock(object):
    def __init__(self, fh):
        self.fh = fh

    def release(self):
        if self.fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def holder(path=None):
    """Who has it, for a message. Best effort: the answer is already stale."""
    try:
        with open(path or LOCK_PATH) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def take(name, wait=0.0, path=None):
    """Take the lock for `name`, or raise Busy. Waits up to `wait` seconds.

    A short wait is worth having where one script follows another: the previous
    process may be a few milliseconds from exiting, and failing the whole job
    over that would be silly. A long wait is not, because whoever is standing
    at the machine wants to be told, not left watching a prompt.
    """
    path = path or LOCK_PATH
    if fcntl is None:
        print("NOTE: no flock on this platform, so the arm lock is not held.",
              file=sys.stderr)
        return Lock(None)

    try:
        fh = open(path, "a+")
    except OSError as e:
        # /run needs root on some setups. Refusing to run over a lock we cannot
        # take would be worse than running without it, but say so loudly.
        print("NOTE: cannot open {} ({}), carrying on without the arm lock."
              .format(path, e), file=sys.stderr)
        return Lock(None)

    deadline = time.time() + max(0.0, wait)
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() >= deadline:
                who = holder(path)
                fh.close()
                raise Busy(who)
            time.sleep(0.2)

    fh.seek(0)
    fh.truncate()
    fh.write("{} (pid {}) since {}\n".format(
        name, os.getpid(), time.strftime("%H:%M:%S")))
    fh.flush()
    return Lock(fh)


def hold(name, wait=0.0, path=None):
    """take(), as a context manager. The common case."""
    return take(name, wait, path)


if __name__ == "__main__":
    # `python armlock.py` says who has the arm, which is the question somebody
    # standing in front of a stopped machine is actually asking.
    who = holder()
    print(who if who else "nothing has taken the arm lock")
