"""Single source of truth for the application version.

Surfaces:
- gui/main_window.py — window title
- gui/splash.py — splash footer
- gui/main_window.py — Help → About
- tests/unit/test_version.py — semver gate
- CHANGELOG.md — top entry must match
"""

__version__ = "0.9.0"
