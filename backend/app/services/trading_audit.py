from __future__ import annotations

"""Fact-only audit of the recorded trading journal.

The service deliberately does not reconstruct missing market paths or deployment
history.  Every unavailable conclusion is returned together with the precise
source that would be required to calculate it.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean, pstdev
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import EquitySnapshot, Position, PositionStatus, StrategyRelease, TradePostMortem, UserSettings


AUDIT_TIMEZONE = "Europe/Simferopol"
INSUFFICIENT = "Данных недостаточно для определения."


class TradingAuditService:
    """Build a thirty-full-calendar-day audit from persisted records only."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.tz = ZoneInfo(AUDIT_TIMEZONE)

    async def report(
        self,
        db: AsyncSession,
        user_settings: UserSettings | None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        start_local, end_local = self.window(now)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        closed = list(
            (
                await db.execute(
                    select(Position)
                    .where(
                        Position.status == PositionStatus.CLOSED.value,
                        Position.closed_at.is_not(None),
                        Position.closed_at >= start_utc,
                        Position.closed_at < end_utc,
                    )
                    .order_by(Position.closed_at.asc(), Position.id.asc())
                )
            ).scalars().all()
        )
        overlapping = list(
            (
                await db.execute(
                    select(Position)
                    .where(
                        Position.entered_at < end_utc,
                        or_(Position.closed_at.is_(None), Position.closed_at >= start_utc),
                    )
                    .order_by(Position.entered_at.asc(), Position.id.asc())
                )
            ).scalars().all()
        )
        post_mortems = list(
            (
                await db.execute(
                    select(TradePostMortem)
                    .where(
                        TradePostMortem.closed_at >= start_utc,
                        TradePostMortem.closed_at < end_utc,
                    )
                    .order_by(TradePostMortem.closed_at.asc(), TradePostMortem.id.asc())
                )
            ).scalars().all()
        )
        report = self.build(
            closed,
            post_mortems,
            overlapping,
            user_settings,
            now=now,
        )
        mode = "PAPER" if self.settings.paper_trading or not self.settings.live_trading_enabled else "LIVE"
        count, first, last, max_dd = (await db.execute(select(
            func.count(EquitySnapshot.id), func.min(EquitySnapshot.captured_at),
            func.max(EquitySnapshot.captured_at), func.max(EquitySnapshot.drawdown_percent),
        ).where(EquitySnapshot.mode == mode, EquitySnapshot.captured_at >= start_utc,
                EquitySnapshot.captured_at < end_utc))).one()
        collection_started = (await db.execute(select(func.min(EquitySnapshot.captured_at)).where(
            EquitySnapshot.mode == mode,
        ))).scalar_one()
        report["capital_and_risk"]["historical_capital"] = {
            "snapshots_in_period": count, "mode": mode,
            "first": first.isoformat() if first else None, "last": last.isoformat() if last else None,
            "collection_started_at": collection_started.isoformat() if collection_started else None,
            "max_recorded_drawdown_percent": max_dd,
            "message": "Показаны фактические срезы за период. Подробности по дням доступны в дневном аудите." if count else
                       "За 30 полных дней отчёта срезов нет. Сегодняшние срезы доступны в дневном аудите; прошедший equity не восстановлен.",
        }
        releases = list((await db.execute(select(StrategyRelease).where(
            StrategyRelease.deployed_at <= self._aware(now or datetime.now(timezone.utc)),
        ).order_by(StrategyRelease.deployed_at.desc()).limit(30))).scalars().all())
        if releases:
            report["before_after_changes"] = {
                "status": "RECORDED_RELEASES",
                "message": "Журнал релизов доступен, включая сегодняшний день. Сам факт смены версии не доказывает влияние на доходность.",
                "periods": [{"version": r.version, "deployed_at": r.deployed_at.isoformat(), "config_hash": r.config_hash} for r in releases],
            }
        for item in report["limitations"]:
            if item["area"] == "Капитал и portfolio drawdown":
                item["message"] = report["capital_and_risk"]["historical_capital"]["message"]
            elif item["area"] == "До/после изменений" and releases:
                item["message"] = "Релизы записываются; для причинного сравнения нужны сделки по каждой версии и сопоставимые рыночные условия."
        report["final_15"] = self._final_15(report)
        report["final_conclusion"] = self._final_conclusion(report)
        return report

    def window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        current = self._aware(now or datetime.now(timezone.utc)).astimezone(self.tz)
        end = datetime.combine(current.date(), time.min, tzinfo=self.tz)
        return end - timedelta(days=30), end

    def build(
        self,
        closed_positions: Iterable[Any],
        post_mortems: Iterable[Any],
        overlapping_positions: Iterable[Any],
        user_settings: Any | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Pure builder used by the HTTP route and unit tests."""
        start, end = self.window(now)
        closed = sorted(
            list(closed_positions),
            key=lambda item: (self._aware(getattr(item, "closed_at", None)), int(getattr(item, "id", 0) or 0)),
        )
        mortems = list(post_mortems)
        positions_in_window = list(overlapping_positions)
        daily = self._daily_rows(closed, start.date())
        totals = self._metrics(closed)
        symbol_rows = self._symbol_rows(closed)
        losses = [item for item in closed if self._pnl(item) < 0]
        postmortem_by_position = {
            int(getattr(item, "position_id", 0) or 0): item
            for item in mortems
            if getattr(item, "position_id", None) is not None
        }

        data_quality = self._data_quality(closed, losses, mortems)
        report = {
            "generated_at": self._aware(now or datetime.now(timezone.utc)).isoformat(),
            "timezone": AUDIT_TIMEZONE,
            "period": {
                "start": self._display_hour(start),
                "end_exclusive": self._display_hour(end),
                "end_inclusive": self._display_hour(end - timedelta(hours=1)),
                "full_calendar_days": 30,
                "closed_trades": len(closed),
                "trading_days": sum(1 for row in daily if row["trades"] > 0),
                "basis": "Закрытые позиции: Position.status=CLOSED, группировка по closed_at в Europe/Simferopol.",
            },
            "data_quality": data_quality,
            "total": totals,
            "daily_results": daily,
            "daily_analysis": self._daily_analysis(daily),
            "time_sequence": self._time_sequence(daily, closed),
            "before_after_changes": self._before_after_changes(),
            "by_symbol": symbol_rows,
            "top_symbols": {
                "positive": sorted(
                    (row for row in symbol_rows if row["net_pnl"] > 0),
                    key=lambda row: row["net_pnl"],
                    reverse=True,
                )[:5],
                "negative": sorted(
                    (row for row in symbol_rows if row["net_pnl"] < 0),
                    key=lambda row: row["net_pnl"],
                )[:5],
            },
            "loss_causes": self._loss_causes(losses, mortems),
            "entry_analysis": self._entry_analysis(losses, mortems),
            "take_profit": self._take_profit(closed, mortems, user_settings),
            "stop_loss": self._stop_loss(closed, user_settings),
            "capital_and_risk": self._capital_and_risk(closed, user_settings),
            "position_correlation": self._position_correlation(positions_in_window, start, end),
            "streaks": self._streaks(closed),
            "decision_algorithm": self._decision_algorithm(),
            "external_sources": self._external_sources(),
            "limitations": self._limitations(closed, losses, mortems, postmortem_by_position),
        }
        report["final_15"] = self._final_15(report)
        report["final_conclusion"] = self._final_conclusion(report)
        return report

    def _daily_rows(self, positions: list[Any], first_day: date) -> list[dict[str, Any]]:
        groups: dict[date, list[Any]] = defaultdict(list)
        for position in positions:
            closed_at = self._aware(getattr(position, "closed_at", None)).astimezone(self.tz)
            groups[closed_at.date()].append(position)
        return [self._daily_metric_row(first_day + timedelta(days=index), groups.get(first_day + timedelta(days=index), [])) for index in range(30)]

    def _daily_metric_row(self, day: date, positions: list[Any]) -> dict[str, Any]:
        metrics = self._metrics(positions)
        return {
            "date": day.isoformat(),
            "trades": metrics["trades"],
            "profitable": metrics["profitable"],
            "losing": metrics["losing"],
            "breakeven": metrics["breakeven"],
            "win_rate": metrics["win_rate"],
            "gross_profit": metrics["gross_profit"],
            "gross_loss": metrics["gross_loss"],
            "net_pnl": metrics["net_pnl"],
            "average_pnl": metrics["average_pnl"],
            "max_drawdown": metrics["max_drawdown"],
            "profit_factor": metrics["profit_factor"],
        }

    def _metrics(self, positions: Iterable[Any]) -> dict[str, Any]:
        rows = list(positions)
        pnls = [self._pnl(item) for item in rows]
        profits = [item for item in pnls if item > 0]
        losses = [item for item in pnls if item < 0]
        gross_profit = sum(profits)
        gross_loss = abs(sum(losses))
        durations = [self._duration_minutes(item) for item in rows]
        measured_durations = [item for item in durations if item is not None]
        return {
            "trades": len(rows),
            "profitable": len(profits),
            "losing": len(losses),
            "breakeven": len(rows) - len(profits) - len(losses),
            "win_rate": self._round(len(profits) / len(rows) * 100) if rows else 0.0,
            "gross_profit": self._round(gross_profit),
            "gross_loss": self._round(gross_loss),
            "net_pnl": self._round(sum(pnls)),
            "average_pnl": self._round(sum(pnls) / len(rows)) if rows else 0.0,
            "average_profit": self._round(gross_profit / len(profits)) if profits else None,
            "average_loss": self._round(sum(losses) / len(losses)) if losses else None,
            "profit_factor": self._round(gross_profit / gross_loss) if gross_loss > 0 else None,
            "max_drawdown": self._realized_drawdown(pnls),
            "max_win": self._round(max(pnls)) if pnls else None,
            "max_loss": self._round(min(pnls)) if pnls else None,
            "average_duration_minutes": self._round(mean(measured_durations)) if measured_durations else None,
        }

    def _symbol_rows(self, positions: list[Any]) -> list[dict[str, Any]]:
        groups: dict[str, list[Any]] = defaultdict(list)
        for position in positions:
            groups[str(getattr(position, "symbol", "UNKNOWN"))].append(position)
        result = []
        for symbol, rows in groups.items():
            metrics = self._metrics(rows)
            result.append(
                {
                    "symbol": symbol,
                    "trades": metrics["trades"],
                    "win_rate": metrics["win_rate"],
                    "gross_profit": metrics["gross_profit"],
                    "gross_loss": metrics["gross_loss"],
                    "net_pnl": metrics["net_pnl"],
                    "average_pnl": metrics["average_pnl"],
                    "max_loss": metrics["max_loss"],
                    "max_win": metrics["max_win"],
                    "profit_factor": metrics["profit_factor"],
                }
            )
        return sorted(result, key=lambda row: (row["net_pnl"], row["symbol"]), reverse=True)

    def _daily_analysis(self, daily: list[dict[str, Any]]) -> dict[str, Any]:
        active = [row for row in daily if row["trades"] > 0]
        profitable_days = [row for row in active if row["net_pnl"] > 0]
        losing_days = [row for row in active if row["net_pnl"] < 0]
        stable, unstable = self._stable_windows(daily)
        return {
            "best_day": max(active, key=lambda row: row["net_pnl"], default=None),
            "worst_day": min(active, key=lambda row: row["net_pnl"], default=None),
            "maximum_daily_profit": max((row["net_pnl"] for row in profitable_days), default=None),
            "maximum_daily_loss": min((row["net_pnl"] for row in losing_days), default=None),
            "longest_profitable_days": self._day_streak(daily, positive=True),
            "longest_losing_days": self._day_streak(daily, positive=False),
            "most_stable_period": stable,
            "most_unstable_period": unstable,
            "stability_basis": (
                "Скользящее окно 7 календарных дней; стабильность = минимальное, "
                "нестабильность = максимальное стандартное отклонение дневного реализованного PnL."
            ) if stable else INSUFFICIENT + " Для 7-дневного сравнения нужно минимум 7 дней с закрытыми сделками.",
        }

    def _stable_windows(self, daily: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        windows: list[dict[str, Any]] = []
        for index in range(len(daily) - 6):
            rows = daily[index:index + 7]
            if sum(row["trades"] for row in rows) == 0:
                continue
            values = [float(row["net_pnl"]) for row in rows]
            windows.append(
                {
                    "start": rows[0]["date"],
                    "end": rows[-1]["date"],
                    "trades": sum(row["trades"] for row in rows),
                    "pnl_standard_deviation": self._round(pstdev(values)),
                    "net_pnl": self._round(sum(values)),
                }
            )
        if not windows:
            return None, None
        return min(windows, key=lambda item: item["pnl_standard_deviation"]), max(windows, key=lambda item: item["pnl_standard_deviation"])

    def _time_sequence(self, daily: list[dict[str, Any]], closed: list[Any]) -> dict[str, Any]:
        candidate = self._change_point(daily)
        per_symbol = self._symbol_rows(closed)
        return {
            "timeline": [
                {
                    "day": index + 1,
                    "date": row["date"],
                    "net_pnl": row["net_pnl"],
                    "win_rate": row["win_rate"],
                    "trades": row["trades"],
                    "max_drawdown": row["max_drawdown"],
                }
                for index, row in enumerate(daily)
            ],
            "change_point": candidate,
            "symbol_effectiveness": {
                "available": bool(per_symbol),
                "rows": per_symbol,
                "note": "Изменение качества отдельных монет требует сопоставимых сделок до и после точки изменения." if candidate["status"] != "DETECTED" else "Сравнение выполнено для рассчитанной точки изменения.",
            },
        }

    def _change_point(self, daily: list[dict[str, Any]]) -> dict[str, Any]:
        # The threshold prevents a single lucky/failed trade from being presented
        # as a structural change.  The raw daily sequence remains the evidence.
        candidates: list[dict[str, Any]] = []
        for index in range(7, len(daily) - 7):
            before, after = daily[index - 7:index], daily[index:index + 7]
            before_trades = sum(row["trades"] for row in before)
            after_trades = sum(row["trades"] for row in after)
            before_active = sum(row["trades"] > 0 for row in before)
            after_active = sum(row["trades"] > 0 for row in after)
            if min(before_trades, after_trades) < 5 or min(before_active, after_active) < 3:
                continue
            before_metrics = self._aggregate_daily_window(before)
            after_metrics = self._aggregate_daily_window(after)
            movement = (
                abs(after_metrics["average_pnl"] - before_metrics["average_pnl"])
                + abs(after_metrics["win_rate"] - before_metrics["win_rate"]) / 100
                + abs(after_metrics["trades_per_day"] - before_metrics["trades_per_day"])
                + abs(after_metrics["max_drawdown"] - before_metrics["max_drawdown"])
            )
            candidates.append(
                {
                    "date": daily[index]["date"],
                    "before": before_metrics,
                    "after": after_metrics,
                    "movement_score": self._round(movement),
                }
            )
        if not candidates:
            return {
                "status": "INSUFFICIENT_DATA",
                "message": INSUFFICIENT + " Для устойчивой точки изменения нужны две соседние 7-дневные выборки, в каждой не менее 5 закрытых сделок и 3 торговых дней.",
            }
        # This is a reproducible candidate, not proof of causal code change.
        selected = max(candidates, key=lambda item: item["movement_score"])
        return {
            "status": "DETECTED",
            "message": "Кандидат на устойчивое изменение рассчитан по наибольшему совокупному сдвигу двух сопоставимых 7-дневных окон. Это не доказывает причину изменения.",
            **selected,
        }

    def _aggregate_daily_window(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        trades = sum(row["trades"] for row in rows)
        wins = sum(row["profitable"] for row in rows)
        return {
            "trades": trades,
            "average_pnl": self._round(sum(row["net_pnl"] for row in rows) / trades) if trades else 0.0,
            "win_rate": self._round(wins / trades * 100) if trades else 0.0,
            "trades_per_day": self._round(trades / len(rows)) if rows else 0.0,
            "max_drawdown": self._realized_drawdown([row["net_pnl"] for row in rows]),
        }

    def _before_after_changes(self) -> dict[str, Any]:
        return {
            "status": "MISSING_DEPLOYMENT_HISTORY",
            "message": INSUFFICIENT + " В базе нет журнала развёртываний, изменений параметров или версий алгоритма с датой и временем. По одной истории сделок нельзя достоверно определить момент изменения алгоритма и причинный эффект.",
            "required_data": [
                "неизменяемый журнал release/deployment с версией, YYYY-MM-DD HH и часовым поясом",
                "история изменения каждого торгового параметра с прежним и новым значением",
                "привязка каждой позиции к версии стратегии и настройкам на момент входа",
            ],
            "periods": [],
        }

    def _loss_causes(self, losses: list[Any], mortems: list[Any]) -> dict[str, Any]:
        loss_ids = {int(getattr(item, "id", 0) or 0) for item in losses}
        mortems = [item for item in mortems if int(getattr(item, "position_id", 0) or 0) in loss_ids]
        labels = Counter()
        for item in mortems:
            value = getattr(item, "behavior_labels", []) or []
            if not isinstance(value, list):
                value = []
            labels.update(str(label) for label in value)
        translation = {
            "EARLY_EXIT_FROM_PROFIT": "Ранняя фиксация прибыли",
            "HELD_AFTER_EARLY_INVALIDATION": "Поздняя фиксация убытка после ранней инвалидации",
            "ENTRY_AGAINST_ORDER_FLOW": "Вход против подтверждённого стаканом и лентой потока",
            "LATE_ENTRY_EXHAUSTION": "Слишком поздний вход в истощённое движение",
            "EXECUTION_COST_DAMAGE": "Комиссии и проскальзывание существенно повредили риск",
            "VALID_STOP": "Стоп исполнен по плану; причина самого входа не установлена",
            "UNCLASSIFIED_LOSS": "Причина не классифицирована",
        }
        return {
            "loss_trades": len(losses),
            "post_mortems": len(mortems),
            "coverage_percent": self._round(len(mortems) / len(losses) * 100) if losses else None,
            "causes": [
                {"label": label, "description": translation.get(label, label), "trades": count}
                for label, count in labels.most_common()
            ],
            "note": "Это фактические ярлыки post-mortem; они не являются причиной для сделок без сохранённого post-mortem." if mortems else INSUFFICIENT + " Нет записей TradePostMortem по убыточным сделкам.",
        }

    def _entry_analysis(self, losses: list[Any], mortems: list[Any]) -> dict[str, Any]:
        loss_ids = {int(getattr(item, "id", 0) or 0) for item in losses}
        mortems = [item for item in mortems if int(getattr(item, "position_id", 0) or 0) in loss_ids]
        paths = []
        for item in mortems:
            snapshot = getattr(item, "market_snapshot", {}) or {}
            path = snapshot.get("path") if isinstance(snapshot, dict) else None
            if isinstance(path, dict) and any(key in path for key in ("mfe_r", "mae_r", "adverse_first_10m_r")):
                paths.append(path)
        def values(key: str) -> list[float]:
            return [float(path[key]) for path in paths if self._number(path.get(key)) is not None]
        adverse = values("adverse_first_10m_r")
        late = sum("LATE_ENTRY_EXHAUSTION" in (getattr(item, "behavior_labels", []) or []) for item in mortems)
        return {
            "post_mortem_path_coverage": self._round(len(paths) / len(losses) * 100) if losses else None,
            "average_mfe_r": self._mean_or_none(values("mfe_r")),
            "average_mae_r": self._mean_or_none(values("mae_r")),
            "average_adverse_first_10m_r": self._mean_or_none(adverse),
            "initial_adverse_move_count": sum(value < 0 for value in adverse),
            "initial_adverse_move_percent": self._round(sum(value < 0 for value in adverse) / len(adverse) * 100) if adverse else None,
            "late_entry_count": late,
            "late_entry_basis": "Только ярлык LATE_ENTRY_EXHAUSTION в TradePostMortem.",
            "too_early_entry": {
                "value": None,
                "message": INSUFFICIENT + " В схеме post-mortem нет ярлыка или правила, которое фиксирует «слишком ранний вход».",
            },
            "time_to_max_profit": {
                "value": None,
                "message": INSUFFICIENT + " Сохраняются только агрегированные MFE/MAE, а не временные метки экстремумов по каждой сделке.",
            },
            "time_to_max_loss": {
                "value": None,
                "message": INSUFFICIENT + " Сохраняются только агрегированные MFE/MAE, а не временные метки экстремумов по каждой сделке.",
            },
        }

    def _take_profit(self, closed: list[Any], mortems: list[Any], user_settings: Any | None) -> dict[str, Any]:
        partial_exits = Counter(str(getattr(item, "exit_reason", "") or "") for item in closed)
        return {
            "current_algorithm": {
                "initial_take": "TP = entry ± (risk_per_unit × risk_reward_ratio); знак + для LONG, − для SHORT.",
                "partial_take": "TP1 = Price_BU: entry × (1 ± (entry fee rate + expected exit fee rate + slippage buffer)); при подтверждённом исполнении закрывается 10–30% объёма.",
                "second_take": "TP2 закрывает 30–40% первоначального объёма на +/− 1.0–1.5% от входа; до реализации детектора S/R это фиксированный fallback.",
                "breakeven": "Только после подтверждённого FILLED/PARTIAL TP1 стоп переводится к вычисленному Price_BU; касание цены без исполнения не считается фиксацией.",
                "protective_stop": "Для деривативов Binance, OKX и Bybit движок отменяет предыдущий reduce-only Stop Market и фиксирует ID подтверждённого нового ордера. В spot-режиме используется локальный монитор, так как reduce-only стоп небезопасен для баланса кошелька.",
                "trailing_stop": "После защиты безубытка стоп подтягивается от favorable extreme на trailing_stop_percent %; стоп никогда не расширяется.",
                "dynamic_take": "После начального TP при включённой глобальной опции закрывается часть остатка, новый TP и стоп сдвигаются на ATR × dynamic_take_profit_extension_atr; число продлений ограничено.",
                "current_parameters": {
                    "partial_take_profit_r": self._setting(user_settings, "partial_take_profit_r"),
                    "partial_close_percent": self._setting(user_settings, "partial_close_percent"),
                    "trailing_stop_percent": self._setting(user_settings, "trailing_stop_percent"),
                    "dynamic_take_profit_enabled": bool(self.settings.dynamic_take_profit_enabled),
                    "dynamic_take_profit_partial_close_percent": self.settings.dynamic_take_profit_partial_close_percent,
                    "dynamic_take_profit_extension_atr": self.settings.dynamic_take_profit_extension_atr,
                    "dynamic_take_profit_max_extensions": self.settings.dynamic_take_profit_max_extensions,
                    "min_exit_notional_usdt": self.settings.min_exit_notional_usdt,
                    "scale_out_tp2_percent": self.settings.scale_out_tp2_percent,
                    "scale_out_tp2_distance_percent": self.settings.scale_out_tp2_distance_percent,
                    "breakeven_slippage_buffer_bps": self.settings.breakeven_slippage_buffer_bps,
                    "protective_stop_replace_timeout_seconds": self.settings.protective_stop_replace_timeout_seconds,
                    "native_protective_stops_enabled": self.settings.native_protective_stops_enabled,
                    "exchange_default_type": self.settings.exchange_default_type,
                },
                "configuration_note": "Это текущая конфигурация кода/настроек, а не доказательство того, что она действовала для каждой исторической позиции.",
            },
            "observed_close_reasons": dict(sorted(partial_exits.items())),
            "lost_potential_profit": {
                "value": None,
                "message": INSUFFICIENT + " Для всех закрытых сделок не хранится путь цены после выхода и MFE после закрытия. Post-mortem описывает зафиксированный выход; потенциальная прибыль после выхода не восстанавливается.",
            },
        }

    def _stop_loss(self, closed: list[Any], user_settings: Any | None) -> dict[str, Any]:
        planned = [self._context_number(item, "planned_risk") for item in closed]
        planned = [value for value in planned if value is not None]
        stop_exits = sum(str(getattr(item, "exit_reason", "") or "") == "STOP_LOSS" for item in closed)
        return {
            "current_formula": "risk_per_unit = ATR × atr_stop_multiplier; если ATR отсутствует, entry × stop_loss_percent / 100. LONG SL = entry − risk_per_unit; SHORT SL = entry + risk_per_unit.",
            "current_parameters": {
                "atr_stop_multiplier": self._setting(user_settings, "atr_stop_multiplier"),
                "fallback_stop_loss_percent": self._setting(user_settings, "stop_loss_percent"),
                "risk_percent_per_trade": self._setting(user_settings, "risk_percent"),
                "daily_risk_percent": self._setting(user_settings, "daily_risk_percent"),
                "daily_risk_reserve_enabled": bool(self.settings.daily_risk_reserve_enabled),
            },
            "observed": {
                "stop_loss_exits": stop_exits,
                "recorded_planned_risk_min": self._round(min(planned)) if planned else None,
                "recorded_planned_risk_max": self._round(max(planned)) if planned else None,
                "recorded_planned_risk_average": self._mean_or_none(planned),
            },
            "configuration_note": "Текущие параметры не подменяют отсутствующую историю настроек на момент старых входов.",
        }

    def _capital_and_risk(self, closed: list[Any], user_settings: Any | None) -> dict[str, Any]:
        balances = [self._context_number(item, "balance") for item in closed]
        balances = [value for value in balances if value is not None]
        notionals = [self._context_number(item, "notional") for item in closed]
        notionals = [value for value in notionals if value is not None]
        example = self._position_example(closed)
        return {
            "sizing_formula": "loss_budget = balance × risk_percent / 100; risk_volume = loss_budget / |entry − stop|; cap_volume = (balance × max_position_size_percent / 100) / entry; volume = min(risk_volume, cap_volume).",
            "current_limits": {
                "risk_percent_per_trade": self._setting(user_settings, "risk_percent"),
                "daily_risk_percent": self._setting(user_settings, "daily_risk_percent"),
                "max_positions": self._setting(user_settings, "max_positions"),
                "max_position_size_percent": self.settings.max_position_size_percent,
                "max_gross_exposure_percent": self.settings.max_gross_exposure_percent,
                "max_symbol_exposure_percent": self.settings.max_symbol_exposure_percent,
            },
            "recorded_at_entry": {
                "balance_min": self._round(min(balances)) if balances else None,
                "balance_max": self._round(max(balances)) if balances else None,
                "notional_max": self._round(max(notionals)) if notionals else None,
                "notional_average": self._mean_or_none(notionals),
            },
            "historical_capital": {
                "message": INSUFFICIENT + " Нет снимков общего, свободного, зарезервированного капитала и открытого риска по времени. В entry_context может быть только баланс в момент части входов.",
            },
            "position_example": example,
        }

    def _position_example(self, positions: list[Any]) -> dict[str, Any]:
        for position in positions:
            context = self._context(position)
            balance = self._number(context.get("balance"))
            risk_percent = self._number(context.get("risk_percent"))
            entry = self._number(getattr(position, "entry_price", None))
            stop = self._number(getattr(position, "stop", None))
            volume = self._number(getattr(position, "volume", None))
            if None not in (balance, risk_percent, entry, stop, volume) and entry and abs(entry - stop) > 0:
                budget = balance * risk_percent / 100
                return {
                    "position_id": getattr(position, "id", None),
                    "symbol": getattr(position, "symbol", None),
                    "balance": self._round(balance),
                    "risk_percent": self._round(risk_percent),
                    "entry": self._round(entry),
                    "stop": self._round(stop),
                    "recorded_volume": self._round(volume, 8),
                    "loss_budget": self._round(budget),
                    "price_risk_per_unit": self._round(abs(entry - stop), 8),
                    "risk_sized_volume": self._round(budget / abs(entry - stop), 8),
                    "note": "Пример взят из фактически сохранённого entry_context; текущий лимит размера позиции может отличаться от исторического.",
                }
        return {"message": INSUFFICIENT + " В закрытых позициях нет полного набора balance, risk_percent, entry, stop и volume."}

    def _position_correlation(self, positions: list[Any], start: datetime, end: datetime) -> dict[str, Any]:
        events: list[tuple[datetime, int, Any]] = []
        for position in positions:
            entered = max(self._aware(getattr(position, "entered_at", None)), start.astimezone(timezone.utc))
            closed = getattr(position, "closed_at", None)
            exited = min(self._aware(closed), end.astimezone(timezone.utc)) if closed else end.astimezone(timezone.utc)
            if entered < exited:
                events.append((entered, 1, position))
                events.append((exited, -1, position))
        active: dict[int, Any] = {}
        maximum = 0
        max_risk: float | None = 0.0
        pairs = 0
        simultaneous_loss_pairs = 0
        for timestamp, action, position in sorted(events, key=lambda item: (item[0], item[1])):
            position_id = int(getattr(position, "id", 0) or 0)
            if action < 0:
                active.pop(position_id, None)
                continue
            for other in active.values():
                pairs += 1
                if self._pnl(position) < 0 and self._pnl(other) < 0:
                    simultaneous_loss_pairs += 1
            active[position_id] = position
            maximum = max(maximum, len(active))
            risks = [self._context_number(item, "planned_risk") for item in active.values()]
            if any(value is None for value in risks):
                max_risk = None
            elif max_risk is not None:
                max_risk = max(max_risk, sum(value for value in risks if value is not None))
        return {
            "positions_observed": len(positions),
            "maximum_simultaneous_positions": maximum,
            "overlapping_position_pairs": pairs,
            "simultaneous_losing_pairs": simultaneous_loss_pairs,
            "maximum_recorded_simultaneous_planned_risk": self._round(max_risk) if max_risk is not None else None,
            "price_correlation": {
                "value": None,
                "message": INSUFFICIENT + " В журнале позиций нет синхронных рядов доходности по каждой позиции/BTC. Перекрытие по времени не доказывает корреляцию или общую ставку на рынок.",
            },
            "conclusion": "Нельзя сделать вывод о зависимости от BTC только по факту одновременного открытия. Для проверки требуются синхронные доходности активов и BTC на интервале удержания.",
        }

    def _streaks(self, closed: list[Any]) -> dict[str, Any]:
        max_losses = max_wins = current_losses = current_wins = 0
        loss_runs: list[dict[str, Any]] = []
        active_loss: list[Any] = []
        for position in closed:
            pnl = self._pnl(position)
            if pnl < 0:
                current_losses += 1
                current_wins = 0
                active_loss.append(position)
                max_losses = max(max_losses, current_losses)
            else:
                if active_loss:
                    loss_runs.append(self._loss_run(active_loss))
                    active_loss = []
                current_losses = 0
                if pnl > 0:
                    current_wins += 1
                    max_wins = max(max_wins, current_wins)
                else:
                    current_wins = 0
        if active_loss:
            loss_runs.append(self._loss_run(active_loss))
        maximum_run = max(loss_runs, key=lambda item: item["trades"], default=None)
        return {
            "maximum_loss_streak": max_losses,
            "average_loss_streak": self._round(mean([item["trades"] for item in loss_runs])) if loss_runs else 0.0,
            "maximum_win_streak": max_wins,
            "maximum_loss_streak_detail": maximum_run,
            "pre_streak_pattern": {
                "message": INSUFFICIENT + " Нет единого сохранённого признака непосредственно перед каждой серией и недостаточно стандартизированной истории всех отклонённых сигналов для проверки закономерности.",
            },
            "parameter_reaction": {
                "message": "Фактическая реакция после серии определяется только если в entry_context/журнале есть снимки настроек. Полной истории параметров нет; текущий код применяет глобальный и символный guard/cooldown к новым входам.",
            },
        }

    def _loss_run(self, positions: list[Any]) -> dict[str, Any]:
        return {
            "trades": len(positions),
            "pnl": self._round(sum(self._pnl(item) for item in positions)),
            "start": self._display_hour(self._aware(getattr(positions[0], "closed_at", None)).astimezone(self.tz)),
            "end": self._display_hour(self._aware(getattr(positions[-1], "closed_at", None)).astimezone(self.tz)),
            "symbols": list(dict.fromkeys(str(getattr(item, "symbol", "UNKNOWN")) for item in positions)),
        }

    def _decision_algorithm(self) -> dict[str, Any]:
        return {
            "source": "Текущий исходный код; это описание реализованного алгоритма, а не реконструкция настроек исторической сделки.",
            "sequence": [
                "Получение завершённых 1h OHLCV и тикера по разрешённым парам основного рынка (по умолчанию фьючерсы).",
                "Расчёт EMA20/50/200, RSI14, MACD(12,26), ATR14, 24h quote volume и среднего 24h объёма за предшествующие 7 дней.",
                "Проверка режима рынка, качества рынка, стратегии, глобального/символьного guard, cooldown, pre-trade качества и risk/exposure limits.",
                "Получение 1m стакана, ленты сделок и краткой динамики; вход только при достаточном количестве независимых источников и направлении, если опции не ослаблены.",
                "Расчёт ATR-стопа, TP, размера позиции; рыночный ордер и открытие только при подтверждённом исполнении.",
            ],
            "simultaneous_conditions": [
                "Направленный BUY/SELL: режим не LOW_LIQUIDITY/HIGH_VOLATILITY; regime_score не ниже 45, если режим известен.",
                "ATR от 0.25% до 7.5% цены; цена не дальше strategy_max_entry_distance_atr от EMA20.",
                "LONG: EMA20 > EMA50 > EMA200, RSI 50–68, цена выше EMA20, MACD > 0; SHORT: обратные условия, RSI 32–50.",
                "24h объём не меньше среднего × strategy_min_volume_ratio; рейтинг строго выше 80.",
                "Пройдены лимиты риска, позиции, экспозиции, дневного резерва, guard/cooldown/pre-trade quality и обязательная микроструктура.",
            ],
            "inputs": [
                {"category": "График", "parameter": "1h OHLCV", "used": True, "how": "Базовый таймфрейм сканера и индикаторов; незавершённая свеча исключается."},
                {"category": "График", "parameter": "1m OHLCV", "used": True, "how": "Краткий импульс и realized volatility в микроструктурном gate."},
                {"category": "График", "parameter": "EMA20/50/200, RSI14, MACD", "used": True, "how": "Все направленные условия основной стратегии должны совпасть."},
                {"category": "График", "parameter": "Паттерны / явные support-resistance", "used": False, "how": "В коде нет отдельного детектора паттернов или уровней S/R как условия входа."},
                {"category": "График", "parameter": "Режим/тренд", "used": True, "how": "Блокирует LOW_LIQUIDITY, HIGH_VOLATILITY и вход против направленного режима."},
                {"category": "Объём", "parameter": "24h quote volume и rolling average", "used": True, "how": "Проверка ликвидности и подтверждение объёма ≥ среднего × порог."},
                {"category": "Стакан", "parameter": "bid/ask, глубина, spread, imbalance", "used": True, "how": "Микроструктурный gate ограничивает spread и сверяет направленность imbalance."},
                {"category": "Стакан", "parameter": "Крупные заявки", "used": True, "how": "Используется только явно помеченный iceberg_proxy, не доказательство скрытой заявки."},
                {"category": "Стакан", "parameter": "Изменение ликвидности", "used": False, "how": "Нет отдельного правила сравнения ликвидности во времени при входе."},
                {"category": "Фьючерсы", "parameter": "Funding Rate / Open Interest", "used": bool(get_settings().derivatives_context_enabled), "how": "Запрашивается фьючерсный контекст при включённом derivatives_context_enabled. Наличие данных фиксируется отдельно; недоступные показатели не считаются подтверждением сигнала."},
                {"category": "Волатильность", "parameter": "ATR14", "used": True, "how": "Фильтр входа, ATR-стоп и первоначальный TP."},
                {"category": "Волатильность", "parameter": "1m realized volatility", "used": True, "how": "Сохраняется в микроструктуре; базовый gate использует режим и краткий импульс."},
            ],
        }

    def _external_sources(self) -> dict[str, Any]:
        rows = [
            ("События Binance", bool(get_settings().binance_event_priority_enabled), "Кэшируемый календарь при включённом приоритете событий", "отдельные события Binance / BNB", True, False),
            ("Новости проектов / листинги / делистинги", False, "Не запрашиваются", "—", False, False),
            ("Макроэкономические новости", False, "Не запрашиваются", "—", False, False),
            ("Новости BTC/ETH", False, "Не запрашиваются", "—", False, False),
            ("Социальные сети", False, "Не запрашиваются", "—", False, False),
            ("Биржевые market data", True, "Каждый торговый цикл", "тикер, OHLCV, стакан, последние сделки", True, False),
        ]
        return {
            "sources": [
                {"source": source, "used": used, "frequency": frequency, "data": data, "affects_entry": entry, "affects_exit": exit_}
                for source, used, frequency, data, entry, exit_ in rows
            ],
            "technical_only_entry": True,
            "answer": "Полной новостной проверки нет. Отдельный календарь событий Binance / BNB влияет на приоритет; направление входа определяется техническими, риск- и микроструктурными условиями.",
        }

    def _data_quality(self, closed: list[Any], losses: list[Any], mortems: list[Any]) -> dict[str, Any]:
        contexts = [self._context(item) for item in closed]
        return {
            "closed_position_records": len(closed),
            "loss_records": len(losses),
            "positions_with_entry_context": sum(bool(item) for item in contexts),
            "post_mortems": len(mortems),
            "post_mortem_loss_coverage_percent": self._round(len({getattr(m, "position_id", None) for m in mortems} & {getattr(p, "id", None) for p in losses}) / len(losses) * 100) if losses else None,
            "drawdown_definition": "Max DD в ежедневной таблице — падение накопленного реализованного PnL закрытых сделок внутри дня, USDT. Portfolio-equity drawdown показывается отдельно по сохранённым срезам.",
            "currency": "USDT для PnL, gross profit/loss и realised drawdown; цены и объёмы не конвертируются.",
        }

    def _limitations(self, closed: list[Any], losses: list[Any], mortems: list[Any], postmortem_by_position: dict[int, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not closed:
            rows.append({"area": "Результаты за 30 дней", "message": INSUFFICIENT + " Нет доступных записей закрытых позиций за период."})
        if not mortems:
            rows.append({"area": "Причины убытков и движение после входа", "message": INSUFFICIENT + " Нет TradePostMortem с MFE/MAE, ярлыками причин и execution snapshot."})
        elif len(postmortem_by_position) < len(losses):
            rows.append({"area": "Причины убытков", "message": INSUFFICIENT + f" Post-mortem есть только для {len(postmortem_by_position)} из {len(losses)} убыточных закрытий."})
        rows.extend([
            {"area": "До/после изменений", "message": INSUFFICIENT + " Не ведётся неизменяемый журнал релизов и изменений параметров с временем развёртывания."},
            {"area": "Капитал и portfolio drawdown", "message": INSUFFICIENT + " Не ведутся исторические снимки total/free/reserved equity и открытого риска."},
            {"area": "Корреляция с BTC", "message": INSUFFICIENT + " Нет синхронных временных рядов доходности позиций и BTC на интервале удержания."},
            {"area": "Потенциал после выхода", "message": INSUFFICIENT + " Нет сохранённого 1m пути цены после каждого выхода для всех сделок."},
        ])
        return rows

    def _final_15(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        total = report["total"]
        change = report["time_sequence"]["change_point"]
        return [
            {"question": "Какой общий PnL за 30 дней?", "answer": total["net_pnl"]},
            {"question": "Какой результат по каждому дню?", "answer": "См. daily_results: 30 календарных строк без пропусков."},
            {"question": "Какой результат до последних изменений?", "answer": report["before_after_changes"]["message"]},
            {"question": "Какой результат после последних изменений?", "answer": report["before_after_changes"]["message"]},
            {"question": "С какой даты наблюдается изменение результата?", "answer": change.get("date") if change["status"] == "DETECTED" else change["message"]},
            {"question": "Что именно изменилось?", "answer": report["before_after_changes"]["message"]},
            {"question": "Какие 5 монет дают лучший результат?", "answer": report["top_symbols"]["positive"]},
            {"question": "Какие 5 монет дают худший результат?", "answer": report["top_symbols"]["negative"]},
            {"question": "Почему худшие монеты дают убыток?", "answer": report["loss_causes"]},
            {"question": "Как определяется точка входа?", "answer": report["decision_algorithm"]["sequence"]},
            {"question": "Как определяется размер позиции?", "answer": report["capital_and_risk"]["sizing_formula"]},
            {"question": "Как работает Take Profit?", "answer": report["take_profit"]["current_algorithm"]},
            {"question": "Как работает Stop Loss?", "answer": report["stop_loss"]["current_formula"]},
            {"question": "Что сейчас является главным источником убытков?", "answer": report["loss_causes"]["causes"] or INSUFFICIENT + " Нет классифицированных post-mortem."},
            {"question": "Какие данные необходимо добавить?", "answer": report["limitations"]},
        ]

    def _final_conclusion(self, report: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        positive = report["top_symbols"]["positive"]
        negative = report["top_symbols"]["negative"]
        causes = report["loss_causes"]["causes"]
        works = [
            {
                "statement": f"Положительный реализованный PnL у {row['symbol']}: {row['net_pnl']:.4f} USDT.",
                "evidence": f"{row['trades']} закрытых сделок, Win Rate {row['win_rate']:.2f}%, Profit Factor {row['profit_factor']}.",
            }
            for row in positive
        ]
        if not works:
            works = [{"statement": INSUFFICIENT, "evidence": "Нет прибыльных закрытых пар в доступной выборке."}]
        not_works = [
            {
                "statement": f"Отрицательный реализованный PnL у {row['symbol']}: {row['net_pnl']:.4f} USDT.",
                "evidence": f"{row['trades']} закрытых сделок, Win Rate {row['win_rate']:.2f}%, Profit Factor {row['profit_factor']}.",
            }
            for row in negative
        ]
        not_works.extend(
            {
                "statement": f"Post-mortem: {item['description']}.",
                "evidence": f"Ярлык {item['label']} на {item['trades']} записях post-mortem.",
            }
            for item in causes
        )
        if not not_works:
            not_works = [{"statement": INSUFFICIENT, "evidence": "Нет отрицательных закрытий или классифицированных post-mortem в доступной выборке."}]
        priorities = []
        for item in report["limitations"][:5]:
            priorities.append(
                {
                    "problem": item["area"],
                    "evidence": item["message"],
                    "expected_impact": "Без этого нельзя проверить причинную связь и принять статистически обоснованное изменение стратегии.",
                    "required_data": item["message"].replace(INSUFFICIENT, "").strip(),
                }
            )
        return {"what_works": works, "what_does_not_work": not_works, "check_first": priorities}

    def _day_streak(self, daily: list[dict[str, Any]], *, positive: bool) -> dict[str, Any] | None:
        current: list[dict[str, Any]] = []
        best: list[dict[str, Any]] = []
        for row in daily:
            matches = row["net_pnl"] > 0 if positive else row["net_pnl"] < 0
            if matches:
                current.append(row)
                if len(current) > len(best):
                    best = list(current)
            else:
                current = []
        if not best:
            return None
        return {"days": len(best), "start": best[0]["date"], "end": best[-1]["date"], "net_pnl": self._round(sum(row["net_pnl"] for row in best))}

    def _realized_drawdown(self, pnls: Iterable[float]) -> float:
        cumulative = peak = max_drawdown = 0.0
        for pnl in pnls:
            cumulative += float(pnl)
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        return self._round(max_drawdown)

    def _pnl(self, position: Any) -> float:
        return float(getattr(position, "pnl", 0.0) or 0.0)

    def _duration_minutes(self, position: Any) -> float | None:
        entered, closed = getattr(position, "entered_at", None), getattr(position, "closed_at", None)
        if not entered or not closed:
            return None
        return max((self._aware(closed) - self._aware(entered)).total_seconds() / 60, 0.0)

    def _context(self, position: Any) -> dict[str, Any]:
        value = getattr(position, "entry_context", {}) or {}
        return value if isinstance(value, dict) else {}

    def _context_number(self, position: Any, key: str) -> float | None:
        return self._number(self._context(position).get(key))

    def _setting(self, settings: Any | None, key: str) -> Any:
        return getattr(settings, key, None) if settings is not None else None

    def _number(self, value: Any) -> float | None:
        try:
            number = float(value)
            return number if number == number and abs(number) != float("inf") else None
        except (TypeError, ValueError):
            return None

    def _mean_or_none(self, values: list[float]) -> float | None:
        return self._round(mean(values)) if values else None

    def _aware(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    def _display_hour(self, value: datetime) -> str:
        return self._aware(value).astimezone(self.tz).strftime("%Y-%m-%d %H")

    def _round(self, value: float | None, digits: int = 4) -> float | None:
        return round(float(value), digits) if value is not None else None
