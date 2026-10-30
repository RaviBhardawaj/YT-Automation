"""Original chart graphics drawn from verified bars (Pillow). No third-party chart images."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .. import config
from ..media import graphics

BG = (12, 15, 22)
GRID = (38, 44, 56)
TEXT = (230, 233, 238)
MUTED = (140, 148, 162)
UP = (38, 166, 154)
DOWN = (239, 83, 80)
RES = (255, 167, 38)      # resistance levels
SUP = (66, 165, 245)      # support levels
PIV = (236, 239, 241)
HL = (255, 214, 0)


def _f(text: str, weight: str, size: int):
    return graphics.font_for(text, weight, size)


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


LANG = "en"          # set by the episode before drawing


def _short_date(iso: str) -> str:
    from . import i18n
    return i18n.short_date(iso, LANG)


BG_TOP, BG_BOTTOM = (20, 27, 48), (6, 8, 15)     # studio-style gradient instead of a flat fill
ZONE_RES, ZONE_SUP = (255, 120, 60), (30, 200, 150)
_BG_CACHE: dict[tuple[int, int], Image.Image] = {}


def _background(w: int, h: int) -> Image.Image:
    """Vertical gradient with a soft vignette (cached per frame size)."""
    if (w, h) not in _BG_CACHE:
        grad = Image.new("RGB", (1, h))
        for yy in range(h):
            t = yy / max(h - 1, 1)
            grad.putpixel((0, yy), tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
        bg = grad.resize((w, h))
        vig = Image.new("L", (w, h), 0)
        ImageDraw.Draw(vig).ellipse([-w * 0.25, -h * 0.35, w * 1.25, h * 1.35], fill=255)
        from PIL import ImageFilter
        vig = vig.filter(ImageFilter.GaussianBlur(max(w, h) // 12))
        dark = Image.new("RGB", (w, h), BG_BOTTOM)
        _BG_CACHE[(w, h)] = Image.composite(bg, dark, vig)
    return _BG_CACHE[(w, h)].copy()


def _zone_label(kind: str) -> str:
    from . import i18n
    return i18n.t("zone_res" if kind == "res" else "zone_sup", LANG)


class Canvas:
    def __init__(self, fmt: str):
        self.fmt = fmt
        self.w, self.h = config.frame_size(fmt)
        self.img = _background(self.w, self.h).convert("RGBA")
        self.d = ImageDraw.Draw(self.img)
        s = min(self.w, self.h)
        self.u = s / 1080                              # unit scale
        if fmt == "short":
            self.plot = (int(70 * self.u), int(self.h * 0.24), int(self.w - 230 * self.u), int(self.h * 0.58))
        else:
            self.plot = (int(90 * self.u), int(200 * self.u), int(self.w - 300 * self.u), int(self.h - 330 * self.u))
        self._brand()

    def translucent(self, box: tuple[float, float, float, float], colour: tuple[int, int, int], alpha: int,
                    radius: int = 0) -> None:
        layer = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(box, radius=radius, fill=colour + (alpha,))
        self.img.alpha_composite(layer)
        self.d = ImageDraw.Draw(self.img)

    def pill(self, xy: tuple[float, float], text: str, colour: tuple[int, int, int], size: int, anchor_right: bool = True,
             weight: str = "heavy") -> None:
        """Price tag: rounded pill in the level's colour with dark text (readable on any background)."""
        x, ymid = xy
        limit = (x - 6 * self.u) if anchor_right else (self.w - 6 * self.u - x)   # never run off the frame
        while True:
            f = _f(text, weight, size)
            tw = self.d.textlength(text, font=f)
            pad_x, h = size * 0.55, size * 1.45
            if tw + 2 * pad_x <= limit or size <= 12:
                break
            size -= 1
        box = (x - tw - 2 * pad_x, ymid - h / 2, x, ymid + h / 2) if anchor_right else (x, ymid - h / 2, x + tw + 2 * pad_x, ymid + h / 2)
        self.translucent(box, colour, 235, radius=int(h / 2))
        self.d.text(((box[0] + box[2]) / 2, ymid), text, font=f, fill=(12, 14, 20), anchor="mm")

    def _brand(self) -> None:
        from .. import settings
        name = ((settings.get("brand") or {}).get("channel_name") or "").strip()
        if name:
            size = int((30 if self.fmt == "short" else 24) * self.u)
            y = int(self.h * 0.05) if self.fmt == "short" else int(56 * self.u)
            self.pill((self.w - int(30 * self.u), y), name, HL, size)

    def title(self, text: str, sub: str = "") -> None:
        size = int((64 if self.fmt == "short" else 54) * self.u)
        y = int((self.h * 0.10) if self.fmt == "short" else 60 * self.u)
        self.d.text((self.w // 2, y), text, font=_f(text, "heavy", size), fill=TEXT, anchor="ma")
        if sub:
            self.d.text((self.w // 2, y + int(size * 1.35)), sub, font=_f(sub, "body", int(size * 0.55)), fill=MUTED,
                        anchor="ma")

    def footer(self, text: str) -> None:
        # top edge: the bottom of the frame belongs to captions and the disclosure ribbon
        size = int(22 * self.u)
        while size > 12 and self.d.textlength(text, font=_f(text, "body", size)) > self.w * 0.94:
            size -= 1
        self.d.text((self.w // 2, int(14 * self.u)), text, font=_f(text, "body", size), fill=MUTED, anchor="ma")

    def save(self, path: Path) -> Path:
        self.img.convert("RGB").save(path, "PNG")
        return path


def _yscale(lo: float, hi: float, top: int, bottom: int):
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad

    def y(v: float) -> float:
        return bottom - (v - lo) / (hi - lo) * (bottom - top)
    return y, lo, hi


def _grid(c: Canvas, y, lo: float, hi: float, labels: bool = True) -> None:
    """Grid lines; axis numbers only on charts without level price tags (the tags use the right margin)."""
    x0, top, x1, bottom = c.plot
    step = (hi - lo) / 5
    for i in range(6):
        v = lo + step * i
        yy = y(v)
        c.d.line([(x0, yy), (x1, yy)], fill=GRID, width=max(1, int(c.u)))
        if labels:
            c.d.text((x1 + int(14 * c.u), yy), f"{v:,.0f}", font=_f("0", "body", int(22 * c.u)), fill=MUTED, anchor="lm")


def _candle(c: Canvas, x: float, w: float, bar: dict[str, Any], y, outline: tuple | None = None) -> None:
    if bar.get("open") is None:                   # open unknown: high-low range bar with a close tick, nothing invented
        c.d.line([(x, y(bar["high"])), (x, y(bar["low"]))], fill=HL, width=max(3, int(w * 0.18)))
        c.d.line([(x, y(bar["close"])), (x + w / 2, y(bar["close"]))], fill=HL, width=max(3, int(w * 0.12)))
        if outline:
            c.d.rectangle([x - w / 2 - 6 * c.u, y(bar["high"]) - 6 * c.u, x + w / 2 + 6 * c.u, y(bar["low"]) + 6 * c.u],
                          outline=outline, width=max(2, int(3 * c.u)))
        return
    col = UP if bar["close"] >= bar["open"] else DOWN
    c.d.line([(x, y(bar["high"])), (x, y(bar["low"]))], fill=col, width=max(2, int(w * 0.12)))
    top, bot = sorted((y(bar["open"]), y(bar["close"])))
    c.d.rectangle([x - w / 2, top, x + w / 2, max(bot, top + 2)], fill=col)
    if outline:
        c.d.rectangle([x - w / 2 - 6 * c.u, y(bar["high"]) - 6 * c.u, x + w / 2 + 6 * c.u, y(bar["low"]) + 6 * c.u],
                      outline=outline, width=max(2, int(3 * c.u)))


def candles_chart(bars: list[dict[str, Any]], highlight: dict[str, str], fmt: str, title: str, sub: str, footer: str,
                  out: Path, levels: dict[str, float] | None = None) -> Path:
    c = Canvas(fmt)
    c.title(title, sub)
    vals = [b["low"] for b in bars] + [b["high"] for b in bars] + (list(levels.values()) if levels else [])
    x0, top, x1, bottom = c.plot
    y, lo, hi = _yscale(min(vals), max(vals), top, bottom)
    _grid(c, y, lo, hi, labels=not levels)
    if levels:
        _level_lines(c, levels, y)
    n = len(bars)
    slot = (x1 - x0) / max(n, 1)
    for i, b in enumerate(bars):
        x = x0 + slot * (i + 0.5)
        _candle(c, x, slot * 0.6, b, y, outline=HL if b["trade_date"] in highlight else None)
        if b["trade_date"] in highlight:
            label = highlight[b["trade_date"]]
            row = list(highlight).index(b["trade_date"])          # stagger adjacent highlighted labels
            c.d.text((x, bottom + int((18 + 30 * row) * c.u)), label, font=_f(label, "heavy", int(22 * c.u)), fill=HL,
                     anchor="ma")
        elif i % max(1, n // 5) == 0:
            c.d.text((x, bottom + int(18 * c.u)), _short_date(b["trade_date"]), font=_f("0", "body", int(20 * c.u)),
                     fill=MUTED, anchor="ma")
    c.footer(footer)
    return c.save(out)


def _reached() -> str:
    from . import i18n
    return i18n.t("c_reached", LANG)


def _spread_labels(ys: list[float], gap: float, top: float, bottom: float) -> list[float]:
    """Move label positions apart (keeping order) so no two are closer than gap; lines themselves never move."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    pos = [ys[i] for i in order]
    for k in range(1, len(pos)):
        pos[k] = max(pos[k], pos[k - 1] + gap)
    overflow = pos[-1] - bottom if pos else 0
    if overflow > 0:
        pos = [y - overflow for y in pos]
        for k in range(len(pos) - 2, -1, -1):
            pos[k] = min(pos[k], pos[k + 1] - gap)
    if pos and pos[0] < top:
        pos = [y + (top - pos[0]) for y in pos]
    out = [0.0] * len(ys)
    for k, i in enumerate(order):
        out[i] = pos[k]
    return out


def _level_lines(c: Canvas, levels: dict[str, float], y, emphasise: tuple[str, ...] = ("S2", "R2"),
                 touched: tuple[str, ...] = (), x_range: tuple[float, float] | None = None,
                 zone_labels: bool = True, strong: bool = False) -> None:
    x0, top, x1, bottom = c.plot
    if x_range:
        x0, x1 = x_range
    if not levels:
        return
    # Shaded zones between level pairs: what traders call the resistance / support zone
    for a, b, colour, kind in (("R1", "R2", ZONE_RES, "res"), ("S1", "S2", ZONE_SUP, "sup")):
        if a in levels and b in levels:
            ya, yb = sorted((y(levels[a]), y(levels[b])))
            ya, yb = max(ya, top), min(yb, bottom)
            if yb - ya > 4:
                c.translucent((x0, ya, x1, yb), colour, 72 if strong else 38)
                if strong:                                 # crisp edges so the zone reads as one band
                    for edge in (ya, yb):
                        c.d.line([(x0, edge), (x1, edge)], fill=colour + (200,), width=max(1, int(2 * c.u)))
                zl = _zone_label(kind) + (f"  {a}–{b}" if strong else "")
                zs = int((20 if c.fmt == "short" else 18) * c.u)
                if zone_labels and yb - ya > zs * 1.3:      # off on candle-dense charts: text must not cover candles
                    c.d.text((x0 + int(12 * c.u), (ya + yb) / 2), zl, font=_f(zl, "heavy", zs), fill=colour + (220,),
                             anchor="lm")
    names = list(levels)
    ys = [y(levels[n]) for n in names]
    sizes = [int((25 if n in emphasise else 20) * c.u) for n in names]
    label_y = _spread_labels(ys, max(sizes) * 1.65, top + max(sizes), bottom)
    for name, yy, ly, size in zip(names, ys, label_y, sizes):
        v = levels[name]
        col = PIV if name == "P" else (RES if name.startswith("R") else SUP)
        width = max(2, int((5 if name in emphasise else 2) * c.u))
        if name in emphasise:                         # soft glow under the key levels
            c.translucent((x0, yy - 7 * c.u, x1, yy + 7 * c.u), col, 40, radius=int(7 * c.u))
        if name == "P":
            for xx in range(int(x0), int(x1), int(24 * c.u)):
                c.d.line([(xx, yy), (min(xx + 12 * c.u, x1), yy)], fill=col, width=width)
        else:
            c.d.line([(x0, yy), (x1, yy)], fill=col, width=width)
        label = f"{name}  {_fmt(v)}" + (f"  · {_reached()}" if name in touched else "")
        if abs(ly - yy) > 2:                          # label displaced: short leader from the line end to its tag
            c.d.line([(x1, yy), (x1 + 6 * c.u, ly)], fill=col, width=max(1, int(c.u)))
        c.pill((x1 + int(8 * c.u), ly), label, col, size, anchor_right=False,
               weight="heavy" if name in emphasise else "body")


REVEAL_ORDER = ["P", "S1", "R1", "S2", "R2", "S3", "R3"]


def _zone_card(c: Canvas, levels: dict[str, float]) -> None:
    """The next session's zones as plain numbers in the lower part of a Short (below the spoken captions)."""
    from . import i18n
    rows = [(i18n.t("card_res", LANG), levels.get("R1"), levels.get("R2"), ZONE_RES),
            (i18n.t("card_piv", LANG), levels.get("P"), None, PIV),
            (i18n.t("card_sup", LANG), levels.get("S1"), levels.get("S2"), ZONE_SUP)]
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return
    x0, x1 = int(70 * c.u), int(c.w - 70 * c.u)
    top, rh = int(c.h * 0.715), int(74 * c.u)
    note = i18n.t("card_note", LANG)
    c.translucent((x0, top, x1, top + rh * len(rows) + int(70 * c.u)), (20, 24, 34), 235, radius=int(24 * c.u))
    for i, (name, a, b, col) in enumerate(rows):
        yy = top + int(22 * c.u) + rh * i + rh // 2
        c.translucent((x0 + int(26 * c.u), yy - int(16 * c.u), x0 + int(34 * c.u), yy + int(16 * c.u)), col, 255,
                      radius=int(4 * c.u))
        c.d.text((x0 + int(52 * c.u), yy), name, font=_f(name, "heavy", int(30 * c.u)), fill=TEXT, anchor="lm")
        val = _fmt(a) if b is None else f"{_fmt(min(a, b))} – {_fmt(max(a, b))}"
        c.d.text((x1 - int(28 * c.u), yy), val, font=_f(val, "heavy", int(32 * c.u)), fill=col, anchor="rm")
    c.d.text(((x0 + x1) / 2, top + rh * len(rows) + int(40 * c.u)), note, font=_f(note, "body", int(22 * c.u)),
             fill=MUTED, anchor="mm")


def levels_chart(basis: dict[str, Any], levels: dict[str, float], fmt: str, title: str, sub: str, footer: str, out: Path,
                 review: dict[str, Any] | None = None, review_label: str = "", basis_label: str = "",
                 touched: tuple[str, ...] = (), reveal: int | None = None,
                 history: list[dict[str, Any]] | None = None, card: bool = False) -> Path:
    """reveal = how many levels to draw (in REVEAL_ORDER) — used for the build-up animation.
    history = the recent daily bars up to the basis day: drawn as a real chart on the left, with the next session's
    zones running on to the right. card = the zones as numbers in the lower part of a Short."""
    if history:
        return _next_session_chart(history, levels, fmt, title, sub, footer, out, reveal=reveal, card=card)
    c = Canvas(fmt)
    c.title(title, sub)
    x0, top, x1, bottom = c.plot
    vals = list(levels.values()) + [basis["high"], basis["low"]] + ([review["high"], review["low"]] if review else [])
    y, lo, hi = _yscale(min(vals), max(vals), top, bottom)
    _grid(c, y, lo, hi, labels=False)
    shown = levels if reveal is None else {k: levels[k] for k in REVEAL_ORDER[:reveal] if k in levels}
    _level_lines(c, shown, y, touched=touched)
    width = (x1 - x0) * (0.10 if fmt == "short" else 0.06)
    xs = [x0 + (x1 - x0) * 0.22] + ([x0 + (x1 - x0) * 0.52] if review else [])
    _candle(c, xs[0], width, basis, y)
    c.d.text((xs[0], bottom + int(18 * c.u)), basis_label, font=_f(basis_label, "heavy", int(22 * c.u)), fill=MUTED, anchor="ma")
    if review:
        _candle(c, xs[1], width, review, y, outline=HL)
        c.d.text((xs[1], bottom + int(18 * c.u)), review_label, font=_f(review_label, "heavy", int(22 * c.u)), fill=HL,
                 anchor="ma")
    c.footer(footer)
    return c.save(out)


def _next_session_chart(history: list[dict[str, Any]], levels: dict[str, float], fmt: str, title: str, sub: str,
                        footer: str, out: Path, reveal: int | None = None, card: bool = False) -> Path:
    from . import i18n
    c = Canvas(fmt)
    c.title(title, sub)
    x0, top, x1, bottom = c.plot
    vals = list(levels.values()) + [b["high"] for b in history] + [b["low"] for b in history]
    y, lo, hi = _yscale(min(vals), max(vals), top, bottom)
    _grid(c, y, lo, hi, labels=False)
    split = x0 + (x1 - x0) * 0.62                        # recent sessions on the left, the next session on the right
    c.translucent((split, top, x1, bottom), (255, 255, 255), 10)
    for xx in range(int(top), int(bottom), int(14 * c.u)):
        c.d.line([(split, xx), (split, min(xx + 7 * c.u, bottom))], fill=MUTED, width=max(1, int(c.u)))
    ns = i18n.t("c_next_session", LANG)
    c.d.text(((split + x1) / 2, top + int(10 * c.u)), ns, font=_f(ns, "heavy", int(20 * c.u)), fill=HL, anchor="ma")
    shown = levels if reveal is None else {k: levels[k] for k in REVEAL_ORDER[:reveal] if k in levels}
    _level_lines(c, shown, y, strong=True, x_range=(split, x1))
    step = (split - x0) / (len(history) + 0.5)
    for i, bar in enumerate(history):
        _candle(c, x0 + step * (i + 0.75), step * 0.55, bar, y)
    for i in (0, len(history) - 1):                     # first and last date under the bars
        lab = i18n.short_date(history[i]["trade_date"], LANG)
        c.d.text((x0 + step * (i + 0.75), bottom + int(18 * c.u)), lab, font=_f(lab, "heavy", int(20 * c.u)),
                 fill=MUTED, anchor="ma")
    if card and fmt == "short" and (reveal is None or reveal >= len(REVEAL_ORDER)):
        _zone_card(c, levels)
    c.footer(footer)
    return c.save(out)


def intraday_chart(bars: list[dict[str, Any]], levels: dict[str, float], fmt: str, title: str, sub: str, footer: str,
                   out: Path, marks: dict[str, str] | None = None, upto: int | None = None,
                   emphasise: tuple[str, ...] = ("S2", "R2")) -> Path:
    """15-minute / 1-hour candles of one session with the reference levels that fall near its range.
    upto = draw only the first N bars (reveal animation); marks = {bar time HH:MM: label}."""
    c = Canvas(fmt)
    c.title(title, sub)
    x0, top, x1, bottom = c.plot
    lo_b, hi_b = min(b["low"] for b in bars), max(b["high"] for b in bars)
    rng = hi_b - lo_b or 1
    near = {k: v for k, v in levels.items() if lo_b - 0.6 * rng <= v <= hi_b + 0.6 * rng}
    y, lo, hi = _yscale(min([lo_b, *near.values()]), max([hi_b, *near.values()]), top, bottom)
    _grid(c, y, lo, hi, labels=False)
    _level_lines(c, near, y, emphasise=emphasise, zone_labels=False)
    off = sorted(set(levels) - set(near), key=lambda k: levels[k])
    if off:
        note = ", ".join(f"{k} {_fmt(levels[k])}" for k in off)
        c.d.text((x0, top - int(10 * c.u)), "· " + note, font=_f(note, "body", int(19 * c.u)), fill=MUTED, anchor="ld")
    n = len(bars)
    slot = (x1 - x0) / max(n, 1)
    every = max(1, n // (4 if fmt == "short" else 7))
    marks = marks or {}
    mark_row = 0
    for i, b in enumerate(bars[: upto if upto is not None else n]):
        x = x0 + slot * (i + 0.5)
        tm = b["ts"][11:16]
        _candle(c, x, slot * 0.62, b, y, outline=HL if tm in marks else None)
        if i % every == 0:
            c.d.text((x, bottom + int(18 * c.u)), tm, font=_f("0", "body", int(20 * c.u)), fill=MUTED, anchor="ma")
        if tm in marks:
            label = marks[tm]
            lf = _f(label, "heavy", int(22 * c.u))
            half = c.d.textlength(label, font=lf) / 2 + 6 * c.u
            span = [bb for j, bb in enumerate(bars) if abs((x0 + slot * (j + 0.5)) - x) <= half + slot / 2]
            clear_above = y(max(bb["high"] for bb in span)) - int(14 * c.u)       # above every bar under the label
            clear_below = y(min(bb["low"] for bb in span)) + int(14 * c.u)
            above = clear_above - 30 * c.u > top if mark_row % 2 == 0 else clear_below + 30 * c.u > bottom
            yy = clear_above - int(34 * c.u) * (mark_row // 2) if above else clear_below + int(34 * c.u) * (mark_row // 2)
            c.d.line([(x, yy + (4 if above else -4) * c.u), (x, y(b["high"] if above else b["low"]))], fill=HL,
                     width=max(1, int(c.u)))
            c.d.text((x, yy), label, font=lf, fill=HL, anchor="md" if above else "ma")
            mark_row += 1
    c.footer(footer)
    return c.save(out)


def mtf_panel(columns: list[tuple[str, dict[str, float]]], fmt: str, title: str, sub: str, footer: str, out: Path,
              note: str = "") -> Path:
    """Side-by-side S2 / P / R2 ladders for Daily, 15-Min and 1-Hour levels (values exactly as calculated)."""
    c = Canvas(fmt)
    c.title(title, sub)
    x0, top, x1, bottom = c.plot
    x1 = c.w - x0
    if fmt == "short":                                # stack vertically on a phone screen
        rows = len(columns)
        hgt = (bottom - top + int(c.h * 0.12)) / rows
        for r, (name, lv) in enumerate(columns):
            yy = top + hgt * r
            c.d.text((c.w // 2, yy), name, font=_f(name, "heavy", int(44 * c.u)), fill=HL, anchor="ma")
            for k, (lvl, col) in enumerate((("R2", RES), ("P", PIV), ("S2", SUP))):
                t_ = f"{lvl} {_fmt(lv[lvl])}"
                c.d.text((c.w // 2, yy + int((64 + 50 * k) * c.u)), t_, font=_f(t_, "body", int(40 * c.u)), fill=col, anchor="ma")
    else:
        colw = (x1 - x0) / len(columns)
        for i, (name, lv) in enumerate(columns):
            cx = x0 + colw * (i + 0.5)
            c.d.text((cx, top + int(20 * c.u)), name, font=_f(name, "heavy", int(46 * c.u)), fill=HL, anchor="ma")
            for k, (lvl, col) in enumerate((("R2", RES), ("P", PIV), ("S2", SUP))):
                t_ = f"{lvl}  {_fmt(lv[lvl])}"
                c.d.text((cx, top + int((130 + 110 * k) * c.u)), t_, font=_f(t_, "heavy", int(44 * c.u)), fill=col, anchor="ma")
    if note:
        nf = _f(note, "body", int((30 if fmt == "short" else 28) * c.u))
        for i, ln in enumerate(graphics.wrap(note, nf, int(c.w * 0.8), c.d)):
            c.d.text((c.w // 2, bottom + int((10 + 40 * i) * c.u)), ln, font=nf, fill=MUTED, anchor="ma")
    c.footer(footer)
    return c.save(out)


def frames(make, steps: list[Any], out_dir: Path) -> Path:
    """Render one PNG per animation step with make(step, path); returns the directory (last frame = final chart)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("f_*.png"):
        old.unlink()
    for i, st in enumerate(steps):
        make(st, out_dir / f"f_{i:03d}.png")
    return out_dir


def text_panel(lines: list[tuple[str, str, tuple]], fmt: str, title: str, footer: str, out: Path) -> Path:
    """lines: (text, weight, colour). Wrapped and vertically centred."""
    c = Canvas(fmt)
    c.title(title)
    maxw = int(c.w * 0.84)
    blocks = []
    for text, weight, col in lines:
        long_text = len(text) > 120                       # paragraphs (disclosure, limitations) stay readable
        size = int((46 if long_text else 78 if weight == "heavy" else 50) * c.u * (1.1 if fmt == "short" else 1))
        f = _f(text, weight, size)
        wrapped = graphics.wrap(text, f, maxw, c.d)
        blocks.append((wrapped, f, col, int(size * 1.35)))
    total = sum(len(w) * lh + int(18 * c.u) for w, _, _, lh in blocks)
    yy = (c.h - total) // 2 + int(30 * c.u)
    for wrapped, f, col, lh in blocks:
        for ln in wrapped:
            c.d.text((c.w // 2, yy), ln, font=f, fill=col, anchor="ma")
            yy += lh
        yy += int(18 * c.u)
    c.footer(footer)
    return c.save(out)


def section_label_png(text: str, frame_w: int, frame_h: int, fmt: str, out: Path) -> dict[str, int]:
    """Small chapter label for the top-left corner, clear of the centred chart title and the bottom captions."""
    u = min(frame_w, frame_h) / 1080
    f = _f(text, "heavy", int(30 * u))
    probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    tw = int(probe.textlength(text, font=f))
    w, h = tw + int(48 * u), int(58 * u)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=HL + (235,))
    d.text((w // 2, h // 2), text, font=f, fill=BG + (255,), anchor="mm")
    img.save(out)
    y = int(frame_h * 0.22) if fmt == "short" else int(44 * u)
    return {"x": int(36 * u), "y": y, "w": w, "h": h}
