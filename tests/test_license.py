"""The money path — every bypass this module already closed, frozen so the
next edit cannot quietly reopen it.

license.py is the most-patched file in the app and had zero tests. Its scar
comments name real bypasses that were fixed: a hand-written OFFLINE line that
bought three thousand years of trial; an offline grace that renewed itself
forever; an unverifiable permanent key that fell through to the trial path and
then read as "expired"; a fake license server (ECHO_BLOOM_LICENSE_SERVER points
anywhere) handing back any token it liked; a trial reset with `timedatectl
set-time`. Each of those is one careless edit from coming back, and nothing
would catch it.

So every test here disables a guard in spirit and demands the failure. No
pytest, no network, isolated HOME per test — this must run on a customer's box.
"""
import base64
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import license as lic  # noqa: E402


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


class LicenseTestBase(unittest.TestCase):
    """Each test gets its own HOME so nothing leaks between them and nothing
    touches the real ~/.config/kin_app."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="eb_lic_"))
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)          # _stamp_paths reads Path.home()
        cfg = self.home / ".config/kin_app"
        cfg.mkdir(parents=True, exist_ok=True)
        # Redirect every import-time path constant at the fresh home.
        lic.LICENSE_PATH      = cfg / "license"
        lic.REVOKED_KEY_PATH  = cfg / "revoked_key"
        lic.TRIAL_TOKEN_PATH  = cfg / "trial_token"
        lic.FINGERPRINT_PATH  = cfg / "machine_id"
        lic._FIRST_SEEN_PATH  = cfg / "first_run"
        # Deterministic, isolated fingerprint (no /etc/machine-id dependency).
        lic.FINGERPRINT_PATH.write_text("testfingerprint0000")
        # Reset module caches and the real crypto flag.
        lic.invalidate_status_cache()
        lic._last_upgrade_attempt = 0.0
        self._crypto_ok = lic._CRYPTO_OK

    def tearDown(self):
        lic._CRYPTO_OK = self._crypto_ok
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        else:
            os.environ.pop("HOME", None)


class OfflineGraceIsCappedAndAuthenticated(LicenseTestBase):

    def test_a_hand_forged_offline_token_is_rejected(self):
        """`echo OFFLINE:99999999999:fp:whatever > trial_token` must not work.
        The MAC binds the token to this machine's fingerprint."""
        fp = lic.get_fingerprint()
        lic.TRIAL_TOKEN_PATH.write_text(f"OFFLINE:99999999999:{fp}:{'0'*32}")
        self.assertIsNone(lic._read_trial_token())
        # and the poisoned token is removed, not left to be retried
        self.assertFalse(lic.TRIAL_TOKEN_PATH.exists())

    def test_a_valid_mac_token_is_still_capped_at_three_days(self):
        """Even a token whose MAC checks out cannot outlive the grace window
        measured from first run — the min() cap is the wall."""
        fp = lic.get_fingerprint()
        lic._first_seen()  # stamp first run = now
        forever = lic._now() + 3000 * 365 * 86400
        lic.TRIAL_TOKEN_PATH.write_text(lic._offline_token(forever, fp))
        st = lic._read_trial_token()
        self.assertEqual(st["state"], "trial")
        self.assertLessEqual(st["days_left"], lic._OFFLINE_GRACE_DAYS)

    def test_grace_is_measured_from_first_seen_not_from_the_token(self):
        """Rewriting the token later cannot buy more grace: first_seen is the
        origin, and it was stamped in the past."""
        fp = lic.get_fingerprint()
        past = lic._now() - 10 * 86400          # first run was ten days ago
        lic._write_stamp("first_run", past)
        lic.TRIAL_TOKEN_PATH.write_text(lic._offline_token(lic._now() + 999999999, fp))
        st = lic._read_trial_token()
        # first_seen + 3d is long gone, so this reads expired, not a fresh grace
        self.assertEqual(st["state"], "expired")


class PermanentKeyGate(LicenseTestBase):

    def test_a_tampered_key_does_not_verify(self):
        """A payload the attacker wrote with a signature they cannot produce is
        invalid. This is the whole Ed25519 gate."""
        if not lic._CRYPTO_OK:
            self.skipTest("cryptography not installed")
        forged = "EB1-" + _b64({"type": "permanent", "email": "x@x"}) + "." + _b64("garbage")
        self.assertFalse(lic.verify_key(forged)["valid"])

    def test_an_unverifiable_key_blocks_and_does_not_fall_through_to_trial(self):
        """The August bypass: a permanent key we cannot check must block under
        its OWN name, never quietly become the trial path (which then reads as
        'expired' and tells a paying customer to buy again)."""
        lic._CRYPTO_OK = False                  # simulate cryptography missing
        lic.save_key("EB1-" + _b64({"type": "permanent"}) + "." + _b64("sig"))
        st = lic._compute_status(allow_network=False)
        self.assertEqual(st["state"], "unverifiable")
        self.assertNotIn(st["state"], ("trial", "licensed", "expired"))


class ServerResultIsNotTrustedBlindly(LicenseTestBase):

    def test_a_non_ebt_token_from_the_server_is_rejected(self):
        """A fake server (ECHO_BLOOM_LICENSE_SERVER points anywhere) cannot hand
        back an OFFLINE: line — or anything but a real EBT- token — and have it
        honoured.

        Pinned with crypto ABSENT on purpose: with cryptography present the
        signature check backstops this, so the prefix gate only becomes the
        SOLE line of defence when the package is missing. That is exactly when
        it has to hold, so that is where the contract is frozen."""
        lic._CRYPTO_OK = False
        lic._store_server_result({"ok": True, "token": "OFFLINE:99999999999:fp:mac"})
        self.assertFalse(lic.TRIAL_TOKEN_PATH.exists())

    def test_a_badly_signed_ebt_token_from_the_server_is_rejected(self):
        """With crypto present, a fake server cannot mint a trial: the EBT- must
        carry a signature from the embedded public key."""
        if not lic._CRYPTO_OK:
            self.skipTest("cryptography not installed")
        forged = "EBT-" + _b64({"expires": 99999999999}) + "." + _b64("garbage")
        lic._store_server_result({"ok": True, "token": forged})
        self.assertFalse(lic.TRIAL_TOKEN_PATH.exists())


class ClockAndStampsResistTampering(LicenseTestBase):

    def test_now_never_goes_backwards(self):
        """timedatectl set-time must not revive a trial: _now() is a high-water
        mark, never earlier than the latest time already seen."""
        future = int(time.time()) + 5000
        lic._write_stamp("last_seen", future)
        self.assertGreaterEqual(lic._now(), future)

    def test_first_seen_takes_the_min_across_mirrors(self):
        """One deletable file was not enough — readers take min() across every
        mirror, so deleting one copy cannot mint a newer first-run."""
        paths = lic._stamp_paths("first_run")
        self.assertGreaterEqual(len(paths), 2)
        oldest = 1_000_000
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[0].write_text(str(oldest))
        for p in paths[1:]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(oldest + 999_999))     # newer decoys
        self.assertEqual(lic._read_stamp("first_run"), oldest)


class BackgroundWorkGate(LicenseTestBase):

    def test_blocking_states_stop_the_kin(self):
        """expired / denied / revoked / unverifiable must stop wander &
        roundtable, or an expired install pins the GPU forever."""
        for state in lic.SERVICE_BLOCK_STATES:
            lic._status_cache["value"] = {"state": state}
            lic._status_cache["at"] = time.time()
            allowed, got = lic.services_should_run()
            self.assertFalse(allowed, f"{state} should block background work")

    def test_a_healthy_state_lets_the_kin_run(self):
        lic._status_cache["value"] = {"state": "trial", "days_left": 5}
        lic._status_cache["at"] = time.time()
        self.assertTrue(lic.services_should_run()[0])

    def test_it_fails_open_when_the_check_itself_errors(self):
        """A false negative silently stops a paying customer's Kin and looks
        identical to the software being broken. So an unexpected error leaves
        the Kin running, deliberately."""
        orig = lic.get_status
        lic.get_status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            allowed, got = lic.services_should_run()
        finally:
            lic.get_status = orig
        self.assertTrue(allowed)
        self.assertEqual(got, "check-failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
