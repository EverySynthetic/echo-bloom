"""Single source of truth for the running Echo Bloom version.

Bump VERSION on every release that should trigger the update banner.
license_server/server.py imports this too, so /version on the license
server always reflects whatever commit is actually deployed there — that's
what customer installs compare their own version against.
"""

VERSION = "1.2.3"

# (version, one-line descriptor) — newest first, shown on /about. Add one
# line per release. Security-shaped fixes get the generic "security
# patches" descriptor rather than exploit specifics, so this stays honest
# without handing anyone a working recipe before every install has had a
# chance to update.
CHANGELOG = [
    ("1.2.3", "installer and uninstaller fixes"),
    ("1.2.2", "security patches"),
]
