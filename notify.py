#!/usr/bin/env python3
"""Beep when a painting is finished, and wait for a mouse click to start the next.

The Pi 5 has no speaker of its own, so the beep is an active buzzer on a GPIO
pin: buzzer positive to the pin, the other leg to any ground. With nothing
wired up this still runs, it just makes no sound, so the queue never stalls
waiting for hardware that is not there.

The click is any plain USB mouse. It reads /dev/input/event* directly, so there
is nothing to install, and the pi user is already in the input group. It does
not care which event node the mouse lands on and it rescans while waiting, so
unplugging and replugging mid show is fine.

    python3 notify.py beep      # test the buzzer
    python3 notify.py wait      # blocks until you left click

KINOVA_BUZZER_PIN   BCM pin the buzzer sits on, default 18
"""
import glob
import os
import select
import struct
import sys
import time

BUZZER_PIN = int(os.environ.get("KINOVA_BUZZER_PIN", "18"))

# struct input_event: two longs of timestamp, then type, code, value
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01
BTN_LEFT = 0x110
RESCAN_EVERY = 5.0


def beep(times=2, on=0.18, gap=0.12):
    """Drive the buzzer pin. True means the pin was driven, NOT that a sound happened.

    Nothing here can tell whether a buzzer is actually wired to it, so do not
    let this claim otherwise. The only test is your ears.
    """
    try:
        from gpiozero import Buzzer
    except Exception:
        return False
    try:
        buzzer = Buzzer(BUZZER_PIN)
    except Exception as e:
        print("  no buzzer on BCM {}: {}".format(BUZZER_PIN, e))
        return False
    try:
        for i in range(times):
            buzzer.on()
            time.sleep(on)
            buzzer.off()
            if i + 1 < times:
                time.sleep(gap)
    finally:
        buzzer.close()
    return True


def open_inputs():
    handles = {}
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            handles[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
        except OSError:
            pass          # some nodes are not readable, that is fine
    return handles


def wait_for_click(timeout=None):
    """Block until someone left clicks a mouse. False if the timeout ran out."""
    deadline = None if timeout is None else time.time() + timeout
    while deadline is None or time.time() < deadline:
        handles = open_inputs()
        if not handles:
            time.sleep(1.0)
            continue
        try:
            until = time.time() + RESCAN_EVERY
            while time.time() < until:
                if deadline is not None and time.time() >= deadline:
                    return False
                ready, _, _ = select.select(list(handles), [], [], 0.5)
                for fd in ready:
                    try:
                        data = os.read(fd, EVENT_SIZE * 64)
                    except OSError:
                        continue
                    for at in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                        _, _, etype, code, value = struct.unpack(
                            EVENT_FORMAT, data[at:at + EVENT_SIZE])
                        if etype == EV_KEY and code == BTN_LEFT and value == 1:
                            return True
        finally:
            for fd in handles:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return False


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "beep"
    if what == "beep":
        if beep():
            print("drove BCM {} twice. Heard nothing? Then nothing is wired there yet."
                  .format(BUZZER_PIN))
        else:
            print("could not drive BCM {} at all.".format(BUZZER_PIN))
    elif what == "wait":
        print("waiting for a left click, Ctrl-C to give up")
        try:
            print("clicked" if wait_for_click() else "timed out")
        except KeyboardInterrupt:
            print("\ngave up")
    else:
        sys.exit(__doc__)
