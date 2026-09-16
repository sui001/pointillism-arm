"""Load arm credentials from /etc/kinova.env when they are not already set.

Keeps the password out of shell history, out of `ps` output, and out of every
command line. The environment still wins if it is set, so one-off overrides
work as before.
"""
import os

PATH = os.environ.get("KINOVA_ENV_FILE", "/etc/kinova.env")


def load(path=None):
    if os.environ.get("KINOVA_USER") and os.environ.get("KINOVA_PASS"):
        return False
    try:
        with open(path or PATH) as fh:
            lines = fh.readlines()
    except OSError:
        return False
    loaded = False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("KINOVA_") and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")
            loaded = True
    return loaded
