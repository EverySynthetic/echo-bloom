"""Test suite configuration.

Redirects application logs during test runs to echo_bloom.test.log so that
test mocks (such as forced RuntimeError failures) and synthetic runs do not
pollute the production log or bury real warnings.
"""

import os
from pathlib import Path

_TEST_LOG = Path.home() / ".local/share/echo_bloom/logs/echo_bloom.test.log"
os.environ["ECHO_BLOOM_LOG_FILE"] = str(_TEST_LOG)
