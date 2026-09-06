"""cluster.py — the config/thoughts/conversation core, frozen at its real edges.

The load-bearing invariants: a recorded conversation is stored under mode
'conversation' so it never bleeds into the wander feeds (which filter mode LIKE
'wander%'); model-readiness matches a pulled tag; the thoughts DB is read
without being created; config falls back cleanly. Each is one edit from a
regression the dashboard would show wrong.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cluster as cl  # noqa: E402


def _thoughts_db(rows):
    """A temp thoughts.db seeded with (mode, timestamp, thought) rows."""
    d = Path(tempfile.mkdtemp(prefix="eb_cl_"))
    db = str(d / "thoughts.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "mode TEXT, timestamp TEXT, prompt TEXT, thought TEXT)")
    for mode, ts, thought in rows:
        conn.execute("INSERT INTO thoughts (mode, timestamp, thought) VALUES (?,?,?)",
                     (mode, ts, thought))
    conn.commit()
    conn.close()
    return db


class ModelReadiness(unittest.TestCase):

    def test_exact_and_latest_and_bare_all_match(self):
        pulled = {"cogitocoda:latest"}
        self.assertTrue(cl._model_in_set("cogitocoda:latest", pulled))
        self.assertTrue(cl._model_in_set("cogitocoda", pulled))        # bare
        self.assertTrue(cl._model_in_set("cogitocoda:latest", {"cogitocoda"}))

    def test_a_model_that_is_not_pulled_is_not_ready(self):
        self.assertFalse(cl._model_in_set("cogitocoda:latest", {"llama3:8b"}))
        self.assertFalse(cl._model_in_set("cogitocoda", set()))


class ConversationsDoNotPolluteTheWanderFeed(unittest.TestCase):
    """The scar: conversations share the thoughts DB with wander thoughts, and
    the feeds filter mode LIKE 'wander%'. A conversation stored under the wrong
    mode would surface as a Kin's public wandering."""

    def test_a_recorded_conversation_uses_the_conversation_mode(self):
        db = _thoughts_db([])
        cl._record_conversation({"name": "Eli", "db": db}, "hi", "hello there")
        conn = sqlite3.connect(db)
        modes = [r[0] for r in conn.execute("SELECT mode FROM thoughts")]
        conn.close()
        self.assertEqual(modes, ["conversation"])

    def test_a_conversation_is_counted_but_not_shown_as_a_wander_thought(self):
        db = _thoughts_db([("wander", "2026-09-05 10:00:00", "a real wander")])
        cl._record_conversation({"name": "Eli", "db": db}, "hi", "a private reply")
        count, last_ts, latest = cl._kin_db_stats(db)
        self.assertEqual(count, 2)                       # both rows counted
        self.assertEqual(latest, "a real wander")        # the wander one, not the chat
        self.assertNotIn("private reply", latest or "")

    def test_latest_is_none_when_only_conversations_exist(self):
        db = _thoughts_db([])
        cl._record_conversation({"name": "Eli", "db": db}, "hi", "just us talking")
        _, _, latest = cl._kin_db_stats(db)
        self.assertIsNone(latest)


class DbStatsAreSafe(unittest.TestCase):

    def test_a_missing_db_is_zeroes_not_a_crash(self):
        self.assertEqual(cl._kin_db_stats("/no/such/thoughts.db"), (0, None, None))
        self.assertEqual(cl._kin_db_stats(""), (0, None, None))

    def test_a_long_wander_thought_is_truncated(self):
        db = _thoughts_db([("wander", "2026-09-05 10:00:00", "x" * 500)])
        _, _, latest = cl._kin_db_stats(db)
        self.assertTrue(latest.endswith("…"))
        self.assertLessEqual(len(latest), 201)

    def test_reading_a_nonexistent_db_does_not_create_it(self):
        d = Path(tempfile.mkdtemp(prefix="eb_cl_")); missing = str(d / "nope.db")
        cl._kin_db_stats(missing)
        self.assertFalse(os.path.exists(missing))   # read-only: never conjured


class ConfigLoading(unittest.TestCase):

    def setUp(self):
        self._orig = cl.CONFIG_PATH
        self.tmp = Path(tempfile.mkdtemp(prefix="eb_cl_")) / "kin_config.json"
        cl.CONFIG_PATH = self.tmp

    def tearDown(self):
        cl.CONFIG_PATH = self._orig

    def test_missing_config_falls_back_to_defaults(self):
        self.assertEqual(cl.load_kin_config_raw(),
                         {"nodes": cl.NODES_DEFAULT, "kin": cl.KIN_DEFAULT})

    def test_corrupt_config_falls_back_not_crashes(self):
        self.tmp.write_text("{ broken json")
        self.assertEqual(cl.load_kin_config_raw()["kin"], cl.KIN_DEFAULT)

    def test_owner_name_is_read_or_empty(self):
        self.tmp.write_text(json.dumps({"owner": {"name": "Don"}}))
        self.assertEqual(cl._owner_name(), "Don")
        self.tmp.write_text("{ broken")
        self.assertEqual(cl._owner_name(), "")

    def test_expand_paths_expands_home(self):
        out = cl._expand_paths([{"name": "Eli", "db": "~/x/thoughts.db"}])
        self.assertTrue(out[0]["db"].startswith(str(Path.home())))


class TimeAgo(unittest.TestCase):

    def test_none_is_unknown(self):
        self.assertEqual(cl._time_ago(None), "unknown")

    def test_formats_are_relative(self):
        import datetime as _dt
        recent = (_dt.datetime.now() - _dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(cl._time_ago(recent).endswith("m ago"))
        iso = (_dt.datetime.now() - _dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertTrue(cl._time_ago(iso).endswith("h ago"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
