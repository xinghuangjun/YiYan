# -*- coding: utf-8 -*-
"""生成 app.ico：程序化绘制图标（纯标准库，多尺寸 PNG 负载）。"""
import struct, zlib
from pathlib import Path

ACCENT = (0x2F, 0x6B, 0xFF, 255)
WHITE = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)


def _png(size, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * size * 4:(y + 1) * size * 4])
                   for y in range(size))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _canvas(n):
    return [[CLEAR] * n for _ in range(n)]


def _rounded(buf, x1, y1, x2, y2, r, color, n):
    for y in range(max(0, int(y1)), min(n, int(y2) + 1)):
        for x in range(max(0, int(x1)), min(n, int(x2) + 1)):
            cx = min(max(x, x1 + r), x2 - r)
            cy = min(max(y, y1 + r), y2 - r)
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + 0.5:
                buf[y][x] = color


def _circle(buf, cx, cy, r, color, n):
    for y in range(max(0, int(cy - r - 1)), min(n, int(cy + r + 2))):
        for x in range(max(0, int(cx - r - 1)), min(n, int(cx + r + 2))):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r + 0.5:
                buf[y][x] = color


def _triangle(buf, p1, p2, p3, color, n):
    xs = [p1[0], p2[0], p3[0]]
    ys = [p1[1], p2[1], p3[1]]
    for y in range(max(0, int(min(ys))), min(n, int(max(ys)) + 1)):
        for x in range(max(0, int(min(xs))), min(n, int(max(xs)) + 1)):
            d1 = (x - p2[0]) * (p1[1] - p2[1]) - (p1[0] - p2[0]) * (y - p2[1])
            d2 = (x - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (y - p3[1])
            d3 = (x - p1[0]) * (p3[1] - p1[1]) - (p3[0] - p1[0]) * (y - p1[1])
            neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
            pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
            if not (neg and pos):
                buf[y][x] = color


def draw(size):
    k = 4
    n = size * k
    buf = _canvas(n)
    _rounded(buf, 0.01 * n, 0.01 * n, 0.99 * n, 0.99 * n, 0.24 * n, ACCENT, n)
    _rounded(buf, 0.19 * n, 0.23 * n, 0.81 * n, 0.63 * n, 0.13 * n, WHITE, n)
    _triangle(buf, (0.30 * n, 0.57 * n), (0.46 * n, 0.57 * n), (0.29 * n, 0.79 * n),
              WHITE, n)
    for cx in (0.35, 0.5, 0.65):
        _circle(buf, cx * n, 0.43 * n, 0.047 * n, ACCENT, n)
    out = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            r = g = b = a = 0
            for dy in range(k):
                for dx in range(k):
                    px = buf[y * k + dy][x * k + dx]
                    r += px[0]; g += px[1]; b += px[2]; a += px[3]
            tot = k * k
            o = (y * size + x) * 4
            out[o] = r // tot; out[o + 1] = g // tot
            out[o + 2] = b // tot; out[o + 3] = a // tot
    return bytes(out)


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [(s, _png(s, draw(s))) for s in sizes]
    out = Path(__file__).with_name("app.ico")
    offset = 6 + 16 * len(images)
    head = struct.pack("<HHH", 0, 1, len(images))
    entries, payload = b"", b""
    for s, data in images:
        w = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    out.write_bytes(head + entries + payload)
    print("已生成 %s（%.1f KB，尺寸 %s）" % (out, out.stat().st_size / 1024,
                                        ", ".join(str(s) for s in sizes)))


if __name__ == "__main__":
    main()