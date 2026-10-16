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

    def __init__(self, rows=30, cols=100, layout=False, last_target=None, fail=False):
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
        name = "herdr-snip-test-" + uuid.uuid4().hex
        self.listener = None
        if HAS_ABSTRACT_SOCKETS:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.listener.bind("\0" + name)
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
        if HAS_ABSTRACT_SOCKETS:
            heard = p.sounds()
            self.assertEqual(heard[:3], ["hit", "lock", "continue"])
            self.assertIn("type1", heard)
            self.assertIn("select", heard)
            self.assertEqual(heard[-1], "sent")

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
        if HAS_ABSTRACT_SOCKETS:
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
