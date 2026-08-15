"""TimeTree label colour to Google colorId mapping."""

from __future__ import annotations

import pytest

from tt2gcal.colors import DEFAULT_EVENT_COLORS, ColorMapper, nearest_color_id, parse_hex


@pytest.mark.parametrize(
    ("hex_color", "expected_id", "name"),
    [
        ("#dc2127", "11", "Tomato"),
        ("#51b749", "10", "Basil"),
        ("#5484ed", "9", "Blueberry"),
        ("#e1e1e1", "8", "Graphite"),
        ("#fbd75b", "5", "Banana"),
    ],
)
def test_exact_palette_colors_map_to_themselves(hex_color, expected_id, name):
    assert nearest_color_id(hex_color) == expected_id, name


@pytest.mark.parametrize(
    ("hex_color", "expected_id"),
    [("#ff0000", "11"), ("#00ff00", "10"), ("#0000ff", "9"), ("#ffd700", "5")],
)
def test_arbitrary_colors_map_to_the_perceptually_nearest(hex_color, expected_id):
    assert nearest_color_id(hex_color) == expected_id


@pytest.mark.parametrize("value", ["", "garbage", "#12345", "#gggggg", None, 123])
def test_unparseable_colors_yield_none_so_google_uses_its_default(value):
    assert nearest_color_id(value) is None


def test_hex_parsing_accepts_both_prefixed_and_bare():
    assert parse_hex("#a4bdfc") == parse_hex("a4bdfc") == (0xA4, 0xBD, 0xFC)


def test_mapping_is_deterministic_across_calls():
    """An unstable mapping would change ttHash and rewrite every event each run."""
    first = {c: nearest_color_id(c) for c in DEFAULT_EVENT_COLORS.values()}
    second = {c: nearest_color_id(c) for c in DEFAULT_EVENT_COLORS.values()}
    assert first == second


def test_manual_cache_entries_win_over_computed_ones():
    mapper = ColorMapper(cache={"#ff0000": "3"})
    assert mapper.color_id_for("#FF0000") == "3"


def test_computed_results_are_cached():
    mapper = ColorMapper()
    assert mapper.color_id_for("#ff0000") == "11"
    assert mapper.cache == {"#ff0000": "11"}
