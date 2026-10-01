"""Pytest bootstrap for the timetracking suite.

Puts the repo root on sys.path so `cogs...` imports resolve regardless of where
pytest is invoked, and points logging at a throwaway file so nothing writes into
the repo during a test run.
"""
import os
import sys
import tempfile

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

os.environ.setdefault("LOG_FILE", os.path.join(tempfile.mkdtemp(), "test.log"))
