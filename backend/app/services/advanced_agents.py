from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Candle, Position
from app.schemas.dto import AgentDecisionOut, MarketCoin


def _decision(
    name: str,
    coin: MarketCoin,
    action: str,
    confidence: float,
    rationale: str,
    **context: Any,
) -> AgentDecisionOut:
    return AgentDecisionOut(
        agent_name=name,
        symbol=coin.symbol,
        action=action,
        confidence=round(max(0.0, min(float(confidence), 1.0)), 2),
        rationale=rationale,
        context={"gate_kind": "SAFETY", **context},
    )


class PortfolioCorrelationAgent:
    """Limits duplicated directional exposure using synchronized candle returns."""

    name = "PortfolioCorrelationAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    async def decide(
        self,
        db: AsyncSession,
        coin: MarketCoin,
        signal: str,
        timeframe: str = "1h",
    ) -> AgentDecisionOut:
        positions = list(
            (
                await db.execute(
                    select(Position).where(
                        Position.status == "OPEN",
                        Position.symbol != coin.symbol,
                    )
                )
            ).scalars().all()
        )
        if not positions:
            return _decision(
                self.name,
                coin,
                "ALLOW",
                0.9,
                "В портфеле нет других открытых позиций; корреляционная концентрация отсутствует.",
                open_positions=0,
                risk_multiplier=1.0,
            )

        symbols = {coin.symbol, "BTC/USDT", "BNB/USDT", *(position.symbol for position in positions)}
        rows = list(
            (
                await db.execute(
                    select(Candle)
                    .where(Candle.symbol.in_(symbols), Candle.timeframe == timeframe)
                    .order_by(Candle.timestamp.desc())
                    .limit(max(len(symbols), 1) * 140)
                )
            ).scalars().all()
        )
        series = self._series(rows)
        return self.evaluate(coin, signal, positions, series)

    def evaluate(
        self,
        coin: MarketCoin,
        signal: str,
        positions: list[Any],
        series: dict[str, dict[Any, float]],
    ) -> AgentDecisionOut:
        candidate_side = 1 if signal == "BUY" else -1
        exposures: list[dict[str, Any]] = []
        for position in positions:
            correlation, samples = self._correlation(series.get(coin.symbol, {}), series.get(position.symbol, {}))
            if correlation is None:
                continue
            position_side = 1 if str(position.side).upper() == "LONG" else -1
            effective = correlation * candidate_side * position_side
            notional = max(float(position.current_price or position.entry_price) * float(position.volume), 0.0)
            exposures.append(
                {
                    "symbol": position.symbol,
                    "correlation": round(correlation, 4),
                    "effective_correlation": round(effective, 4),
                    "notional": round(notional, 4),
                    "samples": samples,
                }
            )

        total_notional = sum(item["notional"] for item in exposures)
        weighted = (
            sum(max(item["effective_correlation"], 0.0) * item["notional"] for item in exposures) / total_notional
            if total_notional > 0
            else 0.0
        )
        clustered = [item for item in exposures if item["effective_correlation"] >= 0.7]
        btc_corr, btc_samples = self._correlation(series.get(coin.symbol, {}), series.get("BTC/USDT", {}))
        bnb_corr, bnb_samples = self._correlation(series.get(coin.symbol, {}), series.get("BNB/USDT", {}))
        block_threshold = float(getattr(self.settings, "portfolio_correlation_block_threshold", 0.85))
        reduce_threshold = float(getattr(self.settings, "portfolio_correlation_reduce_threshold", 0.65))

        if len(clustered) >= 2 and weighted >= block_threshold:
            action, confidence, multiplier = "BLOCK", 0.94, 0.0
            rationale = (
                f"Вход создаёт чрезмерный корреляционный кластер: {len(clustered)} позиции, "
                f"взвешенная направленная корреляция={weighted:.2f}."
            )
        elif weighted >= reduce_threshold or clustered:
            action, confidence = "REDUCE_SIZE", 0.84
            multiplier = float(getattr(self.settings, "portfolio_correlation_risk_multiplier", 0.6))
            rationale = (
                f"Новый вход частично дублирует портфельный риск: кластеров={len(clustered)}, "
                f"взвешенная корреляция={weighted:.2f}; риск снижен до {multiplier:.2f}x."
            )
        elif not exposures:
            action, confidence = "REDUCE_SIZE", 0.62
            multiplier = float(getattr(self.settings, "portfolio_correlation_missing_data_multiplier", 0.8))
            rationale = (
                "Для открытых позиций недостаточно синхронной истории корреляции; "
                f"вход разрешён только с риском {multiplier:.2f}x."
            )
        else:
            action, confidence, multiplier = "ALLOW", 0.84, 1.0
            rationale = f"Корреляционная концентрация приемлема: взвешенное значение={weighted:.2f}."

        return _decision(
            self.name,
            coin,
            action,
            confidence,
            rationale,
            open_positions=len(positions),
            weighted_directional_correlation=round(weighted, 4),
            clustered_positions=len(clustered),
            exposures=exposures,
            btc_correlation=round(btc_corr, 4) if btc_corr is not None else None,
            btc_samples=btc_samples,
            bnb_correlation=round(bnb_corr, 4) if bnb_corr is not None else None,
            bnb_samples=bnb_samples,
            risk_multiplier=multiplier,
        )

    def _series(self, rows: list[Candle]) -> dict[str, dict[Any, float]]:
        closes: dict[str, dict[Any, float]] = defaultdict(dict)
        for row in rows:
            if float(row.close) > 0:
                closes[row.symbol][row.timestamp] = float(row.close)
        returns: dict[str, dict[Any, float]] = {}
        for symbol, values in closes.items():
            # The newest persisted candle may still be forming.  Excluding it
            # prevents a partial bar from creating a transient correlation.
            ordered = sorted(values.items())[:-1]
            returns[symbol] = {
                current_time: math.log(current / previous)
                for (_, previous), (current_time, current) in zip(ordered, ordered[1:])
                if previous > 0 and current > 0
            }
        return returns

    def _correlation(
        self,
        left: dict[Any, float],
        right: dict[Any, float],
    ) -> tuple[float | None, int]:
        keys = sorted(set(left).intersection(right))
        minimum = max(int(getattr(self.settings, "portfolio_correlation_min_samples", 30)), 10)
        if len(keys) < minimum:
            return None, len(keys)
        x = [left[key] for key in keys]
        y = [right[key] for key in keys]
        x_mean, y_mean = mean(x), mean(y)
        numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
        denominator = math.sqrt(sum((a - x_mean) ** 2 for a in x) * sum((b - y_mean) ** 2 for b in y))
        return (numerator / denominator if denominator > 0 else 0.0), len(keys)


class ExecutionCostAgent:
    """Rejects entries whose estimated round-trip friction consumes the expected move."""

    name = "ExecutionCostAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    def decide(
        self,
        coin: MarketCoin,
        snapshot: dict[str, Any] | None,
        candidate_notional: float,
    ) -> AgentDecisionOut:
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        spread_bps = max(float(snapshot.get("spread_bps") or coin.spread_bps or 0.0), 0.0)
        fee_bps = max(float(self.settings.paper_fee_rate), 0.0) * 20_000
        base_impact = max(float(self.settings.execution_market_impact_bps), 0.0) * 2
        book_depth = max(
            float(snapshot.get("bid_depth_quote") or 0.0),
            float(snapshot.get("ask_depth_quote") or 0.0),
        )
        participation = candidate_notional / book_depth if candidate_notional > 0 and book_depth > 0 else 0.0
        dynamic_impact = min(participation * 1_000, 100.0)
        total_cost_bps = spread_bps + fee_bps + base_impact + dynamic_impact
        expected_move_bps = max(float(coin.atr) / max(float(coin.price), 1e-9) * 10_000, 1.0)
        edge_fraction = total_cost_bps / expected_move_bps
        hard_bps = float(getattr(self.settings, "execution_cost_hard_max_bps", 60.0))
        hard_fraction = float(getattr(self.settings, "execution_cost_hard_edge_fraction", 0.45))
        reduce_fraction = float(getattr(self.settings, "execution_cost_reduce_edge_fraction", 0.25))

        if total_cost_bps > hard_bps or edge_fraction > hard_fraction or participation > 0.08:
            action, confidence, multiplier = "BLOCK", 0.95, 0.0
            rationale = (
                f"Ожидаемые издержки {total_cost_bps:.1f} bps поглощают {edge_fraction:.0%} ATR-движения "
                f"(участие в видимой глубине={participation:.1%}); вход экономически невыгоден."
            )
        elif edge_fraction > reduce_fraction or participation > 0.03:
            action, confidence = "REDUCE_SIZE", 0.86
            multiplier = float(getattr(self.settings, "execution_cost_risk_multiplier", 0.65))
            rationale = (
                f"Издержки повышены: {total_cost_bps:.1f} bps, {edge_fraction:.0%} ожидаемого движения; "
                f"риск снижен до {multiplier:.2f}x."
            )
        else:
            action, confidence, multiplier = "ALLOW", 0.88, 1.0
            rationale = f"Издержки приемлемы: {total_cost_bps:.1f} bps или {edge_fraction:.0%} ATR-движения."

        return _decision(
            self.name,
            coin,
            action,
            confidence,
            rationale,
            spread_bps=round(spread_bps, 4),
            round_trip_fee_bps=round(fee_bps, 4),
            estimated_impact_bps=round(base_impact + dynamic_impact, 4),
            total_cost_bps=round(total_cost_bps, 4),
            expected_move_bps=round(expected_move_bps, 4),
            edge_fraction=round(edge_fraction, 4),
            depth_participation=round(participation, 4),
            risk_multiplier=multiplier,
        )


class CrossTimeframeAgent:
    """Requires the entry direction to survive independent timeframe structure checks."""

    name = "CrossTimeframeAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    def decide(
        self,
        coin: MarketCoin,
        signal: str,
        candles_by_timeframe: dict[str, list[Any]] | None,
    ) -> AgentDecisionOut:
        structures: dict[str, str] = {}
        for timeframe, rows in (candles_by_timeframe or {}).items():
            # Public OHLCV responses normally include the in-progress bar.
            closes = self._closes(rows)[:-1]
            if len(closes) < 55:
                continue
            ema20 = self._ema(closes, 20)
            ema50 = self._ema(closes, 50)
            previous_ema20 = self._ema(closes[:-5], 20)
            bullish = closes[-1] > ema20 > ema50 and ema20 > previous_ema20
            bearish = closes[-1] < ema20 < ema50 and ema20 < previous_ema20
            structures[timeframe] = "BUY" if bullish else "SELL" if bearish else "NEUTRAL"

        opposing = sum(value in {"BUY", "SELL"} and value != signal for value in structures.values())
        supporting = sum(value == signal for value in structures.values())
        minimum = max(int(getattr(self.settings, "cross_timeframe_min_available", 2)), 1)
        if opposing >= 2:
            action, confidence, multiplier = "BLOCK", 0.94, 0.0
            rationale = f"Два или более таймфрейма направлены против {signal}: {structures}."
        elif len(structures) < minimum:
            action, confidence = "REDUCE_SIZE", 0.62
            multiplier = float(getattr(self.settings, "cross_timeframe_missing_data_multiplier", 0.75))
            rationale = (
                f"Доступно только {len(structures)}/{minimum} пригодных таймфреймов; "
                f"риск снижен до {multiplier:.2f}x."
            )
        elif supporting < 2 or opposing:
            action, confidence = "REDUCE_SIZE", 0.8
            multiplier = float(getattr(self.settings, "cross_timeframe_mixed_risk_multiplier", 0.65))
            rationale = f"Мультитаймфреймовая структура смешанная: {structures}; риск снижен до {multiplier:.2f}x."
        else:
            action, confidence, multiplier = "ALLOW", 0.9, 1.0
            rationale = f"Направление {signal} подтверждено независимыми таймфреймами: {structures}."
        return _decision(
            self.name,
            coin,
            action,
            confidence,
            rationale,
            structures=structures,
            supporting_timeframes=supporting,
            opposing_timeframes=opposing,
            risk_multiplier=multiplier,
        )

    def _closes(self, rows: list[Any]) -> list[float]:
        values: list[float] = []
        for row in rows:
            raw = row[4] if isinstance(row, (list, tuple)) and len(row) >= 5 else getattr(row, "close", None)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                values.append(value)
        return values

    def _ema(self, values: list[float], period: int) -> float:
        alpha = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = alpha * value + (1 - alpha) * result
        return result


class CalibrationDriftAgent:
    """Detects when approved committee confidence stops matching realized outcomes."""

    name = "CalibrationDriftAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    async def decide(self, db: AsyncSession, coin: MarketCoin) -> AgentDecisionOut:
        rows = list(
            (
                await db.execute(
                    select(Position)
                    .where(Position.status == "CLOSED", Position.symbol == coin.symbol)
                    .order_by(Position.closed_at.desc())
                    .limit(80)
                )
            ).scalars().all()
        )
        outcomes: list[tuple[float, float]] = []
        for row in rows:
            context = row.entry_context if isinstance(row.entry_context, dict) else {}
            if context.get("regime") not in {None, "", coin.regime}:
                continue
            confidence = context.get("committee_confidence", context.get("entry_confidence"))
            try:
                probability = max(0.0, min(float(confidence), 1.0))
            except (TypeError, ValueError):
                continue
            outcomes.append((probability, 1.0 if float(row.pnl or 0.0) > 0 else 0.0))
        return self.evaluate(coin, outcomes)

    def evaluate(self, coin: MarketCoin, outcomes: list[tuple[float, float]]) -> AgentDecisionOut:
        minimum = max(int(getattr(self.settings, "calibration_drift_min_outcomes", 10)), 5)
        block_minimum = max(int(getattr(self.settings, "calibration_drift_block_outcomes", 20)), minimum)
        recent = outcomes[:10]
        baseline = outcomes[10:]
        recent_win_rate = mean(item[1] for item in recent) if recent else None
        baseline_win_rate = mean(item[1] for item in baseline) if baseline else None
        brier = mean((probability - outcome) ** 2 for probability, outcome in outcomes) if outcomes else None
        drift = (
            baseline_win_rate - recent_win_rate
            if baseline_win_rate is not None and recent_win_rate is not None
            else 0.0
        )
        if len(outcomes) >= block_minimum and recent_win_rate is not None and recent_win_rate <= 0.3 and drift >= 0.2:
            action, confidence, multiplier = "BLOCK", 0.93, 0.0
            rationale = (
                f"Калибровка {coin.symbol}/{coin.regime} резко ухудшилась: последние 10 исходов "
                f"дают {recent_win_rate:.0%} успеха, падение={drift:.0%}."
            )
        elif len(outcomes) >= minimum and (
            (recent_win_rate is not None and recent_win_rate < 0.45) or (brier is not None and brier > 0.30)
        ):
            action, confidence = "REDUCE_SIZE", 0.84
            multiplier = float(getattr(self.settings, "calibration_drift_risk_multiplier", 0.55))
            rationale = (
                f"Найдена деградация калибровки {coin.symbol}/{coin.regime}: win rate={recent_win_rate:.0%}, "
                f"Brier={brier:.3f}; риск снижен до {multiplier:.2f}x."
            )
        else:
            action, confidence, multiplier = "ALLOW", 0.78 if len(outcomes) >= minimum else 0.58, 1.0
            rationale = (
                f"Калибровка стабильна на {len(outcomes)} исходах; Brier={brier:.3f}."
                if brier is not None and len(outcomes) >= minimum
                else f"Накоплено {len(outcomes)}/{minimum} сопоставимых исходов; статистического veto пока нет."
            )
        return _decision(
            self.name,
            coin,
            action,
            confidence,
            rationale,
            observations=len(outcomes),
            recent_win_rate=round(recent_win_rate, 4) if recent_win_rate is not None else None,
            baseline_win_rate=round(baseline_win_rate, 4) if baseline_win_rate is not None else None,
            brier_score=round(brier, 4) if brier is not None else None,
            drift=round(drift, 4),
            regime=coin.regime,
            risk_multiplier=multiplier,
        )


class EventRiskAgent:
    """Uses only confirmed event/market fields and never converts an event into direction."""

    name = "EventRiskAgent"

    def __init__(self) -> None:
        self.settings = get_settings()

    def decide(self, coin: MarketCoin, event_context: dict[str, Any] | None) -> AgentDecisionOut:
        event_context = event_context if isinstance(event_context, dict) else {}
        market_context = coin.market_context if isinstance(coin.market_context, dict) else {}
        event_score = int(event_context.get("event_score") or 0)
        event_status = str(event_context.get("status") or "NORMAL")
        confirmed_listing = bool(market_context.get("confirmed_listing_event"))
        confirmed_macro = bool(market_context.get("confirmed_macro_event"))
        oi_change = market_context.get("open_interest_change_percent")
        try:
            oi_change_value = abs(float(oi_change)) if oi_change is not None else 0.0
        except (TypeError, ValueError):
            oi_change_value = 0.0
        volume_ratio = coin.volume_24h / coin.volume_average_24h if coin.volume_average_24h > 0 else 1.0
        extreme_funding = abs(float(coin.funding_rate)) > float(self.settings.extreme_funding_rate_abs)
        shock = abs(float(coin.price_change_percent)) >= 12 or volume_ratio >= 3 or oi_change_value >= 25

        if extreme_funding and shock:
            action, confidence, multiplier = "BLOCK", 0.95, 0.0
            rationale = "Аномальный funding совпал с подтверждённым рыночным шоком; вход заблокирован."
        elif confirmed_macro or confirmed_listing or (event_score > 0 and shock):
            action, confidence = "REDUCE_SIZE", 0.88
            multiplier = float(getattr(self.settings, "event_risk_multiplier", 0.6))
            rationale = (
                f"Подтверждённое событие сопровождается повышенным риском: status={event_status}, "
                f"volume={volume_ratio:.2f}x, OI change={oi_change_value:.1f}%; риск={multiplier:.2f}x."
            )
        elif event_score > 0:
            action, confidence, multiplier = "ALLOW", 0.75, 1.0
            rationale = (
                f"Событие {event_status} повышает только приоритет анализа и не меняет направление "
                "или риск без отдельного подтверждённого рыночного шока."
            )
        else:
            action, confidence, multiplier = "ALLOW", 0.8, 1.0
            rationale = "Подтверждённых событий, требующих ограничения входа, нет."
        return _decision(
            self.name,
            coin,
            action,
            confidence,
            rationale,
            event_status=event_status,
            event_score=event_score,
            confirmed_listing=confirmed_listing,
            confirmed_macro=confirmed_macro,
            funding_rate=coin.funding_rate,
            volume_ratio=round(volume_ratio, 4),
            open_interest_change_percent=round(oi_change_value, 4),
            market_shock=shock,
            risk_multiplier=multiplier,
        )
