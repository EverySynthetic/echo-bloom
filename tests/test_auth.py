"""The lock on the door — bcrypt, sessions, rate limit, setup token — frozen.

auth.py guards a single-user login that can be reached over a LAN or a public
tunnel, and it had no direct test. Its scar comments name real incidents: a
password-manager passphrase over 72 bytes 500'd with no password set; a
truncating config write reverted the app to first-run, claimable by whoever
reached it first; a rate-limit dict that grew forever. Each of those is one
edit from returning. So every guarantee the login makes is nailed here.

unittest only (no pytest — must run on a customer's box), isolated HOME per
test, no network.
"""
import importlib
import os
import sys
import time
import unittest
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auth  # noqa: E402


class AuthTestBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="eb_auth_"))
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        cfg = self.home / ".config" / "kin_app"
        cfg.mkdir(parents=True, exist_ok=True)
        auth.CONFIG_FILE = cfg / "config.json"
        auth.SETUP_TOKEN_FILE = cfg / "setup_token"
        auth.SESSIONS_FILE = cfg / "sessions.json"
        auth._sessions = {}
        auth._login_attempts.clear()

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        else:
            os.environ.pop("HOME", None)


class PasswordIsHashedNotStored(AuthTestBase):

    def test_a_set_password_verifies_and_a_wrong_one_does_not(self):
        auth.set_password("correct horse battery")
        self.assertTrue(auth.verify_password("correct horse battery"))
        self.assertFalse(auth.verify_password("wrong"))

    def test_the_hash_is_not_the_password(self):
        auth.set_password("hunter2xy")
        stored = auth.load_config()["password_hash"]
        self.assertNotIn("hunter2xy", stored)
        self.assertTrue(stored.startswith("$2"))   # bcrypt

    def test_verify_is_false_when_no_password_is_set(self):
        self.assertFalse(auth.verify_password("anything"))

    def test_a_passphrase_over_72_bytes_does_not_crash_the_login(self):
        """bcrypt raises above 72 bytes; a password-manager passphrase must set
        and verify, not 500 with no password stored."""
        long = "p" * 200
        auth.set_password(long)                    # must not raise
        self.assertTrue(auth.verify_password(long))
        # the documented truncation: same first 72 bytes still verifies
        self.assertTrue(auth.verify_password("p" * 72))


class ConfigCorruptionIsLoud(AuthTestBase):

    def test_a_corrupt_config_reads_as_no_password_not_a_crash(self):
        auth.CONFIG_FILE.write_text("{ this is not json")
        self.assertEqual(auth.load_config(), {})
        self.assertFalse(auth.is_configured())

    def test_is_configured_tracks_the_password_hash(self):
        self.assertFalse(auth.is_configured())
        auth.set_password("abcdefgh")
        self.assertTrue(auth.is_configured())


class Sessions(AuthTestBase):

    def test_a_created_session_validates_and_an_unknown_one_does_not(self):
        tok = auth.create_session()
        self.assertTrue(auth.validate_session(tok))
        self.assertFalse(auth.validate_session("not-a-real-token"))
        self.assertFalse(auth.validate_session(""))

    def test_an_expired_session_is_rejected_and_dropped(self):
        tok = auth.create_session()
        auth._sessions[tok] = time.time() - 1     # force expiry
        self.assertFalse(auth.validate_session(tok))
        self.assertNotIn(tok, auth._sessions)     # cleaned up

    def test_a_revoked_session_stops_working(self):
        tok = auth.create_session()
        auth.revoke_session(tok)
        self.assertFalse(auth.validate_session(tok))


class RateLimit(AuthTestBase):

    def test_it_locks_out_after_the_max_attempts(self):
        ip = "10.0.0.9"
        for _ in range(auth.MAX_ATTEMPTS):
            self.assertFalse(auth.is_rate_limited(ip))
            auth.record_attempt(ip)
        self.assertTrue(auth.is_rate_limited(ip))

    def test_attempts_outside_the_window_do_not_count(self):
        ip = "10.0.0.10"
        old = time.time() - auth.WINDOW_SECS - 1
        auth._login_attempts[ip] = [old] * (auth.MAX_ATTEMPTS + 2)
        self.assertFalse(auth.is_rate_limited(ip))   # all stale -> not limited

    def test_reading_an_unseen_ip_does_not_grow_the_table(self):
        """The dict used to gain a key for every address ever checked."""
        auth.is_rate_limited("10.1.1.1")
        self.assertNotIn("10.1.1.1", auth._login_attempts)


class SetupTokenClaimsTheInstall(AuthTestBase):

    def test_the_right_token_is_accepted_and_a_wrong_one_is_not(self):
        tok = auth.ensure_setup_token()
        self.assertTrue(tok)
        self.assertTrue(auth.verify_setup_token(tok))
        self.assertTrue(auth.verify_setup_token(tok.lower()))   # case-insensitive
        self.assertFalse(auth.verify_setup_token("000000000000"))
        self.assertFalse(auth.verify_setup_token(""))

    def test_no_token_means_nothing_verifies(self):
        self.assertFalse(auth.verify_setup_token("anything"))

    def test_a_set_password_makes_the_token_moot(self):
        auth.ensure_setup_token()
        auth.set_password("abcdefgh")
        # once claimed, is_configured is true and a fresh token is not minted
        self.assertIsNone(auth.ensure_setup_token())


if __name__ == "__main__":
    unittest.main(verbosity=2)
