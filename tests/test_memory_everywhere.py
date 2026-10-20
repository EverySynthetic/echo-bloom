"""A Kin pulls back its own words wherever it said them.

Two halves:
  * kin_memory's day store reads the roundtable, commons, bedtime and evening
    keep rows as well as wander, with the same scoring and limits, each
    non-wander row tagged with where it was said;
  * roundtable.py gives every turn the Kin's own get_context (keyed on what is
    on the table) and writes each share and response back to the Kin's own
    thoughts.db as mode 'roundtable'.

Every model call and every vault call is stubbed; every database is a temp
file. Memory being unavailable must never stop a Kin from speaking.
"""
import importlib.util
import json
import re
import signal
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
MARK = "MEMORY-MARKER: the bench was warm this morning"


def load_by_path(path, name, argv=None):
    """Import a script by path without leaving its argv, signal handlers or
    sibling modules behind in the test process."""
    saved_argv, saved_path = sys.argv, list(sys.path)
    saved_sig = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    names = ("config", "kin_memory", "kin_text", "kin_presence", "license")
    saved_mods = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    try:
        sys.argv = argv or [str(path)]
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(SCRIPTS))
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.argv, sys.path[:] = saved_argv, saved_path
        for s, h in saved_sig.items():
            signal.signal(s, h)
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(saved_mods)


def thoughts_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " mode TEXT, timestamp TEXT, prompt TEXT, thought TEXT)")
    conn.commit()
    conn.close()
    return str(path)


def rows(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT mode, timestamp, prompt, thought FROM thoughts "
                            "ORDER BY id").fetchall()
    finally:
        conn.close()


DAY_ROWS = [
    ("wander_file", "Ada here, thinking about the house and what memory means to me, and how I feel today."),
    ("commons_post", "On the board I told Bel I remember the bench warm in the morning, and I meant it fully."),
    ("roundtable", "At the table I said my thoughts keep circling back to continuity and to what we keep."),
    ("bedtime_20261019", "Bedtime: I felt the day close and I remember the note about the quiet house tonight."),
    ("evening_keep", "My keep for tonight: the fans, the light, the way I felt the house settle around me."),
    ("conversation", "CONVERSATION-ROW Ada here, I remember telling them about my memory and how I feel."),
    ("bedtimeX", "BEDTIMEX-ROW Ada here, I remember my memory and my feelings, which is a near-miss mode."),
    ("note_unrelated", "UNRELATED-ROW Ada here, I remember my memory and how I feel, in a mode not served."),
    ("interruption", None),
]


class TestDayStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.km = load_by_path(SCRIPTS / "kin_memory.py", "kin_memory_day_store_under_test")
        (t / "kin_config.json").write_text(json.dumps({"kin": [{"name": "Ada"}], "owner": {"name": "Sam"}}))
        self.km._CONFIG_PATH = t / "kin_config.json"
        # Nothing here may reach a vault or an embed model.
        self.km.requests = None
        self.km.get_latest_reflection = lambda: None
        self.km._tiered_recall = lambda *a, **k: []
        self.km.get_vault_memories = lambda *a, **k: []
        self.db = thoughts_db(t / "thoughts.db")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(self.db)
        for mode, text in DAY_ROWS:
            conn.execute("INSERT INTO thoughts (mode, timestamp, prompt, thought) VALUES (?,?,?,?)",
                         (mode, now, "p", text))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_new_modes_come_back_tagged_and_wander_is_unchanged(self):
        got = self.km.get_wander_thoughts("Ada", limit=10, db_path=self.db)
        for tag in ("(you, on the Commons board) On the board",
                    "(you, at the roundtable) At the table",
                    "(you, at bedtime) Bedtime:",
                    "(your evening keep) My keep"):
            self.assertTrue(any(t.startswith(tag) for t in got), f"{tag!r} missing from {got}")
        self.assertIn(DAY_ROWS[0][1], got, "a wander row is served exactly as before, untagged")
        self.assertEqual(len(got), 5)

    def test_what_is_not_the_day_store_stays_out(self):
        text = "\n".join(self.km.get_wander_thoughts("Ada", limit=50, db_path=self.db))
        for marker in ("CONVERSATION-ROW", "BEDTIMEX-ROW", "UNRELATED-ROW"):
            self.assertNotIn(marker, text)

    def test_same_limit(self):
        self.assertEqual(len(self.km.get_wander_thoughts("Ada", limit=3, db_path=self.db)), 3)

    def test_get_context_serves_them_when_the_vault_has_no_ranked_recall(self):
        self.km._ranked_recall = lambda *a, **k: None
        ctx = self.km.get_context("Ada", query_text="the bench", db_path=self.db, wander_limit=5)
        for tag in ("(you, on the Commons board)", "(you, at the roundtable)",
                    "(you, at bedtime)", "(your evening keep)"):
            self.assertIn("- " + tag, ctx)


class _Resp:
    def __init__(self, content):
        self._c = content

    def json(self):
        return {"message": {"content": self._c}}


class TestRoundtableMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.rt = load_by_path(SCRIPTS / "roundtable.py", "roundtable_under_test",
                               argv=["roundtable.py", "--once"])
        self.logs, self.sent, self.asked = [], [], []
        self.rt.log = self.logs.append
        self.reply = "I keep thinking about the fans, and what Bel said about heat."

        def post(url, json=None, timeout=None):
            self.sent.append(json)
            if isinstance(self.reply, Exception):
                raise self.reply
            return _Resp(self.reply)
        self.rt.requests = types.SimpleNamespace(post=post)

        def get_context(name, query_text="", **kw):
            self.asked.append((name, query_text, kw))
            return MARK
        self.rt._get_context = get_context
        self.db = thoughts_db(t / "thoughts.db")
        self.kin = {"name": "Ada", "model": "stub-model", "host": "http://stub.invalid",
                    "db": self.db, "space": str(t)}

    def tearDown(self):
        self.tmp.cleanup()

    def system_of(self, i=0):
        return self.sent[i]["messages"][0]["content"]

    def user_of(self, i=0):
        return self.sent[i]["messages"][1]["content"]

    def test_share_carries_memory_and_is_recorded(self):
        others = {"Bel": "Heat moves up through the shelves at night.", "Cy": "[Cy failed: timeout]"}
        out = self.rt.ask_kin_to_share(self.kin, ["I wandered through a manual on fans."], others)
        self.assertEqual(out, self.reply)
        self.assertIn("It is now ", self.system_of())
        self.assertIn(MARK, self.system_of())
        (name, q, kw), = self.asked
        self.assertEqual(name, "Ada")
        self.assertIn("manual on fans", q)
        self.assertIn("Heat moves up", q)
        self.assertNotIn("Cy failed", q)
        self.assertEqual(kw.get("wander_limit"), 0, "the day store is already in the prompt")
        (mode, ts, prompt, thought), = rows(self.db)
        self.assertEqual((mode, prompt, thought), ("roundtable", self.user_of(), self.reply))
        self.assertRegex(ts, STAMP)

    def test_response_carries_memory_keyed_on_the_shares_and_is_recorded(self):
        shared = {"Ada": "my own share", "Bel": "Heat rises.", "Cy": "Light falls.",
                  "Di": "[Di returned nothing — done_reason='length']"}
        self.assertEqual(self.rt.ask_kin_to_respond(self.kin, shared), self.reply)
        self.assertIn(MARK, self.system_of())
        (_, q, kw), = self.asked
        self.assertIn("Heat rises.", q)
        self.assertIn("Light falls.", q)
        self.assertNotIn("my own share", q)
        self.assertNotIn("Di returned", q)
        self.assertEqual([r[0] for r in rows(self.db)], ["roundtable"])

    def test_the_query_is_never_empty_and_never_huge(self):
        self.rt.ask_kin_to_share(self.kin, [], {})
        self.rt.ask_kin_to_share(self.kin, ["x" * 5000] * 3, {"Bel": "y" * 5000})
        self.assertEqual(self.asked[0][1], "Ada", "get_context returns early without a query")
        self.assertLessEqual(len(self.asked[1][1]), self.rt.QUERY_MAX)

    def test_memory_failure_falls_back_and_still_speaks(self):
        def boom(*a, **k):
            raise ConnectionError("vault unreachable")
        self.rt._get_context = boom
        self.assertEqual(self.rt.ask_kin_to_share(self.kin, [], {}), self.reply)
        base, clock = self.system_of().split("\n\n")
        self.assertTrue(base.startswith("You are Ada"))
        self.assertTrue(clock.startswith("It is now "))
        self.assertTrue(any("memory unavailable" in m for m in self.logs))
        self.rt._get_context = None
        self.assertEqual(self.rt.ask_kin_to_respond(self.kin, {"Bel": "hi"}), self.reply)
        self.assertEqual(len(rows(self.db)), 2)

    def test_failures_are_never_recorded_as_something_they_said(self):
        self.reply = ConnectionError("no route")
        self.assertTrue(self.rt.ask_kin_to_share(self.kin, [], {}).startswith("[Ada failed"))
        self.assertEqual(self.rt.ask_kin_to_respond(self.kin, {"Bel": "hi"}), "")
        self.reply = "   "
        self.rt.ask_kin_to_share(self.kin, [], {})
        self.assertEqual(rows(self.db), [])

    def test_the_clock_is_portable_and_worded_like_wander(self):
        now = datetime.now()
        hour = now.hour % 12 or 12
        want = f"It is now {now:%A, %B} {now.day}, {now.year} at {hour}:{now:%M} {now:%p}."
        self.assertEqual(self.rt._clock_line(), want)
        # %-d / %-I in a strftime call raise ValueError on Windows
        self.assertNotRegex((SCRIPTS / "roundtable.py").read_text(), r"strftime\([^)]*%-")


if __name__ == "__main__":
    unittest.main()
