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


if __name__ == "__main__":
    unittest.main()
