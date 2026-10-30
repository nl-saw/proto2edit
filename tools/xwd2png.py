#!/usr/bin/env python3
"""Convert an XWD dump (from xwd(1)) to PNG, so a GUI can be inspected visually.

    xwd -id 0x380005c -silent -out shot.xwd
    python3 tools/xwd2png.py shot.xwd shot.png
"""
import struct
import sys

from PIL import Image

HDR = 100


def convert(src, dst):
    raw = open(src, "rb").read()
    if len(raw) < HDR:
        raise ValueError("file too short to be XWD")
    (header_size, version, fmt, depth, width, height, xoffset, byte_order,
     bitmap_unit, bit_order, bitmap_pad, bpp, bytes_per_line, visual_class,
     red_mask, green_mask, blue_mask, bits_per_rgb, colormap_entries,
     ncolors, window_width, window_height, window_x, window_y,
     bdrwidth) = struct.unpack(">25I", raw[:HDR])

    if version != 7:
        raise ValueError(f"unsupported XWD version {version}")

    pix = HDR + (header_size - HDR) + ncolors * 12
    stride = bytes_per_line

    def bits(v, mask):
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        return (v & mask) >> shift

    out = Image.new("RGB", (width, height))
    px = out.load()
    for y in range(height):
        line = raw[pix + y * stride: pix + (y + 1) * stride]
        for x in range(width):
            off = x * (bpp // 8)
            if off + (bpp // 8) > len(line):
                continue
            if bpp == 32:
                (v,) = struct.unpack_from("<I", line, off)
            elif bpp == 24:
                b, g, r = line[off], line[off + 1], line[off + 2]
                px[x, y] = (r, g, b)
                continue
            elif bpp == 16:
                (v,) = struct.unpack_from("<H", line, off)
            else:
                raise ValueError(f"unsupported bits_per_pixel {bpp}")
            r, g, b = bits(v, red_mask), bits(v, green_mask), bits(v, blue_mask)
            if red_mask == 0xFF0000:            # standard 8-bit channels
                px[x, y] = (r, g, b)
            else:                               # scale arbitrary masks to 8 bit
                rm, gm, bm = red_mask, green_mask, blue_mask
                px[x, y] = (
                    r * 255 // max(1, rm >> ((rm & -rm).bit_length() - 1)),
                    g * 255 // max(1, gm >> ((gm & -gm).bit_length() - 1)),
                    b * 255 // max(1, bm >> ((bm & -bm).bit_length() - 1)),
                )
    out.save(dst)
    print(f"{dst}: {width}x{height} depth={depth} bpp={bpp} "
          f"masks={red_mask:06x}/{green_mask:06x}/{blue_mask:06x}")
    return 0


if __name__ == "__main__":
    sys.exit(convert(sys.argv[1], sys.argv[2]))
