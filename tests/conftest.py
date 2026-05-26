"""Add the repo root to sys.path so tests can `import mac_cookie_sources`
etc. without an editable install."""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
