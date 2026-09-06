"""Presence status as the dashboard reads it.

kin_presence derives a Kin's status — present / thinking / quiet / offline —
from the cache. That derivation was silently dead: get_presence spread the raw
cache last, so a wander loop's heartbeat("alive") overwrote the computed status
and the dashboard showed the internal word instead of the derived one. These
freeze the derivation as authoritative, plus the kin/agent split, the vault
handoff, and dismissal.
"""
import os
import sys
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import kin_presence as kp  # noqa: E402


class KinPresenceBase(unittest.TestCase):
    def setUp(self):
        kp._presence_cache.clear()
        self._log = kp.log
        kp.log = lambda m: None
        # a fixed household, no config file read
        self._known = kp._known_kin_names
        kp._known_kin_names = lambda: {"eli", "coda"}

    def tearDown(self):
        kp.log = self._log
        kp._known_kin_names = self._known
        kp._presence_cache.clear()


class DerivedStatusIsAuthoritative(KinPresenceBase):

    def test_a_heartbeat_reads_as_thinking_not_its_raw_status(self):
        kp.heartbeat("Eli", "alive")
        self.assertEqual(kp.get_presence("Eli")["status"], "thinking")

    def test_an_old_heartbeat_goes_quiet(self):
        kp.heartbeat("Eli", "alive")
        kp._presence_cache[kp._presence_key("Eli")]["last_heartbeat"] = time.time() - 400
        self.assertEqual(kp.get_presence("Eli")["status"], "quiet")

    def test_a_never_seen_kin_is_offline(self):
        self.assertEqual(kp.get_presence("Eli")["status"], "offline")

    def test_a_returned_thought_reads_as_present(self):
        with mock.patch.object(kp.requests, "post",
                               return_value=mock.Mock(ok=True, status_code=200)):
            kp.record_thought_return("Eli", "a thought", mode="wander")
        self.assertEqual(kp.get_presence("Eli")["status"], "present")


class KinVsAgent(KinPresenceBase):

    def test_configured_names_are_kin_others_are_agents(self):
        self.assertEqual(kp.entity_type("Eli"), "kin")
        self.assertEqual(kp.entity_type("CODA"), "kin")       # case-insensitive
        self.assertEqual(kp.entity_type("Marvin"), "agent")


class VaultHandoff(KinPresenceBase):

    def test_a_wander_thought_writes_the_wander_layer(self):
        with mock.patch.object(kp.requests, "post",
                               return_value=mock.Mock(ok=True, status_code=200)) as post:
            ok = kp.record_thought_return("Eli", "dusk on the shop", mode="wander")
        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["author"], "Eli")
        self.assertEqual(payload["layer"], "wander")
        self.assertEqual(payload["source"], "wandered")

    def test_a_reflection_mode_writes_the_reflection_layer(self):
        with mock.patch.object(kp.requests, "post",
                               return_value=mock.Mock(ok=True, status_code=200)) as post:
            kp.record_thought_return("Eli", "looking back", mode="reflection")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["layer"], "reflection")
        self.assertEqual(payload["source"], "experienced")

    def test_a_vault_failure_is_reported_not_raised(self):
        with mock.patch.object(kp.requests, "post", side_effect=Exception("down")):
            self.assertFalse(kp.record_thought_return("Eli", "x", mode="wander"))


class Dismiss(KinPresenceBase):

    def test_dismiss_drops_the_cache_and_reports_whether_it_existed(self):
        kp.heartbeat("Marvin", "alive")
        self.assertTrue(kp.dismiss("Marvin"))
        self.assertEqual(kp.get_presence("Marvin")["status"], "offline")
        self.assertFalse(kp.dismiss("Marvin"))     # already gone


class Overview(KinPresenceBase):

    def test_every_configured_kin_appears_even_if_never_seen(self):
        with mock.patch.object(kp.cfg, "get_kin",
                               return_value=[{"name": "Eli"}, {"name": "Coda"}]):
            all_p = kp.get_all_presence()["presence"]
        self.assertEqual(set(all_p), {"Eli", "Coda"})
        self.assertEqual(all_p["Coda"]["status"], "offline")


if __name__ == "__main__":
    unittest.main(verbosity=2)
