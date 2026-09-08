"""The taskbar label's picture, asserted without a desktop.

The label is handed to Windows as one finished RGBA image, so everything about
how it looks can be checked here: the glyph each provider gets, the vertical
room around a stacked row, and the byte order Windows reads the result in.
"""

from __future__ import annotations

import pytest
from PIL import Image

from claudemonitor import label_art
from claudemonitor.label_art import (
    glyph_extent,
    premultiplied_bgra,
    stacked_bands,
    taskbar_glyph,
    text_layer,
)


def _alpha_of(image: Image.Image, x: int, y: int) -> int:
    return image.getpixel((x, y))[3]


class TestTaskbarGlyph:
    """One provider's mark, drawn at whatever size the row leaves for it."""

    def test_the_glyph_is_drawn_at_the_size_asked_for(self):
        assert taskbar_glyph("claude", 22, uses_light_theme=False).size == (22, 22)

    def test_each_size_is_drawn_rather_than_rescaled_from_one_bitmap(self):
        # Upscaling a 16px bitmap to 22px was what made the mark look ragged on
        # a 125% display, so every size has to come from the artwork itself.
        small = taskbar_glyph("claude", 16, uses_light_theme=False)
        large = taskbar_glyph("claude", 32, uses_light_theme=False)

        assert small.resize((32, 32)).tobytes() != large.tobytes()

    def test_the_result_is_reused_rather_than_redrawn_every_paint(self):
        first = taskbar_glyph("codex", 20, uses_light_theme=False)
        second = taskbar_glyph("codex", 20, uses_light_theme=False)

        assert first is second

    def test_the_codex_mark_follows_the_theme(self):
        dark = taskbar_glyph("codex", 20, uses_light_theme=False)
        light = taskbar_glyph("codex", 20, uses_light_theme=True)

        assert dark.tobytes() != light.tobytes()

    def test_the_claude_mark_reads_against_either_theme(self):
        # Anthropic's asterisk carries its own colour, so it does not follow the
        # taskbar the way the monochrome OpenAI mark has to.
        dark = taskbar_glyph("claude", 20, uses_light_theme=False)
        light = taskbar_glyph("claude", 20, uses_light_theme=True)

        assert dark.tobytes() == light.tobytes()

    def test_an_unknown_provider_gets_the_claude_mark(self):
        # A provider key nobody drew must never raise inside a paint.
        fallback = taskbar_glyph("something-else", 20, uses_light_theme=False)

        assert fallback.tobytes() == taskbar_glyph(
            "claude", 20, uses_light_theme=False
        ).tobytes()

    def test_a_partly_covered_pixel_keeps_its_own_alpha(self):
        # This is the whole point of the per-pixel alpha label. The mark is
        # mostly anti-aliased edge, and flattening that edge onto black is what
        # drew the dark outline the user saw around both glyphs.
        glyph = taskbar_glyph("claude", 20, uses_light_theme=False)

        alphas = {pixel[3] for pixel in glyph.get_flattened_data()}
        assert alphas - {0, 255}

    def test_a_size_of_zero_is_refused_rather_than_drawn(self):
        with pytest.raises(ValueError):
            taskbar_glyph("claude", 0, uses_light_theme=False)


class TestStackedBands:
    """Each provider gets a horizontal band, and the stack keeps clear of the
    taskbar's own edges."""

    def test_one_row_is_inset_from_both_edges(self):
        assert stacked_bands(48, rows=1, padding=5) == [(5, 43)]

    def test_two_rows_split_what_the_padding_leaves(self):
        assert stacked_bands(48, rows=2, padding=5) == [(5, 24), (24, 43)]

    def test_the_rows_touch_so_no_pixel_belongs_to_neither(self):
        bands = stacked_bands(60, rows=3, padding=6)

        assert [band[0] for band in bands[1:]] == [band[1] for band in bands[:-1]]

    def test_no_rows_means_no_bands(self):
        assert stacked_bands(48, rows=0, padding=5) == []

    def test_padding_is_dropped_before_a_band_collapses(self):
        # A very short taskbar cannot afford the inset. Showing the label
        # cramped beats showing nothing at all.
        assert stacked_bands(10, rows=2, padding=5) == [(0, 5), (5, 10)]


class TestGlyphExtent:
    """The mark is sized to its row, not to the display, so stacking two
    providers cannot make them touch."""

    def test_a_tall_band_gets_the_full_nominal_size(self):
        assert glyph_extent(48, nominal=20, padding=4) == 20

    def test_a_shared_band_shrinks_the_mark_to_fit_its_padding(self):
        assert glyph_extent(24, nominal=20, padding=4) == 16

    def test_a_band_too_short_for_any_padding_still_yields_a_mark(self):
        assert glyph_extent(6, nominal=20, padding=4) == 6

    def test_a_band_with_no_height_yields_nothing(self):
        assert glyph_extent(0, nominal=20, padding=4) == 0


class TestTextLayer:
    """GDI can only rasterise text as coverage, so the colour is applied here."""

    def test_covered_pixels_take_the_requested_colour(self):
        mask = Image.new("L", (1, 1), 255)

        assert text_layer(mask, (245, 245, 245)).getpixel((0, 0)) == (
            245,
            245,
            245,
            255,
        )

    def test_uncovered_pixels_are_fully_transparent(self):
        mask = Image.new("L", (1, 1), 0)

        assert text_layer(mask, (245, 245, 245)).getpixel((0, 0))[3] == 0

    def test_a_half_covered_pixel_keeps_the_colour_and_halves_the_alpha(self):
        # The letter edge has to stay the text colour at partial strength.
        # Darkening it towards the background instead is what fringed the text.
        mask = Image.new("L", (1, 1), 128)

        assert text_layer(mask, (245, 245, 245)).getpixel((0, 0)) == (
            245,
            245,
            245,
            128,
        )


class TestPremultipliedBgra:
    """UpdateLayeredWindow reads premultiplied BGRA, top row first."""

    def test_an_opaque_pixel_is_reordered_to_bgra(self):
        image = Image.new("RGBA", (1, 1), (255, 0, 0, 255))

        assert premultiplied_bgra(image) == bytes([0, 0, 255, 255])

    def test_a_half_transparent_pixel_has_its_colour_scaled_down(self):
        # Windows expects the colour already multiplied by the alpha. Handing it
        # the unscaled colour makes every edge glow.
        image = Image.new("RGBA", (1, 1), (255, 255, 255, 128))

        assert premultiplied_bgra(image) == bytes([128, 128, 128, 128])

    def test_a_fully_transparent_pixel_carries_no_colour(self):
        image = Image.new("RGBA", (1, 1), (255, 255, 255, 0))

        assert premultiplied_bgra(image) == bytes([0, 0, 0, 0])

    def test_every_pixel_is_four_bytes_with_no_row_padding(self):
        # A 32bpp DIB row is always a multiple of four bytes, so unlike the
        # 24bpp buffer this replaced, no padding is ever needed.
        image = Image.new("RGBA", (3, 2), (1, 2, 3, 255))

        assert len(premultiplied_bgra(image)) == 3 * 2 * 4


class TestComposeLabel:
    """One row's mark sits over the text layer at the position it was given."""

    def test_a_glyph_is_pasted_where_it_was_placed(self):
        text = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
        glyph = Image.new("RGBA", (4, 4), (255, 0, 0, 255))

        composed = label_art.compose_label(text, [(glyph, (10, 8))])

        assert composed.getpixel((10, 8)) == (255, 0, 0, 255)
        assert _alpha_of(composed, 0, 0) == 0

    def test_the_text_underneath_survives_where_the_glyph_is_transparent(self):
        text = Image.new("RGBA", (4, 4), (245, 245, 245, 255))
        glyph = Image.new("RGBA", (4, 4), (0, 0, 0, 0))

        composed = label_art.compose_label(text, [(glyph, (0, 0))])

        assert composed.getpixel((0, 0)) == (245, 245, 245, 255)

    def test_a_glyph_hanging_off_the_edge_does_not_raise(self):
        # A taskbar caught mid-resize can leave the label narrower than its own
        # content for one paint, and a paint must never raise.
        text = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
        glyph = Image.new("RGBA", (8, 8), (255, 0, 0, 255))

        assert label_art.compose_label(text, [(glyph, (2, 2))]).size == (4, 4)

    def test_no_glyphs_leaves_the_text_untouched(self):
        text = Image.new("RGBA", (4, 4), (245, 245, 245, 200))

        assert label_art.compose_label(text, []).tobytes() == text.tobytes()
