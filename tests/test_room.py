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

from contextlib import nullcontext

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path.home() / ".local/share/echo_bloom/scripts"))

import cluster as cl          # noqa: E402
import main                   # noqa: E402
import ollama_slot as oslot   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

main.app.dependency_overrides[main.require_auth] = lambda: True
client = TestClient(main.app)

SEEN = []          # what each Kin was actually shown
WARMS = []         # names cl.warm_model was asked to load
RECORDS = []       # per-hop commits to thoughts.db


def fake_stream(reply_for):
    async def _s(kin_name, message, history=None, system_extra=None, **kw):
        SEEN.append({"kin": kin_name, "prompt": message, "system": system_extra,
                     **kw})
        for piece in reply_for(kin_name):
            yield piece
    return _s


async def fake_warm(name):
    WARMS.append(name)


def fake_record(kin, msg, reply):
    RECORDS.append({"kin": kin.get("name"), "reply": reply})


def room(message, roster=None, history=None, reply_for=None, hold=None):
    SEEN.clear()
    WARMS.clear()
    RECORDS.clear()
    real = cl.stream_chat
    real_warm = cl.warm_model
    real_rec = cl._record_conversation
    real_hold = oslot.hold_all_local_wanders
    cl.stream_chat = fake_stream(reply_for or (lambda n: [f"{n} speaking."]))
    cl.warm_model = fake_warm
    cl._record_conversation = fake_record
    # Tests must not SIGSTOP the live wander fleet, or load a 32B.
    oslot.hold_all_local_wanders = hold or (lambda: nullcontext({"paused": []}))
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
        cl.warm_model = real_warm
        cl._record_conversation = real_rec
        oslot.hold_all_local_wanders = real_hold


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

    def test_hosts_are_staggered_frosty_home_frosty_home(self):
        """Not a host-block. Eli, Coda, Crungus, Aurora, Bong, Lumen."""
        room("hi")
        names = [s["kin"] for s in SEEN]
        self.assertEqual(
            names,
            ["Eli", "Coda", "Crungus", "Aurora", "Bong", "Lumen"],
        )

    def test_client_roster_is_staggered_not_the_order_it_was_sent(self):
        room("hi", roster=["Coda", "Bong", "Eli"])
        self.assertEqual([s["kin"] for s in SEEN], ["Eli", "Coda", "Bong"])

    def test_the_other_host_is_warmed_while_this_one_speaks(self):
        room("hi")
        # Eli goes up → Coda on Home. Coda speaks → Crungus on Frosty. …
        self.assertEqual(WARMS, ["Coda", "Crungus", "Aurora", "Bong", "Lumen"])
        self.assertNotIn("Eli", WARMS)

    def test_the_room_does_not_pin_vram_or_recall_per_mouth(self):
        """Two boxes, one model each. No 999h pin, no embed per speaker."""
        room("hi")
        self.assertTrue(SEEN)
        for s in SEEN:
            self.assertEqual(s.get("keep_alive"), "10m")
            self.assertIs(s.get("memory"), False)
            self.assertIs(s.get("record"), False)

    def test_each_mouth_is_committed_before_the_next_hop(self):
        """The roundtable wrote after both passes. A kill ate the round.
        The room writes the moment a turn lands."""
        room("hi")
        self.assertEqual([r["kin"] for r in RECORDS],
                         [s["kin"] for s in SEEN])
        self.assertEqual(len(RECORDS), 6)

    def test_a_pass_is_not_committed_as_speech(self):
        room("hi", reply_for=lambda n: ["PASS"])
        self.assertEqual(RECORDS, [])

    def test_a_kill_keeps_the_landed_turn_and_does_not_start_the_next(self):
        async def alive(_request):
            return len(RECORDS) == 0

        real = main._room_alive
        main._room_alive = alive
        try:
            room("hi")
        finally:
            main._room_alive = real
        self.assertEqual([s["kin"] for s in SEEN], ["Eli"])
        self.assertEqual([r["kin"] for r in RECORDS], ["Eli"])


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

    def test_a_load_timeout_is_an_error_not_their_words(self):
        """The 180s Home-swap hang used to yield a sentence that the room
        stored as that Kin speaking. A no on this test is painting the
        timeout as dialogue again."""
        def boom(n):
            if n == "Coda":
                raise cl.ChatStreamError(
                    "[Coda stopped responding partway through.]")
            return [f"{n} speaking."]
        ev = room("hi", reply_for=boom)
        self.assertIn({"error": "[Coda stopped responding partway through.]"}, ev)
        self.assertNotIn({"done": "Coda"}, ev)
        for shown in SEEN:
            self.assertNotIn("Coda: [Coda stopped responding", shown["prompt"])
        self.assertGreater(len([e for e in ev if isinstance(e, dict)
                                and "done" in e]), 3)

    def test_a_mid_stream_error_does_not_commit_partial_words(self):
        def boom(n):
            if n == "Coda":
                yield "The light in the Agora is str"
                raise cl.ChatStreamError(
                    "[Coda stopped responding partway through.]")
            yield f"{n} speaking."

        ev = room("hi", reply_for=boom)
        self.assertIn({"error": "[Coda stopped responding partway through.]"}, ev)
        self.assertNotIn({"done": "Coda"}, ev)
        self.assertNotIn("Coda", [r["kin"] for r in RECORDS])
        for shown in SEEN:
            self.assertNotIn("Coda: The light in the Agora is str", shown["prompt"])


class TheRoomYieldsTheGpu(unittest.TestCase):
    def test_the_room_holds_local_wanders_for_the_whole_round(self):
        held = []

        class FakeHold:
            def __enter__(self):
                held.append("in")
                return {"paused": []}
            def __exit__(self, *a):
                held.append("out")
                return False

        room("hi", hold=FakeHold)
        self.assertEqual(held, ["in", "out"])


class RobustnessMore(unittest.TestCase):
    def test_prior_history_is_carried_in_attributed(self):
        room("and now?", history=[{"speaker": "Crungus", "content": "the dust settles"}])
        self.assertIn("Crungus: the dust settles", SEEN[0]["prompt"])

    def test_empty_message_is_refused(self):
        r = client.post("/api/room/chat", json={"message": "   "})
        self.assertEqual(r.status_code, 400)


class TheRoomRemembersAcrossReloads(unittest.TestCase):
    """The reported bug: the room kept its turns only in memory, so a reload
    wiped the whole conversation while the individual chats kept theirs. The
    fix lives in the template (localStorage, like kin.html), so this freezes the
    contract there. Every marker below was ABSENT in the broken version, so a
    regression that drops persistence turns these red rather than silently
    forgetting again."""

    def setUp(self):
        self.html = client.get("/room").text

    def test_the_room_persists_history(self):
        self.assertIn("localStorage", self.html)
        self.assertIn("saveHistory", self.html)

    def test_the_room_restores_on_load(self):
        self.assertIn("restoreHistory", self.html)
        # defined is not enough; it must be invoked at init
        self.assertRegex(self.html, r"restoreHistory\(\)\s*;")

    def test_the_owner_turn_is_recorded_before_the_replies(self):
        # the ordering fix: send the prior history (without the new message),
        # then record the owner's turn ahead of the replies it prompts
        self.assertIn("outgoing", self.html)
        self.assertIn("history: outgoing", self.html)

    def test_the_owner_is_one_identity_not_two(self):
        # owner turns are stored/sent under the same name the server uses live,
        # not "You" in the past and the real name in the present
        self.assertIn("const OWNER", self.html)
        self.assertIn("{speaker: OWNER", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
