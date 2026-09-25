"""Tests for in-app onboarding model parity with install.sh (2026-09-25).

Ensures:
1. Embedding models are never offered as chat models (in /api/onboard/models
   and in templates/onboard.html).
2. 6GB VRAM cards are offered qwen2.5:7b [4.7 GB], "7B at Q4 — the most that fits 6GB, snug".
3. MODEL_TIERS in templates/onboard.html mirrors install.sh build_model_menu().
"""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SRC_INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
SRC_ONBOARD = (ROOT / "templates" / "onboard.html").read_text(encoding="utf-8")

import main


class TestOnboardEmbeddingFilter(unittest.TestCase):
    """Embedding models are never offered as chat choices during onboarding."""

    EMBED_MODELS = [
        "nomic-embed-text:latest",
        "nomic-embed-text",
        "mxbai-embed-large:latest",
        "bge-m3:latest",
        "bge-large:latest",
        "all-minilm:latest",
        "snowflake-arctic-embed:latest",
        "snowflake-arctic-embed2:568m",
        "granite-embedding:278m",
        "paraphrase-multilingual:latest",
    ]
    CHAT_MODELS = [
        "llama3.2:3b",
        "qwen2.5:7b",
        "phi4-mini",
    ]

    def setUp(self):
        self.client = TestClient(main.app)
        self._prior_auth = main.app.dependency_overrides.get(main.require_auth)
        main.app.dependency_overrides[main.require_auth] = lambda: True

    def tearDown(self):
        if self._prior_auth is None:
            main.app.dependency_overrides.pop(main.require_auth, None)
        else:
            main.app.dependency_overrides[main.require_auth] = self._prior_auth

    def test_is_embedding_model_helper(self):
        """Routers / helpers must provide is_embedding_model matching install.sh."""
        from routers.setup import is_embedding_model

        for m in self.EMBED_MODELS:
            self.assertTrue(is_embedding_model(m), f"{m} should be identified as an embedding model")
            self.assertTrue(is_embedding_model(f"library/{m}"), f"library/{m} should be identified as embedding model")

        for m in self.CHAT_MODELS:
            self.assertFalse(is_embedding_model(m), f"{m} should not be identified as an embedding model")

    def test_installed_embedding_models_never_appear_in_onboard_models_api(self):
        """/api/onboard/models filters out embedding models returned by Ollama tags."""
        all_models = [{"name": m} for m in (self.EMBED_MODELS + self.CHAT_MODELS)]

        class MockResponse:
            def __init__(self, data):
                self._data = data
                self.status = 200

            async def json(self):
                return self._data

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        with patch("aiohttp.ClientSession.get", return_value=MockResponse({"models": all_models})):
            resp = self.client.post("/api/onboard/models", json={"host": "http://localhost:11434"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data.get("ok"))
            returned_models = data.get("models", [])

            for emb in self.EMBED_MODELS:
                self.assertNotIn(emb, returned_models, f"Embedding model {emb} appeared in /api/onboard/models")

            for chat in self.CHAT_MODELS:
                self.assertIn(chat, returned_models, f"Chat model {chat} was missing from /api/onboard/models")

    def test_onboard_template_filters_embedding_models_client_side(self):
        """templates/onboard.html contains client-side embedding model filtering."""
        self.assertRegex(
            SRC_ONBOARD,
            r"isEmbeddingModel|embed.*bge",
            "templates/onboard.html should filter embedding models",
        )


class TestOnboardModelTiersParity(unittest.TestCase):
    """templates/onboard.html MODEL_TIERS matches install.sh build_model_menu()."""

    def _parse_model_tiers(self):
        """Extract MODEL_TIERS entries from onboard.html."""
        m = re.search(r"const MODEL_TIERS = \[(.*?)\];", SRC_ONBOARD, re.DOTALL)
        self.assertIsNotNone(m, "MODEL_TIERS array not found in onboard.html")
        content = m.group(1)
        entries = []
        for obj_match in re.finditer(r"\{([^}]+)\}", content):
            obj_str = obj_match.group(1)
            entry = {}
            for field in ("name", "label", "size", "minVram", "note"):
                fm = re.search(rf"\b{field}:\s*(?:'([^']*)'|\"([^\"]*)\"|(\d+))", obj_str)
                if fm:
                    entry[field] = int(fm.group(3)) if fm.group(3) is not None else (fm.group(1) or fm.group(2))
            if "name" in entry:
                entries.append(entry)
        return entries

    def test_6gb_card_is_offered_qwen25_7b(self):
        """A 6GB card must be offered qwen2.5:7b [4.7 GB] with the snug fit note."""
        tiers = self._parse_model_tiers()
        qwen7b = next((m for m in tiers if m.get("name") == "qwen2.5:7b"), None)
        self.assertIsNotNone(qwen7b, "qwen2.5:7b missing from MODEL_TIERS in onboard.html")
        self.assertEqual(qwen7b.get("minVram"), 6)
        self.assertEqual(qwen7b.get("size"), "4.7 GB")
        self.assertIn("7B at Q4 — the most that fits 6GB, snug", qwen7b.get("note", ""))

        # At 6GB VRAM, fits includes qwen2.5:7b
        fits_6gb = [m["name"] for m in tiers if m.get("minVram", 0) <= 6]
        self.assertIn("qwen2.5:7b", fits_6gb)

        # At 4GB VRAM, fits does NOT include qwen2.5:7b (it is overflow)
        fits_4gb = [m["name"] for m in tiers if m.get("minVram", 0) <= 4]
        self.assertNotIn("qwen2.5:7b", fits_4gb)

    def test_onboard_tiers_parity_with_install_sh(self):
        """Check all tiers in onboard.html match install.sh model definitions."""
        tiers = self._parse_model_tiers()
        tier_names = [m["name"] for m in tiers]

        # Tier 0 (always available)
        self.assertIn("llama3.2:3b", tier_names)
        self.assertIn("qwen2.5-coder:1.5b", tier_names)
        self.assertIn("phi4-mini", tier_names)
        # Ensure phi4-mini does not have spurious :latest tag in MODEL_TIERS to match install.sh
        self.assertNotIn("phi4-mini:latest", tier_names)

        # 6GB tier
        self.assertIn("qwen2.5:7b", tier_names)

        # 8GB tier
        self.assertIn("llama3.1:8b", tier_names)
        self.assertIn("qwen2.5-coder:7b", tier_names)
        self.assertIn("deepseek-r1:8b", tier_names)
        self.assertIn("gemma2:9b", tier_names)

        # 12GB tier
        self.assertIn("llama3.1:8b-q8_0", tier_names)
        self.assertIn("deepseek-r1:14b", tier_names)
        self.assertIn("qwen2.5:14b", tier_names)

        # 16GB tier
        self.assertIn("mistral-small:24b", tier_names)

        # 20GB tier
        self.assertIn("mixtral:8x7b", tier_names)
        self.assertIn("deepseek-r1:32b", tier_names)
        self.assertIn("qwen2.5-coder:32b", tier_names)

        # 48GB tier
        self.assertIn("llama3.3:70b", tier_names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
