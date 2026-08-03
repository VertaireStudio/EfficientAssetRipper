"""Color scheme definitions for EfficientAssetRipper.

Each scheme is a simple dict with named color tokens. The active scheme
is selected by name and consumed by ``gui.theme`` to build palettes and
stylesheets.  To add a new scheme, copy an existing dict and tweak the
values — no other code changes needed.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Scheme registry
# ---------------------------------------------------------------------------

SCHEMES: dict[str, dict[str, str]] = {}


def _register(name: str, colors: dict[str, str]) -> None:
    SCHEMES[name] = colors


# ---------------------------------------------------------------------------
# Built-in schemes
# ---------------------------------------------------------------------------

_register("Dusk", {
    # Surfaces
    "bg_darkest":       "#1a1a2e",
    "bg_dark":          "#22223a",
    "bg_mid":           "#2c2c44",
    "bg_light":         "#363650",
    "bg_input":         "#1e1e32",

    # Text
    "text_primary":     "#d8d8e8",
    "text_secondary":   "#9898b0",
    "text_disabled":    "#606078",
    "text_bright":      "#f0f0ff",

    # Accent
    "accent":           "#7b68ee",
    "accent_hover":     "#8f80ff",
    "accent_muted":     "#554d9e",
    "highlight_text":   "#ffffff",

    # Links (kept distinct from accent so they read as clickable)
    "link":             "#82c8ff",
    "link_hover":       "#a8d9ff",

    # Semantic
    "success":          "#5ecd73",
    "success_hover":    "#72e088",
    "warning":          "#e8c44a",
    "error":            "#e85a5a",
    "info":             "#64b5f6",

    # Buttons
    "btn_primary":      "#5a4fcf",
    "btn_primary_hover":"#6e62e0",
    "btn_secondary":    "#3a3a54",
    "btn_secondary_hover":"#484868",
    "btn_disabled":     "#2e2e44",
    "btn_text":         "#e0e0f0",

    # Borders / separators
    "border":           "#3c3c58",
    "border_light":     "#4a4a68",

    # Progress
    "progress_bg":      "#2a2a40",
})


_register("Bloom", {
    # Inspired by oeksound Bloom — warm rosy purples, soft pastels
    "bg_darkest":       "#2a1f2d",
    "bg_dark":          "#332836",
    "bg_mid":           "#3e3242",
    "bg_light":         "#4a3d50",
    "bg_input":         "#261c29",

    "text_primary":     "#e0d4e4",
    "text_secondary":   "#a898b0",
    "text_disabled":    "#6e5e78",
    "text_bright":      "#f8f0fc",

    "accent":           "#c084d8",
    "accent_hover":     "#d09ae8",
    "accent_muted":     "#8a5c9e",
    "highlight_text":   "#ffffff",

    "link":             "#7ad4ff",
    "link_hover":       "#a8e3ff",

    "success":          "#7cc89a",
    "success_hover":    "#92dab0",
    "warning":          "#e0b85a",
    "error":            "#d86070",
    "info":             "#8ab4e0",

    "btn_primary":      "#9a5eb8",
    "btn_primary_hover":"#ae72cc",
    "btn_secondary":    "#3e3242",
    "btn_secondary_hover":"#504458",
    "btn_disabled":     "#342a38",
    "btn_text":         "#e8dced",

    "border":           "#4a3e52",
    "border_light":     "#5c4e66",

    "progress_bg":      "#2e2432",
})


_register("Slate", {
    # Cool blue-grey, close to the original but refined
    "bg_darkest":       "#181c24",
    "bg_dark":          "#1e2430",
    "bg_mid":           "#272e3c",
    "bg_light":         "#313a4a",
    "bg_input":         "#161a22",

    "text_primary":     "#d0d8e0",
    "text_secondary":   "#8898a8",
    "text_disabled":    "#586878",
    "text_bright":      "#f0f4f8",

    "accent":           "#4ea8df",
    "accent_hover":     "#62bcf0",
    "accent_muted":     "#2e6e9e",
    "highlight_text":   "#ffffff",

    "link":             "#69d2e7",
    "link_hover":       "#9ae3ee",

    "success":          "#5ec488",
    "success_hover":    "#72d89c",
    "warning":          "#dab050",
    "error":            "#d65c5c",
    "info":             "#64b0e8",

    "btn_primary":      "#3580b8",
    "btn_primary_hover":"#4894cc",
    "btn_secondary":    "#2a3444",
    "btn_secondary_hover":"#354050",
    "btn_disabled":     "#222a36",
    "btn_text":         "#dce4ec",

    "border":           "#303a4c",
    "border_light":     "#3e4a5e",

    "progress_bg":      "#1c222c",
})


_register("Midnight", {
    # Deep navy/indigo with electric accents
    "bg_darkest":       "#0e1118",
    "bg_dark":          "#141820",
    "bg_mid":           "#1c2230",
    "bg_light":         "#262e40",
    "bg_input":         "#0c0f16",

    "text_primary":     "#c8d0e0",
    "text_secondary":   "#7888a0",
    "text_disabled":    "#4a586e",
    "text_bright":      "#e8f0ff",

    "accent":           "#5b8af0",
    "accent_hover":     "#70a0ff",
    "accent_muted":     "#3c5ea8",
    "highlight_text":   "#ffffff",

    "link":             "#7fc8ff",
    "link_hover":       "#a8d9ff",

    "success":          "#48c878",
    "success_hover":    "#5ee090",
    "warning":          "#d4a840",
    "error":            "#e04858",
    "info":             "#58a4e8",

    "btn_primary":      "#4470d0",
    "btn_primary_hover":"#5884e4",
    "btn_secondary":    "#1e2838",
    "btn_secondary_hover":"#283448",
    "btn_disabled":     "#181e2c",
    "btn_text":         "#d0d8ea",

    "border":           "#242e42",
    "border_light":     "#303c54",

    "progress_bg":      "#141820",
})


# ---------------------------------------------------------------------------
# Default scheme
# ---------------------------------------------------------------------------

DEFAULT_SCHEME = "Dusk"

# All colour token names that every scheme must define
SCHEME_KEYS: list[str] = list(SCHEMES[DEFAULT_SCHEME].keys())


def get_scheme(name: str | None = None) -> dict[str, str]:
    """Return a scheme by name, falling back to the default."""
    if name and name in SCHEMES:
        return SCHEMES[name]
    return SCHEMES[DEFAULT_SCHEME]


def register_custom_scheme(name: str, colors: dict[str, str]) -> None:
    """Register (or overwrite) a user-defined colour scheme.

    Missing tokens are filled from the default scheme; unknown keys
    (e.g. tokens removed in older versions) are dropped.
    """
    base = dict(SCHEMES[DEFAULT_SCHEME])
    base.update({k: v for k, v in colors.items() if k in base})
    SCHEMES[name] = base


def list_scheme_names() -> list[str]:
    """Return all registered scheme names in sorted order."""
    return sorted(SCHEMES.keys())
