"""Observed whole-position net PnL; immutable original risk, no price-path invention."""
from datetime import datetime, timezone
import math


def record_excursion(position, pnl, *, at=None, final=False, from_entry=False):
    pnl = float(pnl)
    if not math.isfinite(pnl):
        return
    context = dict(position.entry_context or {})
    state = dict(context.get("profit_excursion") or {})
    instant = (at or datetime.now(timezone.utc)).isoformat()
    if not state:
        risk = float(context.get("planned_risk") or 0)
        state = {"version": 1, "risk_basis_usdt": risk if math.isfinite(risk) and risk > 0 else None,
                 "coverage": "FROM_ENTRY" if from_entry else "SINCE_FIRST_OBSERVATION",
                 "first_observed_at": instant, "samples": 0}
    best = max(float(state.get("best_observed_pnl_usdt", pnl)), pnl)
    worst = min(float(state.get("worst_observed_pnl_usdt", pnl)), pnl)
    if best == pnl:
        state["best_observed_at"] = instant
    if worst == pnl:
        state["worst_observed_at"] = instant
    state.update(best_observed_pnl_usdt=best, worst_observed_pnl_usdt=worst,
                 max_profit_usdt=max(best, 0.0), max_loss_usdt=max(-worst, 0.0),
                 last_pnl_usdt=pnl, last_observed_at=instant, samples=state["samples"] + 1)
    # Giveback includes falling from a positive peak through zero into a loss.
    state["giveback_usdt"] = max(best - pnl, 0.0) if best > 0 else 0.0
    if final:
        state["final_pnl_usdt"] = pnl
    for key in ("max_profit", "max_loss", "giveback", "last_pnl", "final_pnl"):
        if f"{key}_usdt" in state:
            risk = state["risk_basis_usdt"]
            state[f"{key}_r"] = state[f"{key}_usdt"] / risk if risk else None
    state["basis"] = "realized_plus_remaining_unrealized; paid_fees_included; future_exit_costs_excluded"
    context["profit_excursion"] = state
    position.entry_context = context


def excursion_summary(context):
    state = (context or {}).get("profit_excursion")
    if not state:
        return "Экстремумы PnL: наблюдения ещё не записаны.\n"
    def value(key):
        r = state.get(f"{key}_r")
        return f"{state.get(key + '_usdt', 0):.4f} USDT / " + (f"{r:.2f}R" if r is not None else "R неизвестен")
    coverage = "с момента входа" if state["coverage"] == "FROM_ENTRY" else "только с начала наблюдений"
    return (f"Макс. прибыль: {value('max_profit')}\n"
            f"Макс. убыток: {value('max_loss')}\n"
            f"Отдано от положительного пика: {value('giveback')}\n"
            f"Наблюдения: {coverage}; между проверками экстремумы могли быть больше.\n"
            "Пик включает реализованный и плавающий PnL с уплаченными комиссиями, "
            "без будущих расходов выхода.\n")
