"""
Convert app.ico -> app.icns for the macOS build.

Run on the macOS GitHub Actions runner after make_icon.py has produced
app.ico. Needs Pillow (already in requirements-mac.txt) and Apple's iconutil
(preinstalled on every macOS runner).

Produces a multi-resolution .icns suitable for PyInstaller's --icon flag.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from PIL import Image


def main() -> int:
    if not os.path.exists("app.ico"):
        print("app.ico not found; run make_icon.py first", file=sys.stderr)
        return 1

    img = Image.open("app.ico")

    # .ico files often contain multiple sizes. Pick the largest frame for
    # the source, then resample down to each .icns slot.
    if hasattr(img, "n_frames") and img.n_frames > 1:
        best = img
        best_size = best.size[0]
        for i in range(img.n_frames):
            img.seek(i)
            if img.size[0] > best_size:
                best = img.copy()
                best_size = img.size[0]
        img = best
    img = img.convert("RGBA")

    # Apple's .icns spec wants both 1x and 2x at each canonical logical size.
    # iconutil reads them from an .iconset directory and packs them.
    with tempfile.TemporaryDirectory() as d:
        iconset = os.path.join(d, "app.iconset")
        os.makedirs(iconset)
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ""), (2, "@2x")):
                pixels = size * scale
                resized = img.resize((pixels, pixels), Image.LANCZOS)
                out = os.path.join(iconset, f"icon_{size}x{size}{suffix}.png")
                resized.save(out, "PNG")
        subprocess.run(
            ["iconutil", "-c", "icns", iconset, "-o", "app.icns"],
            check=True,
        )

    if not os.path.exists("app.icns"):
        print("iconutil ran but app.icns is missing", file=sys.stderr)
        return 1
    print(f"Wrote app.icns ({os.path.getsize('app.icns')} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
