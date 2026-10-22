"""/api/pull-model only talks to Ollama.

The route used to POST to "<whatever host the request named>/api/pull", any
address, port or path. These tests pin the narrower rule: scheme + host + port
only, port 11434 or a configured node's own Ollama port, and model names from
Ollama's registry or Hugging Face.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path.home() / ".local/share/echo_bloom/scripts"))

from fastapi import HTTPException   # noqa: E402
import main                         # noqa: E402


class PullTarget(unittest.TestCase):

    def setUp(self):
        self._nodes = main.cl.NODES
        main.cl.NODES = [{"name": "Box", "ip": "192.168.1.50", "ollama_port": 11500}]

    def tearDown(self):
        main.cl.NODES = self._nodes

    def ok(self, raw, want):
        self.assertEqual(main._pull_target(raw), want)

    def bad(self, raw):
        with self.assertRaises(HTTPException) as cm:
            main._pull_target(raw)
        self.assertEqual(cm.exception.status_code, 400)

    def test_default_is_local_ollama(self):
        self.ok(None, "http://localhost:11434")
        self.ok("", "http://localhost:11434")

    def test_typed_address_with_default_port_works(self):
        self.ok("192.168.1.120:11434", "http://192.168.1.120:11434")
        self.ok("http://192.168.1.120", "http://192.168.1.120:11434")
        self.ok("http://192.168.1.120:11434/", "http://192.168.1.120:11434")

    def test_configured_node_port_works_only_for_that_node(self):
        self.ok("http://192.168.1.50:11500", "http://192.168.1.50:11500")
        self.bad("http://192.168.1.51:11500")

    def test_other_ports_refused(self):
        for raw in ("http://192.168.1.120:80", "http://192.168.1.115:6333", "http://localhost:8765"):
            self.bad(raw)

    def test_paths_queries_and_credentials_refused(self):
        for raw in ("http://host:11434/admin", "http://host:11434/?x=1", "http://host:11434/#f",
                    "http://u:p@host:11434", "http://u@host:11434"):
            self.bad(raw)

    def test_other_schemes_refused(self):
        for raw in ("ftp://host:11434", "file:///etc/passwd", "gopher://host:11434"):
            self.bad(raw)

    def test_garbage_refused(self):
        for raw in ("http://", "http://host:notaport", "http://ho st:11434"):
            self.bad(raw)


class PullModelName(unittest.TestCase):

    def ok(self, raw):
        self.assertEqual(main._pull_model_name(raw), raw.strip())

    def bad(self, raw):
        with self.assertRaises(HTTPException) as cm:
            main._pull_model_name(raw)
        self.assertEqual(cm.exception.status_code, 400)

    def test_ordinary_names(self):
        for raw in ("gemma3:12b", "llama3.2", "library/gemma3:4b", "user/model:tag",
                    "hf.co/bartowski/Some-Model-GGUF:Q4_K_M", "registry.ollama.ai/library/gemma3"):
            self.ok(raw)

    def test_unknown_registries_refused(self):
        for raw in ("example.com/ns/model", "10.0.0.5:5000/ns/model", "localhost/ns/model"):
            self.bad(raw)

    def test_shapes_refused(self):
        for raw in ("", "   ", "../model", "/abs/model", "a b", "model;rm", "x" * 201):
            self.bad(raw)


if __name__ == "__main__":
    unittest.main()
