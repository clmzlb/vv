"""Verify the adaptive launcher icon and its legacy bitmaps inside a release APK."""
import re
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from PIL import Image

RES = Path('app/src/main/res')
adaptive = (RES / 'mipmap-anydpi-v26/ic_launcher.xml').read_text(encoding='utf-8')
for element in ('<adaptive-icon', '@drawable/ic_launcher_foreground',
                '@drawable/ic_launcher_monochrome', '@color/ic_launcher_background'):
    assert element in adaptive, f'Adaptive icon is missing {element}'

sources = {path.parent.name.split('-', 1)[1]: path for path in RES.glob('mipmap-*/ic_launcher.png')}
assert set(sources) == {'mdpi', 'hdpi', 'xhdpi', 'xxhdpi', 'xxxhdpi'}, sorted(sources)

with ZipFile(sys.argv[1]) as apk:
    names = apk.namelist()
    assert any(name.endswith('mipmap-anydpi-v26/ic_launcher.xml') for name in names), \
        'Adaptive icon missing from APK'
    for layer in ('ic_launcher_foreground.xml', 'ic_launcher_monochrome.xml'):
        assert any(name.endswith('drawable/' + layer) for name in names), f'{layer} missing from APK'

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
    print('Verified adaptive icon and', len(verified), 'legacy bitmaps:', ', '.join(sorted(verified)))
