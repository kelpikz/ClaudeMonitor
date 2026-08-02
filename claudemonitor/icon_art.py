"""Draw the tray status icon: a rounded tile in the status color with the
Claude asterisk knocked out of it.

Kept apart from tray.py so the artwork can be rendered and asserted on without
pulling in pystray or any Windows state.
"""
from __future__ import annotations

import math

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

Color = tuple[int, int, int]


def tile_icon(color: Color, size: int = TRAY_ICON_SIZE) -> Image.Image:
    """Return the status tile in the given color, at the given pixel size."""
    scale = size * _SUPERSAMPLE
    tile = _rounded_tile(scale, color)
    tile.putalpha(_knock_out(tile.getchannel("A"), _asterisk_mask(scale)))
    return tile.resize((size, size), Image.LANCZOS)


def _rounded_tile(size: int, color: Color) -> Image.Image:
    """Draw the filled rounded square that forms the body of the icon."""
    tile = Image.new("RGBA", (size, size), color + (_TRANSPARENT,))
    ImageDraw.Draw(tile).rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=size * _CORNER_RADIUS,
        fill=color + (_OPAQUE,),
    )
    return tile


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


def _knock_out(alpha: Image.Image, mask: Image.Image) -> Image.Image:
    """Clear the masked area from an alpha channel, so the taskbar shows through
    the glyph instead of the glyph being painted on top of the tile."""
    cleared = Image.new("L", alpha.size, _TRANSPARENT)
    return Image.composite(cleared, alpha, mask)
