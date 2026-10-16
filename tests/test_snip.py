"""Tests for herdr-snip. Standard library only:

    python3 -m unittest discover -s tests -v

The end-to-end tests run the real picker in a pseudo-terminal, drive it with the
escape sequences a terminal sends for mouse clicks and keys, and stand in for
Herdr with tests/fake-herdr. Everything they write goes to a temporary folder.
"""
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import wave
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
FAKE_HERDR = ROOT / "tests" / "fake-herdr"
sys.path.insert(0, str(ROOT / "lib"))
import snip_settings as cfg  # noqa: E402

HAS_PTY = os.name == "posix"
HAS_ABSTRACT_SOCKETS = sys.platform.startswith("linux")


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


snip = load("snip", ROOT / "bin" / "snip")
snip_sound = load("snip_sound", ROOT / "bin" / "snip-sound")


def fixture_grid():
    return snip.parse_snapshot((FIXTURES / "screen.ansi").read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def test_truecolor_bold_and_reset(self):
        row = snip.parse_snapshot("\x1b[1;38;2;1;2;3mA\x1b[0mB")[0]
        self.assertEqual(row[0]["st"], (("r", 1, 2, 3), None, frozenset({1})))
        self.assertEqual(row[1]["st"], (None, None, frozenset()))

    def test_256_and_basic_colors(self):
        row = snip.parse_snapshot("\x1b[38;5;196;44mX")[0]
        self.assertEqual(row[0]["st"][:2], (("i", 196), ("b", 44)))

    def test_wide_glyph_takes_two_cells(self):
        row = snip.parse_snapshot("a测b")[0]
        self.assertEqual([c["w"] for c in row], [1, 2, 0, 1])
        self.assertEqual(snip.row_text(row), "a测b")
        # cut through the wide glyph at the left edge: its half becomes a space
        self.assertEqual(snip.row_text(row, 2, 3), " b")

    def test_emoji_presentation_selector_widens_the_glyph(self):
        row = snip.parse_snapshot("✔️!")[0]
        self.assertEqual([c["w"] for c in row], [2, 0, 1])

    def test_tab_expands_to_the_next_stop(self):
        row = snip.parse_snapshot("\tx")[0]
        self.assertEqual(snip.row_text(row).index("x"), 8)

    def test_fixture_screen(self):
        grid = fixture_grid()
        self.assertEqual(len(grid), 30)
        self.assertIn("........F...", snip.row_text(grid[7]))


class WrapTests(unittest.TestCase):
    def test_words_move_whole(self):
        self.assertEqual(snip.wrap("hello world", 5), ["hello", "world"])

    def test_long_word_is_cut(self):
        self.assertEqual(snip.wrap("abcdefgh", 3), ["abc", "def", "gh"])

    def test_trailing_space_puts_the_cursor_on_the_next_line(self):
        self.assertEqual(snip.wrap("abcd ", 4), ["abcd", ""])


class SparkTests(unittest.TestCase):
    def test_every_frame_draws_and_then_stops(self):
        for k in range(snip.SPARK_STEPS):
            cells = snip.spark_cells(k)
            self.assertTrue(cells, "frame %d is empty" % k)
            self.assertTrue(all(0 < bits <= 0xFF for bits, _ in cells.values()))
        self.assertEqual(snip.spark_cells(snip.SPARK_STEPS), {})

    def test_ring_is_not_filled(self):
        self.assertNotIn((0, 0), snip.spark_cells(4))


class SoundTests(unittest.TestCase):
    USED = {"freeze", "move", "hit", "lock", "type1", "type2", "type3", "erase",
            "continue", "back", "select", "sent", "error"}

    def test_every_sound_the_picker_plays_exists(self):
        self.assertTrue(self.USED <= set(snip_sound.sounds()))

    def test_no_sound_clips(self):
        for name, samples in snip_sound.sounds().items():
            gain = snip_sound.VOLUME_BY_SOUND.get(name, snip_sound.VOLUME)
            self.assertLessEqual(max(abs(s) * gain for s in samples), 1.0, name)

    def test_pcm_is_16_bit(self):
        self.assertEqual(len(snip_sound.to_pcm([0.0, 0.5, -1.0])), 6)


STUB_AFPLAY = """#!/usr/bin/env python3
import os, sys, time
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("start %f %s\\n" % (time.time(), os.path.basename(sys.argv[1])))
time.sleep(float(os.environ.get("STUB_SECONDS", "0.05")))
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("end %f %s\\n" % (time.time(), os.path.basename(sys.argv[1])))
"""


class MacPlayerTests(unittest.TestCase):
    """The macOS player (afplay) on any posix system, with a stub that logs each
    call in place of afplay, and a socket file as on macOS."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "state").mkdir()
        (self.tmp / "config").mkdir()
        (self.tmp / "config" / "config").write_text("sound = on\n")
        stub = self.tmp / "afplay"
        stub.write_text(STUB_AFPLAY)
        stub.chmod(0o755)
        self.log = self.tmp / "afplay.log"
        self.sock = str(self.tmp / "sound.sock")
        self.env = dict(os.environ, SNIP_SOUND_BACKEND="afplay", SNIP_AFPLAY=str(stub), STUB_LOG=str(self.log),
                        SNIP_STATE_DIR=str(self.tmp / "state"), SNIP_CONFIG_DIR=str(self.tmp / "config"),
                        SNIP_SOUND_SOCKET=self.sock, SNIP_SOUND_IDLE="3")
        self.addCleanup(self.stop)

    def play(self, name, **env):
        subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "snip-sound"), "play", name],
                       env=dict(self.env, **env), timeout=10, check=True)

    def send(self, name):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(name.encode(), self.sock)
        finally:
            s.close()

    def stop(self):
        try:
            self.send("quit")
        except OSError:
            pass

    def wait_for_player(self, timeout=5):
        end = time.time() + timeout
        while time.time() < end and not os.path.exists(self.sock):
            time.sleep(0.02)
        self.assertTrue(os.path.exists(self.sock), "player never started")

    def calls(self, settle=0.6):
        time.sleep(settle)
        if not self.log.exists():
            return []
        return [line.split() for line in self.log.read_text().splitlines()]

    def started(self, settle=0.6):
        return [c[2] for c in self.calls(settle) if c[0] == "start"]

    def test_each_sound_plays_its_own_wav(self):
        self.play("hit")
        self.wait_for_player()
        for name in ("lock", "sent"):
            time.sleep(0.15)
            self.send(name)
        self.assertEqual(self.started(), ["hit.wav", "lock.wav", "sent.wav"])
        with wave.open(str(self.tmp / "state" / "sounds" / "hit.wav")) as w:
            self.assertEqual((w.getframerate(), w.getsampwidth(), w.getnchannels()), (snip_sound.RATE, 2, 1))

    def test_fast_taps_are_spaced_out(self):
        self.play("freeze")
        self.wait_for_player()
        time.sleep(0.3)
        for i in range(20):                         # 20 taps in one second
            self.send("type%d" % (i % 3 + 1))
            time.sleep(0.05)
        taps = [s for s in self.started() if s.startswith("type")]
        self.assertLessEqual(len(taps), 13)
        self.assertGreaterEqual(len(taps), 5)

    def test_never_more_than_four_at_once(self):
        self.env["STUB_SECONDS"] = "0.8"
        self.play("hit")
        self.wait_for_player()
        for name in ("lock", "sent", "select", "continue", "back", "error", "freeze"):
            self.send(name)
        events = sorted((float(t), kind) for kind, t, _ in self.calls(1.5))
        playing = most = 0
        for _, kind in events:
            playing += 1 if kind == "start" else -1
            most = max(most, playing)
        self.assertEqual(most, 4)

    def test_quits_when_idle_and_removes_its_socket(self):
        self.play("hit", SNIP_SOUND_IDLE="0.6")
        self.wait_for_player()
        time.sleep(1.5)
        self.assertFalse(os.path.exists(self.sock))

    def test_a_stale_socket_file_is_replaced(self):
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        dead.bind(self.sock)
        dead.close()                                # the file stays, nobody listens
        self.play("hit")
        self.assertEqual(self.started(1.0), ["hit.wav"])

    def test_no_player_no_socket(self):
        start = time.time()
        self.play("hit", SNIP_SOUND_BACKEND="none")
        self.assertLess(time.time() - start, 2)
        self.assertEqual(self.started(0.5), [])
        self.assertFalse(os.path.exists(self.sock))


class SoundSocketTests(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("SNIP_SOUND_SOCKET", "SNIP_STATE_DIR", "SNIP_CONFIG_DIR",
                                                      "SSH_CONNECTION")}
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        for k in self.saved:
            os.environ.pop(k, None)
        os.environ["SNIP_STATE_DIR"] = self.tmp
        os.environ["SNIP_CONFIG_DIR"] = self.tmp

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_abstract_on_linux_a_file_on_macos(self):
        self.assertEqual(cfg.sound_socket("linux"), "\0herdr-snip-sound")
        self.assertEqual(cfg.sound_socket("darwin"), str(Path(self.tmp) / "herdr-snip-sound.sock"))

    def test_ssh_keeps_the_mac_quiet_unless_asked(self):
        self.assertEqual(snip_sound.afplay_allowed(), "afplay")
        os.environ["SSH_CONNECTION"] = "10.0.0.2 51000 10.0.0.1 22"
        self.assertIsNone(snip_sound.afplay_allowed())
        Path(self.tmp, "config").write_text("sound = on\n")
        self.assertEqual(snip_sound.afplay_allowed(), "afplay")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = os.environ.get("SNIP_CONFIG_DIR")
        os.environ["SNIP_CONFIG_DIR"] = self.dir

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("SNIP_CONFIG_DIR", None)
        else:
            os.environ["SNIP_CONFIG_DIR"] = self.saved
        shutil.rmtree(self.dir)

    def write(self, text):
        Path(self.dir, "config").write_text(text, encoding="utf-8")

    def test_defaults_without_a_file(self):
        self.assertEqual(cfg.settings(), cfg.DEFAULTS)

    def test_comments_and_unknown_keys(self):
        self.write("# mine\noperator = Ada Lovelace   # me\ncolor = red\nsound=off\n")
        values = cfg.settings()
        self.assertEqual(values["operator"], "Ada Lovelace")
        self.assertNotIn("color", values)
        self.assertFalse(cfg.sound_on(values))

    def test_volume_is_clamped(self):
        self.assertEqual(cfg.volume({"volume": "2"}), 1.0)
        self.assertEqual(cfg.volume({"volume": "-1"}), 0.0)
        self.assertEqual(cfg.volume({"volume": "loud"}), 1.0)


class Picker:
    """The real picker in a pseudo-terminal, fed from the fixtures."""

    def __init__(self, rows=30, cols=100, layout=False, last_target=None, fail=False, listen=True):
        import pty  # posix only
        self.tmp = Path(tempfile.mkdtemp())
        self.state, config = self.tmp / "state", self.tmp / "config"
        self.state.mkdir()
        config.mkdir()
        shutil.copy(FIXTURES / "screen.ansi", self.state / "snapshot.ansi")
        shutil.copy(FIXTURES / "source.json", self.state / "source.json")
        (self.state / "source-pane").write_text("w1:p1\n")
        if layout:
            shutil.copy(FIXTURES / "layout.json", self.state / "source-layout.json")
        if last_target:
            (self.state / "last-target").write_text(last_target + "\n")
        (config / "config").write_text("operator = Ada\nsound = on\n")
        self.log = self.tmp / "herdr.log"
        # an abstract socket on Linux, a socket file elsewhere, as cfg.sound_socket() picks
        name = "herdr-snip-test-" + uuid.uuid4().hex if HAS_ABSTRACT_SOCKETS else str(self.tmp / "sound.sock")
        self.listener = None
        if listen:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.listener.bind("\0" + name if HAS_ABSTRACT_SOCKETS else name)
            self.listener.setblocking(False)
        env = dict(os.environ, HERDR_BIN_PATH=str(FAKE_HERDR), FAKE_HERDR_LOG=str(self.log),
                   SNIP_STATE_DIR=str(self.state), SNIP_CONFIG_DIR=str(config), SNIP_SOUND_SOCKET=name)
        if fail:
            env["FAKE_HERDR_FAIL"] = "1"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            import fcntl
            import termios
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(sys.executable, [sys.executable, "-I", str(ROOT / "bin" / "snip")], env)
        self.pump(0.5)

    def pump(self, seconds):
        """Read the picker's output so it never blocks on a full terminal."""
        import select
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([self.fd], [], [], 0.02)
            if ready:
                try:
                    if not os.read(self.fd, 65536):
                        return False
                except OSError:
                    return False
        return True

    def send(self, data, wait=0.25):
        os.write(self.fd, data.encode())
        self.pump(wait)

    def click(self, col, row):
        """A left click at a terminal cell (1-based, as terminals report it)."""
        self.send("\x1b[<0;%d;%dM\x1b[<0;%d;%dm" % (col, row, col, row))

    def finish(self, timeout=8):
        end = time.time() + timeout
        while time.time() < end and self.pump(0.2):
            pass
        os.waitpid(self.pid, 0)
        return json.loads((self.state / "result.json").read_text(encoding="utf-8"))

    def prompts(self):
        if not self.log.exists():
            return []
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [c[2:] for c in calls if c[:2] == ["agent", "prompt"]]

    def sounds(self):
        heard = []
        while self.listener:
            try:
                heard.append(self.listener.recv(64).decode())
            except BlockingIOError:
                break
        return heard

    def close(self):
        if self.listener:
            self.listener.close()
        shutil.rmtree(self.tmp)


@unittest.skipUnless(HAS_PTY, "needs a pseudo-terminal")
class PickerTests(unittest.TestCase):
    def picker(self, **kw):
        p = Picker(**kw)
        self.addCleanup(p.close)
        return p

    def test_box_question_agent_send(self):
        p = self.picker()
        p.click(3, 13)
        p.click(60, 15)
        p.send("\r")                               # open the question card
        p.send("why does this fail? ação")
        p.send("\r", 0.5)                          # Continue: the agent list
        p.send("\x1b[B")                           # rook -> skips blocked mira -> orchestrator
        p.send("\r")                               # Send
        result = p.finish()

        self.assertEqual(result["outcome"], "sent")
        self.assertEqual(result["box"], {"top": 13, "left": 3, "bottom": 15, "right": 60})
        [(target, message)] = p.prompts()
        self.assertEqual(target, "orchestrator")
        grid = fixture_grid()
        text = "\n".join(snip.row_text(grid[r], 2, 59) for r in range(12, 15))
        self.assertEqual(message, "\n".join([
            "Ada (the operator) boxed part of agent tess's screen and is asking:",
            "why does this fail? ação",
            "",
            "Source agent: tess (claude), pane w1:p1",
            "Session: Fix the quote parser",
            "Directory: /home/dev/parser",
            "Box: rows 13-15, columns 3-60",
            "```",
            text,
            "```",
        ]))
        self.assertEqual((p.state / "last-target").read_text().strip(), "orchestrator")
        heard = p.sounds()
        self.assertEqual(heard[:3], ["hit", "lock", "continue"])
        self.assertIn("type1", heard)
        self.assertIn("select", heard)
        self.assertEqual(heard[-1], "sent")

    def test_runs_the_same_with_no_sound_player(self):
        p = self.picker(listen=False)              # sound on, nobody listening
        start = time.time()
        p.click(3, 13)
        p.click(20, 13)
        p.send("\r")
        p.send("why?")
        p.send("\r", 0.5)
        p.send("\r")
        result = p.finish()
        self.assertEqual(result["outcome"], "sent")
        self.assertLess(time.time() - start, 5)

    def test_last_agent_is_preselected(self):
        p = self.picker(last_target="tess")
        p.click(3, 13)
        p.click(20, 13)
        p.send("\r")
        p.send("\r", 0.5)                          # no question
        p.send("\r")
        p.finish()
        [(target, message)] = p.prompts()
        self.assertEqual(target, "tess")
        self.assertTrue(message.startswith("Ada (the operator) boxed part of your own screen and sent it to you."))

    def test_escape_steps_back_then_closes(self):
        p = self.picker()
        p.click(3, 13)
        p.click(20, 14)
        p.send("\r")                               # question card
        p.send("\x1b")                             # back to the box
        p.send("\x1b")                             # close
        result = p.finish()
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(p.prompts(), [])
        self.assertEqual(p.sounds()[-2:], ["back", "freeze"])

    def test_failed_send_is_reported(self):
        p = self.picker(fail=True)
        p.click(3, 13)
        p.click(20, 13)
        p.send("\r")
        p.send("\r", 0.5)
        p.send("\r")
        result = p.finish()
        self.assertEqual(result["outcome"], "send_failed")
        self.assertFalse((p.state / "last-target").exists())

    def test_frozen_copy_lines_up_with_the_pane(self):
        # the pane is 100 x 30; Herdr's overlay frame leaves 98 x 28 inside
        p = self.picker(rows=28, cols=98, layout=True)
        p.click(10, 10)
        p.send("\x1b")
        result = p.finish()
        self.assertEqual(result["clicks"], [{"row": 11, "col": 11}])

    def test_corners_stay_on_the_pane(self):
        p = self.picker(rows=35, cols=110, layout=True)
        p.click(108, 33)
        p.send("\x1b")
        self.assertEqual(p.finish()["clicks"], [{"row": 30, "col": 100}])


class OpenTests(unittest.TestCase):
    def test_saves_the_pane_and_opens_the_overlay(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "config").mkdir()
        (tmp / "config" / "config").write_text("sound = off\n")
        log = tmp / "herdr.log"
        env = dict(os.environ, HERDR_BIN_PATH=str(FAKE_HERDR), FAKE_HERDR_LOG=str(log),
                   SNIP_STATE_DIR=str(tmp / "state"), SNIP_CONFIG_DIR=str(tmp / "config"))
        subprocess.run([sys.executable, "-I", str(ROOT / "bin" / "snip-open")], env=env, check=True, timeout=20)

        state = tmp / "state"
        self.assertEqual((state / "snapshot.ansi").read_text(encoding="utf-8"),
                         (FIXTURES / "screen.ansi").read_text(encoding="utf-8"))
        self.assertEqual((state / "source-pane").read_text().strip(), "w1:p1")
        self.assertIn('"name": "tess"', (state / "source.json").read_text())
        self.assertTrue((state / "source-layout.json").exists())
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertIn(["plugin", "pane", "open", "--plugin", cfg.PLUGIN_ID, "--entrypoint", "snip",
                       "--placement", "overlay", "--cwd", str(ROOT)], calls)


if __name__ == "__main__":
    unittest.main()
