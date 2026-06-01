"""
pytest configuration: ensures the project root is on ``sys.path`` so that
``termux_spoke`` package imports work without manual ``sys.path.insert``
in individual test files.
"""
from __future__ import annotations

import os
import sys

# Add the project root to sys.path so tests can import termux_spoke
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)