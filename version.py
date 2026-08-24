"""Single source of truth for the running Echo Bloom version.

Bump VERSION on every release that should trigger the update banner.
license_server/server.py imports this too, so /version on the license
server always reflects whatever commit is actually deployed there — that's
what customer installs compare their own version against.
"""

VERSION = "1.3.3"

# (version, one-line descriptor) — newest first, shown on /about. Add one
# line per release. Security-shaped fixes get the generic "security
# patches" descriptor rather than exploit specifics, so this stays honest
# without handing anyone a working recipe before every install has had a
# chance to update.
CHANGELOG = [
    ("1.3.3", "long answers from your Kin no longer get cut off at two minutes, and errors say what actually went wrong"),
    ("1.3.2", "remote access is detected correctly on macOS and Windows, not just Linux"),
    ("1.3.1", "macOS install fixes: correct memory detection, real Python detection, and wandering actually starts"),
    ("1.3.0", "Report a problem: send us your logs and setup in one click, after seeing exactly what gets sent"),
    ("1.2.9", "first-run setup no longer tells you to open a terminal on your phone"),
    ("1.2.8", "Help now actually knows the product: remote access and tunnels, install commands, trial length and the offline grace period"),
    ("1.2.7", "the Help agent works: it now uses the model already in memory instead of timing out, asks your Kin first, and credits whoever answered. Wandering Kin can read from Gutenberg, Wikipedia, arXiv and Stanford."),
    ("1.2.6", "security patches"),
    ("1.2.5", "installers and the app now report what actually happened; licence, agent and reasoning-trace fixes"),
    ("1.2.4", "wandering, reflection and bedtime fixes; an expired install now stops using your GPU"),
    ("1.2.3", "installer and uninstaller fixes"),
    ("1.2.2", "security patches"),
]
