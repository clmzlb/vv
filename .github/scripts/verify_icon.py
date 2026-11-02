"""Verify every user-facing brand mark in a release APK.

Four surfaces have to agree: the adaptive launcher icon, its legacy bitmaps, the
in-app mark (About header / drawer) and the status-bar glyph. All of them are the V
monogram, and none of them may fall back to a raster.
"""
import re
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from PIL import Image

RES = Path('app/src/main/res')
CANVAS = 108.0


def read(name):
    return (RES / name).read_text(encoding='utf-8')


def v_geometry(vector, name):
    """Return ((apex), (left top), (right top)) for a two-arm V vector."""
    arms = re.findall(
        r'android:pathData="M\s*([\d.]+)\s*,\s*([\d.]+)\s*L\s*([\d.]+)\s*,\s*([\d.]+)\s*"',
        vector)
    assert len(arms) == 2, f'{name}: expected two arm paths, found {len(arms)}'
    segments = [((float(a), float(b)), (float(c), float(d))) for a, b, c, d in arms]
    for start, end in segments:
        assert 'strokeLineCap="round"' in vector, f'{name}: arms must have round caps'
    # The arms meet at the apex and their tops are level and symmetric about the canvas.
    apexes = [{start, end} for start, end in segments]
    apex = next(iter(apexes[0] & apexes[1]), None)
    assert apex is not None, f'{name}: arms do not share the apex'
    tops = [start if end == apex else end for start, end in segments]
    assert abs(tops[0][1] - tops[1][1]) < 0.01, f'{name}: tops are not level'
    assert abs(tops[0][0] + tops[1][0] - CANVAS) < 0.01, f'{name}: V is not centred'
    return apex, tops[0], tops[1]


# --- launcher: adaptive layers, then the five legacy bitmaps -------------------------
adaptive = read('mipmap-anydpi-v26/ic_launcher.xml')
for element in ('<adaptive-icon', '@drawable/ic_launcher_foreground',
                '@drawable/ic_launcher_monochrome', '@color/ic_launcher_background'):
    assert element in adaptive, f'Adaptive icon is missing {element}'

foreground = read('drawable/ic_launcher_foreground.xml')

sources = {path.parent.name.split('-', 1)[1]: path for path in RES.glob('mipmap-*/ic_launcher.png')}
assert set(sources) == {'mdpi', 'hdpi', 'xhdpi', 'xxhdpi', 'xxxhdpi'}, sorted(sources)
stray = sorted(str(p) for p in RES.rglob('*.png') if p not in sources.values())
assert not stray, f'Raster art must not ship inside res/: {stray}'

# --- in-app mark: vector twin of the launcher, same V, edge to edge ------------------
artwork = read('drawable/arcaenbox_artwork.xml')
assert '<bitmap' not in artwork, 'The in-app mark must stay a vector'
assert 'fillColor="#36656F"' in artwork, 'The in-app mark lost its teal tile'
apex, left, _ = v_geometry(artwork, 'arcaenbox_artwork')
assert 'strokeColor="#FFFFFF"' in foreground and 'strokeColor="#C6E2E8"' in foreground, \
    'Launcher and in-app mark no longer share the two-tone arms'


def stroke_width(vector):
    values = {float(v) for v in re.findall(r'android:strokeWidth="([\d.]+)"', vector)}
    assert len(values) == 1, f'Expected one stroke width, found {values}'
    return values.pop()


def uncrop(value):
    """Map a coordinate from the launcher's 72dp visible box onto this 108dp canvas."""
    return (value - 18) * 108 / 72


launcher_apex, launcher_left, _ = v_geometry(foreground, 'ic_launcher_foreground')
for got, want in ((apex[0], launcher_apex[0]), (apex[1], launcher_apex[1]),
                  (left[0], launcher_left[0]), (left[1], launcher_left[1])):
    assert abs(got - uncrop(want)) < 0.02, f'In-app V drifted from the launcher V: {got} vs {uncrop(want)}'
assert abs(stroke_width(artwork) - stroke_width(foreground) * 108 / 72) < 0.02, \
    'In-app stroke no longer matches the launcher stroke'

# --- status bar: the old A glyph must not survive anywhere ---------------------------
legacy = sorted(str(p.relative_to(RES)) for p in RES.rglob('*.xml') if 'L 2 21' in p.read_text(encoding='utf-8'))
assert not legacy, f'Legacy A glyph still in {legacy}'

for glyph in ('ic_service_active', 'ic_service_busy', 'ic_service_idle'):
    body = read(f'drawable/{glyph}.xml')
    assert '12,17.8' in body, f'{glyph} is not the V'
    assert 'strokeColor="#' in body, f'{glyph} must be a stroked silhouette'

# The mark is stroked, so the pulse animations have to drive strokeAlpha.
for animated in ('ic_service_connected', 'ic_service_stopping'):
    body = read(f'drawable/{animated}.xml')
    assert 'propertyName="strokeAlpha"' in body, f'{animated} no longer pulses the stroke'
    assert 'propertyName="fillAlpha"' not in body, f'{animated} pulses a fill that does not exist'

# --- README ------------------------------------------------------------------------
readme = Path('README.md').read_text(encoding='utf-8')
assert 'docs/icon.png' in readme, 'README no longer shows the mark'
assert Path('docs/icon.png').exists(), 'docs/icon.png is missing'
assert 'arcaenbox_artwork.png' not in readme, 'README still points at the retired bitmap'

# --- what actually ships -----------------------------------------------------------
with ZipFile(sys.argv[1]) as apk:
    names = apk.namelist()
    assert any(name.endswith('mipmap-anydpi-v26/ic_launcher.xml') for name in names), \
        'Adaptive icon missing from APK'
    for layer in ('ic_launcher_foreground.xml', 'ic_launcher_monochrome.xml',
                  'arcaenbox_artwork.xml', 'ic_arcaenbox.xml',
                  'ic_service_active.xml', 'ic_service_busy.xml', 'ic_service_idle.xml'):
        assert any(name.endswith('drawable/' + layer) for name in names), f'{layer} missing from APK'

    retired = [name for name in names if 'arcaenbox_artwork' in name and name.endswith('.png')]
    assert not retired, f'Retired bitmap artwork is still packaged: {retired}'

    verified = set()
    for name in names:
        match = re.search(r'mipmap-(mdpi|hdpi|xhdpi|xxhdpi|xxxhdpi)(?:-v\d+)?/ic_launcher\.png$', name)
        if not match:
            continue
        density = match.group(1)
        source = Image.open(sources[density]).convert('RGBA')
        packed = Image.open(BytesIO(apk.read(name))).convert('RGBA')
        assert packed.size == source.size, f'{name}: {packed.size} != {source.size}'
        # AAPT discards RGB data in fully transparent pixels. Compare alpha and
        # visible pixels over both black and white, not invisible RGB padding.
        assert packed.getchannel('A').tobytes() == source.getchannel('A').tobytes(), \
            f'{name}: alpha changed'
        for color in ('black', 'white'):
            background = Image.new('RGBA', source.size, color)
            assert Image.alpha_composite(background, packed).tobytes() == \
                Image.alpha_composite(background, source).tobytes(), f'{name}: visible pixels changed'
        verified.add(density)

    assert verified == set(sources), f'Missing densities in APK: {sorted(set(sources) - verified)}'
    print('Verified adaptive icon, in-app mark, status-bar glyph and',
          len(verified), 'legacy bitmaps:', ', '.join(sorted(verified)))
