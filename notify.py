#!/usr/bin/env python3
"""Beep when a painting is finished, and wait for a mouse click to start the next.

The Pi 5 has no speaker of its own, so the beep is a piezo or buzzer on a GPIO
pin. With nothing wired up this still runs, it just makes no sound, so the
queue never stalls waiting for hardware that is not there.

Wiring a three pin module (S, middle, -):

    S       -> BCM 18, physical pin 12
    middle  -> 3V3, physical pin 1
    -       -> GND, physical pin 6

It drives the pin with PWM rather than holding it high, because a passive
piezo has no oscillator of its own and only clicks without a frequency to
follow. An active buzzer gates its own oscillator and sounds fine either way,
so PWM covers both and needs no knowledge of which one is plugged in.
Piezos are loudest near resonance, usually 2 to 4 kHz: `notify.py sweep`
plays a range so you can pick the one that carries in the room. Sui picked
3000 Hz by ear on the piece's own piezo, five beeps, which is the default
here. Override with KINOVA_BUZZER_HZ.

The click is any plain USB mouse. It reads /dev/input/event* directly, so there
is nothing to install, and the pi user is already in the input group. It does
not care which event node the mouse lands on and it rescans while waiting, so
unplugging and replugging mid show is fine.

Left, right and middle are all readable. The queue only ever wants left, since
one gesture is the whole interface during a show. Teaching wants right to
capture a point and middle to scrap the last one, which is why
`wait_for_button` takes names rather than assuming.

    python3 notify.py beep      # test the buzzer
    python3 notify.py wait      # blocks until you left click
    python3 notify.py buttons   # names each button you press, to check a mouse

KINOVA_BUZZER_PIN   BCM pin the buzzer sits on, default 18
"""
import glob
import os
import select
import struct
import sys
import time

BUZZER_PIN = int(os.environ.get("KINOVA_BUZZER_PIN", "18"))
BUZZER_HZ = float(os.environ.get("KINOVA_BUZZER_HZ", "3000"))

# struct input_event: two longs of timestamp, then type, code, value
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}
RESCAN_EVERY = 5.0


def beep(times=5, on=0.2, gap=0.15, hz=None):
    """Drive the buzzer pin. True means the pin was driven, NOT that a sound happened.

    Nothing here can tell whether a buzzer is actually wired to it, so do not
    let this claim otherwise. The only test is your ears.
    """
    try:
        from gpiozero import PWMOutputDevice
    except Exception as e:
        # Silence here once cost a whole run: the venv that drives the arm had no
        # gpiozero, so every beep failed without a sound OR a word about it.
        print("  NO BEEP: cannot import gpiozero ({}). Install it into the python "
              "running this, not just the system one.".format(e))
        return False
    try:
        buzzer = PWMOutputDevice(BUZZER_PIN, frequency=hz or BUZZER_HZ, initial_value=0)
    except Exception as e:
        print("  could not open BCM {}: {}".format(BUZZER_PIN, e))
        return False
    try:
        for i in range(times):
            buzzer.value = 0.5      # square drive, the loudest a piezo will go
            time.sleep(on)
            buzzer.value = 0
            if i + 1 < times:
                time.sleep(gap)
    finally:
        buzzer.close()
    return True


def sweep(low=1000, high=4500, step=500, hold=0.6):
    """Play a range of frequencies so you can hear which one carries. Piezos are
    peaky, so the right one can be several times louder than its neighbours."""
    for hz in range(low, high + 1, step):
        print("  {} Hz".format(hz))
        if not beep(times=1, on=hold, hz=hz):
            return False
        time.sleep(0.2)
    return True


def open_inputs():
    handles = {}
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            handles[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
        except OSError:
            pass          # some nodes are not readable, that is fine
    return handles


def drain(handles):
    """Throw away anything already queued on these devices.

    Without this, a click made before the wait began can satisfy it the instant
    it starts, which is not consent: the click has to happen while the machine
    is asking, not at some point beforehand.
    """
    for fd in handles:
        while True:
            try:
                if not os.read(fd, EVENT_SIZE * 64):
                    break
            except OSError:
                break


def wait_for_button(names=("left",), timeout=None):
    """Block until one of these mouse buttons is pressed. Its name, or None on timeout.

    Teaching needs more than one button: one to capture a point and another to
    scrap the last one. They arrive on the same devices, so watching for several
    costs nothing over watching for one.
    """
    wanted = {}
    for name in names:
        if name not in BUTTONS:
            raise ValueError("unknown button '{}', try {}".format(
                name, ", ".join(sorted(BUTTONS))))
        wanted[BUTTONS[name]] = name
    deadline = None if timeout is None else time.time() + timeout
    while deadline is None or time.time() < deadline:
        handles = open_inputs()
        if not handles:
            time.sleep(1.0)
            continue
        drain(handles)
        try:
            until = time.time() + RESCAN_EVERY
            while time.time() < until:
                if deadline is not None and time.time() >= deadline:
                    return None
                ready, _, _ = select.select(list(handles), [], [], 0.5)
                for fd in ready:
                    try:
                        data = os.read(fd, EVENT_SIZE * 64)
                    except OSError:
                        continue
                    for at in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
                        _, _, etype, code, value = struct.unpack(
                            EVENT_FORMAT, data[at:at + EVENT_SIZE])
                        if etype == EV_KEY and code in wanted and value == 1:
                            return wanted[code]
        finally:
            for fd in handles:
                try:
                    os.close(fd)
                except OSError:
                    pass
    return None


def wait_for_click(timeout=None):
    """Block until someone left clicks a mouse. False if the timeout ran out."""
    return wait_for_button(("left",), timeout) is not None


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "beep"
    if what == "beep":
        if beep():
            print("drove BCM {} five times at {:.0f} Hz. Heard nothing? Either nothing is "
                  "wired there yet, or try `notify.py sweep`.".format(BUZZER_PIN, BUZZER_HZ))
        else:
            print("could not drive BCM {} at all.".format(BUZZER_PIN))
    elif what == "sweep":
        args = [int(a) for a in sys.argv[2:5]]
        low, high, step = (args + [1000, 4500, 500][len(args):])[:3]
        print("sweeping BCM {} from {} to {} Hz, listen for the loudest"
              .format(BUZZER_PIN, low, high))
        sweep(low, high, step)
    elif what == "wait":
        print("waiting for a left click, Ctrl-C to give up")
        try:
            print("clicked" if wait_for_click() else "timed out")
        except KeyboardInterrupt:
            print("\ngave up")
    elif what == "buttons":
        print("press any mouse button to see it named, Ctrl-C to stop.")
        print("this is the way to check a mouse reports all three before setup day.")
        try:
            while True:
                print("  {}".format(wait_for_button(tuple(BUTTONS)) or "nothing"))
        except KeyboardInterrupt:
            print("\ndone")
    else:
        sys.exit(__doc__)
