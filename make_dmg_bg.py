"""
Generate dmg_bg.png -- the background image for the macOS install DMG.

Run this once locally (python make_dmg_bg.py) and commit dmg_bg.png to the
repo. The build-mac.yml workflow picks it up automatically if present.

Design: 600x400 (matches the DMG window-size in build-mac.yml). The .app
icon goes at (175, 190) and the Applications folder shortcut at (425, 190),
so we draw an arrow between those two anchors plus instruction text below.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    W, H = 600, 400
    img = Image.new("RGB", (W, H), (245, 247, 250))  # very light grey-blue
    draw = ImageDraw.Draw(img)

    # Subtle vertical gradient: lighter at top, slightly darker at bottom.
    for y in range(H):
        t = y / H
        r = int(245 + (235 - 245) * t)
        g = int(247 + (240 - 247) * t)
        b = int(250 + (244 - 250) * t)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    # Try to load a system font; fall back to default if unavailable.
    title_font = None
    body_font = None
    small_font = None
    for path in [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]:
        try:
            title_font = ImageFont.truetype(path, 22)
            body_font  = ImageFont.truetype(path, 14)
            small_font = ImageFont.truetype(path, 12)
            break
        except OSError:
            continue
    if title_font is None:
        title_font = ImageFont.load_default()
        body_font = title_font
        small_font = title_font

    # Title at top.
    title = "Install Claude Usage"
    bbox = draw.textbbox((0, 0), title, font=title_font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) / 2, 30), title, fill=(30, 38, 60), font=title_font)

    subtitle = "Drag the icon into the Applications folder"
    bbox = draw.textbbox((0, 0), subtitle, font=body_font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) / 2, 64), subtitle, fill=(80, 92, 120), font=body_font)

    # Arrow from (175, 190) -> (425, 190). The app icon will be at the first
    # anchor and the Applications shortcut at the second. Arrow body sits
    # above them slightly so it doesn't overlap the icons.
    arrow_y = 190  # vertical center
    arrow_start_x = 245   # right edge of app icon area
    arrow_end_x = 365     # left edge of Applications icon area
    # Shaft
    draw.line(
        [(arrow_start_x, arrow_y), (arrow_end_x, arrow_y)],
        fill=(120, 140, 170), width=4,
    )
    # Arrowhead
    head_len = 16
    head_w = 10
    draw.polygon(
        [
            (arrow_end_x, arrow_y),
            (arrow_end_x - head_len, arrow_y - head_w),
            (arrow_end_x - head_len, arrow_y + head_w),
        ],
        fill=(120, 140, 170),
    )

    # Instruction text below.
    lines = [
        "First time? After dragging the app into Applications:",
        "  1. Open Applications, find Claude Usage",
        "  2. Right-click it and choose Open",
        "  3. Click Open in the macOS security dialog",
        "",
        "This is a one-time macOS security step. The widget reads usage from",
        "the Claude desktop app (claude.ai/download). No browser extension needed.",
    ]
    y = 280
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=small_font)
        tw = bbox[2] - bbox[0]
        # Left-align the numbered list, center the surrounding lines.
        x = 80 if line.startswith(" ") else (W - tw) / 2
        if not line.startswith(" ") and line.startswith("  "):
            x = 80
        if line.strip() == "":
            y += 6
            continue
        draw.text((x, y), line, fill=(60, 72, 100), font=small_font)
        y += 18

    img.save("dmg_bg.png", "PNG")
    print(f"Wrote dmg_bg.png ({W}x{H})")


if __name__ == "__main__":
    main()
