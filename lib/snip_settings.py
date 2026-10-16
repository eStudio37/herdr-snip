"""Paths and user settings shared by the snip scripts.

Settings live in Herdr's config folder for this plugin
(`herdr plugin config-dir estudio37.snip`), in a file named `config` with one
`key = value` per line; `#` starts a comment:

    operator = Ada      # named in each message: "Ada (the operator) boxed part of..."
    sound = on          # on or off
    volume = 1.0        # 0.0 to 1.0, on top of each sound's built-in level

Runtime state (the frozen snapshot, the last result, the sound log, the agent
picked last) goes to $XDG_STATE_HOME/herdr-snip, never into the plugin folder,
which belongs to Herdr once the plugin is installed.
"""
import os
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = "estudio37.snip"
ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {"operator": "", "sound": "on", "volume": "1.0"}


def herdr():
    """The Herdr CLI. Herdr tells plugin processes where it is."""
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def run_herdr(*args, timeout=10):
    """Run a Herdr CLI command; output is decoded as UTF-8 whatever the locale."""
    return subprocess.run([herdr(), *args], capture_output=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    path = Path(os.environ.get("SNIP_STATE_DIR") or Path(base) / "herdr-snip")
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir():
    if os.environ.get("SNIP_CONFIG_DIR"):
        return Path(os.environ["SNIP_CONFIG_DIR"])
    try:
        out = run_herdr("plugin", "config-dir", PLUGIN_ID, timeout=5).stdout.strip()
        if out:
            return Path(out)
    except (OSError, subprocess.SubprocessError):
        pass
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "herdr" / "plugins" / "config" / PLUGIN_ID


def configured():
    """Only what the config file sets, without the defaults."""
    values = {}
    try:
        text = (config_dir() / "config").read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        key, sep, value = line.split("#", 1)[0].partition("=")
        if sep and key.strip() in DEFAULTS:
            values[key.strip()] = value.strip()
    return values


def settings():
    values = dict(DEFAULTS)
    values.update(configured())
    return values


def sound_socket(platform=None):
    """Where the sound player listens and the picker sends. Linux has abstract
    unix sockets, which leave no file behind; macOS doesn't, so there it is a
    socket file in the state folder. A SNIP_SOUND_SOCKET with a slash is a path."""
    name = os.environ.get("SNIP_SOUND_SOCKET", "herdr-snip-sound")
    if "/" in name:
        return name
    if (platform or sys.platform).startswith("linux"):
        return "\0" + name
    return str(state_dir() / (name + ".sock"))


def sound_on(values):
    return values["sound"].strip().lower() not in ("off", "no", "false", "0")


def volume(values):
    try:
        return min(1.0, max(0.0, float(values["volume"])))
    except ValueError:
        return 1.0
