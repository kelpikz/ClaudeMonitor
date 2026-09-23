"""Draw the tray status icon: a rounded tile in the status color with one
provider's glyph knocked out of it.

The colour says how much quota is left; the glyph says whose quota it is.
Anthropic's asterisk is drawn from its own geometry; OpenAI's blossom is
artwork rather than geometry, so it ships as a committed greyscale mask
(see tools/extract_codex_glyph.py).

Kept apart from tray.py so the artwork can be rendered and asserted on without
pulling in pystray or any Windows state.
"""
from __future__ import annotations

import functools
import math
from pathlib import Path

from PIL import Image, ImageDraw

# The Windows notification area asks for 16x16 icons.
TRAY_ICON_SIZE = 16

# Everything is drawn this many times larger and then downscaled, because the
# asterisk's diagonal rays are unusably jagged when rasterized straight at 16px.
_SUPERSAMPLE = 16

# Proportions of the artwork, all expressed as a fraction of the icon size so
# the tile renders identically at any size.
_CORNER_RADIUS = 0.28
_RAY_COUNT = 11
_RAY_INNER_RADIUS = 0.06
_RAY_OUTER_RADIUS = 0.47
_RAY_INNER_HALF_WIDTH = 0.055
_RAY_OUTER_HALF_WIDTH = 0.032

_OPAQUE = 255
_TRANSPARENT = 0

_ASSETS = Path(__file__).parent / "assets"

# The OpenAI mark, stored once at high resolution as white-on-black ink.
# Everything Codex-shaped is scaled and coloured from this single source.
CODEX_GLYPH_MASK_PATH = _ASSETS / "codex_glyph.png"

# The tones the label paints each mark in. The OpenAI mark is monochrome, so
# it matches the text beside it and needs one tone per Windows theme.
# Anthropic's asterisk carries its own colour and reads against either.
CODEX_DARK_THEME_COLOR = (245, 245, 245)
CODEX_LIGHT_THEME_COLOR = (26, 26, 26)
CLAUDE_GLYPH_COLOR = (217, 119, 87)

Color = tuple[int, int, int]


def tile_icon(
    color: Color,
    size: int = TRAY_ICON_SIZE,
    glyph: str = "claude",
) -> Image.Image:
    """Return a status tile: a rounded square in ``color`` with ``glyph`` cut out.

    The tray draws only the Claude asterisk now that one icon serves every
    provider. The glyph stays selectable because which shape to cut out is the
    artwork's business rather than the tray's.
    """
    scale = size * _SUPERSAMPLE
    body = _knock_out(_rounded_mask(scale), glyph_mask(glyph, scale))
    return _painted(color, _downscaled(body, size))


def glyph_mask(glyph: str, size: int) -> Image.Image:
    """Return one provider's glyph as a white-on-black mask.

    An unknown name falls back to the asterisk: a wrong-looking tile is better
    than an exception raised inside the poll loop.
    """
    return _GLYPH_MASKS.get(glyph, _asterisk_mask)(size)


@functools.lru_cache(maxsize=1)
def _codex_source_mask() -> Image.Image:
    """Load the committed OpenAI mark once and reuse it for every render."""
    return Image.open(CODEX_GLYPH_MASK_PATH).convert("L")


def _codex_mask(size: int) -> Image.Image:
    """Draw the OpenAI mark at one size, from the committed artwork."""
    return _codex_source_mask().resize((size, size), Image.Resampling.LANCZOS)


def _rounded_mask(size: int) -> Image.Image:
    """Draw the rounded square that forms the body of the icon, as coverage."""
    mask = Image.new("L", (size, size), _TRANSPARENT)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=size * _CORNER_RADIUS,
        fill=_OPAQUE,
    )
    return mask


def _downscaled(mask: Image.Image, size: int) -> Image.Image:
    """Shrink a coverage mask to the size it will be drawn at.

    A mask is the only thing this module ever resizes. Pillow resamples an RGBA
    image with the alpha weighted in and unweights the result afterwards, which
    drifts a flat colour by as much as a tenth wherever the coverage is partial
    — a lighter rim around every mark, and a whole glyph off-colour at the small
    sizes the taskbar asks for.
    """
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _painted(color: Color, coverage: Image.Image) -> Image.Image:
    """Return one flat colour, showing through as much as ``coverage`` allows."""
    image = Image.new("RGBA", coverage.size, color + (_TRANSPARENT,))
    image.putalpha(coverage)
    return image


def _asterisk_mask(size: int) -> Image.Image:
    """Draw the Claude asterisk as a white-on-black mask."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    for index in range(_RAY_COUNT):
        draw.polygon(_ray_points(size, index), fill=255)
    return mask


def _ray_points(size: int, index: int) -> list[tuple[float, float]]:
    """Return the four corners of one tapered ray, anchored at 12 o'clock and
    rotated into its share of the circle."""
    center = size / 2
    angle = math.tau * index / _RAY_COUNT - math.pi / 2
    along_x, along_y = math.cos(angle), math.sin(angle)
    across_x, across_y = -along_y, along_x

    def corner(radius: float, half_width: float, side: int) -> tuple[float, float]:
        return (
            center + along_x * size * radius + across_x * size * half_width * side,
            center + along_y * size * radius + across_y * size * half_width * side,
        )

    return [
        corner(_RAY_INNER_RADIUS, _RAY_INNER_HALF_WIDTH, +1),
        corner(_RAY_OUTER_RADIUS, _RAY_OUTER_HALF_WIDTH, +1),
        corner(_RAY_OUTER_RADIUS, _RAY_OUTER_HALF_WIDTH, -1),
        corner(_RAY_INNER_RADIUS, _RAY_INNER_HALF_WIDTH, -1),
    ]


# How each provider's mark is drawn. Anthropic's asterisk is geometry, so it
# is generated; OpenAI's blossom is artwork, so it is loaded and resized.
_GLYPH_MASKS = {"claude": _asterisk_mask, "codex": _codex_mask}


def _knock_out(alpha: Image.Image, mask: Image.Image) -> Image.Image:
    """Clear the masked area from an alpha channel, so the taskbar shows through
    the glyph instead of the glyph being painted on top of the tile."""
    cleared = Image.new("L", alpha.size, _TRANSPARENT)
    return Image.composite(cleared, alpha, mask)


def glyph_image(glyph: str, color: Color, size: int = TRAY_ICON_SIZE) -> Image.Image:
    """Return a provider glyph painted in ``color`` on a transparent square.

    This is the shape the taskbar label blits, as opposed to ``tile_icon``,
    which cuts the same glyph out of a colored tile for the tray.
    """
    coverage = glyph_mask(glyph, size * _SUPERSAMPLE)
    return _painted(color, _downscaled(coverage, size))
