"""The vault has no login, so who can reach it is the whole defence.

Found in the 2026-10-15 clean-box walk: it listened on 0.0.0.0 and answered CORS
with "*", so anyone on the same network, and any web page open in a browser on
the machine, could read or delete every memory. These tests start the real
server and talk to it over real sockets.
"""
import http.client
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

VAULT = Path(__file__).resolve().parent.parent / "scripts" / "vault_server.py"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))          # no packet is sent
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _get(host, port, path="/", headers=None, connect_to=None):
    c = http.client.HTTPConnection(connect_to or host, port, timeout=5)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


@pytest.fixture
def start(tmp_path):
    procs = []

    def _start(*extra):
        port = _free_port()
        p = subprocess.Popen(
            [sys.executable, "-u", str(VAULT), "--port", str(port),
             "--db", str(tmp_path / f"v{port}.db"), *extra],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(p)
        for _ in range(60):
            try:
                _get("127.0.0.1", port)
                return p, port
            except OSError:
                time.sleep(0.25)
        p.kill()
        pytest.fail("vault did not start:\n" + (p.stdout.read() or ""))

    yield _start
    for p in procs:
        p.kill()
        p.wait()


def test_default_is_this_machine_only(start):
    _, port = start()
    status, _, _ = _get("127.0.0.1", port)
    assert status == 200
    lan = _lan_ip()
    if not lan:
        pytest.skip("no LAN address on this machine")
    with pytest.raises(OSError):
        _get(lan, port)


def test_web_page_from_elsewhere_is_refused(start):
    _, port = start()
    status, headers, body = _get("127.0.0.1", port, "/recall/all",
                                 {"Origin": "https://evil.example"})
    assert status == 403
    assert "access-control-allow-origin" not in {k.lower() for k in headers}
    assert b"content" not in body


def test_local_dashboard_origin_still_works(start):
    _, port = start()
    status, headers, _ = _get("127.0.0.1", port, "/recall/all",
                              {"Origin": "http://localhost:8090"})
    assert status == 200
    assert {k.lower(): v for k, v in headers.items()}["access-control-allow-origin"] \
        == "http://localhost:8090"


def test_dns_rebinding_host_is_refused(start):
    _, port = start()
    status, _, _ = _get("127.0.0.1", port, "/recall/all",
                        {"Host": f"rebind.evil.example:{port}"})
    assert status == 403


def test_programs_with_no_origin_are_unaffected(start):
    _, port = start()
    c = http.client.HTTPConnection("localhost", port, timeout=5)
    c.request("POST", "/remember", json.dumps({"content": "hi", "author": "t"}),
              {"Content-Type": "application/json"})
    assert c.getresponse().status == 200
    status, _, body = _get("127.0.0.1", port, "/recall/all")
    assert status == 200 and b'"hi"' in body


def test_sharing_on_a_lan_is_opt_in_and_warns(start):
    lan = _lan_ip()
    if not lan:
        pytest.skip("no LAN address on this machine")
    p, port = start("--host", "0.0.0.0")
    status, _, _ = _get(lan, port)
    assert status == 200
    p.terminate()
    out = p.stdout.read()
    assert "no login" in out
