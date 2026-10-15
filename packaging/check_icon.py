import struct, zlib
from pathlib import Path

data = Path(r"D:\a\packaging\app.ico").read_bytes()
res = struct.unpack_from("<HHH", data, 0)
print("ICO 头: reserved=%d type=%d count=%d" % res)
entries = []
for i in range(res[2]):
    w, h, cc, r, planes, bpp, size, off = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
    entries.append((w or 256, size, off))
    print("  尺寸 %3d  %5d 字节  偏移 %d  位深 %d" % (w or 256, size, off, bpp))

size, length, off = [e for e in entries if e[0] == 256][0]
png = data[off:off + length]
assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG 头错误"
w, h, depth, ctype = struct.unpack_from(">IIBB", png, 16)
print("PNG 负载: %dx%d 位深=%d 颜色类型=%d(6=RGBA)" % (w, h, depth, ctype))
pos, idat = 8, b""
while pos < len(png):
    ln = struct.unpack_from(">I", png, pos)[0]
    tag = png[pos + 4:pos + 8]
    if tag == b"IDAT":
        idat += png[pos + 8:pos + 8 + ln]
    pos += 12 + ln
raw = zlib.decompress(idat)
rows = [raw[i * (1 + w * 4) + 1:(i + 1) * (1 + w * 4)] for i in range(h)]
assert len(rows) == h and len(rows[0]) == w * 4


def px(x, y):
    o = x * 4
    r, g, b, a = rows[y][o], rows[y][o + 1], rows[y][o + 2], rows[y][o + 3]
    return (r, g, b, a)


print("取色：")
print("  左上角(3,3)      ", px(3, 3), " 期望 alpha≈0（圆角外透明）")
print("  顶边中间(128,14) ", px(128, 14), " 期望接近主题蓝 47,107,255")
print("  气泡中心(128,110)", px(128, 110), " 期望接近主题蓝（三个圆点之一）")
print("  气泡内侧(90,90)  ", px(90, 90), " 期望接近白色")
print("  底部(128,250)    ", px(128, 250), " 期望接近主题蓝")