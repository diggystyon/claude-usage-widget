"""Generate app.ico used as the EXE/window icon, plus the browser
extension's PNG icons.

Renders the same two-bar widget the tray uses, then saves a multi-size .ico
so Windows picks the right resolution for taskbar / file explorer / settings.
"""
import os

from PIL import Image, ImageDraw


def color_for_pct(pct: float):
    pct = max(0.0, min(100.0, float(pct)))
    if pct < 50:
        t = pct / 50.0
        return (int(60 + (235 - 60) * t), 200, 60)
    t = (pct - 50) / 50.0
    return (235, int(200 + (60 - 200) * t), 60)


def render_icon(s: float, w: float, size: int = 256) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    bg_radius = max(6, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=bg_radius, fill=(26, 28, 34, 255))
    margin = max(4, size // 10)
    bar_h = max(10, int(size * 0.28))
    gap = max(4, size // 14)
    bar_w = size - 2 * margin
    total_h = bar_h * 2 + gap
    top_y = (size - total_h) // 2
    bot_y = top_y + bar_h + gap
    bar_radius = bar_h // 3
    for y, pct in ((top_y, s), (bot_y, w)):
        draw.rounded_rectangle(
            [margin, y, margin + bar_w, y + bar_h],
            radius=bar_radius,
            fill=(58, 62, 74, 255),
        )
        clamped = max(0.0, min(100.0, float(pct)))
        fill_w = int(round(bar_w * clamped / 100.0))
        if fill_w >= 2:
            draw.rounded_rectangle(
                [margin, y, margin + fill_w, y + bar_h],
                radius=bar_radius,
                fill=color_for_pct(clamped),
            )
    return img


if __name__ == "__main__":
    img = render_icon(50, 50, size=256)
    img.save(
        "app.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("Wrote app.ico")

    ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extension")
    if os.path.isdir(ext_dir):
        for size in (16, 48, 128):
            ext_img = render_icon(50, 50, size=size)
            ext_img.save(os.path.join(ext_dir, f"icon-{size}.png"))
        print(f"Wrote extension icons in {ext_dir}")
