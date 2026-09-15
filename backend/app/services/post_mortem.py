from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import Order, Position, TradePostMortem
from app.services.exchange import ExchangeClient
from app.services.microstructure import MicrostructureService


AVOIDABLE_LABELS = {
    "EARLY_EXIT_FROM_PROFIT",
    "HELD_AFTER_EARLY_INVALIDATION",
    "ENTRY_AGAINST_ORDER_FLOW",
    "LATE_ENTRY_EXHAUSTION",
    "EXECUTION_COST_DAMAGE",
}

LESSONS = {
    "EARLY_EXIT_FROM_PROFIT": "Не закрывать позицию из-за одиночного шумового движения после заметного MFE; сначала проверить инвалидирующий сигнал.",
    "HELD_AFTER_EARLY_INVALIDATION": "После раннего движения против позиции требовать возврат уровня; без возврата сокращать риск до полного стопа.",
    "ENTRY_AGAINST_ORDER_FLOW": "Не входить, когда стакан и лента одновременно подтверждают противоположную сторону.",
    "LATE_ENTRY_EXHAUSTION": "Не догонять уже растянутый импульс без отката или повторного подтверждения ликвидности.",
    "EXECUTION_COST_DAMAGE": "Пропускать вход, если ожидаемые комиссии, спред и проскальзывание съедают существенную часть планового риска.",
    "VALID_STOP": "Стоп исполнен по плану: сохранять дисциплину риска, но продолжить проверку качества самого входа.",
    "UNCLASSIFIED_LOSS": "Недостаточно данных для точной причины; пример сохранён с высоким приоритетом для повторного анализа.",
}


class PostMortemService:
    def __init__(self, exchange: ExchangeClient) -> None:
        self.exchange = exchange
        self.settings = get_settings()
        self.microstructure = MicrostructureService(exchange)

    async def analyze_loss(
        self,
        db: AsyncSession,
        position: Position,
        exit_order: Order,
        reason: str,
    ) -> TradePostMortem | None:
        if not self.settings.post_mortem_enabled or float(position.pnl or 0.0) >= 0 or not position.id:
            return None
        existing = (
            await db.execute(select(TradePostMortem).where(TradePostMortem.position_id == position.id))
        ).scalar_one_or_none()
        if existing:
            return existing

        entry_context = dict(position.entry_context or {})
        close_snapshot, path = await asyncio.gather(
            self.microstructure.capture(position.symbol),
            self._market_path(position),
        )
        path_metrics = self.path_metrics(position, path)
        planned_risk = float(
            entry_context.get("planned_risk")
            or abs(float(position.initial_risk or 0.0)) * float(position.volume or 0.0)
        )
        result_r = float(position.pnl or 0.0) / planned_risk if planned_risk > 0 else -1.0
        execution = self.execution_snapshot(entry_context, exit_order, position.volume, planned_risk)
        entry_snapshot = entry_context.get("microstructure") if isinstance(entry_context.get("microstructure"), dict) else {}
        labels, strategy_followed = self.classify(
            position=position,
            reason=reason,
            entry_snapshot=entry_snapshot,
            path_metrics=path_metrics,
            execution=execution,
        )
        reward_components = self.reward_components(result_r, labels, reason, strategy_followed)
        shaped_reward = round(sum(reward_components.values()), 4)
        priority = round(min(10.0, 1.0 + abs(result_r) + len(set(labels) & AVOIDABLE_LABELS) * 0.75), 4)
        lessons = [LESSONS[label] for label in labels if label in LESSONS]
        primary_label = labels[0] if labels else "UNCLASSIFIED_LOSS"
        record = TradePostMortem(
            position_id=position.id,
            symbol=position.symbol,
            side=position.side,
            pnl=round(float(position.pnl or 0.0), 4),
            planned_risk=round(planned_risk, 4),
            result_r=round(result_r, 4),
            shaped_reward=shaped_reward,
            priority=priority,
            primary_label=primary_label,
            behavior_labels=labels,
            strategy_followed=strategy_followed,
            market_snapshot={
                "entry": entry_snapshot,
                "during": entry_context.get("position_microstructure", []),
                "exit": close_snapshot,
                "path": path_metrics,
            },
            execution_snapshot=execution,
            reward_components=reward_components,
            lessons=lessons,
            entered_at=self._aware(position.entered_at),
            closed_at=self._aware(position.closed_at or datetime.now(timezone.utc)),
        )
        db.add(record)
        await db.flush()
        entry_context["post_mortem"] = {
            "id": record.id,
            "primary_label": primary_label,
            "behavior_labels": labels,
            "strategy_followed": strategy_followed,
            "result_r": round(result_r, 4),
            "shaped_reward": shaped_reward,
            "priority": priority,
            "lessons": lessons,
        }
        position.entry_context = entry_context
        return record

    async def _market_path(self, position: Position) -> list[list[float]]:
        entered_at = self._aware(position.entered_at)
        closed_at = self._aware(position.closed_at or datetime.now(timezone.utc))
        lookback = max(int(self.settings.post_mortem_lookback_minutes), 10)
        duration_minutes = max(int((closed_at - entered_at).total_seconds() // 60), 1)
        limit = min(max(lookback + duration_minutes + 5, 35), 500)
        since = int((entered_at - timedelta(minutes=lookback)).timestamp() * 1000)
        try:
            return await asyncio.wait_for(
                self.exchange.fetch_ohlcv(position.symbol, timeframe="1m", limit=limit, since=since),
                timeout=max(float(self.settings.post_mortem_market_timeout_seconds), 1.0),
            )
        except Exception:
            return []

    def path_metrics(self, position: Position, rows: list[list[float]]) -> dict[str, Any]:
        entered_ms = int(self._aware(position.entered_at).timestamp() * 1000)
        closed_ms = int(self._aware(position.closed_at or datetime.now(timezone.utc)).timestamp() * 1000)
        valid = [row for row in rows if isinstance(row, (list, tuple)) and len(row) >= 6 and self._number(row[4]) > 0]
        pre = [row for row in valid if self._number(row[0]) < entered_ms]
        during = [row for row in valid if entered_ms <= self._number(row[0]) <= closed_ms + 60_000]
        direction = 1.0 if position.side.upper() == "LONG" else -1.0
        entry = max(float(position.entry_price), 1e-12)
        risk_per_unit = max(float(position.initial_risk or abs(position.entry_price - position.stop)), 1e-12)
        risk_percent = risk_per_unit / entry * 100
        pre_change = 0.0
        if pre:
            pre_change = (self._number(pre[-1][4]) / self._number(pre[0][4]) - 1) * 100
        directional_r: list[tuple[int, float]] = []
        closes: list[float] = []
        for row in during:
            timestamp = int(self._number(row[0]))
            close = self._number(row[4])
            closes.append(close)
            directional_percent = direction * (close / entry - 1) * 100
            directional_r.append((timestamp, directional_percent / risk_percent if risk_percent > 0 else 0.0))
        first_ten = [value for timestamp, value in directional_r if timestamp <= entered_ms + 10 * 60_000]
        adverse_first_ten = min(first_ten, default=0.0)
        first_invalid_index = next((index for index, (_timestamp, value) in enumerate(directional_r) if value <= -0.5), None)
        recovery = 0.0
        if first_invalid_index is not None:
            invalid_value = directional_r[first_invalid_index][1]
            recovery = max((value - invalid_value for _timestamp, value in directional_r[first_invalid_index:]), default=0.0)
        log_returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:]) if previous > 0]
        favorable_price = float(position.highest_price or entry) if direction > 0 else float(position.lowest_price or entry)
        adverse_price = float(position.lowest_price or entry) if direction > 0 else float(position.highest_price or entry)
        mfe_r = direction * (favorable_price - entry) / risk_per_unit
        mae_r = direction * (adverse_price - entry) / risk_per_unit
        return {
            "candles": len(valid),
            "pre_entry_change_percent": round(pre_change, 4),
            "pre_entry_directional_r": round(direction * pre_change / risk_percent, 4) if risk_percent > 0 else 0.0,
            "during_change_percent": round((closes[-1] / entry - 1) * 100, 4) if closes else 0.0,
            "realized_volatility_1m_percent": round(pstdev(log_returns) * 100, 4) if len(log_returns) > 1 else 0.0,
            "mfe_r": round(max(mfe_r, 0.0), 4),
            "mae_r": round(min(mae_r, 0.0), 4),
            "adverse_first_10m_r": round(adverse_first_ten, 4),
            "recovery_after_invalidation_r": round(max(recovery, 0.0), 4),
        }

    def execution_snapshot(
        self,
        entry_context: dict[str, Any],
        exit_order: Order,
        volume: float,
        planned_risk: float,
    ) -> dict[str, Any]:
        entry = entry_context.get("entry_execution") if isinstance(entry_context.get("entry_execution"), dict) else {}
        entry_fee = self._number(entry.get("fee"))
        exit_fee = self._number(exit_order.fee)
        entry_slippage_cost = self._number(entry.get("slippage")) * self._number(entry.get("volume") or volume)
        exit_slippage_cost = self._number(exit_order.slippage) * self._number(exit_order.filled_amount or volume)
        total_cost = entry_fee + exit_fee + entry_slippage_cost + exit_slippage_cost
        return {
            "entry_fee": round(entry_fee, 6),
            "exit_fee": round(exit_fee, 6),
            "entry_slippage_cost": round(entry_slippage_cost, 6),
            "exit_slippage_cost": round(exit_slippage_cost, 6),
            "total_cost": round(total_cost, 6),
            "cost_to_planned_risk": round(total_cost / planned_risk, 4) if planned_risk > 0 else 0.0,
            "exit_order_id": exit_order.id,
        }

    def classify(
        self,
        *,
        position: Position,
        reason: str,
        entry_snapshot: dict[str, Any],
        path_metrics: dict[str, Any],
        execution: dict[str, Any],
    ) -> tuple[list[str], bool]:
        labels: list[str] = []
        if reason == "MANUAL" and float(path_metrics.get("mfe_r") or 0.0) >= 0.75:
            labels.append("EARLY_EXIT_FROM_PROFIT")
        if (
            reason == "STOP_LOSS"
            and float(path_metrics.get("adverse_first_10m_r") or 0.0) <= -0.5
            and float(path_metrics.get("recovery_after_invalidation_r") or 0.0) < 0.25
        ):
            labels.append("HELD_AFTER_EARLY_INVALIDATION")
        direction = 1.0 if position.side.upper() == "LONG" else -1.0
        flow_votes: list[float] = []
        if entry_snapshot.get("order_book_available"):
            flow_votes.append(direction * self._number(entry_snapshot.get("order_book_imbalance")))
        if entry_snapshot.get("tape_available"):
            flow_votes.append(direction * self._number(entry_snapshot.get("trade_flow_imbalance")))
        if len(flow_votes) >= 2 and sum(value <= -0.1 for value in flow_votes) >= 2:
            labels.append("ENTRY_AGAINST_ORDER_FLOW")
        if (
            float(path_metrics.get("pre_entry_directional_r") or 0.0) >= 1.0
            and float(path_metrics.get("mfe_r") or 0.0) < 0.35
        ):
            labels.append("LATE_ENTRY_EXHAUSTION")
        if float(execution.get("cost_to_planned_risk") or 0.0) >= 0.25:
            labels.append("EXECUTION_COST_DAMAGE")
        strategy_followed = reason == "STOP_LOSS" and not (set(labels) & AVOIDABLE_LABELS)
        if strategy_followed:
            labels.append("VALID_STOP")
        if not labels:
            labels.append("UNCLASSIFIED_LOSS")
        return labels, strategy_followed

    def reward_components(
        self,
        result_r: float,
        labels: list[str],
        reason: str,
        strategy_followed: bool,
    ) -> dict[str, float]:
        components = {"financial_outcome": round(max(min(result_r, 3.0), -3.0), 4)}
        if reason == "STOP_LOSS":
            components["risk_discipline"] = 0.35 if strategy_followed else 0.15
        penalties = {
            "EARLY_EXIT_FROM_PROFIT": -0.75,
            "HELD_AFTER_EARLY_INVALIDATION": -0.6,
            "ENTRY_AGAINST_ORDER_FLOW": -0.45,
            "LATE_ENTRY_EXHAUSTION": -0.4,
            "EXECUTION_COST_DAMAGE": -0.3,
        }
        for label in labels:
            if label in penalties:
                components[label.lower()] = penalties[label]
        return components

    def _number(self, value: Any) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _aware(self, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class BadExperienceReplay:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def apply(self, db: AsyncSession, symbol: str, frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
        experiences = list(
            (
                await db.execute(
                    select(TradePostMortem)
                    .where(TradePostMortem.symbol == symbol)
                    .order_by(TradePostMortem.priority.desc(), TradePostMortem.closed_at.desc())
                    .limit(200)
                )
            ).scalars().all()
        )
        weighted = frame.copy(deep=True)
        weighted["replay_weight"] = 1.0
        if experiences and "timestamp" in weighted.columns:
            timestamps = pd.to_datetime(weighted["timestamp"], utc=True)
            for item in experiences:
                start = self._aware(item.entered_at) - timedelta(minutes=max(int(self.settings.post_mortem_lookback_minutes), 10))
                end = self._aware(item.closed_at)
                weight = min(max(float(item.priority), 1.0), max(float(self.settings.bad_replay_max_weight), 1.0))
                mask = (timestamps >= start) & (timestamps <= end)
                weighted.loc[mask, "replay_weight"] = weighted.loc[mask, "replay_weight"].clip(lower=weight)
                item.replay_count = int(item.replay_count or 0) + 1
        weighted_count = int((weighted["replay_weight"] > 1.0).sum())
        return weighted, {
            "bad_experiences_seen": len(experiences),
            "replay_weighted_candles": weighted_count,
            "max_replay_weight": round(float(weighted["replay_weight"].max()), 2),
        }

    def _aware(self, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
