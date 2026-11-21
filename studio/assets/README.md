# Offline colour emoji

`emoji.zip` contains 3,991 PNG glyphs (3,999 Unicode sequences) and a Unicode sequence index extracted
from Google's Noto Color Emoji font. Rendering uses these local images, with
no network requests or system emoji-font dependency. Joined emoji, skin tones,
keycaps and flags are matched as whole sequences. The source font is not
required at runtime. No extra Python dependencies are required at runtime.

Source: https://github.com/googlefonts/noto-emoji
Revision: `99701e07d3d1fe1bdbcdb14964547f0fd592966f`
Font: `2D/fonts/NotoColorEmoji.ttf`
Sequence list: `2D/png/128/emoji_u*.png` and `3D/png/128/emoji_u*.png` in that same revision's Git tree.
Licenses: `OFL-noto-emoji.txt` and `LICENSE-noto-emoji.txt` accompany the artwork.

To reproduce, download the matching Git tree JSON and font. In a temporary
build environment install `fonttools==4.68.0` and `uharfbuzz==0.57.0`, then run:

```sh
python scripts/vendor_emoji.py tree.json NotoColorEmoji.ttf studio/assets/emoji.zip
```

The bitmap pack keeps the font's original PNG glyphs. Unsupported future
emoji require updating this pack; rendering never downloads artwork on demand.
