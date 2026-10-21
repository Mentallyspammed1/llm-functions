"""Price Action & Market Structure engine for the Bybit trading suite.

Pure-logic module: it consumes OHLCV candle lists (Bybit format, newest-first)
and produces structure analysis, signals, and trade plans. Because it is pure,
the same code drives live signals (`BybitRealm`), the backtester, and unit tests.

Candle row format (Bybit v5 kline): [start_ms, open, high, low, close, vol, turnover]

Design note — one decision function
-----------------------------------
`_decide()` holds *all* entry rules. `PriceActionEngine.price_action_signal()`
computes indicators and calls it; the backtester pre-computes the same
indicators as series and calls the *same* function bar by bar. This makes it
structurally impossible for the backtest to drift away from live behaviour,
which is the most common source of fake profitability in trading code.

Trend-quality filters
---------------------
ADX measures the *magnitude* of directional movement, not whether a move is
tradeable trend or chop: a random walk routinely prints ADX > 35. Measured on
synthetic random walks, ADX barely separates a trend from noise. The engine
therefore gates on Kaufman's **Efficiency Ratio** (net move / path length),
which does separate them, plus an anti-chase distance check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# ── small pure helpers ──────────────────────────────────────────────────────


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    k = 2.0 / (period + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1 - k))
    return out


def _atr_series(highs, lows, closes, period: int = 14) -> List[float]:
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if not trs:
        return []
    out = [sum(trs[:period]) / min(period, len(trs))]
    for i in range(period, len(trs)):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    # pad so out aligns with closes[1:]
    return [out[0]] * (period - 1) + out


def _rsi_series(closes: Sequence[float], period: int = 14) -> List[float]:
    """Wilder-smoothed RSI series aligned to `closes` (length == len(closes))."""
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    diffs = [closes[i + 1] - closes[i] for i in range(n - 1)]
    gains = [d if d > 0 else 0.0 for d in diffs]
    losses = [-d if d < 0 else 0.0 for d in diffs]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out = [50.0] * period
    for i in range(period, len(diffs)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    # diffs index i corresponds to bar i+1
    return out[:n]


def _adx_series(highs, lows, closes, period: int = 14) -> List[float]:
    """Wilder ADX series aligned to the candle index (length == len(closes))."""
    n = len(closes)
    if n < 2 * period:
        return [0.0] * n
    trs, pdms, ndms = [], [], []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        pdms.append(up if up > down and up > 0 else 0.0)
        ndms.append(down if down > up and down > 0 else 0.0)
    dx = [0.0] * (n - 1)
    for i in range(period - 1, n - 1):
        s_tr = sum(trs[i - period + 1 : i + 1])
        s_p = sum(pdms[i - period + 1 : i + 1])
        s_n = sum(ndms[i - period + 1 : i + 1])
        di_p = 100 * s_p / s_tr if s_tr else 0.0
        di_n = 100 * s_n / s_tr if s_tr else 0.0
        dx[i] = 100 * abs(di_p - di_n) / (di_p + di_n) if (di_p + di_n) else 0.0
    out = [0.0] * n
    for i in range(2 * period - 1, n):
        out[i] = sum(dx[i - period : i]) / period
    return out


def _efficiency_ratio(closes: Sequence[float], period: int = 20) -> float:
    """Kaufman Efficiency Ratio in [0, 1].

    ``|net move over N bars| / sum(|bar-to-bar move|)``.

    ~1.0 is a clean one-way trend, ~0 is pure chop. Unlike ADX this is
    scale-free and measures *path efficiency*, so it separates trend from
    noise far more reliably.
    """
    if len(closes) < period + 1:
        return 0.0
    seg = closes[-(period + 1):]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i + 1] - seg[i]) for i in range(len(seg) - 1))
    return net / path if path else 0.0


def _percentile_rank(values: Sequence[float], value: float) -> float:
    """Fraction of `values` at or below `value`, in [0, 1]."""
    if not values:
        return 0.5
    return sum(1 for v in values if v <= value) / len(values)


@dataclass
class SwingPoint:
    index: int  # position in the chronological candle list
    price: float
    kind: str  # "high" | "low"


@dataclass
class Signal:
    action: str  # LONG | SHORT | WAIT
    confidence: float  # 0..100
    entry: float
    stop_loss: float
    take_profits: List[float]
    rr: float
    setup: str
    reasons: List[str] = field(default_factory=list)
    filters: List[str] = field(default_factory=list)
    meta: Dict[str, float] = field(default_factory=dict)


class PriceActionEngine:
    """Trend + market-structure analysis for price action trading.

    Terminology:
      * UPTREND   — higher highs and higher lows (HH/HL)
      * DOWNTREND — lower highs and lower lows (LH/LL)
      * RANGE     — no consistent structure

    Tunables (all optional, defaults are the validated ones):
      min_adx           ADX floor for a "trending" tape (default 20)
      min_er            Kaufman efficiency-ratio floor (default 0.15)
      max_ema_dist_atr  hard anti-chase cap: skip if price is this many ATRs
                        away from the fast EMA (default 6.0; <=0 disables)
      caution_ema_dist_atr  soft anti-chase: subtract confidence beyond this
                        distance (default 3.0)
      max_atr_percentile volatility-spike guard (default 0.98; >=1 disables)
      min_slow_slope_atr  directional regime gate: the slow EMA must slope at
                        least this many ATRs over `slope_lookback` bars in the
                        trade direction (default 0.8; 0 disables). This is what
                        keeps the engine out of mean-reverting markets, where a
                        flat slow EMA makes every 'trend' a fade candidate.
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_mid: int = 50,
        ema_slow: int = 200,
        atr_period: int = 14,
        swing_strength: int = 2,
        rsi_period: int = 14,
        adx_period: int = 14,
        er_period: int = 20,
        min_adx: float = 20.0,
        min_er: float = 0.15,
        max_ema_dist_atr: float = 6.0,
        caution_ema_dist_atr: float = 3.0,
        max_atr_percentile: float = 0.98,
        structure_lookback: int = 120,
        slope_lookback: int = 20,
        min_slow_slope_atr: float = 0.8,
    ):
        self.ema_fast = ema_fast
        self.ema_mid = ema_mid
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.swing_strength = swing_strength
        self.rsi_period = rsi_period
        self.adx_period = adx_period
        self.er_period = er_period
        self.min_adx = min_adx
        self.min_er = min_er
        self.max_ema_dist_atr = max_ema_dist_atr
        self.caution_ema_dist_atr = caution_ema_dist_atr
        self.max_atr_percentile = max_atr_percentile
        self.structure_lookback = structure_lookback
        self.slope_lookback = slope_lookback
        self.min_slow_slope_atr = min_slow_slope_atr

    # ── data accessors ──────────────────────────────────────────────────────

    @staticmethod
    def to_chronological(klines: Sequence[Sequence]) -> Tuple[List, List, List, List, List]:
        """Convert newest-first klines to chronological lists."""
        chron = list(reversed(list(klines)))
        o = [float(k[1]) for k in chron]
        h = [float(k[2]) for k in chron]
        l = [float(k[3]) for k in chron]
        c = [float(k[4]) for k in chron]
        v = [float(k[5]) for k in chron]
        return o, h, l, c, v

    # ── structure ───────────────────────────────────────────────────────────

    def swing_points(self, klines: Sequence[Sequence]) -> List[SwingPoint]:
        """Detect swing highs/lows using fractal confirmation."""
        o, h, l, c, v = self.to_chronological(klines)
        s = self.swing_strength
        swings: List[SwingPoint] = []
        for i in range(s, len(h) - s):
            window_h = h[i - s : i + s + 1]
            window_l = l[i - s : i + s + 1]
            if h[i] == max(window_h) and h[i] > max(window_h[:s] + window_h[s + 1 :]):
                swings.append(SwingPoint(i, h[i], "high"))
            elif l[i] == min(window_l) and l[i] < min(window_l[:s] + window_l[s + 1 :]):
                swings.append(SwingPoint(i, l[i], "low"))
        return swings

    def structure_from_arrays(
        self,
        highs: Sequence[float],
        lows: Sequence[float],
        closes: Sequence[float],
        atr: float,
        ema_fast: Optional[float] = None,
        ema_slow: Optional[float] = None,
        swings: Optional[List[SwingPoint]] = None,
    ) -> dict:
        """Classify structure from pre-computed arrays (shared by live + backtest).

        `swings` may be supplied when the caller already detected them, which
        lets the backtester avoid recomputing fractals for every bar.
        """
        if swings is None:
            # Build a kline-shaped proxy so swing_points() can be reused. The
            # caller passes chronological arrays, but swing_points() expects
            # Bybit's newest-first order and reverses internally — so build the
            # proxy newest-first to keep swing indices aligned with these arrays.
            proxy = [
                [0, str(o), str(h), str(l), str(c), "0"]
                for o, h, l, c in zip(closes, highs, lows, closes)
            ]
            proxy.reverse()
            swings = self.swing_points(proxy)
        if len(swings) < 4:
            return {
                "status": "ok",
                "state": "UNKNOWN",
                "note": "not enough swings",
                "swings": [],
                "bos": None,
                "last_high": None,
                "last_low": None,
            }
        recent = swings[-6:]
        highs_s = [s.price for s in recent if s.kind == "high"]
        lows_s = [s.price for s in recent if s.kind == "low"]
        hh = len(highs_s) >= 2 and highs_s[-1] > highs_s[-2]
        hl = len(lows_s) >= 2 and lows_s[-1] > lows_s[-2]
        lh = len(highs_s) >= 2 and highs_s[-1] < highs_s[-2]
        ll = len(lows_s) >= 2 and lows_s[-1] < lows_s[-2]

        if hh and hl:
            state = "UPTREND"
        elif lh and ll:
            state = "DOWNTREND"
        else:
            state = "RANGE"

        # Slope/EMA fallback: in strong trends fractal swings can be sparse
        # (every candle makes an extreme), so confirm with net change vs ATR.
        if state == "RANGE" and len(closes) > 40:
            lookback = min(60, len(closes) // 3)
            net = closes[-1] - closes[-lookback]
            if atr > 0:
                ema_f = ema_fast if ema_fast is not None else _ema_series(closes, self.ema_fast)[-1]
                ema_s = ema_slow if ema_slow is not None else _ema_series(closes, self.ema_slow)[-1]
                if net > 4 * atr and ema_f > ema_s:
                    state = "UPTREND"
                elif net < -4 * atr and ema_f < ema_s:
                    state = "DOWNTREND"

        # Break of structure: price violating the most recent opposing swing
        bos = None
        last_high = max((s for s in recent if s.kind == "high"),
                        key=lambda s: s.index, default=None)
        last_low = min((s for s in recent if s.kind == "low"),
                       key=lambda s: s.index, default=None)
        price = closes[-1]
        if state == "DOWNTREND" and last_high and price > last_high.price:
            bos = "BULLISH_BOS"
        elif state == "UPTREND" and last_low and price < last_low.price:
            bos = "BEARISH_BOS"

        return {
            "status": "ok",
            "state": state,
            "bos": bos,
            "swings": [
                {"index": s.index, "price": round(s.price, 6), "kind": s.kind}
                for s in recent
            ],
            "last_high": round(last_high.price, 6) if last_high else None,
            "last_low": round(last_low.price, 6) if last_low else None,
        }

    def market_structure(self, klines: Sequence[Sequence]) -> dict:
        """Classify market structure from recent swing points."""
        o, h, l, c, v = self.to_chronological(klines)
        atr_s = _atr_series(h, l, c, self.atr_period)
        atr = atr_s[-1] if atr_s else 0.0
        return self.structure_from_arrays(
            h, l, c, atr,
            ema_fast=_ema_series(c, self.ema_fast)[-1],
            ema_slow=_ema_series(c, self.ema_slow)[-1],
        )

    # ── trend / momentum ────────────────────────────────────────────────────

    def ema_stack(self, klines: Sequence[Sequence]) -> dict:
        o, h, l, c, v = self.to_chronological(klines)
        if len(c) < self.ema_slow:
            return {"status": "error", "msg": "not enough candles for EMA stack"}
        f = _ema_series(c, self.ema_fast)[-1]
        m = _ema_series(c, self.ema_mid)[-1]
        s = _ema_series(c, self.ema_slow)[-1]
        price = c[-1]
        if f > m > s and price > f:
            alignment = "BULLISH"
        elif f < m < s and price < f:
            alignment = "BEARISH"
        elif price > s:
            alignment = "MIXED_BULL"
        else:
            alignment = "MIXED_BEAR"
        return {
            "status": "ok",
            "alignment": alignment,
            "price": round(price, 6),
            f"ema_{self.ema_fast}": round(f, 6),
            f"ema_{self.ema_mid}": round(m, 6),
            f"ema_{self.ema_slow}": round(s, 6),
        }

    def rsi(self, klines: Sequence[Sequence]) -> float:
        o, h, l, c, v = self.to_chronological(klines)
        period = self.rsi_period
        if len(c) < period + 1:
            return 50.0
        diffs = [c[i + 1] - c[i] for i in range(len(c) - 1)]
        gains = [d if d > 0 else 0 for d in diffs]
        losses = [-d if d < 0 else 0 for d in diffs]
        avg_g = sum(gains[:period]) / period
        avg_l = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            return 100.0
        return 100 - 100 / (1 + avg_g / avg_l)

    def adx(self, klines: Sequence[Sequence]) -> dict:
        o, h, l, c, v = self.to_chronological(klines)
        period = self.adx_period
        if len(c) < 2 * period:
            return {"status": "error", "adx": 0.0, "di_plus": 0.0, "di_minus": 0.0}
        trs, pdms, ndms = [], [], []
        for i in range(1, len(c)):
            trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
            up = h[i] - h[i - 1]
            down = l[i - 1] - l[i]
            pdms.append(up if up > down and up > 0 else 0)
            ndms.append(down if down > up and down > 0 else 0)
        # Wilder ADX: average of the last `period` DX values
        dx_series = []
        for i in range(period - 1, len(trs)):
            s_tr = sum(trs[i - period + 1 : i + 1])
            s_pdm = sum(pdms[i - period + 1 : i + 1])
            s_ndm = sum(ndms[i - period + 1 : i + 1])
            di_p = 100 * s_pdm / s_tr if s_tr else 0
            di_n = 100 * s_ndm / s_tr if s_tr else 0
            dx_series.append(
                100 * abs(di_p - di_n) / (di_p + di_n) if (di_p + di_n) else 0
            )
        adx = sum(dx_series[-period:]) / period if dx_series else 0
        s_tr = sum(trs[-period:])
        di_p = 100 * sum(pdms[-period:]) / s_tr if s_tr else 0
        di_n = 100 * sum(ndms[-period:]) / s_tr if s_tr else 0
        return {
            "status": "ok",
            "adx": round(adx, 2),
            "di_plus": round(di_p, 2),
            "di_minus": round(di_n, 2),
        }

    def atr(self, klines: Sequence[Sequence]) -> float:
        o, h, l, c, v = self.to_chronological(klines)
        atr = _atr_series(h, l, c, self.atr_period)
        return atr[-1] if atr else 0.0

    def efficiency_ratio(self, klines: Sequence[Sequence], period: Optional[int] = None) -> float:
        """Kaufman efficiency ratio of the close series (0 = chop, 1 = clean trend)."""
        o, h, l, c, v = self.to_chronological(klines)
        return _efficiency_ratio(c, period or self.er_period)

    def atr_percentile(self, klines: Sequence[Sequence], window: int = 100) -> float:
        """Where current ATR sits in its own recent distribution (0..1)."""
        o, h, l, c, v = self.to_chronological(klines)
        series = _atr_series(h, l, c, self.atr_period)
        if len(series) < 5:
            return 0.5
        recent = series[-window:]
        return _percentile_rank(recent, recent[-1])

    def regime(self, klines: Sequence[Sequence]) -> dict:
        """Classify the tape into a tradeable regime.

        Returns ``{"regime": TREND_UP|TREND_DOWN|CHOP|VOLATILE, "tradeable": bool}``.
        This is the filter that keeps the engine out of chop.
        """
        o, h, l, c, v = self.to_chronological(klines)
        if len(c) < 60:
            return {"regime": "UNKNOWN", "tradeable": False}
        er = _efficiency_ratio(c, self.er_period)
        atr_s = _atr_series(h, l, c, self.atr_period)
        atr = atr_s[-1] if atr_s else 0.0
        pctl = _percentile_rank(atr_s[-100:], atr) if atr_s else 0.5
        struct = self.structure_from_arrays(
            h, l, c, atr,
            ema_fast=_ema_series(c, self.ema_fast)[-1],
            ema_slow=_ema_series(c, self.ema_slow)[-1],
        )
        state = struct.get("state")
        if pctl >= self.max_atr_percentile:
            regime = "VOLATILE"
        elif er >= max(self.min_er, 0.30) and state == "UPTREND":
            regime = "TREND_UP"
        elif er >= max(self.min_er, 0.30) and state == "DOWNTREND":
            regime = "TREND_DOWN"
        elif er < self.min_er:
            regime = "CHOP"
        else:
            regime = "RANGE"
        return {
            "regime": regime,
            "tradeable": regime in ("TREND_UP", "TREND_DOWN"),
            "efficiency_ratio": round(er, 3),
            "atr_percentile": round(pctl, 3),
            "structure": state,
        }

    # ── levels ──────────────────────────────────────────────────────────────

    def support_resistance_zones(
        self, klines: Sequence[Sequence], tolerance_atr: float = 0.5
    ) -> dict:
        """Cluster swing levels into S/R zones within a tolerance band."""
        swings = self.swing_points(klines)
        atr = self.atr(klines) or 0
        tol = tolerance_atr * atr
        supports, resistances = [], []
        for s in swings:
            bucket = supports if s.kind == "low" else resistances
            for zone in bucket:
                if abs(s.price - zone["price"]) <= max(tol, 1e-12):
                    zone["touches"] += 1
                    zone["price"] = (zone["price"] * (zone["touches"] - 1) + s.price) / zone["touches"]
                    break
            else:
                bucket.append({"price": s.price, "touches": 1})
        for zone in supports + resistances:
            zone["price"] = round(zone["price"], 6)
        supports.sort(key=lambda z: z["price"], reverse=True)
        resistances.sort(key=lambda z: z["price"])
        return {
            "status": "ok",
            "atr": round(atr, 6),
            "supports": supports[:5],
            "resistances": resistances[:5],
        }

    # ── candle quality ──────────────────────────────────────────────────────

    @staticmethod
    def _candle_shape(k) -> dict:
        o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
        rng = h - l
        body = abs(c - o)
        upper = h - max(o, c)
        lower = min(o, c) - l
        return {
            "bullish": c > o,
            "body_pct": body / rng if rng else 0,
            "upper_wick_pct": upper / rng if rng else 0,
            "lower_wick_pct": lower / rng if rng else 0,
        }

    def rejection_candle(self, klines: Sequence[Sequence]) -> Optional[str]:
        """Detect a pin-bar rejection on the most recent completed candle."""
        if len(klines) < 1:
            return None
        shape = self._candle_shape(klines[0])
        if shape["lower_wick_pct"] >= 0.6 and shape["body_pct"] <= 0.35:
            return "BULLISH_REJECTION"
        if shape["upper_wick_pct"] >= 0.6 and shape["body_pct"] <= 0.35:
            return "BEARISH_REJECTION"
        return None

    # ── the one decision function ───────────────────────────────────────────

    def decide_signal(
        self,
        *,
        price: float,
        atr: float,
        rsi_val: float,
        adx_val: float,
        ema_alignment: str,
        ema_fast_val: float,
        struct_state: str,
        bos: Optional[str] = None,
        last_high: Optional[float] = None,
        last_low: Optional[float] = None,
        rejection: Optional[str] = None,
        er: Optional[float] = None,
        atr_pctl: Optional[float] = None,
        slow_slope_atr: Optional[float] = None,
        min_confidence: float = 55.0,
        rr_min: float = 2.0,
        sl_atr_mult: float = 1.5,
    ) -> Signal:
        """Turn indicator values into a Signal.

        This is the *only* place entry rules live. `price_action_signal()` and
        the backtester both call it, so live and simulated behaviour cannot
        diverge.
        """
        if not price or atr <= 0:
            return Signal("WAIT", 0, price, price, [], 0, "NO_DATA",
                          ["insufficient data"],
                          ["insufficient candles or zero ATR"])

        confidence = 0.0
        reasons: List[str] = []
        filters: List[str] = []
        action = "WAIT"
        setup = "NONE"
        entry = price
        stop = price
        tps: List[float] = []
        rr = 0.0

        # Direction bias from structure + EMA stack
        bull_points = 0
        if struct_state == "UPTREND":
            bull_points += 2
            reasons.append("structure UPTREND (HH/HL)")
        elif struct_state == "DOWNTREND":
            bull_points -= 2
            reasons.append("structure DOWNTREND (LH/LL)")
        if ema_alignment == "BULLISH":
            bull_points += 2
            reasons.append("EMA stack bullish aligned")
        elif ema_alignment == "BEARISH":
            bull_points -= 2
            reasons.append("EMA stack bearish aligned")
        if bos == "BULLISH_BOS":
            bull_points += 1
            reasons.append("bullish break of structure")
        elif bos == "BEARISH_BOS":
            bull_points -= 1
            reasons.append("bearish break of structure")

        # ── regime gates ───────────────────────────────────────────────────
        if adx_val >= self.min_adx:
            reasons.append(f"ADX {adx_val:.1f} confirms directional movement")
        else:
            filters.append(f"ADX {adx_val:.1f} < {self.min_adx:.0f} — no directional move")

        # Efficiency ratio is the real trend-vs-chop filter (ADX alone is not:
        # random walks print high ADX). Only veto when it is clearly chop.
        if er is not None:
            if er < self.min_er:
                filters.append(
                    f"efficiency ratio {er:.2f} < {self.min_er:.2f} — choppy tape"
                )
            elif er >= 0.35:
                reasons.append(f"clean trend (efficiency {er:.2f})")

        # Volatility-spike guard: extreme ATR readings mean stops are unreliable.
        if atr_pctl is not None and atr_pctl >= self.max_atr_percentile:
            filters.append(
                f"ATR at {atr_pctl * 100:.0f}th percentile — volatility spike, stand down"
            )

        # Directional regime gate: a flat slow EMA means mean-reverting tape,
        # where trend entries are systematically faded. Measured on synthetic
        # regimes, EMA200 slope/ATR over 20 bars separates them cleanly:
        # trend ~3.9, choppy no-drift ~0.8, mean-reverting ~0.3.
        slope_val = slow_slope_atr
        if slope_val is not None and self.min_slow_slope_atr > 0:
            if slope_val >= self.min_slow_slope_atr:
                reasons.append(f"slow EMA rising {slope_val:.1f} ATR — trend regime")
            elif slope_val <= -self.min_slow_slope_atr:
                reasons.append(f"slow EMA falling {abs(slope_val):.1f} ATR — downtrend regime")
            else:
                filters.append(
                    f"slow EMA slope {slope_val:+.2f} ATR — mean-reverting tape, no trend edge"
                )

        # Anti-chase, two tiers. A hard cap only blocks genuine blow-off
        # extension (>6 ATR): a healthy trend rides 2-4 ATR from its fast EMA,
        # so blocking at 3 ATR rejects exactly the trends worth trading. The
        # soft band still penalises late entries via the confidence score.
        dist_atr = abs(price - ema_fast_val) / atr if atr else 0.0
        if ema_fast_val:
            soft_cap = self.caution_ema_dist_atr
            hard_cap = self.max_ema_dist_atr
            if hard_cap > 0 and dist_atr > hard_cap:
                filters.append(
                    f"price {dist_atr:.1f} ATR from EMA > {hard_cap:.0f} — blow-off extension, no entry"
                )
            elif soft_cap > 0 and dist_atr > soft_cap:
                filters.append(
                    f"price {dist_atr:.1f} ATR from EMA — late entry, prefer pullback"
                )

        # RSI only vetoes when the trend itself is weak: overbought/oversold
        # readings routinely persist in strong trends (that is what momentum
        # looks like), so we never fight a healthy trend with an oscillator.
        if rsi_val >= 75 and adx_val < 25:
            filters.append(f"RSI {rsi_val:.1f} overbought in weak trend — no new longs")
        elif rsi_val <= 25 and adx_val < 25:
            filters.append(f"RSI {rsi_val:.1f} oversold in weak trend — no new shorts")
        elif rsi_val >= 75:
            filters.append(f"RSI {rsi_val:.1f} hot — trim longs, no adds")
        elif rsi_val <= 25:
            filters.append(f"RSI {rsi_val:.1f} cold — trim shorts, no adds")

        hard_blocks = any(
            f.startswith("efficiency ratio")
            or f.startswith("ATR at")
            or (f.startswith("price ") and "blow-off" in f)
            for f in filters
        )

        slope_ok_long = (
            slope_val is None
            or self.min_slow_slope_atr <= 0
            or slope_val >= self.min_slow_slope_atr
        )
        slope_ok_short = (
            slope_val is None
            or self.min_slow_slope_atr <= 0
            or slope_val <= -self.min_slow_slope_atr
        )

        long_ok = (
            bull_points >= 3
            and adx_val >= self.min_adx
            and not hard_blocks
            and slope_ok_long
            and not (rsi_val >= 75 and adx_val < 25)
        )
        short_ok = (
            bull_points <= -3
            and adx_val >= self.min_adx
            and not hard_blocks
            and slope_ok_short
            and not (rsi_val <= 25 and adx_val < 25)
        )

        # Trigger refinement: pullback + rejection candle adds confidence
        if long_ok:
            action, setup = "LONG", "TREND_CONTINUATION"
            if ema_alignment == "BULLISH" and ema_fast_val:
                pullback = price <= ema_fast_val * 1.005
                if pullback:
                    setup = "PULLBACK_TO_EMA"
                    reasons.append("pullback into EMA zone")
            if rejection == "BULLISH_REJECTION":
                setup = "PULLBACK_REJECTION"
                reasons.append("bullish rejection candle at level")
            # Structure stop (below recent swing low), capped at 3x ATR so the
            # R:R math stays honest; otherwise a far-away swing low produces a
            # stop so wide the TP ladder can never reach the claimed R:R.
            stop = price - atr * sl_atr_mult
            if last_low and last_low < price:
                stop = max(last_low - atr * 0.2, price - 3 * atr)
            tps = [price + (price - stop) * r for r in (rr_min, rr_min * 1.5, rr_min * 2.5)]
            rr = rr_min
        elif short_ok:
            action, setup = "SHORT", "TREND_CONTINUATION"
            if ema_alignment == "BEARISH" and ema_fast_val:
                if price >= ema_fast_val * 0.995:
                    setup = "PULLBACK_TO_EMA"
                    reasons.append("pullback into EMA zone")
            if rejection == "BEARISH_REJECTION":
                setup = "PULLBACK_REJECTION"
                reasons.append("bearish rejection candle at level")
            stop = price + atr * sl_atr_mult
            if last_high and last_high > price:
                stop = min(last_high + atr * 0.2, price + 3 * atr)
            tps = [price - (stop - price) * r for r in (rr_min, rr_min * 1.5, rr_min * 2.5)]
            rr = rr_min

        if action != "WAIT":
            confidence = min(95.0, 40 + abs(bull_points) * 10)
            if setup.startswith("PULLBACK"):
                confidence += 10
            if rejection:
                confidence += 5
            if er is not None and er >= 0.35:
                confidence += 5
            if filters:
                confidence -= 8 * len(filters)

        meta = {
            "adx": round(adx_val, 2),
            "rsi": round(rsi_val, 2),
            "efficiency_ratio": round(er, 3) if er is not None else None,
            "atr_percentile": round(atr_pctl, 3) if atr_pctl is not None else None,
            "ema_distance_atr": round(dist_atr, 2),
            "slow_slope_atr": round(slope_val, 3) if slope_val is not None else None,
            "bull_points": bull_points,
        }

        if action == "WAIT":
            if confidence < min_confidence and filters:
                filters.append(
                    f"confidence {confidence:.0f} below threshold {min_confidence:.0f}"
                )
            return Signal("WAIT", round(confidence, 1), price, stop, tps, rr,
                          setup, reasons, filters, meta)

        if confidence < min_confidence:
            filters.append(f"confidence {confidence:.0f} below threshold {min_confidence:.0f}")
            return Signal("WAIT", round(confidence, 1), price, stop, tps, rr,
                          setup, reasons, filters, meta)

        return Signal(
            action=action,
            confidence=round(confidence, 1),
            entry=round(entry, 6),
            stop_loss=round(stop, 6),
            take_profits=[round(t, 6) for t in tps],
            rr=round(rr, 2),
            setup=setup,
            reasons=reasons,
            filters=filters,
            meta=meta,
        )

    # ── composite signal ────────────────────────────────────────────────────

    def price_action_signal(
        self,
        klines: Sequence[Sequence],
        min_confidence: float = 55.0,
        rr_min: float = 2.0,
        sl_atr_mult: float = 1.5,
    ) -> Signal:
        """Composite price-action trade signal.

        Combines:
          1. Market structure (HH/HL vs LH/LL)
          2. EMA stack alignment
          3. Trend-quality filters (ADX, efficiency ratio, anti-chase)
          4. Optional pullback + rejection-candle trigger
        Returns a Signal with entry/SL/TP ladder and confidence.
        """
        o, h, l, c, v = self.to_chronological(klines)
        if len(c) < 30:
            return Signal("WAIT", 0, 0.0, 0.0, [], 0, "NO_DATA",
                          ["insufficient data"],
                          ["insufficient candles or zero ATR"])

        atr_s = _atr_series(h, l, c, self.atr_period)
        atr = atr_s[-1] if atr_s else 0.0
        ema_f = _ema_series(c, self.ema_fast)
        ema_m = _ema_series(c, self.ema_mid)
        ema_s = _ema_series(c, self.ema_slow)
        f, m, s = ema_f[-1], ema_m[-1], ema_s[-1]
        price = c[-1]
        if f > m > s and price > f:
            alignment = "BULLISH"
        elif f < m < s and price < f:
            alignment = "BEARISH"
        elif price > s:
            alignment = "MIXED_BULL"
        else:
            alignment = "MIXED_BEAR"

        structure = self.structure_from_arrays(h, l, c, atr, ema_fast=f, ema_slow=s)
        rsi_s = _rsi_series(c, self.rsi_period)
        adx_s = _adx_series(h, l, c, self.adx_period)

        k = max(1, self.slope_lookback)
        if len(ema_s) > k and atr > 0:
            slow_slope_atr = (ema_s[-1] - ema_s[-1 - k]) / atr
        else:
            slow_slope_atr = None

        return self.decide_signal(
            price=price,
            atr=atr,
            rsi_val=rsi_s[-1] if rsi_s else 50.0,
            adx_val=adx_s[-1] if adx_s else 0.0,
            ema_alignment=alignment,
            ema_fast_val=f,
            struct_state=structure.get("state"),
            bos=structure.get("bos"),
            last_high=structure.get("last_high"),
            last_low=structure.get("last_low"),
            rejection=self.rejection_candle(klines),
            er=_efficiency_ratio(c, self.er_period),
            atr_pctl=_percentile_rank(atr_s[-100:], atr) if atr_s else None,
            slow_slope_atr=slow_slope_atr,
            min_confidence=min_confidence,
            rr_min=rr_min,
            sl_atr_mult=sl_atr_mult,
        )

    # ── trade plan (risk sizing) ────────────────────────────────────────────

    def trade_plan(
        self,
        signal: Signal,
        balance: float,
        risk_pct: float = 1.0,
        leverage: float = 1.0,
        fee_rate: float = 0.00055,
        rr_min: float = 2.0,
    ) -> dict:
        """Size a position from account balance and per-trade risk.

        Risk is always defined by the SL distance first (risk-first sizing).
        """
        if signal.action == "WAIT":
            return {"status": "no_trade", "msg": "No signal — plan withheld"}
        if balance <= 0:
            return {"status": "error", "msg": "Invalid balance"}
        risk_amount = balance * (risk_pct / 100.0)
        sl_distance = abs(signal.entry - signal.stop_loss)
        if sl_distance <= 0:
            return {"status": "error", "msg": "Invalid stop distance"}
        qty = risk_amount / sl_distance
        notional = qty * signal.entry
        margin = notional / max(leverage, 1.0)
        fees_est = fee_rate * notional * 2  # round trip

        if notional > balance:
            return {
                "status": "error",
                "msg": f"Notional {notional:.2f} USDT exceeds balance {balance:.2f} USDT",
                "suggested_leverage": round(notional / balance, 2),
            }

        # Expected values per R
        r_unit = risk_amount
        return {
            "status": "ok",
            "action": signal.action,
            "confidence": signal.confidence,
            "setup": signal.setup,
            "entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profits": signal.take_profits,
            "sl_distance": round(sl_distance, 6),
            "sl_distance_pct": round(sl_distance / signal.entry * 100, 3),
            "rr": signal.rr,
            "risk_pct": risk_pct,
            "risk_amount": round(risk_amount, 2),
            "qty": round(qty, 8),
            "notional": round(notional, 2),
            "margin": round(margin, 2),
            "est_fees": round(fees_est, 3),
            "leverage": leverage,
            "target_pnl_1r": round(r_unit - fees_est, 2),
            "reasons": signal.reasons,
            "filters": signal.filters,
            "meta": signal.meta,
        }


def analyze_price_action(
    klines: Sequence[Sequence],
    min_confidence: float = 55.0,
    rr_min: float = 2.0,
    engine: Optional[PriceActionEngine] = None,
) -> dict:
    """One-shot convenience wrapper returning the full PA analysis dict."""
    eng = engine or PriceActionEngine()
    structure = eng.market_structure(klines)
    emas = eng.ema_stack(klines)
    atr = eng.atr(klines)
    rsi = eng.rsi(klines)
    adx = eng.adx(klines)
    zones = eng.support_resistance_zones(klines)
    signal = eng.price_action_signal(klines, min_confidence, rr_min)
    return {
        "status": "ok",
        "structure": structure,
        "ema_stack": emas,
        "atr": round(atr, 6),
        "rsi": round(rsi, 2),
        "adx": adx,
        "zones": zones,
        "rejection_candle": eng.rejection_candle(klines),
        "regime": eng.regime(klines),
        "efficiency_ratio": round(eng.efficiency_ratio(klines), 3),
        "signal": {
            "action": signal.action,
            "confidence": signal.confidence,
            "setup": signal.setup,
            "entry": signal.entry,
            "stop_loss": signal.stop_loss,
            "take_profits": signal.take_profits,
            "rr": signal.rr,
            "reasons": signal.reasons,
            "filters": signal.filters,
            "meta": signal.meta,
        },
    }
