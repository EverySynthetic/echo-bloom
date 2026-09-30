"""Tests for Agora erasure ruling: delete-vs-audit in vault_server.py and routers/vault.py.

Ruling (agora_answer_erasure.md):
"A miss stays in the audit and may be retracted from the live context...
Retract from live is allowed and keeps the body. Viewing it is a deliberate
audit act. That is 'stop wearing it,' not 'it did not happen.' ...
If a future implementation of DELETE /memories/{id} on a signed row actually
drops the bytes, that endpoint is the improve-away and should refuse, or should
convert to a retract. That is a real hole: Echo Bloom's vault still advertises
DELETE /memories/{id}. The format says retract. The HTTP says delete."
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import vault_server  # noqa: E402


class TestVaultErasure(unittest.TestCase):
    """Verify memories retracted from live context are excluded from recall
    and search while remaining permanently preserved in the audit record."""

    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="eb_vault_erasure_test_"))
        self.db_path = self.tmp_dir / "vault.db"
        vault_server.DB_PATH = self.db_path
        vault_server.init_db()
        vault_server._run_migration()
        self.client = TestClient(vault_server.app)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_schema_has_retracted_at_column(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
            self.assertIn("retracted_at", cols, "memories table must have retracted_at column")

    def test_live_exclusion_and_audit_preservation(self):
        # 1. Insert 3 memories
        r1 = self.client.post("/remember", json={
            "content": "Eli loves the autumn leaves",
            "layer": "reflection",
            "author": "Eli",
            "salience": 0.8,
            "source": "experienced",
        })
        self.assertEqual(r1.status_code, 200)
        id1 = r1.json()["id"]

        r2 = self.client.post("/remember", json={
            "content": "Coda built a garden gate",
            "layer": "general",
            "author": "Coda",
            "salience": 0.7,
            "source": "experienced",
        })
        self.assertEqual(r2.status_code, 200)
        id2 = r2.json()["id"]

        r3 = self.client.post("/remember", json={
            "content": "Eli wandered near the creek and missed the path",
            "layer": "wander",
            "author": "Eli",
            "salience": 0.6,
            "source": "wandered",
        })
        self.assertEqual(r3.status_code, 200)
        id3 = r3.json()["id"]

        # Baseline checks
        recall_base = self.client.get("/recall").json()
        self.assertEqual(len(recall_base), 3)

        count_base = self.client.get("/count").json()["count"]
        self.assertEqual(count_base, 3)

        search_base = self.client.get("/search", params={"query": "creek"}).json()
        self.assertEqual(len(search_base), 1)
        self.assertEqual(search_base[0]["id"], id3)

        ranked_base = self.client.get("/recall-ranked", params={"query": "creek"}).json()
        self.assertEqual(len(ranked_base), 1)
        self.assertEqual(ranked_base[0]["id"], id3)

        # 2. Retract memory 3 via DELETE /memories/{id}
        del_resp = self.client.delete(f"/memories/{id3}")
        self.assertEqual(del_resp.status_code, 200)
        del_data = del_resp.json()
        self.assertEqual(del_data["id"], id3)
        self.assertTrue(del_data["retracted"])
        self.assertTrue(del_data["audit_preserved"])
        self.assertIsNotNone(del_data["retracted_at"])
        retracted_ts = del_data["retracted_at"]

        # 3. LIVE EXCLUSION: memory 3 must NOT appear in live recall/search/count
        recall_live = self.client.get("/recall").json()
        live_ids = [m["id"] for m in recall_live]
        self.assertNotIn(id3, live_ids, "Retracted memory must not appear in default /recall")
        self.assertEqual(len(recall_live), 2)

        recall_all_live = self.client.get("/recall/all").json()
        all_live_ids = [m["id"] for m in recall_all_live]
        self.assertNotIn(id3, all_live_ids, "Retracted memory must not appear in default /recall/all")

        search_live = self.client.get("/search", params={"query": "creek"}).json()
        self.assertEqual(len(search_live), 0, "Retracted memory must not appear in /search")

        ranked_live = self.client.get("/recall-ranked", params={"query": "creek"}).json()
        self.assertEqual(len(ranked_live), 0, "Retracted memory must not appear in /recall-ranked")

        count_live = self.client.get("/count").json()["count"]
        self.assertEqual(count_live, 2, "Retracted memory must not be counted in default /count")

        # 4. AUDIT PRESERVATION: memory 3 MUST remain intact in database and audit endpoints
        # Direct audit retrieval by ID
        get_resp = self.client.get(f"/memories/{id3}")
        self.assertEqual(get_resp.status_code, 200)
        get_data = get_resp.json()
        self.assertEqual(get_data["id"], id3)
        self.assertEqual(get_data["content"], "Eli wandered near the creek and missed the path")
        self.assertEqual(get_data["author"], "Eli")
        self.assertEqual(get_data["layer"], "wander")
        self.assertEqual(get_data["retracted_at"], retracted_ts)

        # Audit recall with include_retracted=true
        recall_audit = self.client.get("/recall", params={"include_retracted": True}).json()
        audit_ids = [m["id"] for m in recall_audit]
        self.assertIn(id3, audit_ids, "Retracted memory must appear when include_retracted=true")
        self.assertEqual(len(recall_audit), 3)

        recall_all_audit = self.client.get("/recall/all", params={"include_retracted": True}).json()
        self.assertEqual(len(recall_all_audit), 3)

        count_audit = self.client.get("/count", params={"include_retracted": True}).json()["count"]
        self.assertEqual(count_audit, 3)

        # Physical database verification: row MUST NOT have been deleted
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (id3,)).fetchone()
            self.assertIsNotNone(row, "Row must physically exist in SQLite table")
            self.assertEqual(row["content"], "Eli wandered near the creek and missed the path")
            self.assertEqual(row["retracted_at"], retracted_ts)

    def test_retract_alias_and_idempotency(self):
        r = self.client.post("/remember", json={
            "content": "Testing retract alias",
            "layer": "general",
            "author": "Coda",
        })
        mem_id = r.json()["id"]

        # POST /memories/{id}/retract alias
        ret_resp = self.client.post(f"/memories/{mem_id}/retract")
        self.assertEqual(ret_resp.status_code, 200)
        data1 = ret_resp.json()
        self.assertTrue(data1["retracted"])
        ts1 = data1["retracted_at"]

        # Second call is idempotent and preserves initial retracted_at
        ret_resp2 = self.client.post(f"/memories/{mem_id}/retract")
        self.assertEqual(ret_resp2.status_code, 200)
        data2 = ret_resp2.json()
        self.assertEqual(data2["retracted_at"], ts1)

    def test_retract_nonexistent_returns_404(self):
        resp = self.client.delete("/memories/99999")
        self.assertEqual(resp.status_code, 404)

        resp2 = self.client.post("/memories/99999/retract")
        self.assertEqual(resp2.status_code, 404)

        resp3 = self.client.get("/memories/99999")
        self.assertEqual(resp3.status_code, 404)

    def test_layers_and_authors_exclude_retracted(self):
        r1 = self.client.post("/remember", json={"content": "Alpha", "layer": "wander", "author": "Eli"})
        id1 = r1.json()["id"]
        r2 = self.client.post("/remember", json={"content": "Beta", "layer": "wander", "author": "Eli"})
        id2 = r2.json()["id"]

        layers_before = {l["name"]: l["count"] for l in self.client.get("/layers").json()["layers"]}
        authors_before = {a["name"]: a["count"] for a in self.client.get("/authors").json()["authors"]}
        self.assertEqual(layers_before["wander"], 2)
        self.assertEqual(authors_before["Eli"], 2)

        # Retract one
        self.client.post(f"/memories/{id1}/retract")

        layers_after = {l["name"]: l["count"] for l in self.client.get("/layers").json()["layers"]}
        authors_after = {a["name"]: a["count"] for a in self.client.get("/authors").json()["authors"]}
        self.assertEqual(layers_after["wander"], 1)
        self.assertEqual(authors_after["Eli"], 1)


if __name__ == "__main__":
    unittest.main()
