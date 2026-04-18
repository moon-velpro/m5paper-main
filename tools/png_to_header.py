"""Convert 150x150 PNG sprites to 2-bit (4 grayscale levels) C header arrays.

Grayscale mapping:
  0 = black (0x00)
  1 = dark gray (0x55)
  2 = light gray (0xAA)
  3 = white (0xFF)

Packing: 4 pixels per byte, MSB first.
  byte = (px0 << 6) | (px1 << 4) | (px2 << 2) | px3
"""

import os
import sys
from PIL import Image


def quantize_4level(value: int) -> int:
    """Map 8-bit grayscale to 2-bit (0-3)."""
    if value < 64:
        return 0  # black
    elif value < 128:
        return 1  # dark gray
    elif value < 192:
        return 2  # light gray
    else:
        return 3  # white


def convert_png(path: str, resize: tuple[int, int] | None = None) -> tuple[str, int, int, list[int]]:
    img = Image.open(path).convert("L")
    if resize:
        img = img.resize(resize, Image.LANCZOS)
    w, h = img.size
    pixels = list(img.getdata())

    # Quantize to 4 levels and pack 4 pixels per byte
    packed = []
    for i in range(0, len(pixels), 4):
        chunk = pixels[i : i + 4]
        levels = [quantize_4level(p) for p in chunk]
        # Pad if last chunk is incomplete
        while len(levels) < 4:
            levels.append(3)  # white padding
        byte = (levels[0] << 6) | (levels[1] << 4) | (levels[2] << 2) | levels[3]
        packed.append(byte)

    return os.path.splitext(os.path.basename(path))[0], w, h, packed


def main():
    sprite_dir = os.path.join(os.path.dirname(__file__), "..", "assets", "sprites")
    out_path = os.path.join(
        os.path.dirname(__file__), "..", "m5paper_hw", "main", "clothing_sprites.h"
    )

    sprites = sorted(
        f for f in os.listdir(sprite_dir) if f.endswith(".png")
    )

    lines = [
        "#pragma once",
        "// Auto-generated from tools/png_to_header.py — do not edit manually",
        "// 2-bit grayscale (4 levels): 0=black, 1=dark, 2=light, 3=white",
        "// Packing: 4 pixels per byte, MSB first",
        "#include <stdint.h>",
        "",
    ]

    # Get dimensions from first sprite (all same size)
    first_name, first_w, first_h, _ = convert_png(os.path.join(sprite_dir, sprites[0]))
    lines.append(f"#define SPRITE_W {first_w}")
    lines.append(f"#define SPRITE_H {first_h}")
    lines.append("")

    for sprite_file in sprites:
        name, w, h, packed = convert_png(os.path.join(sprite_dir, sprite_file))
        var_name = f"sprite_{name}"
        lines.append(f"static const uint8_t {var_name}[{len(packed)}] = {{")

        # Format as rows of 16 bytes
        for i in range(0, len(packed), 16):
            row = packed[i : i + 16]
            hex_vals = ", ".join(f"0x{b:02X}" for b in row)
            lines.append(f"    {hex_vals},")

        lines.append("};")
        lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    print(f"Generated {out_path}")
    print(f"  {len(sprites)} sprites, {first_w}x{first_h}, 2-bit packed")
    total = sum(
        len(convert_png(os.path.join(sprite_dir, s))[3]) for s in sprites
    )
    print(f"  Total: {total:,} bytes ({total / 1024:.1f} KB)")

    # --- Weather icons (resized to 80x80) ---
    icon_dir = os.path.join(os.path.dirname(__file__), "..", "simulator", "icons")
    icon_out = os.path.join(
        os.path.dirname(__file__), "..", "m5paper_hw", "main", "weather_icons_2bit.h"
    )
    icon_size = (100, 100)
    icon_files = ["sunny.png", "cloudy.png", "partly_cloudy.png", "rainy.png", "snowy.png"]

    ilines = [
        "#pragma once",
        "// Auto-generated weather icons — do not edit manually",
        "// 2-bit grayscale (4 levels), 80x80, packed 4px/byte MSB first",
        "#include <stdint.h>",
        "",
        f"#define WEATHER_ICON_W {icon_size[0]}",
        f"#define WEATHER_ICON_H {icon_size[1]}",
        "",
    ]

    for icon_file in icon_files:
        path = os.path.join(icon_dir, icon_file)
        if not os.path.exists(path):
            print(f"  WARNING: {path} not found, skipping")
            continue
        name, w, h, packed = convert_png(path, resize=icon_size)
        var_name = f"weather_{name}"
        ilines.append(f"static const uint8_t {var_name}[{len(packed)}] = {{")
        for i in range(0, len(packed), 16):
            row = packed[i : i + 16]
            hex_vals = ", ".join(f"0x{b:02X}" for b in row)
            ilines.append(f"    {hex_vals},")
        ilines.append("};")
        ilines.append("")

    with open(icon_out, "w") as f:
        f.write("\n".join(ilines))

    print(f"Generated {icon_out}")
    print(f"  {len(icon_files)} weather icons, {icon_size[0]}x{icon_size[1]}, 2-bit packed")


if __name__ == "__main__":
    main()
