"""Update mode of install.sh (2026-11-03): if Echo Bloom is already installed, the script shows what changed, asks once, updates the files, and
asks nothing else. Everything runs in a throwaway HOME with stub systemctl/ollama/sudo/pip3 first on PATH, so no real service, model or package is touched."""
import os, pty, shutil, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent / "install.sh"


def sh(*a, cwd=None, env=None):
    return subprocess.run(a, cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


class Box:
    """origin repo + a 'user' HOME that has Echo Bloom installed from it."""
    def __init__(self, existing=True, shallow=False):
        self.root = Path(tempfile.mkdtemp(prefix="eb_update_test_"))
        self.home, self.stubs, self.origin = self.root / "home", self.root / "stubs", self.root / "origin.git"
        self.log = self.root / "calls.log"; self.log.write_text("")
        self.home.mkdir(); self.stubs.mkdir()
        work = self.root / "seed"; work.mkdir()
        g = lambda *a, cwd=work: sh("git", "-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=cwd)
        g("init", "-q", "-b", "main", ".")
        self.write(work, "main.py", "app = 1\n"); self.write(work, "version.py", 'VERSION = "1.0.0"\n')
        self.write(work, "requirements.txt", "requests>=2\n"); self.write(work, "scripts/hello.py", "print('v1')\n")
        self.write(work, "kin_presence.py", "x = 1\n"); self.write(work, "license.py", "y = 1\n"); self.write(work, "verify_deploy.py", "import sys\nsys.exit(0)\n")
        g("add", "-A"); g("commit", "-q", "-m", "first release")
        sh("git", "clone", "-q", "--bare", str(work), str(self.origin))
        self.work = work
        if shallow:   # how install.sh really installs the app: git clone --depth 1
            sh("git", "clone", "-q", "--depth", "1", "file://" + str(self.origin), str(self.home / "echo_bloom"))
        else:
            sh("git", "clone", "-q", str(self.origin), str(self.home / "echo_bloom"))
        if existing:
            (self.home / ".config/kin_app").mkdir(parents=True); (self.home / ".config/kin_app/config.json").write_text("{}")
        self._stubs()
        self.script = self.home / "echo-bloom-install.sh"           # where a real user has it (outside the app folder)
        shutil.copy(INSTALL, self.script)

    def write(self, root, rel, text):
        p = Path(root) / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)

    def _stubs(self):
        def stub(name, body):
            p = self.stubs / name; p.write_text("#!/bin/bash\n" + body); p.chmod(0o755)
        log = str(self.log)
        stub("systemctl", f'echo "systemctl $*" >> {log}\ncase "$*" in *"is-active"*) for u in ${{EB_ACTIVE//,/ }}; do case "$*" in *" $u") exit 0;; esac; done; exit 3;; esac\nexit 0\n')
        stub("loginctl", f'echo "loginctl $*" >> {log}\nexit 0\n')
        stub("ollama", f'echo "OLLAMA CALLED $*" >> {log}\nexit 1\n')
        stub("sudo", f'echo "SUDO CALLED $*" >> {log}\nexit 1\n')
        stub("pip3", f'echo "pip3 $*" >> {log}\nexit 0\n')
        stub("whiptail", f'echo "WHIPTAIL CALLED" >> {log}\nexit 1\n')

    def release(self, version="1.1.0", script="print('v2')\n", requirements=None, subject="Better thing"):
        g = lambda *a: sh("git", "-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=self.work)
        self.write(self.work, "version.py", f'VERSION = "{version}"\n'); self.write(self.work, "scripts/hello.py", script)
        if requirements: self.write(self.work, "requirements.txt", requirements)
        g("add", "-A"); g("commit", "-q", "-m", subject); g("push", "-q", str(self.origin), "main")

    def env(self, active=""):
        e = dict(os.environ, HOME=str(self.home), PATH=f"{self.stubs}:{os.environ['PATH']}", EB_ACTIVE=active,
                 EB_INSTALL_LOG=str(self.root / "install.log"), TERM="dumb")
        e.pop("XDG_CONFIG_HOME", None); e.pop("XDG_DATA_HOME", None)
        return e

    def run(self, *args, active="", answer=None, timeout=60):
        """answer=None: no terminal (flags only). answer='y'/'n': a real pty, typed answer."""
        cmd = ["bash", str(self.script), *args]
        if answer is None:
            r = subprocess.run(cmd, env=self.env(active), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
            return r.returncode, r.stdout + r.stderr
        master, slave = pty.openpty()
        p = subprocess.Popen(cmd, env=self.env(active), stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        os.close(slave); time.sleep(1.5); os.write(master, (answer + "\n").encode())
        out, _ = p.communicate(timeout=timeout); os.close(master)
        return p.returncode, out

    def head(self): return sh("git", "rev-parse", "HEAD", cwd=self.home / "echo_bloom").strip()
    def calls(self): return self.log.read_text()
    def cleanup(self): shutil.rmtree(self.root, ignore_errors=True)


class UpdateMode(unittest.TestCase):
    def setUp(self): self.b = Box(); self.addCleanup(self.b.cleanup)

    def test_which_mode(self):
        self.assertEqual(self.b.run("--which-mode")[1].strip().splitlines()[-1], "update")
        fresh = Box(existing=False); self.addCleanup(fresh.cleanup)
        self.assertEqual(fresh.run("--which-mode")[1].strip().splitlines()[-1], "install")
        self.assertEqual(self.b.run("--which-mode", "--install")[1].strip().splitlines()[-1], "install")

    def test_up_to_date_changes_nothing(self):
        before = self.b.head(); rc, out = self.b.run("--yes", active="echo_bloom")
        self.assertEqual(rc, 0); self.assertIn("up to date", out); self.assertEqual(self.b.head(), before)
        self.assertNotIn("restart", self.b.calls())

    def test_check_lists_changes_and_changes_nothing(self):
        self.b.release(subject="Fix the thing people hit"); before = self.b.head()
        rc, out = self.b.run("--check")
        self.assertEqual(rc, 0); self.assertIn("Fix the thing people hit", out); self.assertIn("1.0.0 to 1.1.0", out)
        self.assertEqual(self.b.head(), before); self.assertFalse((self.b.home / ".local/share/echo_bloom/scripts/hello.py").exists())

    def test_yes_updates_files_and_asks_nothing_else(self):
        self.b.release(); before = self.b.head()
        rc, out = self.b.run("--yes", active="echo_bloom,echo_bloom_vault,echo_bloom_wander")
        self.assertEqual(rc, 0, out); self.assertNotEqual(self.b.head(), before)
        self.assertEqual((self.b.home / ".local/share/echo_bloom/scripts/hello.py").read_text(), "print('v2')\n")
        self.assertTrue((self.b.home / ".local/share/echo_bloom/scripts/kin_presence.py").exists())
        for word in ("model", "Model", "name your", "call your AI", "password", "Tailscale", "Continue?"):
            self.assertNotIn(word, out, word)
        calls = self.b.calls()
        for bad in ("OLLAMA CALLED", "SUDO CALLED", "WHIPTAIL CALLED", "loginctl", "enable-linger"): self.assertNotIn(bad, calls)
        self.assertIn("restart echo_bloom", calls); self.assertIn("restart echo_bloom_vault", calls)
        self.assertNotIn("echo_bloom_wander", calls)            # the Kin's own process is never even touched
        self.assertNotIn("restart echo_bloom_pulse", calls)   # asked, found it not running, left it alone
        self.assertNotIn(" stop ", calls); self.assertNotIn(" start ", calls)
        self.assertTrue((self.b.home / ".local/share/echo_bloom/last_update_from.txt").exists())

    def test_asks_once_and_a_no_changes_nothing(self):
        self.b.release(); before = self.b.head()
        rc, out = self.b.run(answer="n", active="echo_bloom")
        self.assertEqual(rc, 0, out); self.assertEqual(out.count("Update now?"), 1); self.assertEqual(self.b.head(), before)
        self.assertNotIn("restart", self.b.calls())

    def test_asks_once_and_a_yes_updates(self):
        self.b.release(); before = self.b.head()
        rc, out = self.b.run(answer="y", active="echo_bloom")
        self.assertEqual(rc, 0, out); self.assertEqual(out.count("Update now?"), 1); self.assertNotEqual(self.b.head(), before)
        self.assertNotIn("Model", out)

    def test_own_changes_are_never_overwritten(self):
        self.b.release(); (self.b.home / "echo_bloom/main.py").write_text("my edit\n")
        before = self.b.head(); rc, out = self.b.run("--yes")
        self.assertEqual(rc, 0); self.assertEqual(self.b.head(), before); self.assertIn("were edited on this computer", out)
        self.assertEqual((self.b.home / "echo_bloom/main.py").read_text(), "my edit\n")

    def test_libraries_reinstalled_only_when_requirements_changed(self):
        self.b.release(); self.b.run("--yes")
        self.assertNotIn("pip3", self.b.calls())
        self.b.release(version="1.2.0", requirements="requests>=2\nrich>=13\n", subject="adds a library"); self.b.run("--yes")
        self.assertIn("pip3", self.b.calls())

    def test_unknown_update_source_is_not_called_up_to_date(self):
        app = self.b.home / "echo_bloom"
        sh("git", "remote", "rename", "origin", "cloud", cwd=app); sh("git", "branch", "--unset-upstream", cwd=app)
        before = self.b.head(); rc, out = self.b.run("--yes")
        self.assertNotEqual(rc, 0); self.assertIn("where updates come from", out); self.assertNotIn("up to date", out); self.assertEqual(self.b.head(), before)

    def test_server_unreachable_changes_nothing(self):
        shutil.rmtree(self.b.origin); before = self.b.head()
        rc, out = self.b.run("--yes")
        self.assertNotEqual(rc, 0); self.assertIn("Nothing was changed", out); self.assertEqual(self.b.head(), before)


    def test_leftover_files_do_not_block_the_update(self):
        # Themess's live copy (11-03) had eli_space/, bong_space/ and old .bak files beside the code.
        app = self.b.home / "echo_bloom"
        self.b.write(app, "eli_space/avatar/eli.png", "png"); self.b.write(app, "install.sh.pre-1.3.5.1", "old")
        self.b.release(); before = self.b.head()
        rc, out = self.b.run("--yes")
        self.assertEqual(rc, 0, out); self.assertNotEqual(self.b.head(), before)
        self.assertEqual((app / "eli_space/avatar/eli.png").read_text(), "png")      # left exactly where it was

    def test_scripts_from_before_are_kept(self):
        self.b.release(version="1.1.0", script="print('v2')\n"); self.b.run("--yes")
        mid = self.b.head()
        self.b.release(version="1.2.0", script="print('v3')\n", subject="third"); rc, out = self.b.run("--yes")
        self.assertEqual(rc, 0, out)
        scripts = self.b.home / ".local/share/echo_bloom/scripts"
        bak = Path(str(scripts) + ".bak-" + mid[:12])
        self.assertEqual((scripts / "hello.py").read_text(), "print('v3')\n")
        self.assertEqual((bak / "hello.py").read_text(), "print('v2')\n"); self.assertIn(str(bak), out)


class UpdateModeShallow(UpdateMode):
    """The same tests on a --depth 1 clone, which is what a real install has."""
    def setUp(self): self.b = Box(shallow=True); self.addCleanup(self.b.cleanup)


if __name__ == "__main__":
    unittest.main()
