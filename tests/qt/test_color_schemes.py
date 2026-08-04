"""Pure-data tests for `gui.color_schemes` (no Qt event loop needed)."""

from __future__ import annotations

import pytest

from gui.color_schemes import (
    BUILTIN_SCHEMES,
    DEFAULT_SCHEME,
    SCHEME_KEYS,
    SCHEMES,
    get_scheme,
    list_scheme_names,
    register_custom_scheme,
)

pytestmark = pytest.mark.qt   # grouped here for organization; no qtbot needed


def test_default_scheme_present():
    assert DEFAULT_SCHEME in SCHEMES


def test_get_scheme_falls_back_to_default():
    fallback = get_scheme("totally-unknown-name")
    assert fallback == SCHEMES[DEFAULT_SCHEME]


def test_get_scheme_named_returns_correct_dict():
    assert get_scheme(DEFAULT_SCHEME) is SCHEMES[DEFAULT_SCHEME]


def test_register_custom_scheme_fills_missing_keys():
    register_custom_scheme("Custom_Test", {"accent": "#abcdef"})
    custom = SCHEMES["Custom_Test"]
    # Every base key must still be present
    for k in SCHEME_KEYS:
        assert k in custom
    assert custom["accent"] == "#abcdef"


def test_register_custom_scheme_drops_unknown_keys():
    """Stored custom schemes from older versions may carry removed tokens;
    they must not be resurrected into the registry."""
    register_custom_scheme(
        "Legacy_Test",
        {"status_ready": "#123456", "highlight": "#654321", "accent": "#abcdef"},
    )
    legacy = SCHEMES.pop("Legacy_Test")
    assert "status_ready" not in legacy
    assert "highlight" not in legacy
    assert legacy["accent"] == "#abcdef"
    assert set(legacy) == set(SCHEME_KEYS)


def test_all_builtin_schemes_have_identical_key_sets():
    assert len(BUILTIN_SCHEMES) >= 8
    for name in BUILTIN_SCHEMES:
        assert set(SCHEMES[name]) == set(SCHEME_KEYS), name


def test_list_scheme_names_sorted():
    names = list_scheme_names()
    assert names == sorted(names)
