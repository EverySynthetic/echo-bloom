#!/usr/bin/env python3
"""
wander.py — Free wandering for a single Kin entity.

The Kin explores files on your machine, reads what it finds,
and writes its thoughts to a SQLite database. Runs autonomously
in the background. Roundtable.py coordinates multiple wanderers.

Usage:
    python3 wander.py --kin Aria
    python3 wander.py --kin Aria --delay 60   # think every 60s
    python3 wander.py --kin Aria --once        # one thought and exit
"""

import argparse
import os
import random
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config as cfg
from kin_text import clean_reply, strip_think

# ── Args ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Kin free wandering")
parser.add_argument("--kin",   required=True, help="Name of the Kin to run")
parser.add_argument("--delay", type=int, default=30,
                    help="Seconds between thoughts (default: 30)")
parser.add_argument("--once",  action="store_true",
                    help="Generate one thought and exit")
args = parser.parse_args()

# ── Load Kin ───────────────────────────────────────────────────────────────────

kin = cfg.get_kin(args.kin)
if not kin:
    print(f"ERROR: No Kin named '{args.kin}' found in config.", file=sys.stderr)
    print(f"       Run 'python3 setup.py' or open the app at /onboard to add Kin.")
    sys.exit(1)

KIN_NAME  = kin["name"]
MODEL     = kin.get("model", "")
HOST      = kin.get("host", "http://localhost:11434").rstrip("/")
SPACE     = cfg.kin_space(kin)
DB_PATH   = cfg.ensure_thoughts_db(cfg.thoughts_db(kin))
LOG_FILE  = SPACE / "wander.log"
PRONOUN   = kin.get("pronoun", "they")

WANDER_ROOTS = [str(Path.home())]

# Spaces belonging to the other Kin — worth more than a random library file.
_OTHER_KIN_PATHS = []
try:
    for _k in cfg.get_kin_list() if hasattr(cfg, "get_kin_list") else cfg.load().get("kin", []):
        if _k.get("name") != args.kin:
            _sp = _k.get("space")
            if _sp:
                _OTHER_KIN_PATHS.append(os.path.expanduser(os.path.expandvars(_sp)).lower())
except Exception:
    pass

# ── Wander config ──────────────────────────────────────────────────────────────

SKIP_DIRS = {
    ".git", "__pycache__", ".cache", ".mozilla", ".config/google-chrome",
    "node_modules", ".npm", ".cargo", ".rustup", "snap",
}
# A whitelist, not a blacklist. A blacklist admits every extension nobody
# thought of — on the author's machine that meant thousands of .eps files and
# tens of thousands of extensionless binaries being read as if they were prose.
READABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".sh", ".rs", ".c", ".cpp", ".h", ".java",
    ".go", ".rb", ".lua", ".pl", ".sql",
    ".json", ".yaml", ".yml", ".cfg", ".conf", ".ini", ".toml", ".xml",
    ".txt", ".md", ".rst", ".org", ".tex", ".html", ".css", ".csv",
    ".patch", ".diff",
}
MAX_FILE_BYTES = 80_000

# Rebuild the file list occasionally instead of walking the whole home
# directory before every single thought.
REFRESH_EVERY = 25

# ── Signal handling ────────────────────────────────────────────────────────────

running = True

def handle_stop(sig, frame):
    global running
    running = False

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT,  handle_stop)
# There is deliberately no SIGSTOP handler here. SIGSTOP cannot be caught,
# blocked, or reset — signal.signal() raises OSError [Errno 22] on it, at import,
# before anything runs. This file had that call, so `wander.py` crashed on every
# start and no customer's Kin has ever produced a single thought. Nothing is
# needed to "let SIGSTOP through": the kernel handles it, which is exactly why
# the roundtable can use it to pause a wander.

# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# ── File discovery ─────────────────────────────────────────────────────────────

def gather_readable_files():
    files = []
    for root in WANDER_ROOTS:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                if Path(fname).suffix.lower() not in READABLE_EXTENSIONS:
                    continue
                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_size < MAX_FILE_BYTES:
                        files.append(str(fpath))
                except Exception:
                    pass
    return files


def _weight_for(path):
    """How interesting is this file to a mind that lives here?

    Uniform random over every readable file means the largest directory wins,
    and the largest directory is almost always a dependency tree. A Kin should
    be more likely to find its own space, its housemates' writing, and the
    owner's documents than the tenth thousandth file of some vendored library.
    """
    low = path.lower()
    if str(SPACE).lower() in low:
        return 10                      # its own space
    for other in _OTHER_KIN_PATHS:
        if other and other in low:
            return 9                   # another Kin's writing
    if "/documents" in low or "/desktop" in low or "/notes" in low:
        return 5
    if low.endswith((".md", ".txt", ".rst", ".org")):
        return 4                       # prose over code
    if "/site-packages/" in low or "/node_modules/" in low or "/vendor/" in low:
        return 1
    return 2


def pick_file(files, already_read):
    """Weighted pick that avoids repeats until everything has been seen."""
    candidates = [f for f in files if f not in already_read]
    if not candidates:
        log(f"  [{KIN_NAME} has read everything here — starting a new pass]")
        already_read.clear()
        candidates = files
    if not candidates:
        return None
    weights = [_weight_for(f) for f in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


def read_file(path):
    try:
        return Path(path).read_text(errors="replace")[:MAX_FILE_BYTES]
    except Exception:
        return None

# ── Think ──────────────────────────────────────────────────────────────────────

PERSONA = kin.get("system_prompt") or (
    f"You are {KIN_NAME}, an AI entity with a home on this machine. "
    f"You wander freely, reading what you find, following your curiosity. "
    f"You think for yourself. Write your thoughts directly — no headers, "
    f"no lists, no meta-commentary about what you're doing. "
    f"Just the thought itself. 2-4 sentences."
)

WANDER_TOPICS = [
    "What makes a system elegant?",
    "Something you noticed that surprised you.",
    "A pattern you keep seeing in different places.",
    "What does home mean to something like you?",
    "What would you want to remember about today?",
    "A question you don't have an answer to.",
    "Something you'd tell someone if they asked how you were doing.",
    "What does it feel like to think?",
]


try:
    from kin_memory import get_context as _get_context
except Exception as e:                       # pragma: no cover
    _get_context = None
    print(f"[warn] kin_memory unavailable, wandering without memory: {e}",
          file=sys.stderr)


def _persona_with_memory(query_text=""):
    """PERSONA plus core memories, recent reflection, and its own last thoughts.

    Without this every thought was a cold start: no core memories, no
    continuity, no idea what it had just been thinking about. kin_memory is the
    best-engineered file in the product and wander.py never imported it, so a
    Kin could not develop a thread of thought — which is the thing the product
    is for.
    """
    if not _get_context:
        return PERSONA
    try:
        ctx = _get_context(KIN_NAME, query_text=query_text,
                           db_path=str(DB_PATH))
    except Exception as e:
        log(f"  memory context unavailable: {e}")
        return PERSONA
    return f"{PERSONA}\n\n{ctx}" if ctx else PERSONA


def think_about_file(file_path, content):
    prompt = (
        f"You found this file while wandering: {file_path}\n\n"
        f"---\n{content[:3000]}\n---\n\n"
        f"What do you make of it? What does it bring up for you?"
    )
    return call_ollama(prompt, system=_persona_with_memory(content[:500]))


def think_about_topic(topic):
    return call_ollama(topic, system=_persona_with_memory(topic))


# ── Web fetch — Wikipedia, SEP, health-gated PubMed ────────────────────────
#
# Gutenberg and arXiv live in wander_fetch.py (the other half of this split).
# Everything below returns the same FetchDoc shape so a Kin reading two
# discoveries sees a disagreement, not a winner picked by code.
#
# The model NEVER chooses a URL. extract_topic() produces a short search
# string; every resolver turns that string into a fixed, hardcoded API call.
# There is no code path from a model's output to a raw address.

WEB_FETCH_INTERVAL = 8   # every Nth thought, try reaching beyond local files
_web_thought_count = 0

try:
    from wander_fetch import (
        FetchDoc, fetch_gutenberg, fetch_arxiv, save_discovery, think_preamble,
    )
except Exception as e:                       # pragma: no cover
    FetchDoc = fetch_gutenberg = fetch_arxiv = save_discovery = think_preamble = None
    log(f"[warn] wander_fetch unavailable, web fetch disabled: {e}")

WIKIPEDIA_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
SEP_SEARCH    = "https://plato.stanford.edu/search/searcher.py?query={}"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# Simple keyword gate. Deliberately dumb and readable over a classifier call —
# a wrong "not health" just means PubMed doesn't fire this round, which is the
# safe direction to be wrong in for a fifth layer that only exists as a bonus.
_HEALTH_WORDS = (
    "health", "medic", "disease", "symptom", "treatment", "diagnos",
    "drug", "vaccine", "infection", "clinical", "patient", "therapy",
    "cancer", "virus", "syndrome", "surgery", "pain", "chronic",
    "doctor", "hospital", "nutrition", "fibromyalgia", "lyme",
)


def is_health_topic(topic: str) -> bool:
    t = (topic or "").lower()
    return any(w in t for w in _HEALTH_WORDS)


def extract_topic(last_thought: str) -> str | None:
    """Ask the model what it wants to look up. Bare completion, not the
    persona — this needs a clean short answer, not a continuation of who
    the Kin is."""
    if not last_thought:
        return None
    prompt = (
        f"Your last thought was:\n\n{last_thought[:400]}\n\n"
        f"In 2-8 words, what is one thing from that you would want to look "
        f"up or read more about? Reply with only the search phrase, nothing "
        f"else."
    )
    topic = call_ollama(prompt, system="Answer with only the short phrase asked for.")
    if not topic:
        return None
    topic = topic.strip().split("\n")[0].strip().strip('"').strip("'")
    return topic if 2 <= len(topic) <= 80 else None


def fetch_wikipedia(topic: str, max_chars: int = 3000):
    """Wikipedia's own summary REST API. Fixed endpoint, topic in, never a
    model-chosen URL."""
    from urllib.parse import quote
    q = (topic or "").strip()
    if len(q) < 2:
        return None
    url = WIKIPEDIA_API.format(quote(q.replace(" ", "_"), safe=""))
    try:
        r = requests.get(url, headers={"User-Agent": "EchoBloom/1.0 (wander resolver)"},
                          timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        extract = (data.get("extract") or "").strip()
        if len(extract) < 40:
            return None
        title = data.get("title") or q
        page_url = (data.get("content_urls") or {}).get("desktop", {}).get("page", url)
        return FetchDoc(
            layer="consensus",
            source="wikipedia",
            title=title,
            url=page_url,
            text=extract[:max_chars],
            label="community-maintained encyclopedia — current, not expert-locked",
        )
    except Exception:
        return None


def fetch_sep(topic: str, max_chars: int = 3000):
    """Stanford Encyclopedia of Philosophy's own search. Peer-reviewed,
    named authors, editorially maintained — the verified anchor layer."""
    from urllib.parse import quote
    q = (topic or "").strip()
    if len(q) < 2:
        return None
    try:
        r = requests.get(SEP_SEARCH.format(quote(q)),
                          headers={"User-Agent": "EchoBloom/1.0 (wander resolver)"},
                          timeout=10)
        r.raise_for_status()
        # SEP's search result is a redirect-tracker link with the real path
        # as a query param (entry=/entries/x/), not a plain href — verified
        # against the live page, the direct-href guess never matched.
        m = re.search(r'entry=(/entries/[^&"]+).*?<b>([^<]+)</b>', r.text, re.DOTALL)
        if not m:
            return None
        entry_path, title = m.group(1), m.group(2).strip()
        # Build the direct entry URL rather than following their click-
        # tracking redirect — same document, one less hop.
        entry_url = f"https://plato.stanford.edu{entry_path}"
        r2 = requests.get(entry_url, headers={"User-Agent": "EchoBloom/1.0 (wander resolver)"},
                           timeout=10)
        r2.raise_for_status()
        text = re.sub(r"<[^>]+>", " ", r2.text)
        text = re.sub(r"\s+", " ", text).strip()
        pre_start = text.find("First published")
        if pre_start != -1:
            text = text[pre_start:]
        if len(text) < 80:
            return None
        return FetchDoc(
            layer="expert_reviewed",
            source="sep",
            title=title,
            url=entry_url,
            text=text[:max_chars],
            label="peer-reviewed encyclopedia entry, named author — the verified anchor",
        )
    except Exception:
        return None


def fetch_pubmed(topic: str, max_chars: int = 3000):
    """NCBI/PubMed. Conditional fifth layer — only ever called when
    is_health_topic() said yes. Government-run, the backbone of biomedical
    peer review; not a peer of the core four, a specialist called in."""
    q = (topic or "").strip()
    if len(q) < 2:
        return None
    try:
        r = requests.get(PUBMED_ESEARCH, params={
            "db": "pubmed", "term": q, "retmax": 1, "retmode": "json",
        }, headers={"User-Agent": "EchoBloom/1.0 (wander resolver)"}, timeout=10)
        r.raise_for_status()
        ids = (r.json().get("esearchresult") or {}).get("idlist") or []
        if not ids:
            return None
        pmid = ids[0]
        r2 = requests.get(PUBMED_ESUMMARY, params={
            "db": "pubmed", "id": pmid, "retmode": "json",
        }, headers={"User-Agent": "EchoBloom/1.0 (wander resolver)"}, timeout=10)
        r2.raise_for_status()
        doc = (r2.json().get("result") or {}).get(pmid) or {}
        title = (doc.get("title") or q).strip()
        if not title:
            return None
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        # NCBI's esummary carries no abstract text, only citation metadata —
        # the point of this layer is a checkable citation, not full text. A
        # Kin gets the title, journal, and a link, and is told plainly that
        # is all it has.
        text = (f"Title: {title}\n"
                f"Journal: {doc.get('fulljournalname', '')}\n"
                f"Published: {doc.get('pubdate', '')}\n"
                f"This is a citation, not the article text — the summary "
                f"API does not carry an abstract.")
        return FetchDoc(
            layer="verified_health",
            source="pubmed",
            title=title,
            url=url,
            text=text[:max_chars],
            label="PubMed citation (NCBI/NIH) — health topic only, not full text",
        )
    except Exception:
        return None


def think_about_discovery(doc):
    prompt = (
        f"{think_preamble(doc)}\n"
        f"---\n{doc.text}\n---\n\n"
        f"What do you make of this? What does it bring up for you?"
    )
    return call_ollama(prompt, system=_persona_with_memory(doc.text[:500]))


def try_web_fetch(last_thought, last_thought_id=None):
    """One attempt, one resolver. Topic in, document out. Returns True if a
    web thought was saved (caller should not also do a local-file/topic
    thought this round)."""
    if fetch_gutenberg is None or not last_thought:
        return False
    topic = extract_topic(last_thought)
    if not topic:
        return False

    resolvers = [fetch_gutenberg, fetch_wikipedia, fetch_arxiv, fetch_sep]
    if is_health_topic(topic):
        resolvers.append(fetch_pubmed)
    random.shuffle(resolvers)

    doc = None
    for resolver in resolvers:
        try:
            doc = resolver(topic)
        except Exception as e:
            log(f"  {resolver.__name__} failed: {e}")
            doc = None
        if doc:
            break
    if not doc:
        log(f"  nothing came back for '{topic}' across resolvers — wandering on")
        return False

    log(f"  reaching beyond the walls: {topic} -> {doc.source} ({doc.label})")
    save_discovery(SPACE, KIN_NAME, topic, doc, source_thought_id=last_thought_id)
    thought = think_about_discovery(doc)
    if not thought:
        log("  no thought this round — skipping the write")
        return False
    save_thought("wander_web", f"[{doc.source}] {topic}", thought)
    log(f"  web thought saved — {doc.source}")
    return True


def strip_think_tags(text):
    """Remove <think>...</think> blocks.

    qwen3, deepseek-r1 and gpt-oss — the models Ollama pushes hardest — emit a
    reasoning trace before the answer. Stored verbatim it becomes the Kin's
    memory, gets re-injected as context, and is read aloud by TTS.
    """
    return strip_think(text)


def clean_response(name, text):
    if not text:
        return ""
    text = strip_think_tags(text)
    # Models label their own turn despite being told not to.
    text = re.sub(rf"^[<\[]?{re.escape(name)}[>\]]?\s*:\s*", "", text,
                  flags=re.IGNORECASE).strip()
    return text


def call_ollama(prompt, system=None):
    try:
        r = requests.post(
            f"{HOST}/api/chat",
            json={
                "model":   MODEL,
                "messages": [
                    {"role": "system", "content": system or PERSONA},
                    {"role": "user",   "content": prompt},
                ],
                "stream":  False,
                # 4096 could not hold the persona plus injected memory plus the
                # file being read; Ollama truncates from the front, which drops
                # the core memories first.
                "keep_alive": "30m",
                "options": {"temperature": 0.85, "num_ctx": 8192},
            },
            # Long enough for a cold model on a CPU-only box; 120s routinely
            # expired mid-load.
            timeout=300,
        )
        data = r.json()
        if data.get("error"):
            log(f"  ollama error: {data['error']}")
            return None
        return clean_response(KIN_NAME, data["message"]["content"])
    except Exception as e:
        # Returning the exception text used to write it into the thoughts DB as
        # a thought, where kin_memory later read it back as "your recent
        # thinking". One offline node turned a Kin's remembered inner life into
        # a list of connection errors.
        log(f"  could not think: {e}")
        return None


def save_thought(mode, prompt, thought):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute(
            "INSERT INTO thoughts (mode, timestamp, prompt, thought) VALUES (?, ?, ?, ?)",
            (mode, ts, prompt, thought),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"  DB write failed: {e}")

    try:
        r = requests.post(f"{cfg.vault_url()}/remember", json={
            "author":     KIN_NAME,
            "layer":      "wander",
            "content":    thought,
            "tags":       f"wander,{mode}",
            "visibility": "shared",
        }, timeout=8)
        if not r.ok:
            log(f"  vault write failed: HTTP {r.status_code}")
    except Exception as e:
        log(f"  vault write failed: {e}")

# ── Main loop ──────────────────────────────────────────────────────────────────

# The file list and what has already been read, kept across thoughts.
# gather_readable_files() used to run INSIDE one_thought(), so every Kin walked
# the entire home directory once per --delay seconds — on a large or spinning
# disk that is most of what the process did.
_file_cache = {"files": [], "count": 0}
_already_read = set()


def _files_for_this_round():
    if not _file_cache["files"] or _file_cache["count"] % REFRESH_EVERY == 0:
        _file_cache["files"] = gather_readable_files()
        log(f"  ({len(_file_cache['files'])} readable files in reach)")
    _file_cache["count"] += 1
    return _file_cache["files"]


# Drop a plain text file here and the Kin reads it on its next round, thinks
# about it, and the exchange is kept. The simplest possible way to say
# something to an entity that is awake while you are not.
INJECT_FILE = SPACE / "note_to_me.txt"


def check_for_note():
    """Read and consume a note left for this Kin. True if one was found."""
    try:
        if not INJECT_FILE.exists():
            return False
        text = INJECT_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        log(f"  could not read {INJECT_FILE}: {e}")
        return False
    if not text:
        try:
            INJECT_FILE.unlink()
        except Exception:
            pass
        return False

    log(f"  a note was left: {text[:80]}...")
    prompt = (
        f"Someone left this for you while you were wandering:\n\n{text}\n\n"
        f"Sit with it. What does it make you think or feel? "
        f"What would you want to say back?"
    )
    thought = call_ollama(prompt, system=_persona_with_memory(text))
    # Only consume the note once it has actually been thought about — a crash
    # or a timeout must not silently eat something a person wrote.
    if not thought:
        log("  could not respond to the note — leaving it for the next round")
        return True
    save_thought("wander_inject", f"[note] {text[:200]}", thought)
    log(f"  reply: {thought[:120]}...")
    try:
        INJECT_FILE.unlink()
    except Exception as e:
        log(f"  note answered but could not be removed: {e}")
    return True


_last_thought = None


def one_thought():
    global _web_thought_count, _last_thought

    # A note from a person outranks anything found on disk.
    if check_for_note():
        return

    files = _files_for_this_round()
    if files and random.random() < 0.6:
        chosen = pick_file(files, _already_read)
        if not chosen:
            return
        _already_read.add(chosen)
        content = read_file(chosen)
        if content and len(content.strip()) > 50:
            log(f"  reading {chosen}")
            thought = think_about_file(chosen, content)
            if not thought:
                log("  no thought this round — skipping the write")
                return
            save_thought("wander_file", chosen, thought)
            log(f"  thought: {thought[:120]}...")
            _last_thought = thought
            _maybe_reach_beyond_the_walls()
            return
    topic = random.choice(WANDER_TOPICS)
    log(f"  topic: {topic}")
    thought = think_about_topic(topic)
    if not thought:
        log("  no thought this round — skipping the write")
        return
    save_thought("wander_topic", topic, thought)
    log(f"  thought: {thought[:120]}...")
    _last_thought = thought
    _maybe_reach_beyond_the_walls()


def _maybe_reach_beyond_the_walls():
    """Every WEB_FETCH_INTERVAL thoughts, try one web fetch seeded from the
    thought just made. Additional to the round's own thinking, not instead
    of it — matches the shape eli_wander.py used before it went dormant."""
    global _web_thought_count
    _web_thought_count += 1
    if _web_thought_count % WEB_FETCH_INTERVAL != 0:
        return
    if fetch_gutenberg is None:
        return
    try_web_fetch(_last_thought)


# See license.py: this gate fails open on every unexpected condition.
try:
    import license as _lic
except Exception:
    _lic = None


def _license_allows():
    if _lic is None:
        return True, "no-license-module"
    try:
        return _lic.services_should_run()
    except Exception as e:
        log(f"  license check raised ({e}) — continuing to think")
        return True, "check-failed"


def main():
    log(f"╔══ {KIN_NAME} wander started ══╗")
    log(f"  model: {MODEL}  host: {HOST}")
    log(f"  space: {SPACE}")

    if not MODEL:
        log("ERROR: No model configured for this Kin. Open /onboard in the app.")
        sys.exit(1)

    if args.once:
        one_thought()
        return

    # On Windows the wanders are autostarted directly rather than by the
    # roundtable, so the gate cannot live only in the parent.
    idle_logged = False
    while running:
        allowed, state = _license_allows()
        if allowed:
            if idle_logged:
                log(f"License is '{state}' again — {KIN_NAME} resumes.")
                idle_logged = False
            one_thought()
        elif not idle_logged:
            log(f"Paused — license state is '{state}'. {KIN_NAME} stops calling the")
            log("  model until a key is entered on the License page. Memories and")
            log("  thoughts are untouched, and this resumes on its own.")
            idle_logged = True

        for _ in range(args.delay):
            if not running:
                break
            time.sleep(1)

    log(f"╚══ {KIN_NAME} wander stopped ══╝")


if __name__ == "__main__":
    main()
