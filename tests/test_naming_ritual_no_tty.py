"""The installer runs the naming ritual with no keyboard (stdin at EOF).
The Kin's self-chosen name must survive that (A15, 2026-09-24)."""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
# The script parses its arguments at import; give it the ones the installer passes.
with mock.patch.object(sys, "argv", ["naming_ritual.py", "--model", "test-model"]):
    import naming_ritual  # noqa: E402


class NoKeyboard(unittest.TestCase):
    def test_the_kin_keeps_the_name_it_chose_when_stdin_is_closed(self):
        answers = iter(["I'll call myself Nova. A fresh start.", "Nova", "she", "I like quiet mornings."])
        with mock.patch.object(naming_ritual, "ask", lambda messages: next(answers)), \
             mock.patch("sys.stdin", io.StringIO("")), \
             mock.patch("sys.stdout", io.StringIO()):
            result = naming_ritual.run_ritual()
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Nova")


if __name__ == "__main__":
    unittest.main()
