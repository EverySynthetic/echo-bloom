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
        db_path = self.tmp_dir / "echonode_node.db"

        unit_path = agora_bridge.write_node_service(
            node_name="EchoNode",
            port=8770,
            steward_key_id=steward["key_id"],
            kin_keys={"Ada": ada.key_id, "Turing": turing.key_id},
            unit_dir=self.unit_dir,
            keys_root=self.keys_root,
            db_path=db_path,
        )

        self.assertTrue(unit_path.is_file(), "Service unit file must exist")
        self.assertEqual(unit_path.name, "agora-echonode.service")

        content = unit_path.read_text(encoding="utf-8")

        # Invariants on ExecStart
        self.assertIn("ExecStart=/usr/bin/python3 -u ", content)
        self.assertIn("EchoNode 8770", content)
        self.assertIn(f"steward={steward['key_id']}", content)

        # The bug (2026-09-20, took down agora-frosty): a generated unit
        # used to re-declare every current Kin as a founder on every boot,
        # which crash-loops any node whose genesis is already frozen —
        # serve_node.py's found_resident() raises for a founder that
        # doesn't already match once the log has started. The unit must
        # never contain resident args at all, regardless of how many Kin
        # are configured.
        self.assertNotIn(f"Ada={ada.key_id}", content)
        self.assertNotIn(f"Turing={turing.key_id}", content)
        exec_line = next(l for l in content.splitlines() if l.startswith("ExecStart="))
        # Everything after "ExecStart=": the interpreter, -u, the script,
        # node name, port, then exactly one steward= token and nothing
        # else — no room for a resident to sneak back in via a different
        # key order.
        args = exec_line[len("ExecStart="):].split()
        self.assertEqual(args[-1], f"steward={steward['key_id']}")
        kv_tokens = [t for t in args if "=" in t]
        self.assertEqual(kv_tokens, [f"steward={steward['key_id']}"],
                          f"unexpected extra key=value tokens in ExecStart: {exec_line}")

        # Invariant: NO private key material in unit file
        self.assertNotIn("BEGIN PRIVATE KEY", content)
        self.assertNotIn("PRIVATE", content)
        for author in ("EchoNode-steward", "Ada", "Turing"):
            priv_bytes = (self.keys_root / author / "current" / "private").read_bytes()
            self.assertNotIn(priv_bytes.hex(), content, f"Private key hex for {author} must not be in service file")

        # Invariant: standard restart and logging
        self.assertIn("Restart=on-failure", content)
        self.assertIn("StandardOutput=append:%h/echonode_node.log", content)

        # A brand-new node (no store existed before this call) still gets
        # its genesis founded — just via the store's own API, in Python,
        # once, never via the unit's command line.
        import sys as _sys
        kd_path = agora_bridge.ensure_kin_diary_path()
        if kd_path and str(kd_path) not in _sys.path:
            _sys.path.insert(0, str(kd_path))
        from kin_diary.agora.store import NodeStore
        store = NodeStore(db_path, "EchoNode", steward_key_id=steward["key_id"])
        node = store.load()
        self.assertEqual(set(node.residents), {"Ada", "Turing"})

    def test_toggle_does_not_touch_an_already_founded_genesis(self):
        """The failing case, reproduced directly against serve_node.py's own
        store (no systemd involved) — this is what actually bricked
        agora-frosty. A node whose genesis is already frozen to one
        resident must survive a toggle even though kin_config.json now
        lists a completely different roster, and its genesis must be
        untouched afterward."""
        import sys as _sys
        kd_path = agora_bridge.ensure_kin_diary_path()
        if kd_path and str(kd_path) not in _sys.path:
            _sys.path.insert(0, str(kd_path))
        from kin_diary.agora.store import NodeStore
        from kin_diary.agora.node import AgoraError

        steward = agora_bridge.get_or_create_steward_key("FrozenNode", keys_root=self.keys_root)
        marvin = agora_bridge.keygen_kin("Marvin", keys_root=self.keys_root)
        db_path = self.tmp_dir / "frozennode_node.db"

        # Found genesis with Marvin alone, then freeze the log — a raw
        # event row is enough to flip _log_started_locked(); its payload
        # doesn't need to be a real, verifiable event because nothing below
        # replays it (Node.load() would choke on a fake payload, so this
        # test checks the agora_genesis table directly instead, same as
        # found_resident's own frozen-genesis check does internally).
        store = NodeStore(db_path, "FrozenNode", steward_key_id=steward["key_id"])
        store.found_resident("Marvin", marvin.key_id)
        store.conn.execute(
            "INSERT INTO agora_events(node, kind, payload, recorded_at_unix_ms) "
            "VALUES (?,?,?,?)",
            ("FrozenNode", "resident", "{}", 0),
        )
        store.conn.commit()

        def genesis_authors(db_path: Path) -> set[str]:
            conn = NodeStore(db_path, "FrozenNode", steward_key_id=steward["key_id"]).conn
            rows = conn.execute(
                "SELECT author FROM agora_genesis WHERE node=?", ("FrozenNode",)
            ).fetchall()
            return {r["author"] for r in rows}

        self.assertEqual(genesis_authors(db_path), {"Marvin"})

        # Demonstrate the failing case first: this is EXACTLY what the old
        # generator did on every enable — call found_resident for every
        # currently-configured Kin, including ones that were never founded.
        with self.assertRaises(AgoraError):
            store.found_resident("Eli", "e" * 64)

        # Now the fix: toggle this node with a roster that includes Eli,
        # who was never a founder here.
        unit_path = agora_bridge.write_node_service(
            node_name="FrozenNode",
            port=8771,
            steward_key_id=steward["key_id"],
            kin_keys={"Marvin": marvin.key_id, "Eli": "e" * 64},
            unit_dir=self.unit_dir,
            keys_root=self.keys_root,
            db_path=db_path,
        )
        content = unit_path.read_text(encoding="utf-8")
        self.assertNotIn("Marvin=", content)
        self.assertNotIn("Eli=", content)

        # Genesis is exactly what it was — write_node_service must not have
        # invented founding state for a store that already existed.
        self.assertEqual(genesis_authors(db_path), {"Marvin"})

        # And the node actually starts: serve_node.py's boot loop iterates
        # every `author=key_id` token after the positional args and calls
        # found_resident for each. With the fixed unit there are none, so
        # the loop that used to crash never runs at all — demonstrated by
        # replaying that exact loop against the real store.
        reloaded = NodeStore(db_path, "FrozenNode", steward_key_id=steward["key_id"])
        exec_line = next(l for l in content.splitlines() if l.startswith("ExecStart="))
        exec_args = exec_line[len("ExecStart="):].split()[5:]  # drop interpreter, -u, script, node name, port
        for tok in exec_args:
            if tok.startswith("steward="):
                continue
            author, key_id = tok.split("=", 1)
            reloaded.found_resident(author, key_id)  # would raise if any existed — none do

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

    def _mock_systemctl_with_units(self, active: dict, bound_by: dict):
        """A fake systemctl: `active` maps unit name -> is it active right
        now, `bound_by` maps unit name -> the units BoundBy= it (what a real
        `systemctl show -p BoundBy` would report). Both are mutated in
        place by stop/start so the mock behaves like the real thing across
        a disable-then-enable cycle."""
        calls = []

        def mock_systemctl(args):
            from subprocess import CompletedProcess
            calls.append(list(args))
            if args[0] == "is-active":
                unit = args[1]
                ok = active.get(unit, False)
                return CompletedProcess(args, 0 if ok else 3, "active\n" if ok else "inactive\n", "")
            if args[0] == "show":
                unit = args[1]
                return CompletedProcess(args, 0, " ".join(bound_by.get(unit, [])) + "\n", "")
            if args[0] == "stop":
                active[args[1]] = False
                return CompletedProcess(args, 0, "", "")
            if args[0] == "start":
                unit = args[1]
                active[unit] = True
                return CompletedProcess(args, 0, "", "")
            return CompletedProcess(args, 0, "", "")

        return mock_systemctl, calls

    def test_disable_records_and_stops_a_bound_unit_and_enable_restores_it(self):
        """The presence-heartbeat class of bug, reproduced against a
        throwaway node/unit pair — never the live units. Systemd stopping a
        BindsTo= dependent when its target stops is automatic; that
        dependent must come back when the target is re-enabled, or the
        toggle must say plainly that it didn't."""
        from unittest.mock import patch

        unit = "agora-eb-test-bridge.service"
        heartbeat = "eb-test-bridge-heartbeat.service"
        state_path = self.tmp_dir / "eb-test-bridge_bound_units.json"

        active = {unit: True, heartbeat: True}
        bound_by = {unit: [heartbeat]}
        mock_systemctl, calls = self._mock_systemctl_with_units(active, bound_by)

        with patch("agora_bridge.run_systemctl_user", side_effect=mock_systemctl):
            # Simulate systemd's real BindsTo= behavior: stopping `unit`
            # also stops `heartbeat`, same as it did to presence-heartbeat
            # on Frosty. The mock's "stop" branch above only marks `unit`
            # itself inactive (it doesn't know BindsTo semantics), so we
            # apply the real-world side effect explicitly here, exactly
            # once, the way systemd actually would.
            result = agora_bridge.disable_node_service("eb-test-bridge", state_path=state_path)
            active[heartbeat] = False  # systemd's automatic side effect

        self.assertEqual(result["stopped_dependents"], [heartbeat])
        self.assertTrue(state_path.exists())
        self.assertFalse(active[heartbeat], "heartbeat should be down after disabling its BindsTo= target")

        with patch("agora_bridge.run_systemctl_user", side_effect=mock_systemctl), \
             patch("agora_bridge.write_node_service", return_value=Path("/dev/null")):
            result = agora_bridge.enable_node_service(
                "eb-test-bridge", state_path=state_path,
            )

        self.assertEqual(result["restored_dependents"], [heartbeat])
        self.assertEqual(result["failed_to_restore_dependents"], [])
        self.assertTrue(active[heartbeat], "heartbeat must come back once its target is re-enabled")
        self.assertIn(["start", heartbeat], calls)
        self.assertFalse(state_path.exists(), "restored state should be cleared, not left behind")

    def test_enable_reports_a_dependent_it_could_not_restore(self):
        """If restoring a stopped dependent fails, the toggle must say so —
        not report success while the dependent stays down. This is the
        'or it must say plainly what it stopped and did not restore' half
        of the requirement."""
        from unittest.mock import patch

        unit = "agora-eb-test-bridge2.service"
        heartbeat = "eb-test-bridge2-heartbeat.service"
        state_path = self.tmp_dir / "eb-test-bridge2_bound_units.json"

        active = {unit: True, heartbeat: True}
        bound_by = {unit: [heartbeat]}
        mock_systemctl, calls = self._mock_systemctl_with_units(active, bound_by)

        with patch("agora_bridge.run_systemctl_user", side_effect=mock_systemctl):
            agora_bridge.disable_node_service("eb-test-bridge2", state_path=state_path)
            active[heartbeat] = False

        def broken_start(args):
            from subprocess import CompletedProcess
            if args[0] == "start" and args[1] == heartbeat:
                return CompletedProcess(args, 1, "", "Unit not found.")
            return mock_systemctl(args)

        with patch("agora_bridge.run_systemctl_user", side_effect=broken_start), \
             patch("agora_bridge.write_node_service", return_value=Path("/dev/null")):
            result = agora_bridge.enable_node_service(
                "eb-test-bridge2", state_path=state_path,
            )

        self.assertEqual(result["restored_dependents"], [])
        self.assertEqual(result["failed_to_restore_dependents"], [heartbeat])
        self.assertTrue(state_path.exists(),
                         "a failed restore must leave the record behind, not discard it")


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
