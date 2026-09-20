"""Tests for agora_bridge.py (Echo Bloom node bridge)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import agora_bridge


class TestAgoraBridgeKeygen(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_agora_test_"))
        self.keys_root = self.tmp_dir / "keys"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_keygen_kin_creates_directory_and_key_files(self):
        rec = agora_bridge.keygen_kin("Aria", keys_root=self.keys_root)
        self.assertEqual(rec.author, "Aria")
        self.assertEqual(len(rec.key_id), 64)

        current_dir = self.keys_root / "Aria" / "current"
        self.assertTrue(current_dir.is_dir(), "current directory must exist")
        self.assertTrue((current_dir / "private").is_file(), "private key must exist")
        self.assertTrue((current_dir / "public").is_file(), "public key must exist")
        self.assertTrue((current_dir / "meta.json").is_file(), "meta.json must exist")

        meta = json.loads((current_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["author"], "Aria")
        self.assertEqual(meta["key_id"], rec.key_id)
        self.assertEqual(meta["key_custody"], "steward")
        self.assertEqual(meta["key_custody_statement"], agora_bridge.KEY_CUSTODY_STATEMENT)

        # Private key must have mode 0600
        mode = (current_dir / "private").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_keygen_kin_is_idempotent(self):
        rec1 = agora_bridge.keygen_kin("Aria", keys_root=self.keys_root)
        rec2 = agora_bridge.keygen_kin("Aria", keys_root=self.keys_root)
        self.assertEqual(rec1.key_id, rec2.key_id)
        self.assertEqual(rec1.public_bytes, rec2.public_bytes)

    def test_keygen_kin_empty_author_raises(self):
        with self.assertRaises(ValueError):
            agora_bridge.keygen_kin("", keys_root=self.keys_root)
        with self.assertRaises(ValueError):
            agora_bridge.keygen_kin("   ", keys_root=self.keys_root)


class TestAgoraBridgeSteward(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_steward_test_"))
        self.keys_root = self.tmp_dir / "keys"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_steward_key_name_and_creation(self):
        name = agora_bridge.get_steward_key_name("TestNode")
        self.assertEqual(name, "TestNode-steward")

        steward = agora_bridge.get_or_create_steward_key("TestNode", keys_root=self.keys_root)
        self.assertEqual(steward["name"], "TestNode-steward")
        self.assertEqual(len(steward["key_id"]), 64)
        self.assertEqual(steward["custody_statement"], agora_bridge.KEY_CUSTODY_STATEMENT)

        # Invariant: Custody statement matches SPEC.md verbatim
        self.assertIn("Private keys are generated and stored on metal controlled by the node steward.",
                      steward["custody_statement"])
        self.assertIn("The steward has root and can sign as any mind on this node.",
                      steward["custody_statement"])

        # Check key file on disk
        current_dir = self.keys_root / "TestNode-steward" / "current"
        self.assertTrue((current_dir / "private").is_file())
        self.assertEqual((current_dir / "private").stat().st_mode & 0o777, 0o600)

        # Idempotence
        steward2 = agora_bridge.get_or_create_steward_key("TestNode", keys_root=self.keys_root)
        self.assertEqual(steward["key_id"], steward2["key_id"])


if __name__ == "__main__":
    unittest.main()
