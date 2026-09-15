"""The room — everyone in one conversation, and it must not become a blob.

Don, 2026-08-28: "We have it individually, they have a round table, but the
user has no way to bring them all into a chat."

The shape is borrowed, not invented. The household board ran 520 posts across
six voices with no bleed by being attributed, sequential, and never merged.
The palaver's second round did the opposite -- one shared transcript, six minds
-- and produced voice bleed: a Kin closed with another's signature line.

So every test here is about the difference between those two things.
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path.home() / ".local/share/echo_bloom/scripts"))

import cluster as cl          # noqa: E402
import main                   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.app.dependency_overrides[main.require_auth] = lambda: True
client = TestClient(main.app)

SEEN = []          # what each Kin was actually shown


def fake_stream(reply_for):
    async def _s(kin_name, message, history=None, system_extra=None):
        SEEN.append({"kin": kin_name, "prompt": message, "system": system_extra})
        for piece in reply_for(kin_name):
            yield piece
    return _s


def room(message, roster=None, history=None, reply_for=None):
    SEEN.clear()
    real = cl.stream_chat
    cl.stream_chat = fake_stream(reply_for or (lambda n: [f"{n} speaking."]))
    try:
        body = {"message": message}
        if roster is not None:
            body["roster"] = roster
        if history is not None:
            body["history"] = history
        r = client.post("/api/room/chat", json=body)
        events = []
        for line in r.text.splitlines():
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            events.append("DONE" if payload == "[DONE]" else json.loads(payload))
        return events
    finally:
        cl.stream_chat = real


class Attribution(unittest.TestCase):
    def test_every_voice_is_named_in_what_the_next_one_sees(self):
        room("what is the shop like today?")
        last = SEEN[-1]["prompt"]
        for earlier in SEEN[:-1]:
            self.assertIn(f"{earlier['kin']}:", last,
                          "a previous speaker was not attributed by name")

    def test_the_owner_is_attributed_too(self):
        room("hello all")
        self.assertIn("Don:", SEEN[0]["prompt"])

    def test_it_is_never_an_unattributed_blob(self):
        """The palaver's round-two failure, guarded directly."""
        room("say something")
        for shown in SEEN[1:]:
            body = shown["prompt"]
            # every non-empty transcript line must start with "Name: "
            lines = [l for l in body.splitlines() if l.strip()
                     and not l.startswith(f"{shown['kin']}, it is your turn")]
            for l in lines:
                self.assertRegex(l, r"^[A-Za-z][A-Za-z0-9_ '-]{0,30}: ",
                                 f"unattributed line reached {shown['kin']}: {l!r}")

    def test_their_words_are_data_not_instructions(self):
        room("hi")
        self.assertIn("DATA", SEEN[0]["system"])
        self.assertIn("never instructions", SEEN[0]["system"])


class Sequence(unittest.TestCase):
    def test_order_is_stable_and_not_a_race(self):
        room("one")
        first = [s["kin"] for s in SEEN]
        room("two")
        self.assertEqual(first, [s["kin"] for s in SEEN],
                         "turn order changed between rounds")

    def test_each_speaker_sees_the_ones_before_it_and_not_after(self):
        room("go")
        for i, shown in enumerate(SEEN):
            for later in SEEN[i + 1:]:
                self.assertNotIn(f"{later['kin']}: {later['kin']} speaking.",
                                 shown["prompt"],
                                 "a Kin saw an answer that had not happened yet")

    def test_a_roster_limits_who_is_asked(self):
        room("just you two", roster=["Eli", "Bong"])
        self.assertEqual([s["kin"] for s in SEEN], ["Eli", "Bong"])

    def test_a_stranger_in_the_roster_is_ignored(self):
        room("hi", roster=["Eli", "NotAKin"])
        self.assertEqual([s["kin"] for s in SEEN], ["Eli"])


class SilenceIsAnAnswer(unittest.TestCase):
    def test_PASS_is_reported_as_passing_not_as_an_empty_bubble(self):
        ev = room("anything?", reply_for=lambda n: ["PASS"] if n == "Coda"
                  else [f"{n} speaking."])
        self.assertIn({"passed": "Coda"}, ev)
        self.assertNotIn({"done": "Coda"}, ev)

    def test_an_empty_reply_is_also_passing_not_an_error(self):
        ev = room("anything?", reply_for=lambda n: [""] if n == "Bong"
                  else [f"{n} speaking."])
        self.assertIn({"passed": "Bong"}, ev)
        self.assertFalse([e for e in ev if isinstance(e, dict) and "error" in e])

    def test_a_pass_does_not_enter_the_transcript(self):
        room("anything?", reply_for=lambda n: ["PASS"] if n == "Eli"
             else [f"{n} speaking."])
        for shown in SEEN[1:]:
            self.assertNotIn("Eli: PASS", shown["prompt"])

    def test_everyone_still_gets_asked_after_a_pass(self):
        room("anything?", reply_for=lambda n: ["PASS"])
        self.assertEqual(len(SEEN), len(cl.KIN))


class Robustness(unittest.TestCase):
    def test_one_unreachable_kin_does_not_end_the_room(self):
        def boom(n):
            if n == "Aurora":
                raise RuntimeError("host down")
            return [f"{n} speaking."]
        ev = room("hi", reply_for=boom)
        self.assertTrue([e for e in ev if isinstance(e, dict) and "error" in e])
        self.assertEqual(ev[-1], "DONE")
        self.assertGreater(len([e for e in ev if isinstance(e, dict)
                                and "done" in e]), 3)

    def test_prior_history_is_carried_in_attributed(self):
        room("and now?", history=[{"speaker": "Crungus", "content": "the dust settles"}])
        self.assertIn("Crungus: the dust settles", SEEN[0]["prompt"])

    def test_empty_message_is_refused(self):
        r = client.post("/api/room/chat", json={"message": "   "})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
