"""Offline-only learning contracts for the paper order-book scalper.

No policy in this module can send an order.  Its inputs are persisted paper
executions and captured public stream observations, never another agent's
hidden state.  A candidate is useful only after it has positive *net* out of
sample expectancy and passes every curriculum stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from statistics import mean, pstdev
from typing import Iterable

from sqlalchemy import select, update

from app.core.config import get_settings
from app.models.entities import Position, RlModel


@dataclass(frozen=True)
class ScalpFill:
    timestamp: float
    price: float
    amount: float
    side: str  # aggressor: buy or sell


@dataclass(frozen=True)
class ScalpResult:
    filled: bool
    exit_price: float
    maker: bool
    fees: float
    slippage: float
    reason: str


def profit_centric_reward(*, gross_pnl: float, fees: float, slippage: float,
                          initial_risk: float, drawdown: float,
                          holding_seconds: float, stop_or_loss: bool,
                          timeout_or_flat: bool, settings=None) -> float:
    """A closed-trade reward; price direction by itself has no value here."""
    settings = settings or get_settings()
    values = (gross_pnl, fees, slippage, initial_risk, drawdown, holding_seconds)
    if not all(math.isfinite(float(value)) for value in values) or initial_risk <= 0:
        raise ValueError("Invalid profit-centric reward inputs")
    costs = max(fees, 0) + max(slippage, 0)
    net = gross_pnl - costs
    coverage = gross_pnl / max(costs, 1e-9)
    if timeout_or_flat:
        return -float(settings.scalp_reward_timeout_gamma)
    if stop_or_loss or net <= 0 or coverage <= 2.5:
        return -float(settings.scalp_reward_loss_beta) * (max(drawdown, 0) / initial_risk + 1.5)
    return net / initial_risk - float(settings.scalp_reward_holding_penalty_per_second) * max(holding_seconds, 0)


def cash_preservation_reward(*, is_flat: bool, spread_bps: float, fee_bps: float,
                            depth_usdt: float, required_depth_usdt: float) -> float:
    """Tiny bounded reward only for correctly avoiding a toxic market.

    It is deliberately unavailable while a position is held and never offsets
    a loss.  Its purpose is to distinguish a correctly skipped wide/thin book
    from a model that merely produces no decisions.
    """
    if not is_flat or not all(math.isfinite(float(value)) for value in (spread_bps, fee_bps, depth_usdt, required_depth_usdt)):
        return 0.0
    toxic = spread_bps > fee_bps or depth_usdt < required_depth_usdt
    return 0.01 if toxic else 0.0


class OrderBookReplay:
    """Deterministic fill model for recorded public book/tape events.

    A maker fill requires opposite aggressive tape to consume the recorded
    visible queue plus the requested amount.  If it does not, expiry forces a
    taker exit at an adverse price; it never grants a free maker fill.
    """
    def __init__(self, *, maker_fee_rate: float, taker_fee_rate: float, slippage_bps: float):
        self.maker_fee_rate = max(float(maker_fee_rate), 0)
        self.taker_fee_rate = max(float(taker_fee_rate), 0)
        self.slippage_rate = max(float(slippage_bps), 0) / 10_000

    def maker_or_taker(self, *, side: str, limit_price: float, quantity: float,
                        visible_queue: float, tape: Iterable[ScalpFill],
                        fallback_mark: float) -> ScalpResult:
        if side not in {"BUY", "SELL"} or min(limit_price, quantity, fallback_mark) <= 0:
            raise ValueError("Invalid replay order")
        remaining = max(float(visible_queue), 0) + quantity
        aggressor = "sell" if side == "BUY" else "buy"
        for fill in sorted(tape, key=lambda item: item.timestamp):
            crosses = fill.side == aggressor and ((fill.price <= limit_price) if side == "BUY" else (fill.price >= limit_price))
            if crosses:
                remaining -= max(fill.amount, 0)
                if remaining <= 0:
                    return ScalpResult(True, limit_price, True, limit_price * quantity * self.maker_fee_rate, 0.0, "MAKER_QUEUE_FILLED")
        direction = 1 if side == "BUY" else -1
        price = fallback_mark * (1 + direction * self.slippage_rate)
        return ScalpResult(True, price, False, price * quantity * self.taker_fee_rate,
            abs(price - fallback_mark) * quantity, "TAKER_AFTER_UNFILLED_MAKER")


def _sharpe(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    deviation = pstdev(values)
    return mean(values) / deviation * math.sqrt(len(values)) if deviation > 0 else (99.0 if mean(values) > 0 else 0.0)


def curriculum_report(samples: list[dict], *, min_sharpe: float) -> dict:
    """Chronological curriculum; later phases cannot bypass phase 1."""
    phases = {
        "PHASE_1_HIGH_OBI_IMPULSE": [s for s in samples if abs(float(s.get("obi", 0))) >= .4 and abs(float(s.get("flow", 0))) >= .4],
        "PHASE_2_FALSE_BREAKOUT": [s for s in samples if bool(s.get("false_breakout", False)) or abs(float(s.get("obi", 0))) >= .25],
        "PHASE_3_NOISY_RISK_MANAGED": list(samples),
    }
    result, prior_passed = {}, True
    for phase, rows in phases.items():
        rewards = [float(row["reward"]) for row in rows]
        net = [float(row["net_pnl"]) for row in rows]
        sharpe = _sharpe(net)
        passed = prior_passed and len(rows) >= 2 and sharpe > min_sharpe and mean(net) > 0
        result[phase] = {"trades": len(rows), "sharpe": round(sharpe, 4), "net_expectancy": round(mean(net), 8) if net else 0.0,
                         "mean_reward": round(mean(rewards), 8) if rewards else 0.0, "passed": passed}
        prior_passed = passed
    return {"phases": result, "passed": prior_passed}


def scalp_walk_forward(samples: list[dict], *, oos_size: int, min_sharpe: float) -> dict:
    """Expanding, strictly chronological folds with no re-use of a test fold."""
    if oos_size < 2 or len(samples) < oos_size * 3:
        raise ValueError("Need one initial window and two unseen scalp OOS windows")
    train_end = len(samples) - oos_size * 2
    folds = []
    while train_end + oos_size <= len(samples):
        training = curriculum_report(samples[:train_end], min_sharpe=min_sharpe)
        test = curriculum_report(samples[train_end:train_end + oos_size], min_sharpe=min_sharpe)
        folds.append({"train_trades": train_end, "test_trades": oos_size, "training": training, "out_of_sample": test})
        train_end += oos_size
    return {"method": "EXPANDING_WALK_FORWARD", "folds": folds,
        "passed": bool(folds) and all(fold["training"]["passed"] and fold["out_of_sample"]["passed"] for fold in folds)}


class ScalpingLearningService:
    """Creates quarantined candidates from closed paper scalps only."""
    SCHEMA_VERSION = 1

    def __init__(self):
        self.settings = get_settings()

    @staticmethod
    def _aware(value):
        return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

    def sample(self, position: Position) -> dict | None:
        context = position.entry_context or {}
        state = context.get("scalping") or {}
        entry = context.get("entry_execution") or {}
        risk = float(context.get("planned_risk") or 0)
        fees = float(entry.get("fee") or 0) + sum(float(fill.get("fee") or 0) for fill in context.get("exit_fills", []))
        path = context.get("scalp_path") or []
        replay = context.get("scalp_replay") or {}
        holding = float(context.get("scalp_holding_seconds") or 0)
        gross = float(position.pnl or 0) + fees
        drawdown = float(state.get("drawdown") or 0)
        if risk <= 0 or not math.isfinite(gross) or not replay.get("tape"):
            return None
        return {"timestamp": self._aware(position.closed_at or position.entered_at).isoformat(), "symbol": position.symbol,
            "obi": float(state.get("obi") or 0), "flow": float(state.get("flow") or 0), "net_pnl": float(position.pnl or 0),
            "reward": profit_centric_reward(gross_pnl=gross, fees=fees, slippage=0, initial_risk=risk,
                drawdown=drawdown, holding_seconds=holding, stop_or_loss=(position.exit_reason == "STOP_LOSS" or position.pnl <= 0),
                timeout_or_flat=position.exit_reason == "SCALP_TIME_STOP", settings=self.settings),
            "false_breakout": position.exit_reason in {"STOP_LOSS", "SCALP_TAPE_REVERSAL"},
            "replay_complete": bool(replay.get("tape")),
            "path_observations": len(path), "exit_reason": position.exit_reason}

    async def train_candidates(self, db) -> list[RlModel]:
        if not self.settings.scalp_training_enabled:
            return []
        rows = (await db.execute(select(Position).where(Position.status == "CLOSED").order_by(Position.closed_at.asc()))).scalars().all()
        samples = [sample for position in rows if (position.entry_context or {}).get("scalping") for sample in [self.sample(position)] if sample]
        minimum = max(int(self.settings.scalp_training_min_trades), int(self.settings.scalp_training_min_oos_trades) * 3)
        if len(samples) < minimum:
            return []
        walk_forward = scalp_walk_forward(samples, oos_size=int(self.settings.scalp_training_min_oos_trades),
            min_sharpe=self.settings.scalp_training_min_sharpe)
        final_oos = walk_forward["folds"][-1]["out_of_sample"]
        passed = bool(walk_forward["passed"] and final_oos["phases"]["PHASE_3_NOISY_RISK_MANAGED"]["net_expectancy"] > self.settings.scalp_training_min_net_expectancy)
        symbol = "SCALP/BASKET"
        await db.execute(update(RlModel).where(RlModel.symbol == symbol, RlModel.timeframe == "stream", RlModel.status == "CANDIDATE").values(status="REJECTED", is_active=False))
        record = RlModel(symbol=symbol, timeframe="stream", algorithm="SCALP_OFFLINE", status="SHADOW" if passed else "REJECTED",
            is_active=False, training_candles=walk_forward["folds"][-1]["train_trades"], validation_candles=int(self.settings.scalp_training_min_oos_trades), artifact=None,
            feature_schema={"version": self.SCHEMA_VERSION, "kind": "PUBLIC_ORDER_BOOK_TAPE", "hidden_state_shared": False},
            metrics={"walk_forward": walk_forward, "out_of_sample": final_oos, "net_profit_required": True,
                "fees_and_slippage_required": True, "status_reason": "OOS_NET_EDGE_REQUIRED" if passed else "REJECTED_NO_OOS_NET_EDGE"})
        db.add(record)
        await db.flush()
        return [record]

    async def rollback_failed_shadow(self, db) -> int:
        """A candidate without a proven OOS edge has no execution authority."""
        result = await db.execute(update(RlModel).where(RlModel.algorithm == "SCALP_OFFLINE", RlModel.status == "SHADOW", RlModel.is_active.is_(False)).values(status="REJECTED"))
        return int(getattr(result, "rowcount", 0) or 0)
