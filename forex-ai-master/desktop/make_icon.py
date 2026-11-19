"""Generate the application icon (desktop/assets/forex_ai_master.ico and .png). Requires Pillow (build time only)."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "assets"


def draw(size: int = 512) -> Image.Image:
    s = size / 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([16 * s, 16 * s, 496 * s, 496 * s], radius=104 * s, fill=(14, 27, 46, 255))
    # rising candlesticks
    candles = [(120, 300, 360, 270, 390, False), (200, 250, 320, 225, 345, True), (280, 210, 290, 180, 312, True),
               (360, 140, 230, 112, 256, True)]
    for x, top, bottom, wick_top, wick_bottom, up in candles:
        color = (64, 156, 255, 255) if up else (120, 140, 165, 255)
        d.line([x * s, wick_top * s, x * s, wick_bottom * s], fill=color, width=max(1, int(10 * s)))
        d.rounded_rectangle([(x - 26) * s, top * s, (x + 26) * s, bottom * s], radius=8 * s, fill=color)
    d.line([100 * s, 420 * s, 412 * s, 420 * s], fill=(62, 194, 127, 255), width=max(1, int(14 * s)))
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    big = draw(512)
    big.save(OUT / "forex_ai_master.png")
    big.save(OUT / "forex_ai_master.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("icon written to", OUT)
