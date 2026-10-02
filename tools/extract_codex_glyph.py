"""Extract the OpenAI blossom into ``claudemonitor/assets/codex_glyph.png``.

Run once, on a machine that has the official ChatGPT VS Code extension
installed, and commit the result. The rest of the app reads only the committed
mask, so no build or test depends on the extension being present.

The mask is greyscale: white where the mark is, black where it is not. Colour
is applied later, because the taskbar label paints the glyph in the same tone
as the text beside it and that tone follows the Windows theme.

    uv run python tools/extract_codex_glyph.py

The source asset is the extension's own ``blossom.dark.png`` — a black tile
carrying the mark in white ink, so the mark is simply the tile's luminance.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from claudemonitor.icon_art import CODEX_GLYPH_MASK_PATH

# Stored well above the 16px the taskbar needs, so the tray tile can supersample
# from it without the knot's thin strokes breaking up.
_MASK_SIZE = 256

# Leaves the mark a little clear of the numbers printed next to it.
_MARGIN = 0.04

_EXTENSION_GLOB = ".vscode/extensions/openai.chatgpt-*/resources/blossom.dark.png"


def _find_source() -> Path:
    """Locate the newest installed ChatGPT extension's blossom tile."""
    candidates = sorted(Path.home().glob(_EXTENSION_GLOB))
    if not candidates:
        raise SystemExit(
            "No ChatGPT VS Code extension found. Install it, or point this "
            "script at any square image carrying the mark in white on black."
        )
    return candidates[-1]


def _mark_mask(tile: Image.Image) -> Image.Image:
    """Return the white ink of the tile as a mask, cropped to the mark itself."""
    pixels = tile.load()
    mask = Image.new("L", tile.size, 0)
    target = mask.load()
    for y in range(tile.height):
        for x in range(tile.width):
            red, green, blue, alpha = pixels[x, y]
            # Transparent tile corners carry no ink, whatever colour they claim.
            target[x, y] = (red + green + blue) // 3 if alpha > 40 else 0
    bounds = mask.getbbox()
    if bounds is None:
        raise SystemExit("the source image has no white ink to extract")
    return mask.crop(bounds)


def _square(mask: Image.Image) -> Image.Image:
    """Fit the mark into a padded square, so it scales without distortion."""
    inner = round(_MASK_SIZE * (1 - 2 * _MARGIN))
    scaled = mask.resize((inner, inner), Image.LANCZOS)
    canvas = Image.new("L", (_MASK_SIZE, _MASK_SIZE), 0)
    offset = (_MASK_SIZE - inner) // 2
    canvas.paste(scaled, (offset, offset))
    return canvas


def main() -> None:
    source = _find_source()
    mask = _square(_mark_mask(Image.open(source).convert("RGBA")))
    mask.save(CODEX_GLYPH_MASK_PATH)
    print(f"read  {source}", file=sys.stderr)
    print(f"wrote {CODEX_GLYPH_MASK_PATH}")


if __name__ == "__main__":
    main()
