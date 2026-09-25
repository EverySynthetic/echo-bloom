"""Fixes from Frosty's install run on Don's A15 (GTX 1660 Ti 6GB, 30GB RAM,
Garuda; Ollama and an old August install already there), 2026-09-24.

install.sh is bash; these tests pull single functions out of it by name and
run them in bash with stubs, so each fix is exercised, not just grepped.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "install.sh").read_text(encoding="utf-8")


def fn(name, src=None):
    """The text of one bash function in install.sh, `name() {` to its `}`."""
    src = src or SRC
    start = src.index(f"\n{name}() {{") + 1
    return src[start:src.index("\n}\n", start) + 3]


def bash(script):
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return r.stdout, r.stderr, r.returncode


MENU_FNS = ["_is_embedding_model", "is_installed", "_add_model", "build_model_menu"]


def menu(installed, vram, ram, src=None):
    """MODEL_IDS for a machine with these models installed."""
    defs = "\n".join(fn(n, src) for n in MENU_FNS if f"\n{n}() {{" in (src or SRC))
    arr = " ".join(f"'{m}'" for m in installed)
    out, err, rc = bash(f"{defs}\nINSTALLED_MODELS=({arr})\n"
                        f"build_model_menu {vram} {ram}\nprintf '%s\\n' \"${{MODEL_IDS[@]}}\"")
    assert rc == 0, err
    return out.split()


class EmbeddingModelsAreNotChatModels(unittest.TestCase):
    """The A15 had nomic-embed-text installed. The menu showed it as item 1,
    the highlighted default; Enter gave a Kin that can't talk, and the
    naming ritual failed."""

    EMBED = ["nomic-embed-text:latest", "nomic-embed-text", "mxbai-embed-large:latest",
             "bge-m3:latest", "bge-large:latest", "all-minilm:latest",
             "snowflake-arctic-embed:latest", "snowflake-arctic-embed2:568m",
             "granite-embedding:278m", "paraphrase-multilingual:latest"]

    def test_an_installed_embedding_model_is_never_a_chat_choice(self):
        ids = menu(self.EMBED + ["phi4-mini:latest"], 6, 30)
        for m in self.EMBED:
            self.assertNotIn(m, ids)
        self.assertEqual(ids[0], "phi4-mini:latest")   # installed chat model still first

    def test_it_stays_known_as_installed(self):
        # is_installed() is how the installer skips re-pulling nomic-embed-text.
        defs = fn("is_installed")
        _, _, rc = bash(f"{defs}\nINSTALLED_MODELS=('nomic-embed-text:latest')\n"
                        "is_installed nomic-embed-text")
        self.assertEqual(rc, 0)


class NamingGetsTheModelToItself(unittest.TestCase):
    """phi4-mini on the A15 failed the ritual: "Read timed out (read
    timeout=60)". A cold load, the first reply, and wander competing for
    Ollama add up past 60s on 6GB, and the re-run's ritual queued behind the
    first run's wander."""

    def _first_timeout(self):
        import importlib
        import sys
        from unittest.mock import patch
        sys.path.insert(0, str(ROOT / "scripts"))
        seen = []

        class R:
            def json(self):
                return {"message": {"content": "hi"}}

        def post(*a, **kw):
            seen.append(kw.get("timeout"))
            return R()
        with patch.object(sys, "argv", ["naming_ritual.py", "--model", "phi4-mini"]):
            sys.modules.pop("naming_ritual", None)
            nr = importlib.import_module("naming_ritual")
        with patch.object(nr.requests, "post", post):
            nr.ask([{"role": "user", "content": "a"}])
            nr.ask([{"role": "user", "content": "b"}])
        return seen

    def test_first_contact_gets_180s(self):
        first, second = self._first_timeout()
        self.assertGreaterEqual(first, 180)
        self.assertGreaterEqual(second, 60)

    def test_the_installer_gives_the_whole_ritual_room(self):
        # The outer _run_timeout used to be 120s: less than first contact alone.
        m = re.search(r"_run_timeout (\d+) python3 \"\$ritual_script\"", SRC)
        self.assertGreaterEqual(int(m.group(1)), 180 + 4 * 120)

    MODEL_USERS = ("echo_bloom_wander", "echo_bloom_reflect.timer", "echo_bloom_bedtime.timer")

    @staticmethod
    def _starts(text, unit):
        """Offsets of systemctl --user start/restart lines naming unit."""
        return [m.start() for m in re.finditer(r"systemctl --user (?:re)?start [^\n]*", text)
                if re.search(rf"(?<![\w.]){re.escape(unit)}(?![\w.])", m.group(0))]

    def test_nothing_that_calls_the_model_starts_before_naming(self):
        deploy = fn("deploy_scripts")
        for unit in self.MODEL_USERS:
            self.assertEqual(self._starts(deploy, unit), [],
                             f"{unit} is started in deploy_scripts, before naming")
        # A re-run stops the old install's wander before the ritual.
        self.assertRegex(deploy, r"systemctl --user stop +echo_bloom_wander")
        naming = SRC.index('\nrun_naming_ritual "$SELECTED_MODEL"')
        for unit in self.MODEL_USERS:
            starts = self._starts(SRC, unit)
            self.assertTrue(starts, f"{unit} is never started")
            self.assertTrue(all(i > naming for i in starts), f"{unit} starts before naming")


class MacOSNamingGetsTheModelToItself(unittest.TestCase):
    """macOS had the same A15 race: deploy_scripts_macos loaded wander
    (RunAtLoad) and reflect (StartInterval fires on load) before naming.
    Mirror Linux — write the plists, stop any old wander, start model users
    only after seed_config."""

    MODEL_PLISTS = ("wander", "reflect", "bedtime")
    OK_NOW = ("vault", "pulse")

    def _run_deploy_macos(self):
        """deploy_scripts_macos with launchctl stubbed; returns recorded calls."""
        import tempfile
        tmp = tempfile.mkdtemp()
        log = f"{tmp}/launchctl.log"
        scripts = f"{tmp}/scripts"
        # Pre-create an "old install" wander plist so unload is meaningful.
        old_agents = f"{tmp}/home/Library/LaunchAgents"
        stubs = rf"""
set +e
HOME="{tmp}/home"
mkdir -p "$HOME" "{scripts}" "{old_agents}"
LAUNCHD_PREFIX="com.everysynthetic.echobloom"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LAUNCHD_LOG_DIR="$HOME/Library/Logs/EchoBloom"
ok() {{ :; }}
warn() {{ :; }}
_real_python() {{ echo /usr/bin/python3; }}
LAUNCHCTL_LOG="{log}"
: > "$LAUNCHCTL_LOG"
launchctl() {{
    # Record every call. load/unload/kickstart are what the installer uses.
    printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
    return 0
}}
"""
        script = (stubs + fn("launchctl_load") + "\n" + fn("deploy_scripts_macos")
                  + f'\ndeploy_scripts_macos "{scripts}"\n')
        out, err, rc = bash(script)
        self.assertEqual(rc, 0, f"stderr={err!r} stdout={out!r}")
        calls = Path(log).read_text(encoding="utf-8").splitlines()
        return calls

    @staticmethod
    def _loads(calls):
        """Plist basenames that were launchctl load'd (starts the agent)."""
        loaded = []
        for c in calls:
            # "load -w /path/to/com.everysynthetic.echobloom.wander.plist"
            if re.match(r"load\b", c):
                m = re.search(r"\.(wander|reflect|bedtime|vault|pulse)\.plist", c)
                if m:
                    loaded.append(m.group(1))
        return loaded

    @staticmethod
    def _unloads(calls):
        unloaded = []
        for c in calls:
            if re.match(r"unload\b", c):
                m = re.search(r"\.(wander|reflect|bedtime|vault|pulse)\.plist", c)
                if m:
                    unloaded.append(m.group(1))
        return unloaded

    def test_deploy_does_not_start_anything_that_calls_the_model(self):
        # On the OLD code this fails: wander and reflect were load'd here.
        calls = self._run_deploy_macos()
        loaded = self._loads(calls)
        for name in self.MODEL_PLISTS:
            self.assertNotIn(name, loaded,
                             f"{name} is launchctl-load'd in deploy_scripts_macos, before naming")
        for name in self.OK_NOW:
            self.assertIn(name, loaded, f"{name} should still start in deploy")

    def test_a_rerun_stops_the_old_wander_before_naming(self):
        calls = self._run_deploy_macos()
        unloaded = self._unloads(calls)
        self.assertIn("wander", unloaded,
                      "re-run must unload the old wander before the ritual")
        self.assertIn("reflect", unloaded,
                      "re-run must unload reflect (StartInterval would fire on a leftover load)")

    def test_model_users_are_started_only_after_naming(self):
        naming = SRC.index('\nrun_naming_ritual "$SELECTED_MODEL"')
        after = SRC[naming:]
        deploy = fn("deploy_scripts_macos")
        # deploy writes plists and loads only vault/pulse.
        self.assertRegex(deploy, r'launchctl_load "\$vault_plist"')
        self.assertRegex(deploy, r'launchctl_load "\$pulse_plist"')
        self.assertNotRegex(deploy, r'launchctl_load "\$wander_plist"')
        self.assertNotRegex(deploy, r'launchctl_load "\$rf_plist"')
        self.assertNotRegex(deploy, r'launchctl_load "\$bt_plist"')
        self.assertNotRegex(deploy, r'launchctl kickstart')
        # After naming: bedtime, reflect, wander get loaded (Linux start mirror).
        self.assertIn('launchctl_load "$LAUNCHD_DIR/${LAUNCHD_PREFIX}.bedtime.plist"', after)
        self.assertIn('launchctl_load "$LAUNCHD_DIR/${LAUNCHD_PREFIX}.reflect.plist"', after)
        wander_starts = list(re.finditer(
            r'launchctl_load "\$wander_plist"|launchctl kickstart[^\n]*wander', after))
        self.assertTrue(wander_starts, "wander is never started on macOS after naming")


class ASkippedTunnelIsNotASilentTunnel(unittest.TestCase):
    """The A15's old August install had left cloudflared.service running,
    putting the dashboard on the internet. Choosing "Skip" for remote access
    said nothing about it."""

    STUBS = r"""
PORT=8090
warn() { echo "! $*"; }
systemctl() {
    case "$*" in
        *list-units*) printf '%s\n' $ACTIVE_UNITS ;;
        *"cat cloudflared.service"*) echo "ExecStart=/usr/bin/cloudflared tunnel --url http://localhost:8090" ;;
        *"cat cloudflared-other.service"*) echo "ExecStart=/usr/bin/cloudflared tunnel --url http://localhost:3000" ;;
        *disable*|*stop*) echo "STOPPED $*" ;;
    esac
}
pgrep() { printf '%s\n' "$PROCS"; }
"""

    def run_skip(self, units="", procs=""):
        script = (self.STUBS + fn("report_existing_tunnel") + fn("setup_remote_access")
                  + f"\nHAS_WHIPTAIL=false; ACTIVE_UNITS='{units}'; PROCS='{procs}'\n"
                  "setup_remote_access <<< 3\n")
        out, err, rc = bash(script)
        self.assertEqual(rc, 0, err)
        return out

    def test_a_leftover_unit_is_named_with_the_command_to_stop_it(self):
        out = self.run_skip(units="cloudflared.service")
        self.assertIn("A Cloudflare tunnel is already putting this dashboard on the internet "
                      "(cloudflared.service).", out)
        self.assertIn("systemctl --user disable --now cloudflared.service", out)
        self.assertNotIn("STOPPED", out)          # told, never stopped silently

    def test_a_tunnel_for_something_else_is_not_reported(self):
        self.assertNotIn("already putting", self.run_skip(units="cloudflared-other.service"))

    def test_a_bare_process_is_reported_too(self):
        out = self.run_skip(procs="4242 cloudflared tunnel --url http://localhost:8090")
        self.assertIn("already putting this dashboard on the internet", out)
        self.assertIn("4242", out)

    def test_nothing_running_says_nothing(self):
        self.assertNotIn("already putting", self.run_skip())


class WhiptailMenusSayArrowKeys(unittest.TestCase):
    """On the A15, typing "3" then Enter in the model menu picked item 1.
    Reproduced in whiptail 0.52 (2026-09-24): 3, Enter -> 1; down, down,
    Enter -> 3. whiptail doesn't jump to a typed number, so every menu says
    to use the arrow keys."""

    def test_every_whiptail_menu_says_use_the_arrow_keys(self):
        for name in ("pick_model_whiptail", "setup_remote_access"):
            body = fn(name)
            self.assertIn("--menu", body, name)
            self.assertIn("arrow keys", body, f"{name}: a whiptail menu that doesn't say arrow keys")


class StaleFailuresAreCleared(unittest.TestCase):
    """The A15's August install left echo_bloom units in a failed state that
    outlived the reinstall. After deploying the new lifecycle scripts, the
    installer clears them."""

    def test_reset_failed_runs_after_the_new_units_load(self):
        deploy = fn("deploy_scripts")
        self.assertRegex(deploy, r"systemctl --user reset-failed +'echo_bloom\*'")
        self.assertLess(deploy.index("daemon-reload"), deploy.index("reset-failed"))
        self.assertLess(deploy.index("reset-failed"), deploy.index("enable echo_bloom_vault"))


class SixGigabyteCardsGetASevenB(unittest.TestCase):
    """The A15 (6GB) was offered 3-4B models only. A 7B at Q4 is ~4.7GB and
    fits; offer one, with its real size on the label."""

    def labels(self, vram, ram):
        defs = "\n".join(fn(n) for n in MENU_FNS)
        out, err, rc = bash(f"{defs}\nINSTALLED_MODELS=()\nbuild_model_menu {vram} {ram}\n"
                            "paste -d'|' <(printf '%s\\n' \"${MODEL_IDS[@]}\") "
                            "<(printf '%s\\n' \"${MODEL_LABELS[@]}\")")
        self.assertEqual(rc, 0, err)
        return dict(line.split("|", 1) for line in out.splitlines())

    def test_a_6gb_card_is_offered_a_7b_that_fits(self):
        menu6 = self.labels(6, 30)
        big = {m: l for m, l in menu6.items() if re.search(r"[:-](7|8)b\b", m)}
        self.assertTrue(big, f"no 7-8B on a 6GB card: {list(menu6)}")
        for m, label in big.items():
            size = float(re.search(r"\[(\d+(?:\.\d+)?) GB\]", label).group(1))
            self.assertLessEqual(size, 5.0, f"{m} is labelled {size}GB, too big for 6GB")

    def test_a_4gb_card_is_not(self):
        self.assertNotIn("qwen2.5:7b", self.labels(4, 8))


if __name__ == "__main__":
    unittest.main()
