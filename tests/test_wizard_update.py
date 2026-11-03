"""The Windows wizard's update logic (install_wizard.ps1, 2026-11-03), run under PowerShell 7 with the web fetch replaced by a stub.
The WinForms pages need Windows; the decisions (is it installed, is there a newer version, what changed, what if the server is down) do not.
Skipped when pwsh is not installed."""
import json, shutil, subprocess, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIZ = (ROOT / "install_wizard.ps1").read_text(encoding="utf-8")
PWSH = shutil.which("pwsh")
START = "# Update mode: Echo Bloom is already here"
END = "# Shared install state"


def functions_only():
    s = WIZ[WIZ.index(START):WIZ.index(END)]
    return s.rsplit("# ───", 1)[0] if s.rstrip().endswith("# ─────────────────────────────────────────────────────────────────────────────") else s


@unittest.skipUnless(PWSH, "pwsh not installed")
class WizardUpdate(unittest.TestCase):
    def ps(self, body, app_dir=None):
        script = "$script:APP_DIR = '%s'\n%s\n%s" % (app_dir or tempfile.mkdtemp(), functions_only(), body)
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout.strip()

    def test_version_compare(self):
        out = self.ps("(Compare-EbVersion '1.3.6.4' '1.3.6.3'); (Compare-EbVersion '1.3.6' '1.3.6.0'); (Compare-EbVersion '1.3.6.3' '1.3.10'); (Compare-EbVersion '2.0' '1.9.9.9')").split()
        self.assertEqual(out, ["1", "0", "-1", "1"])

    def test_version_from_text(self):
        out = self.ps("""Get-EbVersionFromText 'x = 1\nVERSION = "1.3.6.4"\n'; (Get-EbVersionFromText 'nothing here') -eq $null""").split()
        self.assertEqual(out, ["1.3.6.4", "True"])

    def test_change_lines_from_the_real_changelog(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        d = Path(tempfile.mkdtemp()); (d / "c.md").write_text(text, encoding="utf-8")
        out = self.ps("Get-EbChangeLines (Get-Content '%s' -Raw) '1.3.6.2'" % (d / "c.md")).splitlines()
        self.assertEqual(len(out), 2, out); self.assertTrue(out[0].startswith("1.3.6.4  ")); self.assertTrue(out[1].startswith("1.3.6.3  "))
        self.assertEqual(self.ps("@(Get-EbChangeLines (Get-Content '%s' -Raw) '9.9.9').Count" % (d / "c.md")), "0")

    def info(self, local, remote, changelog="## 1.0.1\n\n**Fixes the thing.** more words\n\n## 1.0.0\n\n**Start.**\n", fail=False):
        app = Path(tempfile.mkdtemp()); (app / "main.py").write_text("x"); (app / "version.py").write_text('VERSION = "%s"\n' % local)
        fetch = ("throw 'offline'" if fail else
                 "if ($Url -like '*version.py') { 'VERSION = \"%s\"' } else { @'\n%s\n'@ }" % (remote, changelog))
        return json.loads(self.ps("$script:EbFetch = { param([string]$Url) %s }\nGet-EbUpdateInfo | ConvertTo-Json -Compress" % fetch, str(app)))

    def test_newer_lists_changes(self):
        i = self.info("1.0.0", "1.0.1")
        self.assertEqual((i["State"], i["Local"], i["Remote"]), ("newer", "1.0.0", "1.0.1")); self.assertIn("Fixes the thing", " ".join(i["Lines"]) if isinstance(i["Lines"], list) else i["Lines"])

    def test_same_or_older_is_current(self):
        self.assertEqual(self.info("1.0.1", "1.0.1")["State"], "current")
        self.assertEqual(self.info("1.0.2", "1.0.1")["State"], "current")

    def test_server_down_changes_nothing_and_says_so(self):
        i = self.info("1.0.0", "1.0.1", fail=True)
        self.assertEqual(i["State"], "error"); self.assertIn("Nothing was changed", i["Message"])

    def test_garbage_from_server_is_an_error(self):
        app = Path(tempfile.mkdtemp()); (app / "main.py").write_text("x"); (app / "version.py").write_text('VERSION = "1.0.0"\n')
        i = json.loads(self.ps("$script:EbFetch = { param([string]$Url) '<html>nope</html>' }\nGet-EbUpdateInfo | ConvertTo-Json -Compress", str(app)))
        self.assertEqual(i["State"], "error")

    def test_installed_detection(self):
        d = Path(tempfile.mkdtemp())
        self.assertEqual(self.ps("Test-EbInstalled", str(d)), "False")
        (d / "main.py").write_text("x"); self.assertEqual(self.ps("Test-EbInstalled", str(d)), "False")
        (d / "version.py").write_text("x"); self.assertEqual(self.ps("Test-EbInstalled", str(d)), "True")


class WizardWiring(unittest.TestCase):
    """The pages are wired the way the design says (text checks; the real UI needs Windows)."""
    def test_wiring(self):
        for needle in ("$pgU", "Show-Page 6", "Set-UpdatePage (Get-EbUpdateInfo)", "$script:UPDATE_MODE = $true",
                       "if ($script:UPDATE_MODE) { Complete-Install } else { Show-Page 5 }", "-not $env:EB_FRESH -and (Test-EbInstalled)"):
            self.assertIn(needle, WIZ, needle)
        self.assertIn("@($pg1, $pg2, $pg3, $pg4, $pg5, $pgU)", WIZ)
        block = WIZ.split("if ($n -eq 4 -and $script:UPDATE_MODE) {")[1].split("}", 1)[0]
        self.assertIn("'UPDATED'", block); self.assertIn("were not changed", block)        # the done page says UPDATED in update mode
        self.assertNotIn("name your AI", block)
        self.assertNotIn("ollama pull", WIZ.split(START)[1].split(END)[0])     # the update page asks nothing about models


if __name__ == "__main__":
    unittest.main()
