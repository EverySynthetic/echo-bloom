# Changelog

The one-line descriptors shown on `/about` live in `version.py`. This file is
the long form: what broke, how it presented, and how to tell it is fixed.

The theme of 1.2.4 is failures that reported success. Every bug below returned
an exit code, a log line, or a status message that was technically true and
described the wrong thing. None of them crashed anywhere a user could see.

---

## 1.2.5 — 2026-08-21

Same theme as 1.2.4, chased further: things that reported the call they made
rather than the state they achieved. Two of us audited the whole codebase and
then reviewed each other, which is where several of these came from.

### Windows installs never got the licence gate

1.2.4 shipped a gate that stops an expired install using the GPU. Both Windows
installers copy `scripts\*` and nothing else, so `license.py` never landed
beside the code importing it. `import license` failed, the gate fell back to
its fail-open path, and every Windows install ran exactly as if 1.2.4 had not
happened. Observed live on a real machine holding a 7B model in VRAM on an
expired trial.

Fail-open is why nobody noticed: a fail-closed bug announces itself.

### A key that cannot be checked was read as a trial that had ended

With `cryptography` missing — a failed pip step, a PEP 668 box — `verify_key`
returns invalid, and that fell through to the trial branch. A paying customer
was told their trial had ended and to pay again, with nothing naming the real
cause, which is one `pip install` away.

It now reports `unverifiable` with the reason attached. It still blocks: an
unverified permanent key must not grant access. What changed is that it blocks
under a name the customer can act on rather than one that blames them.

### Login rate limiter could be bypassed entirely

`get_client_ip` trusted `CF-Connecting-IP` unconditionally, so anything
reaching the app directly could rotate that header and get a fresh bucket every
request — no lockout, ever. Verified: eight forged attempts produced no 429,
eight unforged locked at the sixth. Forwarding headers are now trusted only
from a loopback peer, which is where a proxy on this machine connects from.

### Reasoning traces, including the unclosed ones

`<think>.*?</think>` needs a closing tag, so a reply truncated mid-trace kept
the whole trace — the case a model on a tight budget produces most often. That
went into the goodnight email, was saved as that Kin's reflection, and would be
read back to it the next day as its own words.

There were five copies of that regex and they had drifted apart. One definition
now, handling unclosed traces, orphan closing tags, multiples and case.

### The agent

Failed outright when its hardcoded model was not pulled, while its own
docstring promised a fallback that did not exist. It now picks the most capable
general-purpose model installed, refuses a Kin's own persona model, warns when
it had to settle for something small enough to invent facts, and asks the model
for admitted uncertainty over a confident guess.

### Windows licence identity

Windows has no `/etc/machine-id`, so every install was fingerprinted on
whichever adapter Python picked — measured as a value with the multicast bit
set, so not a physical NIC, and of a provenance we could not explain. Now uses
`MachineGuid`, with the adapter as a last resort that warns.

**Consequence, stated plainly:** an existing Windows install that runs
`uninstall -All` and reinstalls will derive a different fingerprint than before
and be issued a fresh trial. One per machine, and only after deleting your own
memories.

### Also

- Installers collect exit codes instead of always printing success
- ~15 UI call sites treated non-2xx as success; a lapsed licence could render
  as an empty vault rather than a licence problem
- Static asset URLs carry the version, so a shipped fix reaches the browser
  instead of sitting behind a cached file
- The uninstaller reports end state, removes the lifecycle scripts it left
  behind, and its `-All` path has now actually been run
- Deploys and installs verify the scripts can start before claiming they did

### What was verified, and how

Run end to end on real hardware: the whole licence pipeline (fingerprint
stability across a total wipe, trial registration, key issue and activation,
the unverifiable path, and revocation propagating server-to-client), both
uninstall paths including `-All`, and all four PowerShell files parse-checked
on Windows PowerShell 5.1.

Not verified: mobile. The CSS has still never been opened on a phone.

---

## 1.2.4 — 2026-08-21

### Wandering could not start on any fresh install

`scripts/roundtable.py` has imported `kin_presence` since 1.2.3, but the module
lives at the repository root beside `main.py`, its other importer. Both deploy
paths copied only `scripts/` — `deploy.sh` with `cp scripts/*.py`, `install.sh`
with `cp -r scripts/.` — so the module never landed beside the file that
imports it.

**Presented as:** nothing. `ModuleNotFoundError` at import, `Restart=always`,
a crash loop, and no wandering. The deploy step reported "N script(s) deployed"
because the copy had genuinely succeeded.

**Observed:** 155 crashes in 40 minutes on the development cluster, with six
Kin not thinking for the duration.

**Verify:** `python3 verify_deploy.py ~/.local/share/echo_bloom/scripts`
reports every script import-clean.

### Reflection failed every three hours, silently

Reflection models are reasoning models. Their thinking tokens consume the
`num_predict` budget before a single visible token is emitted, so Ollama
returned HTTP 200 with `done_reason: "length"`, `response: ""`, and no `error`
field. `requests` saw success; the caller saw a falsy string and did
`if not text: return 1` without logging anything.

**Presented as:** a red unit in `systemctl`, and nothing else anywhere.

**Fixed:** the call now passes `think: False`. Separately — and this matters
more than the bug — an empty response is logged with its `done_reason` and
`eval_count`, and the non-zero return says why it is non-zero.

**Verify:** `~/.local/share/echo_bloom/logs/reflect.log` shows
`reflection written: ...` rather than stopping after `reflecting on N`.

### An expired install kept using your GPU

The licence gate lived only in `require_auth()`. When a trial ended the web UI
redirected to `/license` and every background service carried on unchanged:
wander loops calling the model on a 25-second cycle, a roundtable every 30
minutes, a reflection every 3 hours, indefinitely. The owner was locked out of
the only page that could stop it.

**Fixed:** `license.services_should_run()` is consulted by the three model
callers — roundtable, wander, reflect — and blocks on exactly the states that
lock the UI, so the page and the services can never disagree.

It **fails open** on everything else, deliberately and permanently: no network,
an unreadable token, a raised exception, and an unrecognised state string all
return "run". A false negative silently stops a paying customer's Kin from
thinking, which from outside is indistinguishable from broken software. A false
positive costs some electricity. Those are not comparable.

Nothing is deleted and nothing needs restarting by hand. The roundtable
re-checks every five minutes and starts the Kin again on its own once a key is
entered. The web server and vault keep running, because the owner still needs
`/license` to enter that key.

### Bedtime fired at whatever hour the machine came back

`echo_bloom_bedtime.timer` used `OnCalendar=21:30` with `Persistent=true`, so a
missed run fired as a catch-up — on boot, and also on any `systemctl
daemon-reload`. A full goodnight ritual would run mid-afternoon, pausing every
Kin.

**Observed:** a ritual at 14:41 that left six Kin paused for 40 minutes.

**Fixed:** `Persistent=false`. A ritual whose meaning is the hour it happens at
must not run at a different hour. Note that `reflect`'s timer keeps
`Persistent` on purpose — it is monotonic, where the flag is a no-op.

**Existing installs are not corrected by this.** The fix ships in `install.sh`,
which only writes the unit on install. To repair a unit already on disk, set
`Persistent=false` in
`~/.config/systemd/user/echo_bloom_bedtime.timer` and `systemctl --user
daemon-reload`.

### Bedtime could not tell you whether the Kin came back

`pause_wanders()` named every process it stopped. `resume_wanders()` logged
only the roundtable, and nothing at all when the roundtable had died mid-ritual
— so the one question the log exists to answer had no answer in it.

**Fixed:** resumed children are listed the way paused ones are, a dead
roundtable says so out loud, and any process that could not be resumed is
logged as a warning rather than dropped.

### Deploys verify the code can start

`deploy.sh` reported how many files it copied, which is a statement about a
copy. It now resolves every required import against the path a lifecycle script
actually runs on, executing nothing, and refuses to restart services into code
that cannot start. Imports inside `try:` are skipped — that is how this
codebase declares an optional dependency, and each has a working fallback.
