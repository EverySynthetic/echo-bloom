"""Single source of truth for the running Echo Bloom version.

Bump VERSION on every release that should trigger the update banner.
license_server/server.py imports this too, so /version on the license
server always reflects whatever commit is actually deployed there — that's
what customer installs compare their own version against.
"""

VERSION = "1.2.1"
