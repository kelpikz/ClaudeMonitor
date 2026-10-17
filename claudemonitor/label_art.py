"""Compose the taskbar label's picture: pure Pillow, no Windows.

The label is a per-pixel alpha window, so Windows is handed one finished RGBA
image rather than a sequence of drawing calls. That makes every question about
how the label looks — which mark a provider gets, how much room a stacked row
leaves around it, what an anti-aliased edge is worth — answerable here, without
a desktop.

Windows contributes exactly one thing to that image: the coverage of the text,
rasterised by GDI so the label keeps the system UI font. Everything else,
including the colour that coverage is painted in, is decided in this module.
"""

from __future__ import annotations

import functools

from PIL import Image, ImageChops

from .models import Provider
from .icon_art import (
    CLAUDE_GLYPH_COLOR,
    CODEX_DARK_THEME_COLOR,
    CODEX_LIGHT_THEME_COLOR,
    glyph_image,
)

Color = tuple[int, int, int]
Placement = tuple[int, int]

# Each provider's mark on a dark and on a light taskbar. Anthropic's asterisk
# carries its own colour and reads against either, so it names one tone twice;
# OpenAI's mark is monochrome and has to follow the theme as the text does.
_GLYPH_COLORS: dict[str, tuple[Color, Color]] = {
    "claude": (CLAUDE_GLYPH_COLOR, CLAUDE_GLYPH_COLOR),
    "codex": (CODEX_DARK_THEME_COLOR, CODEX_LIGHT_THEME_COLOR),
}


@functools.lru_cache(maxsize=32)
def taskbar_glyph(
    provider: Provider,
    size: int,
    *,
    uses_light_theme: bool,
) -> Image.Image:
    """Return one provider's mark drawn at exactly ``size`` pixels square.

    Drawn rather than rescaled: the label previously blitted a 16px bitmap
    stretched up to whatever a scaled display asked for, and the marks are thin
    enough that the stretch was plainly visible. Each size is cached, so this
    costs one render per size the taskbar ever asks for.

    A provider with no tone of its own raises rather than borrowing another
    provider's mark, which would put the wrong name beside a real number.
    """
    if size <= 0:
        raise ValueError(f"a glyph needs a positive size, not {size}")
    dark_color, light_color = _GLYPH_COLORS[provider.key]
    return glyph_image(
        provider.key, light_color if uses_light_theme else dark_color, size
    )


def stacked_bands(height: int, rows: int, padding: int) -> list[tuple[int, int]]:
    """Return the (top, bottom) each row occupies, inset from the label's edges.

    The inset is what keeps the marks off the taskbar's own top and bottom
    edges. Windows' notification icons sit in the middle of the bar with room
    to spare, and a label pressed against both edges reads as a misplaced
    window rather than as part of the shell.
    """
    if rows <= 0:
        return []

    # A short taskbar cannot afford the inset. Cramped beats invisible.
    if height - 2 * padding < rows:
        padding = 0

    band_height = (height - 2 * padding) // rows
    return [
        (padding + index * band_height, padding + (index + 1) * band_height)
        for index in range(rows)
    ]


def glyph_extent(band_height: int, nominal: int, padding: int) -> int:
    """Return the square a mark is drawn at inside a band of that height.

    Sized to its band rather than to the display: two providers halve the room
    each one has, and a mark scaled only by DPI grew to fill its share of a
    125% taskbar completely, leaving a pixel of clearance above and below.
    """
    inset = band_height - 2 * padding
    if inset > 0:
        return min(nominal, inset)
    return max(0, min(nominal, band_height))


def coverage_mask(rendered: Image.Image) -> Image.Image:
    """Turn white-on-black text GDI drew into the coverage it stands for.

    GDI writes colour but never alpha, so the text arrives as brightness on a
    surface cleared to black. Drawn in white, that brightness *is* how much of
    each pixel the letter covers. The brightest channel is taken because a
    build still rendering with ClearType weights the three unequally.
    """
    red, green, blue, _alpha = rendered.split()
    return ImageChops.lighter(ImageChops.lighter(red, green), blue)


def text_layer(mask: Image.Image, color: Color) -> Image.Image:
    """Paint GDI's text coverage in one colour, keeping the coverage as alpha.

    A letter's edge stays the text colour at partial strength. Flattening it
    onto an assumed background is what fringed both the text and the marks.
    """
    layer = Image.new("RGBA", mask.size, color + (0,))
    layer.putalpha(mask)
    return layer


def compose_label(
    text: Image.Image,
    glyphs: list[tuple[Image.Image, Placement]],
) -> Image.Image:
    """Lay each row's mark over the text layer at the position it was given."""
    composed = text.copy()
    for glyph, (left, top) in glyphs:
        fitted = _clipped_to(glyph, composed.size, left, top)
        if fitted is not None:
            composed.alpha_composite(fitted, (left, top))
    return composed


def _clipped_to(
    glyph: Image.Image,
    canvas_size: tuple[int, int],
    left: int,
    top: int,
) -> Image.Image | None:
    """Trim a mark to the part of it that lands on the label, or None if none does.

    A taskbar caught mid-resize can leave the label narrower than its own
    content for one paint, and a paint must never raise.
    """
    canvas_width, canvas_height = canvas_size
    visible_width = min(glyph.width, canvas_width - left)
    visible_height = min(glyph.height, canvas_height - top)
    if visible_width <= 0 or visible_height <= 0 or left < 0 or top < 0:
        return None
    if (visible_width, visible_height) == glyph.size:
        return glyph
    return glyph.crop((0, 0, visible_width, visible_height))


def premultiplied_bgra(image: Image.Image) -> bytes:
    """Pack an RGBA image the way ``UpdateLayeredWindow`` reads it.

    Windows wants premultiplied alpha in BGRA order, top row first. Handing it
    unmultiplied colour makes every anti-aliased edge glow.
    """
    red, green, blue, alpha = image.split()
    premultiplied = Image.merge(
        "RGBA",
        (
            ImageChops.multiply(blue, alpha),
            ImageChops.multiply(green, alpha),
            ImageChops.multiply(red, alpha),
            alpha,
        ),
    )
    return premultiplied.tobytes()
