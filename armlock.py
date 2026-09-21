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

The file lives in /run/user/<uid>, not /run: everything here runs as pi and
/run is root owned, so /run would have failed to open, said so in a NOTE nobody
reads, and carried on without locking. KINOVA_LOCK overrides it, and every
process that takes it must agree on the path or it is not a lock at all.

Windows has no flock and no arm, so there it degrades to no locking and says
so once. That is for running the tests on a laptop, nothing else.
"""
import os
import sys
import tempfile
import time


def _default_path():
    """Somewhere every one of these processes can write, and the same one.

    Not `/run`, which is the obvious answer and is wrong: it is root owned, and
    everything here runs as pi, services included. The lock would have failed to
    open, printed a NOTE nobody reads, and carried on without locking, which is
    the exact failure it exists to prevent. Found by trying it on the Pi rather
    than by thinking about it.

    `/run/user/<uid>` is the per-user runtime directory, writable, and cleared
    on reboot, which is what a lock file wants. Every service here is User=pi
    and so is anybody at a terminal, so they all land on one path. Falling back
    to /tmp keeps it working on a machine that has no such directory.
    """
    runtime = "/run/user/{}".format(os.getuid()) if hasattr(os, "getuid") else ""
    if runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
        return os.path.join(runtime, "kinova.arm.lock")
    return os.path.join(tempfile.gettempdir(), "kinova.arm.lock")


LOCK_PATH = os.environ.get("KINOVA_LOCK") or _default_path()

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
        # Refusing to run over a lock we cannot take would be worse than
        # running without it, but say so loudly: an unlocked arm is the
        # condition this file exists to make impossible.
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
