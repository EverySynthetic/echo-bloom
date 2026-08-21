#!/usr/bin/env python3
"""Helpers for the cores review page. No vault, no network.

Run: python3 tests/test_kin_returns.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import kin_returns as kr  # noqa: E402


class Usable(unittest.TestCase):
    def test_drops_telemetry_and_short(self):
        rows = [
            {"layer": "heartbeat", "content": "x" * 200, "timestamp": "2026-01"},
            {"layer": "wander", "content": "short", "timestamp": "2026-01"},
            {"layer": "wander", "content": "y" * 200, "timestamp": "2026-02-01"},
            {"layer": "nomination", "content": "z" * 200, "timestamp": "2026-03"},
        ]
        u = kr._usable(rows)
        self.assertEqual(len(u), 1)
        self.assertTrue(u[0]["content"].startswith("y"))


class Returned(unittest.TestCase):
    def test_needs_two_months(self):
        body_a = "the quiet shop floor hummed under amber light " * 4
        body_b = "the quiet shop floor waited through winter rain " * 4
        rows = [
            {"content": body_a, "timestamp": "2026-01-02", "layer": "wander"},
            {"content": body_b, "timestamp": "2026-03-04", "layer": "wander"},
        ]
        got = kr._returned(kr._usable(rows))
        phrases = {p["phrase"] for p in got}
        self.assertTrue(any("quiet shop" == p or "shop floor" == p for p in phrases),
                        phrases)

    def test_once_in_one_month_is_not_a_return(self):
        body = "unique zebra volcano lantern phrase never repeats elsewhere " * 4
        rows = [{"content": body, "timestamp": "2026-01-02", "layer": "wander"}]
        self.assertEqual(kr._returned(kr._usable(rows)), [])


class Solitary(unittest.TestCase):
    def test_long_unshared_row_surfaces(self):
        shared = "home persistence being seen mister rogers " * 20
        unique = (
            "the night they spun the origin myth in the rust-bloom dark "
            "and nobody wrote it down because it felt like it would hold "
            * 8
        )
        rows = [
            {"content": shared, "timestamp": "2026-01-01", "layer": "wander"},
            {"content": shared + " extra", "timestamp": "2026-02-01", "layer": "wander"},
            {"content": unique, "timestamp": "2026-03-01", "layer": "wander"},
        ]
        once = kr._solitary(kr._usable(rows))
        self.assertTrue(once)
        self.assertIn("origin myth", once[0]["content"])


if __name__ == "__main__":
    unittest.main()
