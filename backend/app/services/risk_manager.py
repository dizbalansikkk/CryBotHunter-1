import math
from collections.abc import Iterable
from dataclasses import dataclass

from app.schemas.dto import StrategySignal


@dataclass
class RiskSettings:
    balance: float
    risk_percent: float
    daily_risk_percent: float
    max_positions: int
    min_rating: int
    stop_loss_percent: float
    take_profit_percent: float
    trailing_stop_percent: float
    atr_stop_multiplier: float = 1.5
    risk_reward_ratio: float = 2.0
    breakeven_trigger_r: float = 1.0
    breakeven_offset_percent: float = 0.05
    partial_take_profit_r: float = 1.0
    partial_close_percent: float = 25.0
    max_risk_percent_per_trade: float = 2.0
    min_risk_reward_ratio: float = 1.5
    max_position_size_percent: float = 25.0


@dataclass(frozen=True)
class DynamicExitPlan:
    stop_loss: float
    take_profit: float
    risk_per_unit: float


@dataclass(frozen=True)
class DrawdownAssessment:
    starting_equity: float
    peak_equity: float
    current_equity: float
    drawdown_percent: float
    threshold_percent: float
    emergency: bool


class RiskManager:
    def can_open(
        self,
        signal: StrategySignal,
        settings: RiskSettings,
        open_positions_count: int,
        daily_pnl: float,
    ) -> tuple[bool, str]:
        numeric_inputs = (
            settings.balance,
            settings.risk_percent,
            settings.daily_risk_percent,
            settings.max_risk_percent_per_trade,
            settings.risk_reward_ratio,
            settings.min_risk_reward_ratio,
        )
        if not all(self._is_positive_finite(value) for value in numeric_inputs) or not self._is_finite(daily_pnl):
            return False, "invalid risk inputs"
        if signal.signal == "WAIT":
            return False, "strategy returned WAIT"
        if open_positions_count >= settings.max_positions:
            return False, "maximum open positions reached"
        if signal.score < settings.min_rating:
            return False, "signal score below minimum rating"
        if settings.risk_percent > settings.max_risk_percent_per_trade:
            return False, "risk per trade above safety limit"
        if settings.risk_reward_ratio < settings.min_risk_reward_ratio:
            return False, "risk/reward ratio below safety limit"
        daily_loss_limit = -settings.balance * settings.daily_risk_percent / 100
        if daily_pnl <= daily_loss_limit:
            return False, "daily loss limit reached"
        return True, "risk accepted"

    def calculate_dynamic_exits(
        self,
        entry_price: float,
        atr: float | None,
        side: str,
        atr_multiplier: float = 2.0,
        risk_reward_ratio: float = 2.0,
        fallback_stop_percent: float = 1.5,
    ) -> DynamicExitPlan:
        entry = self._positive_number(entry_price, "entry_price")
        multiplier = self._positive_number(atr_multiplier, "atr_multiplier")
        reward_ratio = self._positive_number(risk_reward_ratio, "risk_reward_ratio")
        normalized_side = side.strip().upper()
        if normalized_side in {"BUY", "LONG"}:
            normalized_side = "LONG"
        elif normalized_side in {"SELL", "SHORT"}:
            normalized_side = "SHORT"
        else:
            raise ValueError(f"Unsupported position side: {side}")

        if self._is_positive_finite(atr):
            risk_per_unit = float(atr) * multiplier
        else:
            fallback = self._positive_number(fallback_stop_percent, "fallback_stop_percent")
            risk_per_unit = entry * fallback / 100
        if risk_per_unit <= 0 or not math.isfinite(risk_per_unit):
            raise ValueError("Dynamic stop distance must be finite and positive")

        reward = risk_per_unit * reward_ratio
        if normalized_side == "LONG":
            stop_loss = entry - risk_per_unit
            take_profit = entry + reward
        else:
            stop_loss = entry + risk_per_unit
            take_profit = entry - reward
        if stop_loss <= 0 or take_profit <= 0:
            raise ValueError("Dynamic exit plan produced a non-positive price")
        return DynamicExitPlan(
            stop_loss=round(stop_loss, 8),
            take_profit=round(take_profit, 8),
            risk_per_unit=round(risk_per_unit, 8),
        )

    def calculate_position_size(
        self,
        balance: float,
        risk_percent: float,
        entry_price: float,
        stop_price: float,
        max_position_percent: float = 100.0,
    ) -> float:
        values = (balance, risk_percent, entry_price, stop_price, max_position_percent)
        if not all(self._is_positive_finite(value) for value in values):
            return 0.0
        loss_budget = float(balance) * float(risk_percent) / 100
        price_risk = abs(float(entry_price) - float(stop_price))
        if price_risk <= 0:
            return 0.0
        risk_sized_volume = loss_budget / price_risk
        position_cap = float(balance) * min(float(max_position_percent), 100.0) / 100
        cap_sized_volume = position_cap / float(entry_price)
        return round(min(risk_sized_volume, cap_sized_volume), 8)

    def position_size(
        self,
        balance: float,
        risk_percent: float,
        entry_price: float,
        stop_price: float,
        max_position_percent: float = 100.0,
    ) -> float:
        return self.calculate_position_size(
            balance=balance,
            risk_percent=risk_percent,
            entry_price=entry_price,
            stop_price=stop_price,
            max_position_percent=max_position_percent,
        )

    def calculate_drawdown(
        self,
        starting_equity: float,
        closed_pnls: Iterable[float],
        open_pnl: float = 0.0,
        threshold_percent: float = 5.0,
    ) -> DrawdownAssessment:
        starting = self._positive_number(starting_equity, "starting_equity")
        threshold = self._positive_number(threshold_percent, "threshold_percent")
        current_open_pnl = self._finite_number(open_pnl, "open_pnl")
        cumulative_pnl = 0.0
        peak_equity = starting
        for index, pnl in enumerate(closed_pnls):
            cumulative_pnl += self._finite_number(pnl, f"closed_pnls[{index}]")
            peak_equity = max(peak_equity, starting + cumulative_pnl)
        current_equity = starting + cumulative_pnl + current_open_pnl
        drawdown = max(peak_equity - current_equity, 0.0)
        drawdown_percent = drawdown / peak_equity * 100 if peak_equity > 0 else 100.0
        return DrawdownAssessment(
            starting_equity=round(starting, 8),
            peak_equity=round(peak_equity, 8),
            current_equity=round(current_equity, 8),
            drawdown_percent=round(drawdown_percent, 4),
            threshold_percent=round(threshold, 4),
            emergency=drawdown_percent >= threshold,
        )

    def position_notional(self, price: float, volume: float) -> float:
        if not self._is_positive_finite(price) or not self._is_finite(volume):
            return 0.0
        return round(abs(float(price) * float(volume)), 4)

    def exposure_percent(self, exposure: float, balance: float) -> float:
        if not self._is_finite(exposure) or not self._is_positive_finite(balance):
            return 0.0
        return round(float(exposure) / float(balance) * 100, 2)

    def can_add_exposure(
        self,
        balance: float,
        current_gross_exposure: float,
        current_symbol_exposure: float,
        candidate_notional: float,
        max_gross_exposure_percent: float,
        max_symbol_exposure_percent: float,
    ) -> tuple[bool, str]:
        values = (
            balance,
            current_gross_exposure,
            current_symbol_exposure,
            candidate_notional,
            max_gross_exposure_percent,
            max_symbol_exposure_percent,
        )
        if not all(self._is_finite(value) for value in values):
            return False, "invalid exposure inputs"
        if balance <= 0:
            return False, "balance is zero"
        if min(current_gross_exposure, current_symbol_exposure, candidate_notional) < 0:
            return False, "invalid exposure inputs"
        gross_limit = balance * max_gross_exposure_percent / 100
        symbol_limit = balance * max_symbol_exposure_percent / 100
        if current_gross_exposure + candidate_notional > gross_limit:
            return False, "gross exposure limit reached"
        if current_symbol_exposure + candidate_notional > symbol_limit:
            return False, "symbol exposure limit reached"
        return True, "exposure accepted"

    def can_add_daily_risk_reserve(
        self,
        *,
        balance: float,
        daily_risk_percent: float,
        daily_pnl: float,
        reserved_open_stop_risk: float,
        candidate_stop_risk: float,
    ) -> tuple[bool, str]:
        """Reject a new position when all open stops plus its stop break the daily budget."""
        if not all(self._is_positive_finite(value) for value in (balance, daily_risk_percent)):
            return False, "invalid daily risk budget inputs"
        if not all(self._is_finite(value) for value in (daily_pnl, reserved_open_stop_risk, candidate_stop_risk)):
            return False, "invalid daily risk budget inputs"
        if reserved_open_stop_risk < 0 or candidate_stop_risk <= 0:
            return False, "invalid daily risk reservation"

        loss_limit = float(balance) * float(daily_risk_percent) / 100
        projected_pnl_at_stops = float(daily_pnl) - float(reserved_open_stop_risk) - float(candidate_stop_risk)
        if projected_pnl_at_stops < -loss_limit:
            return (
                False,
                (
                    "daily risk reserve would exceed limit: "
                    f"projected={projected_pnl_at_stops:.2f}, limit={-loss_limit:.2f}"
                ),
            )
        return True, "daily risk reserve accepted"

    def directional_exposure(
        self,
        side: str,
        side_counts: dict[str, int],
        max_same_side_positions: int,
        reduction_start: int,
        risk_multiplier: float,
    ) -> tuple[bool, str, float]:
        current_count = int(side_counts.get(side, 0))
        if max_same_side_positions > 0 and current_count >= max_same_side_positions:
            return False, f"{side.lower()} direction position limit reached", 0.0
        if reduction_start > 0 and current_count >= reduction_start:
            multiplier = min(max(risk_multiplier, 0.0), 1.0)
            return True, f"{side.lower()} direction risk reduced to {multiplier:.2f}x", multiplier
        return True, "directional exposure accepted", 1.0

    def _is_positive_finite(self, value: float | None) -> bool:
        try:
            return value is not None and math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError):
            return False

    def _is_finite(self, value: float | None) -> bool:
        try:
            return value is not None and math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    def _positive_number(self, value: float | None, name: str) -> float:
        if not self._is_positive_finite(value):
            raise ValueError(f"{name} must be finite and positive")
        return float(value)

    def _finite_number(self, value: float | None, name: str) -> float:
        try:
            parsed = float(value) if value is not None else math.nan
        except (TypeError, ValueError):
            parsed = math.nan
        if not math.isfinite(parsed):
            raise ValueError(f"{name} must be finite")
        return parsed
