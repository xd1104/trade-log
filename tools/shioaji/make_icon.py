# -*- coding: utf-8 -*-
"""
產生桌面 App 的圖示 panel.ico（純標準函式庫，沒有 Pillow 也能跑）。

圖案＝面板頂列那個金色圓環 ＋ 一紅一綠兩根 K 棒，顏色沿用面板的色票。
ICO 裡放 PNG（Vista 之後都支援），256／48／32／16 四個尺寸，
工作列、開始功能表、Alt+Tab 各自會挑合適的那張。
"""
import math
import pathlib
import struct
import zlib

BG    = (0x14, 0x18, 0x20, 255)   # 底：比面板背景稍亮一點，深色工作列上才看得出形狀
GOLD  = (0xE3, 0xA9, 0x51, 255)
UP    = (0xEE, 0x5A, 0x54, 255)   # 台股慣例：紅＝漲
DOWN  = (0x34, 0xB3, 0x7E, 255)   # 綠＝跌
CLEAR = (0, 0, 0, 0)


def draw(n):
    """畫一張 n×n 的 RGBA 點陣圖。座標一律用比例算，換尺寸不會走鐘。"""
    px = [[CLEAR] * n for _ in range(n)]
    s = n / 256.0                      # 以 256 為設計基準的縮放係數

    def rect(x0, y0, x1, y1, c):
        for y in range(max(0, int(y0)), min(n, int(math.ceil(y1)))):
            for x in range(max(0, int(x0)), min(n, int(math.ceil(x1)))):
                px[y][x] = c

    # 圓角方形底
    r = 56 * s
    for y in range(n):
        for x in range(n):
            dx = min(x, n - 1 - x)
            dy = min(y, n - 1 - y)
            if dx >= r or dy >= r:
                px[y][x] = BG
            else:
                if (dx - r) ** 2 + (dy - r) ** 2 <= r * r:
                    px[y][x] = BG

    # 金色圓環（面板頂列那個 mark）
    cx = cy = (n - 1) / 2.0
    ro, ri = 96 * s, 96 * s - max(1.6, 11 * s)
    for y in range(n):
        for x in range(n):
            d = math.hypot(x - cx, y - cy)
            if ri <= d <= ro:
                px[y][x] = GOLD

    # 兩根 K 棒：左紅（漲）右綠（跌），影線＋實體
    bw = max(1.0, 26 * s)              # 實體寬
    wk = max(1.0, 7 * s)               # 影線寬
    for cxx, col, top, bot, bt, bb in (
        (cx - 34 * s, UP,   72 * s, 176 * s,  92 * s, 158 * s),
        (cx + 34 * s, DOWN, 88 * s, 192 * s, 104 * s, 172 * s),
    ):
        rect(cxx - wk / 2, top, cxx + wk / 2, bot, col)
        rect(cxx - bw / 2, bt, cxx + bw / 2, bb, col)
    return px


def png(px):
    n = len(px)
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("4B", *px[y][x]) for x in range(n))
        for y in range(n))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def main():
    sizes = [256, 48, 32, 16]
    imgs = [png(draw(s)) for s in sizes]
    off = 6 + 16 * len(sizes)
    head = struct.pack("<HHH", 0, 1, len(sizes))
    ent = b""
    for s, img in zip(sizes, imgs):
        ent += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), off)
        off += len(img)
    out = pathlib.Path(__file__).with_name("panel.ico")
    out.write_bytes(head + ent + b"".join(imgs))
    print(f"寫好 {out}（{out.stat().st_size} bytes，{len(sizes)} 種尺寸）")

    # PWA 用的圖示：Windows 要把它當成「安裝好的應用程式」才會用我們的圖示，
    # 而安裝需要一份 manifest ＋ 192／512 兩種尺寸的 PNG。
    for n in (192, 512):
        f = pathlib.Path(__file__).with_name(f"icon-{n}.png")
        f.write_bytes(png(draw(n)))
        print(f"寫好 {f.name}（{f.stat().st_size} bytes）")

    # 面板網頁的 favicon（分頁上那顆小圖）直接內嵌，不另外供應靜態檔
    import base64
    small = base64.b64encode(imgs[sizes.index(32)]).decode()
    pathlib.Path(__file__).with_name("_favicon.txt").write_text(small, encoding="ascii")
    print(f"favicon（32px PNG，base64 {len(small)} 字元）寫到 _favicon.txt")


if __name__ == "__main__":
    main()
