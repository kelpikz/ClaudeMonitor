"""Tests for the tray status tile artwork.

The tile is the only thing most users ever look at, and it now has to say two
things at once: how much quota is left (its colour) and which provider it
belongs to (its knocked-out glyph).
"""

from __future__ import annotations


import pytest
from PIL import Image

from claudemonitor.icon_art import (
    CLAUDE_GLYPH_COLOR,
    CODEX_DARK_THEME_COLOR,
    CODEX_GLYPH_MASK_PATH,
    CODEX_LIGHT_THEME_COLOR,
    TRAY_ICON_SIZE,
    glyph_image,
    glyph_mask,
    tile_icon,
)
from claudemonitor.win32_bindings import (
    DARK_THEME_FOREGROUND,
    LIGHT_THEME_FOREGROUND,
    rgb_from_colorref,
)

GREEN = (46, 160, 67)
RED = (218, 54, 51)


def _alpha_map(image: Image.Image) -> list[list[int]]:
    pixels = image.load()
    return [
        [pixels[x, y][3] for x in range(image.width)] for y in range(image.height)
    ]


class TestTileIcon:
    """Every tile is a rounded square of the status colour with a glyph cut out."""

    def test_default_tile_is_a_tray_sized_rgba_image(self):
        tile = tile_icon(GREEN)

        assert tile.size == (TRAY_ICON_SIZE, TRAY_ICON_SIZE)
        assert tile.mode == "RGBA"

    def test_tile_can_be_rendered_at_any_size(self):
        assert tile_icon(GREEN, size=32).size == (32, 32)

    def test_corners_are_rounded_away(self):
        alpha = _alpha_map(tile_icon(GREEN))

        assert alpha[0][0] == 0
        assert alpha[0][-1] == 0
        assert alpha[-1][0] == 0
        assert alpha[-1][-1] == 0

    def test_body_is_painted_in_the_status_colour(self):
        tile = tile_icon(RED)
        pixels = tile.load()
        # Downscaling rings by a point or two at every edge, so the test asks
        # what colour the tile mostly is rather than pinning one pixel exactly.
        opaque = [
            pixels[x, y][:3]
            for y in range(tile.height)
            for x in range(tile.width)
            if pixels[x, y][3] > 200
        ]
        dominant = max(set(opaque), key=opaque.count)

        assert all(abs(a - b) <= 8 for a, b in zip(dominant, RED))

    @pytest.mark.parametrize("glyph", ["claude", "codex"])
    def test_glyph_is_knocked_out_of_the_body(self, glyph):
        # The taskbar shows through the glyph rather than the glyph being
        # painted on top, so a fully opaque tile means nothing was cut out.
        alpha = _alpha_map(tile_icon(GREEN, glyph=glyph))
        interior = [row[3:-3] for row in alpha[3:-3]]

        assert any(value < 128 for row in interior for value in row)

    def test_the_two_providers_do_not_look_alike(self):
        claude = tile_icon(GREEN, glyph="claude")
        codex = tile_icon(GREEN, glyph="codex")

        assert claude.tobytes() != codex.tobytes()

    def test_an_unknown_provider_still_renders_a_tile(self):
        # A tile is better than a crash in the poll loop if a new provider key
        # ever reaches here before its artwork does.
        assert tile_icon(GREEN, glyph="something-else").size == (
            TRAY_ICON_SIZE,
            TRAY_ICON_SIZE,
        )

    def test_the_same_glyph_renders_identically_every_time(self):
        assert tile_icon(GREEN, glyph="codex").tobytes() == (
            tile_icon(GREEN, glyph="codex").tobytes()
        )


class TestCodexGlyphMask:
    """The OpenAI mark is artwork, not geometry, so it ships as a mask."""

    def test_the_mask_is_committed(self):
        assert CODEX_GLYPH_MASK_PATH.exists()

    def test_the_mask_renders_at_any_requested_size(self):
        assert glyph_mask("codex", 64).size == (64, 64)
        assert glyph_mask("codex", 256).size == (256, 256)

    def test_the_mask_actually_marks_something(self):
        mask = glyph_mask("codex", 64)
        pixels = mask.load()
        inked = sum(
            1 for y in range(64) for x in range(64) if pixels[x, y] > 128
        )

        assert inked > 100

    def test_the_mask_leaves_its_edges_clear(self):
        # A mark touching the frame would collide with the numbers beside it.
        # Resampling leaves a few units of ringing at the border, which is far
        # below anything visible, so this asks for "clear" rather than "zero".
        mask = glyph_mask("codex", 64)
        pixels = mask.load()
        faint = 16

        assert all(pixels[x, 0] < faint for x in range(64))
        assert all(pixels[x, 63] < faint for x in range(64))


class TestLabelGlyphs:
    """The taskbar label draws each mark itself, at whatever size its row leaves.

    These used to be two committed PNGs, blitted at 16px and stretched to suit a
    scaled display. Drawing them removes the stretch, and with it the ragged
    edges the label showed at 125%.
    """

    @pytest.mark.parametrize("size", [12, 16, 20, 24], ids=str)
    def test_a_mark_is_drawn_at_the_size_asked_for(self, size):
        assert glyph_image("codex", CODEX_DARK_THEME_COLOR, size).size == (size, size)

    @pytest.mark.parametrize("size", [12, 16, 20, 24], ids=str)
    def test_a_mark_actually_draws_something(self, size):
        image = glyph_image("codex", CODEX_DARK_THEME_COLOR, size)
        pixels = image.load()
        inked = sum(
            1
            for y in range(image.height)
            for x in range(image.width)
            if pixels[x, y][3] > 0
        )

        assert inked > size

    def test_the_two_themes_are_drawn_in_different_tones(self):
        dark = glyph_image("codex", CODEX_DARK_THEME_COLOR, TRAY_ICON_SIZE)
        light = glyph_image("codex", CODEX_LIGHT_THEME_COLOR, TRAY_ICON_SIZE)

        assert dark.tobytes() != light.tobytes()

    @pytest.mark.parametrize(
        "color",
        [CODEX_DARK_THEME_COLOR, CODEX_LIGHT_THEME_COLOR, CLAUDE_GLYPH_COLOR],
        ids=["codex-dark", "codex-light", "claude"],
    )
    def test_every_covered_pixel_carries_the_requested_tone(self, color):
        # The mark is one flat colour at varying coverage. A pixel that drifted
        # off that colour would mean the artwork had already been blended with
        # a background, which is the fault the per-pixel alpha label removed.
        image = glyph_image("codex", color, 20)
        pixels = image.load()
        tones = {
            pixels[x, y][:3]
            for y in range(image.height)
            for x in range(image.width)
            if pixels[x, y][3] > 0
        }

        assert tones == {color}

    def test_the_codex_marks_match_the_text_they_sit_beside(self):
        # The OpenAI mark is monochrome, so it is only legible when it follows
        # the theme exactly as the usage text next to it does.
        assert CODEX_DARK_THEME_COLOR == rgb_from_colorref(DARK_THEME_FOREGROUND)
        assert CODEX_LIGHT_THEME_COLOR == rgb_from_colorref(LIGHT_THEME_FOREGROUND)


class TestTheApplicationIcon:
    """The one drawing the exe and the settings window both show."""

    def test_it_is_the_green_tile(self):
        from claudemonitor.icon_art import APPLICATION_ICON_COLOR, application_icon

        assert application_icon(32).tobytes() == (
            tile_icon(APPLICATION_ICON_COLOR, size=32).tobytes()
        )

    def test_it_is_drawn_at_the_size_asked_for(self):
        from claudemonitor.icon_art import application_icon

        image = application_icon(20)

        assert image.size == (20, 20)
        assert image.mode == "RGBA"

    def test_its_png_is_the_same_picture(self):
        import io

        from claudemonitor.icon_art import application_icon, application_icon_png

        decoded = Image.open(io.BytesIO(application_icon_png(24)))

        assert decoded.format == "PNG"
        assert decoded.convert("RGBA").tobytes() == application_icon(24).tobytes()
