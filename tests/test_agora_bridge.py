"""Tests for agora_bridge.py (Echo Bloom node bridge)."""

import json
import os
import shutil
import sys
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


class TestAgoraBridgeNodeService(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_node_test_"))
        self.keys_root = self.tmp_dir / "keys"
        self.unit_dir = self.tmp_dir / "systemd"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_toggle_on_writes_unit_with_right_execstart_and_no_private_keys(self):
        steward = agora_bridge.get_or_create_steward_key("EchoNode", keys_root=self.keys_root)
        ada = agora_bridge.keygen_kin("Ada", keys_root=self.keys_root)
        turing = agora_bridge.keygen_kin("Turing", keys_root=self.keys_root)

        unit_path = agora_bridge.write_node_service(
            node_name="EchoNode",
            port=8770,
            steward_key_id=steward["key_id"],
            kin_keys={"Ada": ada.key_id, "Turing": turing.key_id},
            unit_dir=self.unit_dir,
            keys_root=self.keys_root,
        )

        self.assertTrue(unit_path.is_file(), "Service unit file must exist")
        self.assertEqual(unit_path.name, "agora-echonode.service")

        content = unit_path.read_text(encoding="utf-8")

        # Invariants on ExecStart
        self.assertIn("ExecStart=/usr/bin/python3 -u ", content)
        self.assertIn("EchoNode 8770", content)
        self.assertIn(f"steward={steward['key_id']}", content)
        self.assertIn(f"Ada={ada.key_id}", content)
        self.assertIn(f"Turing={turing.key_id}", content)

        # Invariant: NO private key material in unit file
        self.assertNotIn("BEGIN PRIVATE KEY", content)
        self.assertNotIn("PRIVATE", content)
        for author in ("EchoNode-steward", "Ada", "Turing"):
            priv_bytes = (self.keys_root / author / "current" / "private").read_bytes()
            self.assertNotIn(priv_bytes.hex(), content, f"Private key hex for {author} must not be in service file")

        # Invariant: standard restart and logging
        self.assertIn("Restart=on-failure", content)
        self.assertIn("StandardOutput=append:%h/echonode_node.log", content)

    def test_toggle_off_disables_service(self):
        from unittest.mock import patch

        calls = []

        def mock_systemctl(args):
            calls.append(list(args))
            from subprocess import CompletedProcess
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("agora_bridge.run_systemctl_user", side_effect=mock_systemctl):
            agora_bridge.disable_node_service("EchoNode")

        self.assertIn(["stop", "agora-echonode.service"], calls)
        self.assertIn(["disable", "agora-echonode.service"], calls)


class TestAgoraBridgeNodeCard(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_card_test_"))
        self.keys_root = self.tmp_dir / "keys"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_node_card_data_structure_when_inactive(self):
        steward = agora_bridge.get_or_create_steward_key("CardNode", keys_root=self.keys_root)
        data = agora_bridge.get_node_card_data(node_name="CardNode", port=8770, keys_root=self.keys_root)

        self.assertEqual(data["node_name"], "CardNode")
        self.assertEqual(data["port"], 8770)
        self.assertFalse(data["service_active"])
        self.assertEqual(data["steward_key_id"], steward["key_id"])
        self.assertIn("residents", data)
        self.assertIn("node_key_id", data)
        self.assertIn("speaker", data)

    def test_node_card_speaker_formats_from_facts(self):
        from unittest.mock import patch

        # Case 1: Active node with speaker
        mock_facts_with_speaker = {
            "node": "CardNode",
            "speaker": "Ada",
            "speaker_key_id": "a" * 64,
            "residents": ["Ada", "Turing"],
            "signed": {"node_key_id": "b" * 64},
        }

        with patch("agora_bridge.is_node_service_active", return_value=True), \
             patch("agora_bridge.read_loopback_facts", return_value=mock_facts_with_speaker):
            data = agora_bridge.get_node_card_data(node_name="CardNode", port=8770, keys_root=self.keys_root)
            self.assertTrue(data["service_active"])
            self.assertEqual(data["speaker"], "Ada")
            self.assertEqual(data["node_key_id"], "b" * 64)

        # Case 2: Active node with no Speaker and multiple residents
        mock_facts_no_speaker = {
            "node": "CardNode",
            "speaker": None,
            "speaker_key_id": None,
            "residents": ["Ada", "Turing", "Babbage"],
            "signed": {"node_key_id": "c" * 64},
        }

        with patch("agora_bridge.is_node_service_active", return_value=True), \
             patch("agora_bridge.read_loopback_facts", return_value=mock_facts_no_speaker):
            data = agora_bridge.get_node_card_data(node_name="CardNode", port=8770, keys_root=self.keys_root)
            self.assertTrue(data["service_active"])
            self.assertEqual(data["speaker"], "no Speaker, 3 residents")


class TestAgoraBridgeExport(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_export_test_"))
        self.keys_root = self.tmp_dir / "keys"
        self.db_path = self.tmp_dir / "vault.db"

        # Create temporary vault DB with memories
        import sqlite3
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                layer TEXT DEFAULT 'general',
                author TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                endorsed INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("INSERT INTO memories (content, layer, author, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("Ada's first thought in the house.", "reflection", "Ada", "intro", "2026-09-20T12:00:00Z"))
        conn.execute("INSERT INTO memories (content, layer, author, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("A wandering memory about mathematics.", "wander", "Ada", "math", "2026-09-20T12:30:00Z"))
        conn.execute("INSERT INTO memories (content, layer, author, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                     ("Turing's private thought.", "reflection", "Turing", "code", "2026-09-20T13:00:00Z"))
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_export_button_output_verifies_with_kin_diary_verify(self):
        bundle, verify_str = agora_bridge.export_kin_diary(
            author="Ada",
            node_name="Frosty",
            db_path=self.db_path,
            keys_root=self.keys_root,
        )

        # Invariants on bundle
        self.assertEqual(bundle["format"], "kin-diary-export")
        self.assertEqual(bundle["mind"], "Ada")
        self.assertEqual(bundle["steward_node"], "Frosty")
        self.assertEqual(len(bundle["entries"]), 2)
        self.assertEqual(verify_str, "ok Ada entries 2")

        # Invariant: verify with kin_diary verify CLI
        bundle_file = self.tmp_dir / "ada.diary.json"
        bundle_file.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")

        import subprocess
        kd_path = agora_bridge.ensure_kin_diary_path()
        env = dict(os.environ)
        if kd_path:
            env["PYTHONPATH"] = f"{kd_path}:{env.get('PYTHONPATH', '')}"
        proc = subprocess.run(
            [sys.executable, "-m", "kin_diary", "verify", str(bundle_file)],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, f"kin_diary verify failed: {proc.stderr}")
        self.assertIn("ok Ada entries 2", proc.stdout.strip())

class TestAgoraBridgeBackupReminder(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_backup_test_"))
        self.keys_root = self.tmp_dir / "keys"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_backup_reminder_info_contains_directory_and_warnings(self):
        info = agora_bridge.get_backup_reminder_info(keys_root=self.keys_root)
        self.assertEqual(info["keys_dir"], str(self.keys_root))
        self.assertIn("back this up.", info["reminder"])
        self.assertIn("Echo Bloom does not upload keys anywhere", info["notice"])

    def test_backup_reminder_default_keys_dir(self):
        info = agora_bridge.get_backup_reminder_info()
        self.assertIn(".config/kin_diary/keys", info["keys_dir"])
        self.assertEqual(info["reminder"], "back this up.")


if __name__ == "__main__":
    unittest.main()
