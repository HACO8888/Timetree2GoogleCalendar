"""Map TimeTree label colours onto Google Calendar's 11 fixed event colours.

Google Calendar has no tags, so colour is the only per-event classification
dimension available. TimeTree labels carry an arbitrary RGB value, so we pick the
perceptually nearest Google colour by converting both to CIE L*a*b* and taking the
CIE76 distance. Eleven candidates is few enough that the extra accuracy of
CIEDE2000 buys nothing, and CIE76 stays trivially deterministic — which matters,
because an unstable mapping would rewrite every event on every run.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Google Calendar's default event palette, used when colors.get is unavailable.
# The live values are fetched at runtime; this keeps the mapper testable offline.
DEFAULT_EVENT_COLORS: dict[str, str] = {
    "1": "#a4bdfc",   # Lavender
    "2": "#7ae7bf",   # Sage
    "3": "#dbadff",   # Grape
    "4": "#ff887c",   # Flamingo
    "5": "#fbd75b",   # Banana
    "6": "#ffb878",   # Tangerine
    "7": "#46d6db",   # Peacock
    "8": "#e1e1e1",   # Graphite
    "9": "#5484ed",   # Blueberry
    "10": "#51b749",  # Basil
    "11": "#dc2127",  # Tomato
}


def parse_hex(value: str) -> tuple[int, int, int] | None:
    """Parse '#rrggbb' or 'rrggbb' into an (r, g, b) tuple, or None if unparseable."""
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _srgb_to_linear(channel: float) -> float:
    """Undo the sRGB transfer function for one 0..1 channel."""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Convert 8-bit sRGB to CIE L*a*b* under a D65 white point."""
    r, g, b = (_srgb_to_linear(c / 255.0) for c in rgb)

    # sRGB (D65) to XYZ, then normalise by the D65 reference white.
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy) - 16, 500 * (fx - fy), 200 * (fy - fz)


def nearest_color_id(hex_color: str, palette: dict[str, str] | None = None) -> str | None:
    """Return the Google colorId whose colour is perceptually nearest to hex_color.

    Returns None when the input is unparseable, so callers can simply omit colorId
    and let Google fall back to the calendar's default colour.
    """
    rgb = parse_hex(hex_color)
    if rgb is None:
        return None

    target = rgb_to_lab(rgb)
    palette = palette or DEFAULT_EVENT_COLORS

    best_id, best_distance = None, float("inf")
    for color_id, candidate_hex in sorted(palette.items(), key=lambda kv: int(kv[0])):
        candidate_rgb = parse_hex(candidate_hex)
        if candidate_rgb is None:
            continue
        candidate = rgb_to_lab(candidate_rgb)
        distance = sum((a - b) ** 2 for a, b in zip(target, candidate, strict=True))
        if distance < best_distance:
            best_id, best_distance = color_id, distance

    return best_id


class ColorMapper:
    """Resolves TimeTree label colours to Google colorIds, caching results."""

    def __init__(self, palette: dict[str, str] | None = None, cache: dict[str, str] | None = None):
        self.palette = palette or DEFAULT_EVENT_COLORS
        # User-editable cache in state/color-map.json; manual entries win.
        self.cache = dict(cache or {})

    def color_id_for(self, hex_color: str | None) -> str | None:
        """Return the Google colorId for a TimeTree label colour, or None."""
        if not hex_color:
            return None
        key = hex_color.strip().lower()
        if key not in self.cache:
            resolved = nearest_color_id(key, self.palette)
            if resolved is None:
                return None
            self.cache[key] = resolved
        return self.cache[key]
