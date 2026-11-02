"""Render the legacy launcher bitmaps that mirror drawable/ic_launcher_foreground.xml.

Adaptive icons only exist from API 26; the five mipmap-*/ic_launcher.png files keep
API 21-25 on the same mark. Geometry is duplicated from the vector on purpose: the
launcher shows the central 72dp of the 108dp canvas, so the bitmaps crop to that box
before scaling. Run from anywhere:

    python buildScript/tools/gen_launcher_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

BG = (0x36, 0x65, 0x6F, 255)      # @color/ic_launcher_background
RIGHT = (0xC6, 0xE2, 0xE8, 255)   # dimmer arm, gives the fold its depth
LEFT = (255, 255, 255, 255)

S = 8                             # supersample factor
CANVAS = 108 * S
STROKE = 13 * S
APEX = (54, 74)
TOP_Y = 37
SPAN = 17.5
CROP = 18 * S                     # central 72dp of the canvas

RES = Path(__file__).resolve().parents[2] / 'app/src/main/res'
DENSITIES = {'mdpi': 48, 'hdpi': 72, 'xhdpi': 96, 'xxhdpi': 144, 'xxxhdpi': 192}


def arm(draw, start, end, color):
    draw.line([start, end], fill=color, width=STROKE)
    radius = STROKE / 2
    for x, y in (start, end):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)


def render(size):
    art = Image.new('RGBA', (CANVAS, CANVAS), BG)
    draw = ImageDraw.Draw(art)
    center = (APEX[0] * S, APEX[1] * S)
    arm(draw, center, ((APEX[0] + SPAN) * S, TOP_Y * S), RIGHT)
    arm(draw, ((APEX[0] - SPAN) * S, TOP_Y * S), center, LEFT)
    art = art.crop((CROP, CROP, CANVAS - CROP, CANVAS - CROP)).resize((size, size), Image.LANCZOS)

    # Legacy icons are not masked by the launcher, so round the corners here.
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(2, round(size * 0.2)), fill=255)
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(art, (0, 0), mask)
    return out


for density, pixels in DENSITIES.items():
    folder = RES / f'mipmap-{density}'
    folder.mkdir(parents=True, exist_ok=True)
    render(pixels).save(folder / 'ic_launcher.png', optimize=True)
    print(f'mipmap-{density}/ic_launcher.png  {pixels}x{pixels}')
