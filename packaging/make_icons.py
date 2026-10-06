"""Turn the 256 px app icon into the formats each platform's bundle wants:
a multi-size .ico for Windows and an .icns for macOS. Linux uses the PNG.

    python3 packaging/make_icons.py
"""

from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "bin" / "warmap.png"


def main() -> int:
    image = Image.open(SOURCE).convert("RGBA")
    image.save(HERE / "warmap.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    image.save(HERE / "warmap.icns")
    print(f"wrote {HERE / 'warmap.ico'} and {HERE / 'warmap.icns'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
