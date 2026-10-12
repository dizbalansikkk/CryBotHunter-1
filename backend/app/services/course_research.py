"""Causal course hypotheses. Research only; no exchange or order execution imports.

Source definitions: docs/trading-knowledge/rules.md K01-K03, K06, H01-H02.
Callers supply completed, contiguous bars from one instrument and venue.
"""

from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class ResearchBar:
    timestamp: int  # UTC open time in milliseconds
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Swing:
    index: int
    confirmed_at: int
    price: float


@dataclass(frozen=True)
class CourseContext:
    index: int
    regime: str
    swing_high: Swing | None
    swing_low: Swing | None
    upper_sweep: bool
    lower_sweep: bool
    close_above_high: bool
    close_below_low: bool
    fvg: dict | None


def validate_bars(bars: list[ResearchBar], interval_ms: int) -> None:
    if interval_ms <= 0:
        raise ValueError("interval_ms must be positive")
    for i, bar in enumerate(bars):
        values = (bar.open, bar.high, bar.low, bar.close)
        if not all(math.isfinite(v) and v > 0 for v in values):
            raise ValueError(f"invalid price at bar {i}")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError(f"invalid OHLC at bar {i}")
        if i and bar.timestamp - bars[i - 1].timestamp != interval_ms:
            raise ValueError(f"missing, unordered or duplicate bar at {i}")


def course_contexts(bars: list[ResearchBar], interval_ms: int) -> list[CourseContext]:
    """Each output depends only on the corresponding completed prefix.

    Strict three-bar pivots; last two highs/lows define a deliberately simple
    trend, not the course's discretionary Strong Swing/BOS hierarchy.
    A level is consumed on its first strict breach, including a close beyond it.
    """
    validate_bars(bars, interval_ms)
    highs: list[Swing] = []
    lows: list[Swing] = []
    used_high: int | None = None
    used_low: int | None = None
    result = []
    for i, bar in enumerate(bars):
        if i >= 2:
            left, middle = bars[i - 2], bars[i - 1]
            if middle.high > max(left.high, bar.high):
                highs.append(Swing(i - 1, i, middle.high))
            if middle.low < min(left.low, bar.low):
                lows.append(Swing(i - 1, i, middle.low))
        sh = highs[-1] if highs else None
        sl = lows[-1] if lows else None
        high_breach = bool(sh and sh.confirmed_at < i and used_high != sh.index and bar.high > sh.price)
        low_breach = bool(sl and sl.confirmed_at < i and used_low != sl.index and bar.low < sl.price)
        if high_breach:
            used_high = sh.index
        if low_breach:
            used_low = sl.index
        regime = "UNKNOWN"
        if len(highs) >= 2 and len(lows) >= 2:
            higher = highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price
            lower = highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price
            regime = "UP" if higher else "DOWN" if lower else "MIXED"
        fvg = None
        if i >= 2:
            if bar.low > bars[i - 2].high:
                fvg = {"side": "LONG", "low": bars[i - 2].high, "high": bar.low, "confirmed_at": i}
            elif bar.high < bars[i - 2].low:
                fvg = {"side": "SHORT", "low": bar.high, "high": bars[i - 2].low, "confirmed_at": i}
        result.append(CourseContext(
            i, regime, sh, sl,
            bool(high_breach and bar.close < sh.price),
            bool(low_breach and bar.close > sl.price),
            bool(high_breach and bar.close > sh.price),
            bool(low_breach and bar.close < sl.price),
            fvg,
        ))
    return result


@dataclass(frozen=True)
class ResearchParameters:
    fee_rate: float = 0.0006
    slippage_bps: float = 5.0
    stop_buffer_bps: float = 5.0
    reward_r: float = 2.0
    max_holding_bars: int = 24

    def validate(self) -> None:
        for name in ("fee_rate", "slippage_bps", "stop_buffer_bps", "reward_r"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid {name}")
        if self.fee_rate >= 1 or max(self.slippage_bps, self.stop_buffer_bps) >= 10000:
            raise ValueError("cost/buffer rates must be below 100%")
        if self.reward_r <= 0 or not isinstance(self.max_holding_bars, int) or self.max_holding_bars < 1:
            raise ValueError("reward and holding period must be positive")


def evaluate_sweeps(
    bars: list[ResearchBar], interval_ms: int, *, start: int, end: int,
    parameters: ResearchParameters = ResearchParameters(),
    trend_filter: bool = False,
) -> dict:
    """Fixed-risk, non-overlapping, next-open hypothesis test in [start, end).

    Warm-up uses the prefix, no earlier trades. Exit horizon must fit inside
    the slice. Stop wins intrabar ambiguities. Funding is unavailable: reports
    explicitly prohibit promotion regardless of the observed result.
    """
    parameters.validate()
    if not 0 <= start < end <= len(bars):
        raise ValueError("invalid evaluation slice")
    contexts = course_contexts(bars[:end], interval_ms)
    slip = parameters.slippage_bps / 10000
    buffer = parameters.stop_buffer_bps / 10000
    trades = []
    skipped = {"ambiguous_sweep": 0, "gap_invalid_stop": 0, "trend_filter": 0}
    available_at = start
    for i in range(max(start, 2), end - parameters.max_holding_bars):
        if i < available_at:
            continue
        context = contexts[i]
        if not (context.upper_sweep or context.lower_sweep):
            continue
        if context.upper_sweep and context.lower_sweep:
            skipped["ambiguous_sweep"] += 1
            continue
        direction = 1 if context.lower_sweep else -1
        if trend_filter and context.regime != ("UP" if direction == 1 else "DOWN"):
            skipped["trend_filter"] += 1
            continue
        entry_index = i + 1
        entry = bars[entry_index].open * (1 + direction * slip)
        stop = bars[i].low * (1 - buffer) if direction == 1 else bars[i].high * (1 + buffer)
        risk = direction * (entry - stop)
        if risk <= 0:
            skipped["gap_invalid_stop"] += 1
            continue
        take = entry + direction * risk * parameters.reward_r
        if take <= 0:
            skipped["gap_invalid_stop"] += 1
            continue
        last = entry_index + parameters.max_holding_bars - 1
        exit_price, exit_index, reason = bars[last].close, last, "TIME"
        for j in range(entry_index, last + 1):
            bar = bars[j]
            stop_gap = bar.open <= stop if direction == 1 else bar.open >= stop
            take_gap = bar.open >= take if direction == 1 else bar.open <= take
            stop_hit = bar.low <= stop if direction == 1 else bar.high >= stop
            take_hit = bar.high >= take if direction == 1 else bar.low <= take
            if stop_gap:
                exit_price, exit_index, reason = bar.open, j, "STOP_GAP"
                break
            if take_gap:
                exit_price, exit_index, reason = take, j, "TAKE_GAP"
                break
            if stop_hit:
                exit_price, exit_index, reason = stop, j, "STOP"
                break
            if take_hit:
                exit_price, exit_index, reason = take, j, "TAKE"
                break
        exit_fill = exit_price * (1 - direction * slip)
        fees_r = (entry + exit_fill) * parameters.fee_rate / risk
        net_r = direction * (exit_fill - entry) / risk - fees_r
        trades.append({
            "signal_index": i, "entry_index": entry_index, "exit_index": exit_index,
            "signal_time": bars[i].timestamp, "entry_time": bars[entry_index].timestamp,
            "exit_bar_time": bars[exit_index].timestamp,
            "side": "LONG" if direction == 1 else "SHORT", "regime": context.regime,
            "entry": entry, "stop": stop, "take": take, "exit": exit_fill,
            "reason": reason, "net_r": net_r, "fees_r": fees_r,
        })
        available_at = exit_index + 1
    equity = peak = drawdown = 0.0
    for trade in trades:
        equity += trade["net_r"]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins = sum(t["net_r"] > 0 for t in trades)
    gains = sum(max(t["net_r"], 0) for t in trades)
    losses = -sum(min(t["net_r"], 0) for t in trades)
    return {
        "hypothesis": "H02_WITH_H01" if trend_filter else "H02",
        "parameters": asdict(parameters), "start_index": start, "end_index": end,
        "trades_count": len(trades), "wins": wins,
        "win_rate": wins / len(trades) if trades else None,
        "net_r": equity, "mean_r": equity / len(trades) if trades else None,
        "profit_factor": gains / losses if losses else None,
        "max_closed_trade_drawdown_r": drawdown, "skipped": skipped, "trades": trades,
        "promotion_allowed": False,
        "limitations": ["funding_not_modelled", "fixed_exits_not_production_lifecycle",
                        "no_orderbook_or_liquidation_simulation", "no_intrabar_equity_drawdown"],
    }
