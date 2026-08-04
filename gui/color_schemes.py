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


_register("Ember", {
    # Warm charcoal-brown with a glowing amber accent
    "bg_darkest":       "#1c1613",
    "bg_dark":          "#241d18",
    "bg_mid":           "#2e2620",
    "bg_light":         "#3a3029",
    "bg_input":         "#191310",

    "text_primary":     "#e4d9ce",
    "text_secondary":   "#a8988a",
    "text_disabled":    "#6e6054",
    "text_bright":      "#f8f0e6",

    "accent":           "#e08840",
    "accent_hover":     "#f09c55",
    "accent_muted":     "#9e6030",
    # Amber is too light for white selection text — flip to dark.
    "highlight_text":   "#241505",

    "link":             "#6ec4ff",
    "link_hover":       "#9cd8ff",

    "success":          "#63c878",
    "success_hover":    "#78dc8e",
    "warning":          "#e0c05a",
    "error":            "#e25a4e",
    "info":             "#64b5f6",

    "btn_primary":      "#b5661f",
    "btn_primary_hover":"#cc7830",
    "btn_secondary":    "#382e26",
    "btn_secondary_hover":"#46392f",
    "btn_disabled":     "#2c241e",
    "btn_text":         "#ecdfd2",

    "border":           "#3e332a",
    "border_light":     "#4e4034",

    "progress_bg":      "#261f1a",
})


_register("Moss", {
    # Deep forest greens, easy on the eyes
    "bg_darkest":       "#141c16",
    "bg_dark":          "#1a231c",
    "bg_mid":           "#222e24",
    "bg_light":         "#2c3a2e",
    "bg_input":         "#111812",

    "text_primary":     "#d2dcd2",
    "text_secondary":   "#8ea092",
    "text_disabled":    "#58685c",
    "text_bright":      "#eef6ee",

    "accent":           "#58b878",
    "accent_hover":     "#6cd08c",
    "accent_muted":     "#35704a",
    "highlight_text":   "#0f1a13",

    "link":             "#7ac8e8",
    "link_hover":       "#a4dcf2",

    "success":          "#58c878",
    "success_hover":    "#70dc90",
    "warning":          "#d8b44e",
    "error":            "#d85c5c",
    "info":             "#62aede",

    "btn_primary":      "#2e8452",
    "btn_primary_hover":"#3c9862",
    "btn_secondary":    "#263226",
    "btn_secondary_hover":"#303f30",
    "btn_disabled":     "#1e281f",
    "btn_text":         "#dae4da",

    "border":           "#2e3c30",
    "border_light":     "#3c4c3e",

    "progress_bg":      "#18211a",
})


_register("Lagoon", {
    # Dark teal depths with a cyan accent
    "bg_darkest":       "#101b1e",
    "bg_dark":          "#152226",
    "bg_mid":           "#1c2c31",
    "bg_light":         "#26383e",
    "bg_input":         "#0d171a",

    "text_primary":     "#cfdde0",
    "text_secondary":   "#84a0a8",
    "text_disabled":    "#4e6870",
    "text_bright":      "#ecf6f8",

    "accent":           "#3cb8c4",
    "accent_hover":     "#52ccd8",
    "accent_muted":     "#26747e",
    "highlight_text":   "#0a2226",

    "link":             "#7fc8ff",
    "link_hover":       "#a8dcff",

    "success":          "#56c88a",
    "success_hover":    "#6cdaa0",
    "warning":          "#d8b44e",
    "error":            "#de5a62",
    "info":             "#5fb0e8",

    "btn_primary":      "#237e8c",
    "btn_primary_hover":"#2f92a2",
    "btn_secondary":    "#203238",
    "btn_secondary_hover":"#2a4048",
    "btn_disabled":     "#192830",
    "btn_text":         "#d6e4e8",

    "border":           "#263a40",
    "border_light":     "#334c54",

    "progress_bg":      "#142125",
})


_register("Graphite", {
    # Neutral monochrome greys with a gold accent
    "bg_darkest":       "#161616",
    "bg_dark":          "#1d1d1d",
    "bg_mid":           "#262626",
    "bg_light":         "#313131",
    "bg_input":         "#131313",

    "text_primary":     "#d8d8d8",
    "text_secondary":   "#9a9a9a",
    "text_disabled":    "#646464",
    "text_bright":      "#f4f4f4",

    "accent":           "#d8a848",
    "accent_hover":     "#e8bc5e",
    "accent_muted":     "#8a6c2e",
    "highlight_text":   "#231a06",

    "link":             "#78c0f0",
    "link_hover":       "#a2d4f6",

    "success":          "#5ec878",
    "success_hover":    "#74da8c",
    "warning":          "#e08840",
    "error":            "#e05252",
    "info":             "#64b0e8",

    "btn_primary":      "#96742c",
    "btn_primary_hover":"#aa8638",
    "btn_secondary":    "#2c2c2c",
    "btn_secondary_hover":"#383838",
    "btn_disabled":     "#232323",
    "btn_text":         "#e0e0e0",

    "border":           "#333333",
    "border_light":     "#424242",

    "progress_bg":      "#1c1c1c",
})


# ---------------------------------------------------------------------------
# Default scheme
# ---------------------------------------------------------------------------

DEFAULT_SCHEME = "Dusk"

# Names registered above — everything else in SCHEMES is user-defined.
# Frozen at import time, before any custom scheme can be registered.
BUILTIN_SCHEMES: frozenset[str] = frozenset(SCHEMES)

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
