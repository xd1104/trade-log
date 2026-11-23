# -*- coding: utf-8 -*-
"""
⭐ 2026-11-23 漲跌停空間保護（Benson 點頭）。

台指期每天最多漲跌 10%，基準是前一個一般交易時段的結算價（夜盤屬於下一個交易日，基準＝當天日盤結算價）。
11-23 夜盤：日盤收約 36,856（漲停約 40,540），22:40 跟勢訊號做多、價約 40,100
⇒ 上面最多賺約 440 點，停損 3% 卻約 1,200 點 —— 風報比結構上就是壞的。

規則：**順勢方向離漲／跌停剩下的點數 < 這一口的停損點數 ⇒ 不做**。
- 六年回測（tick-research/limit_room_2026-11-23.md）：夜盤跟勢 346 次觸發，0 次離停板 < 3% ⇒ 這條不改變過去績效，是常識保護。
- 基準用「日盤最後一分鐘收盤」近似結算價（回測量過誤差平均 0.02%、最大 0.36%）。
- ⛔ 基準拿不到或看起來不對（離現價超過 15%）⇒ 判斷不了 ⇒ **照常做**，但在帳本記 room=None（⛔ 不猜、也不因為缺資料擋掉一筆正常的單）。
純函式，⛔ 不碰 I/O、不碰券商。
"""
import math

LIMIT_PCT = 0.10        # 台指期漲跌幅上限
BASE_SANE = 0.15        # 基準離現價超過這個比例 ⇒ 當成資料壞了（判斷不了）


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def limits(base):
    """基準 ⇒ (漲停, 跌停)；基準不對 ⇒ (None, None)。"""
    b = _num(base)
    if b is None:
        return None, None
    return round(b * (1 + LIMIT_PCT)), round(b * (1 - LIMIT_PCT))


def check(direction, px, base, stop_pts):
    """
    ⇒ (可以做嗎, 不做的原因或 None, 紀錄用 dict)。
    direction：'long'／'short'；px：現在（要進場）的價；base：漲跌停基準；stop_pts：這一口的停損點數。
    """
    p, b, s = _num(px), _num(base), _num(stop_pts)
    info = {"limit_base": b, "limit_room": None}
    if p is None or s is None or b is None or abs(p / b - 1) > BASE_SANE or direction not in ("long", "short"):
        return True, None, info                      # 判斷不了 ⇒ 照常（⛔ 不因為缺資料擋單）
    up, dn = limits(b)
    room = (up - p) if direction == "long" else (p - dn)
    info.update(limit_up=up, limit_down=dn, limit_room=round(room, 1))
    if room < s:
        side = "漲停 %d" % up if direction == "long" else "跌停 %d" % dn
        return False, ("離%s只剩 %.0f 點，比停損 %.0f 點還小 —— 賺的空間比可能賠的少，不做"
                       % (side, max(room, 0.0), s)), info
    return True, None, info
