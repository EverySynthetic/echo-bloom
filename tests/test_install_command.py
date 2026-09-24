"""One install command, everywhere a customer can read one.

The 2026-09-24 install rehearsal found three: the install page and README
said `bash <(curl -fsSL https://everysynthetic.org/install.sh)` (plus a fish
variant), the installer's own no-terminal message said a raw.githubusercontent
URL, and the script's header said `curl ... -o install.sh && bash install.sh`.
A stranger who hits a problem and re-reads the instructions shouldn't get a
different answer each time.

version.INSTALL_COMMAND is the one source. Templates and the Help agent read
it; install.sh, README.md and agent_runner.py (copied where version.py isn't)
can't import it, so this test holds them to it word for word, and refuses any other way of fetching install.sh.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Any curl of an Echo Bloom install.sh (not uninstall.sh, not Ollama's or Tailscale's).
FETCH = re.compile(r"curl[^\n`<]*?(?:everysynthetic\.org|echo-bloom)[^\n`<]*?(?<!un)install\.sh[^\n`<]*")

SURFACES = ["install.sh", "README.md", "scripts/agent_runner.py",
            "templates/install.html", "templates/base.html"]


def _command():
    import sys
    sys.path.insert(0, str(ROOT))
    import version
    return version.INSTALL_COMMAND


class OneInstallCommand(unittest.TestCase):

    def test_there_is_one_source(self):
        cmd = _command()
        self.assertTrue(cmd.startswith("curl -fsSL https://raw.githubusercontent.com/"
                                       "EverySynthetic/echo-bloom/main/install.sh"))

    def test_no_surface_carries_a_different_fetch(self):
        cmd = _command()
        for name in SURFACES:
            src = (ROOT / name).read_text(encoding="utf-8")
            for m in FETCH.finditer(src):
                self.assertTrue(cmd.startswith(m.group(0).split(" && ")[0].strip()),
                                f"{name}: a different install command: {m.group(0)!r}")

    def test_installer_and_readme_say_it_word_for_word(self):
        cmd = _command()
        for name in ("install.sh", "README.md", "scripts/agent_runner.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(cmd, src, f"{name} doesn't carry the install command")
        # Twice in install.sh: the header and the no-terminal message.
        self.assertGreaterEqual((ROOT / "install.sh").read_text().count(cmd), 2)

    def test_pages_and_help_read_it_from_the_source(self):
        for name in ("templates/install.html", "templates/base.html"):
            self.assertIn("{{ install_cmd }}", (ROOT / name).read_text(encoding="utf-8"), name)

    def test_the_template_global_is_the_command(self):
        import deps
        self.assertEqual(deps.templates.env.globals["install_cmd"], _command())


if __name__ == "__main__":
    unittest.main()


class SudoIsExplainedFirst(unittest.TestCase):
    """install.sh says what it needs the password for, before preflight (the
    first place it can call sudo), and gives the customer a way out."""

    # Every sudo in install.sh, by what it runs. A new one fails
    # test_every_sudo_is_a_known_one until the notice (and README) say it.
    KNOWN = {"pacman", "apt-get", "dnf", "systemctl start ollama", "loginctl",
             "systemctl enable --now tailscaled", "tailscale up"}
    # What the notice and README must both name.
    NAMED = ("Python 3", "pip", "git", "curl", "Tcl/Tk", "Ollama", "enable-linger", "Tailscale")

    def setUp(self):
        self.src = (ROOT / "install.sh").read_text(encoding="utf-8")

    def _notice(self):
        start = self.src.index("sudo_notice() {")
        return self.src[start:self.src.index("\n}\n", start)]

    def test_the_notice_runs_before_preflight_and_can_stop(self):
        self.assertLess(self.src.index("\nsudo_notice\n"), self.src.index("\npreflight\n"))
        self.assertIn('die "Stopped before anything was installed."', self._notice())

    def test_every_sudo_is_a_known_one(self):
        found = set()
        for line in self.src.splitlines():
            code = line.split("#", 1)[0]
            if "echo" in code or "die " in code or "warn " in code:
                continue
            for m in re.finditer(r"\bsudo\s+(?:timeout\s+\S+\s+)?([^\n|&;]+)", code):
                cmd = m.group(1).strip()
                known = [k for k in self.KNOWN if cmd.startswith(k)]
                self.assertTrue(known, f"a sudo the notice doesn't cover: sudo {cmd}")
                found.update(known)
        self.assertEqual(found, self.KNOWN, "the notice names a sudo the script no longer has")

    def test_notice_and_readme_name_the_same_things(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for thing in self.NAMED:
            self.assertIn(thing, self._notice(), f"install.sh notice doesn't name {thing}")
            self.assertIn(thing, readme, f"README doesn't name {thing}")
