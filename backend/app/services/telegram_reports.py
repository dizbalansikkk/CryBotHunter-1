from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Iterable

from app.models.entities import Position
from app.schemas.dto import PositionUpdateOut, TradingDecision, TradingRunOut, TradingTickOut
from app.services.system_health import ComponentHealth, SystemHealthSnapshot
from app.services.telegram_daily import DailyReportSnapshot


EXIT_REASON_LABELS = {
    "STOP_LOSS": "сработал стоп-лосс",
    "TAKE_PROFIT": "достигнут тейк-профит",
    "EMERGENCY_DRAWDOWN": "аварийное закрытие из-за лимита просадки",
    "MANUAL": "закрыто вручную",
}

DECISION_REASON_LABELS = {
    "strategy returned WAIT": "строгий торговый сигнал ещё не сформирован",
    "position already open for symbol": "по этому инструменту уже есть открытая позиция",
    "maximum open positions reached": "достигнут лимит открытых позиций",
    "signal score below minimum rating": "оценка сигнала ниже допустимого порога",
    "daily loss limit reached": "достигнут дневной лимит убытка",
    "gross exposure limit reached": "достигнут общий лимит нагрузки на депозит",
    "symbol exposure limit reached": "достигнут лимит нагрузки по инструменту",
    "paper exploration position limit reached": "достигнут лимит исследовательских paper-позиций",
    "paper exploration cooldown is active": "для инструмента ещё действует пауза между тестовыми входами",
    "loss streak limit reached": "достигнут лимит серии убытков",
    "win rate below guard threshold": "доля прибыльных сделок ниже защитного порога",
    "recent total profit below guard threshold": "результат последних сделок ниже защитного порога",
    "performance guard recovery position limit reached": "в режиме восстановления разрешена только одна позиция",
}


def format_worker_started(
    *,
    paper_trading: bool,
    loop_seconds: int,
    exploration_enabled: bool,
    exploration_max_positions: int,
    exploration_risk_percent: float,
    report_interval_minutes: int,
) -> str:
    mode = "PAPER · виртуальные средства" if paper_trading else "LIVE · реальные ордера"
    exploration = "включены" if paper_trading and exploration_enabled else "выключены"
    return (
        "<b>🟢 CRYBOTHUNTER · СЕРВИС ЗАПУЩЕН</b>\n"
        f"<code>{mode}</code>\n\n"
        "<b>Рабочий режим</b>\n"
        f"├ Проверка рынка: <code>{loop_seconds} сек.</code>\n"
        f"├ Тестовые входы: <code>{exploration}</code>\n"
        f"├ Лимит позиций: <code>{exploration_max_positions}</code>\n"
        f"├ Риск на позицию: <code>до {exploration_risk_percent:.3f}%</code>\n"
        f"└ Полная сводка: <code>каждые {report_interval_minutes} мин.</code>\n\n"
        "<i>Входы, сопровождение, SL/TP, результат и причины решений будут приходить отдельными понятными блоками.</i>"
    )


def format_trade_opened(
    position: Position,
    *,
    score: int,
    reason: str,
    paper_trading: bool,
    exploration: bool,
) -> str:
    context = position.entry_context or {}
    notional = float(context.get("notional") or float(position.entry_price) * float(position.volume))
    risk_amount = float(
        context.get("planned_risk")
        or abs(float(position.entry_price) - float(position.stop)) * float(position.volume)
    )
    reward_amount = float(
        context.get("planned_reward")
        or abs(float(position.take) - float(position.entry_price)) * float(position.volume)
    )
    risk_reward = float(context.get("risk_reward_ratio") or (reward_amount / risk_amount if risk_amount > 0 else 0))
    stop_distance = float(
        context.get("stop_distance_percent")
        or abs(float(position.entry_price) - float(position.stop)) / float(position.entry_price) * 100
    )
    take_distance = float(
        context.get("take_distance_percent")
        or abs(float(position.take) - float(position.entry_price)) / float(position.entry_price) * 100
    )
    source = (
        "исследовательский paper-вход: строгий сигнал был WAIT, направление выбрано по рыночному уклону"
        if exploration
        else "основная стратегия и все защитные проверки разрешили вход"
    )
    heading = "🧪 ОТКРЫТА ТЕСТОВАЯ ПОЗИЦИЯ" if paper_trading else "🚀 ОТКРЫТА РЕАЛЬНАЯ ПОЗИЦИЯ"
    return (
        f"<b>{heading}</b>\n"
        f"<code>{_html(position.symbol)} · {_html(_side_label(position.side))} · #{position.id}</code>\n\n"
        "<b>Исполнение</b>\n"
        f"├ Вход: <code>{_price(position.entry_price)}</code>\n"
        f"├ Время: <code>{_format_datetime(position.entered_at)}</code>\n"
        f"├ Объём: <code>{float(position.volume):.8f}</code>\n"
        f"└ Сумма: <code>{notional:.2f} USDT</code>\n\n"
        "<b>Риск-план</b>\n"
        f"├ SL: <code>{_price(position.stop)}</code> · {stop_distance:.2f}% · риск ≈ {risk_amount:.2f} USDT\n"
        f"├ TP: <code>{_price(position.take)}</code> · {take_distance:.2f}% · цель ≈ {reward_amount:.2f} USDT\n"
        f"├ Risk/Reward: <code>1:{risk_reward:.2f}</code> · риск баланса {float(context.get('risk_percent') or 0):.3f}%\n"
        f"├ Трейлинг: <code>{float(position.trailing_stop_percent):.2f}%</code>\n"
        f"└ Безубыток: {float(position.breakeven_trigger_r or 1.0):.2f}R · частичная фиксация "
        f"{float(position.partial_close_percent or 50.0):.0f}% при {float(position.partial_take_profit_r or 1.0):.2f}R\n\n"
        "<b>Почему открыт вход</b>\n"
        f"├ Сила сигнала: <code>{score}/100</code>\n"
        f"├ Источник: {_html(source)}\n"
        f"├ Проверки: {_html(human_reason(reason))}\n"
        f"├ Рынок: {_html(_entry_context_summary(context))}\n"
        f"└ Факторы: {_html(_entry_reason_summary(context))}\n\n"
        "<i>Дальше бот отслеживает цену, PnL, перенос защиты, частичную фиксацию и закрытие.</i>"
    )


def format_position_details(position: Position) -> str:
    context = position.entry_context or {}
    notional = float(context.get("notional") or float(position.entry_price) * float(position.volume))
    pnl = float(position.pnl or 0.0)
    pnl_percent = pnl / notional * 100 if notional > 0 else 0.0
    movement = _effective_percent(position.side, position.current_price, position.entry_price)
    stop_distance = _percent_distance(position.current_price, position.stop)
    take_distance = _percent_distance(position.current_price, position.take)
    protection = []
    if position.breakeven_applied:
        protection.append("стоп уже перенесён в безубыток")
    if position.partial_taken:
        protection.append("часть прибыли уже зафиксирована")
    if not protection:
        protection.append("исходный защитный план активен")
    pnl_icon = "🟢" if pnl >= 0 else "🔴"
    return (
        f"<b>{pnl_icon} {_html(position.symbol)} · {_html(_side_label(position.side))}</b>  <code>#{position.id}</code>\n"
        f"<b>PnL: {pnl:+.2f} USDT · {pnl_percent:+.2f}%</b>\n\n"
        "<b>Позиция</b>\n"
        f"├ Статус: <code>{_html(position.status)}</code> · {_duration(position.entered_at, position.closed_at)}\n"
        f"├ Цена: <code>{_price(position.entry_price)} → {_price(position.current_price)}</code> · {movement:+.2f}%\n"
        f"├ Объём: <code>{float(position.volume):.8f}</code> · {notional:.2f} USDT\n"
        f"├ SL: <code>{_price(position.stop)}</code> · расстояние {stop_distance:.2f}%\n"
        f"└ TP: <code>{_price(position.take)}</code> · расстояние {take_distance:.2f}%\n\n"
        "<b>Контроль риска</b>\n"
        f"├ Защита: {_html(', '.join(protection))}\n"
        f"└ Трейлинг: <code>{float(position.trailing_stop_percent):.2f}%</code>\n\n"
        f"<b>Контекст входа</b>\n{_html(_entry_context_summary(context))}\n"
        f"<b>Причины входа</b>\n{_html(_entry_reason_summary(context))}"
    )


def format_protection_update(position: Position, event: str) -> str:
    label = "стоп перенесён в безубыток" if event == "BREAKEVEN" else event
    return (
        "<b>🛡 ЗАЩИТА ПОЗИЦИИ ОБНОВЛЕНА</b>\n"
        f"<code>{_html(position.symbol)} · {_html(_side_label(position.side))} · #{position.id}</code>\n\n"
        f"├ Событие: <b>{_html(label)}</b>\n"
        f"├ Текущая цена: <code>{_price(position.current_price)}</code>\n"
        f"├ Новый SL: <code>{_price(position.stop)}</code>\n"
        f"└ PnL: <b>{float(position.pnl):+.2f} USDT</b>"
    )


def format_partial_take_profit(
    position: Position,
    *,
    closed_volume: float,
    exit_price: float,
    profit: float,
) -> str:
    return (
        "<b>💰 ЧАСТЬ ПРИБЫЛИ ЗАФИКСИРОВАНА</b>\n"
        f"<code>{_html(position.symbol)} · {_html(_side_label(position.side))} · #{position.id}</code>\n\n"
        "<b>Исполнение</b>\n"
        f"├ Закрытый объём: <code>{closed_volume:.8f}</code>\n"
        f"├ Цена фиксации: <code>{_price(exit_price)}</code>\n"
        f"├ Результат части: <b>{profit:+.2f} USDT</b>\n"
        f"├ Остаток: <code>{float(position.volume):.8f}</code>\n"
        f"└ Общий PnL: <b>{float(position.pnl):+.2f} USDT</b>"
    )


def format_trade_closed(position: Position, *, exit_price: float, reason: str) -> str:
    pnl = float(position.pnl or 0.0)
    result = "прибыль" if pnl > 0 else "убыток" if pnl < 0 else "без результата"
    duration = _duration(position.entered_at, position.closed_at)
    context = position.entry_context or {}
    exploration = bool(context.get("paper_exploration"))
    notional = float(context.get("notional") or float(position.entry_price) * float(position.volume))
    pnl_percent = pnl / notional * 100 if notional > 0 else 0.0
    planned_risk = float(context.get("planned_risk") or float(position.initial_risk or 0) * float(position.volume))
    result_r = pnl / planned_risk if planned_risk > 0 else 0.0
    effective_move = _effective_percent(position.side, exit_price, position.entry_price)
    best_move = _effective_percent(
        position.side,
        _positive_or_entry(
            position.highest_price if position.side.upper() == "LONG" else position.lowest_price,
            position.entry_price,
        ),
        position.entry_price,
    )
    worst_move = _effective_percent(
        position.side,
        _positive_or_entry(
            position.lowest_price if position.side.upper() == "LONG" else position.highest_price,
            position.entry_price,
        ),
        position.entry_price,
    )
    learning = (
        "Результат записан в память сделок и будет учитыватьcя при следующих входах."
        if exploration or context
        else "Результат сохранён в истории сделок."
    )
    heading = "✅ ПОЗИЦИЯ ЗАКРЫТА" if pnl >= 0 else "🔴 ПОЗИЦИЯ ЗАКРЫТА С УБЫТКОМ"
    return (
        f"<b>{heading}</b>\n"
        f"<code>{_html(position.symbol)} · {_html(_side_label(position.side))} · #{position.id}</code>\n\n"
        f"<b>ИТОГ: {pnl:+.2f} USDT · {pnl_percent:+.2f}% · {result_r:+.2f}R</b>\n"
        f"<i>{result}</i>\n\n"
        "<b>Исполнение</b>\n"
        f"├ Вход: <code>{_price(position.entry_price)}</code>\n"
        f"├ Выход: <code>{_price(exit_price)}</code>\n"
        f"├ Объём: <code>{float(position.volume):.8f}</code> · {notional:.2f} USDT\n"
        f"├ Время в позиции: <code>{duration}</code>\n"
        f"└ Причина: {_html(EXIT_REASON_LABELS.get(reason, reason))}\n\n"
        "<b>Как прошла сделка</b>\n"
        f"├ Итоговое движение: <code>{effective_move:+.2f}%</code>\n"
        f"├ Лучшее движение: <code>{best_move:+.2f}%</code>\n"
        f"├ Худшее движение: <code>{worst_move:+.2f}%</code>\n"
        f"└ {_html(_close_explanation(position.side, exit_price, position.entry_price, reason))}\n\n"
        f"<b>Условия входа</b>\n{_html(_entry_context_summary(context))}\n"
        f"<b>Первоначальные факторы</b>\n{_html(_entry_reason_summary(context))}\n\n"
        f"<i>{_html(learning)}</i>"
    )


def format_cycle_report(
    run: TradingRunOut,
    tick: TradingTickOut,
    *,
    paper_trading: bool,
) -> str:
    lines = [
        f"<b>📊 ТОРГОВАЯ СВОДКА · {'PAPER' if paper_trading else 'LIVE'}</b>",
        "<code>последний завершённый цикл</code>",
        "",
        "<b>Результат цикла</b>",
        f"├ Рынков проверено: <code>{run.scanned}</code>",
        f"├ Новых входов: <code>{run.opened}</code>",
        f"├ Пропущено: <code>{run.skipped}</code>",
        f"├ Позиций проверено: <code>{tick.checked}</code>",
        f"├ Закрыто: <code>{tick.closed}</code>",
        f"└ Обучающих событий: <code>{tick.closed}</code>",
    ]
    if run.decisions:
        lines.extend(["", "<b>Решения по рынку</b>"])
        lines.extend(_decision_lines(run.decisions))
    if tick.updated:
        lines.extend(["", "<b>Открытые позиции</b>"])
        lines.extend(_position_update_lines(tick.updated))
    if run.opened == 0 and tick.closed == 0:
        lines.extend(["", "<i>Бот работает штатно. В этом цикле новые входы не прошли условия риска или сигнала.</i>"])
    return "\n".join(lines)


def format_worker_error(title: str, detail: str) -> str:
    return (
        "<b>⚠️ ОШИБКА ТОРГОВОГО СЕРВИСА</b>\n\n"
        f"<b>Событие</b>\n{_html(title)}\n\n"
        f"<b>Технические подробности</b>\n<code>{_html(detail[:1200])}</code>\n\n"
        "<i>Новые входы временно не выполняются. Открытые позиции будут проверены повторно после восстановления цикла.</i>"
    )


def format_worker_heartbeat_event(
    *,
    kind: str,
    worker_name: str,
    status: str,
    age_seconds: int,
    detail: dict | None = None,
    stale_after_seconds: int | None = None,
) -> str:
    recovered = kind.upper() == "RECOVERED"
    heading = "✅ HEARTBEAT ВОССТАНОВЛЕН" if recovered else "🚨 НЕТ HEARTBEAT ОТ WORKER"
    age = _duration_seconds(age_seconds)
    stale_after = max(int(stale_after_seconds or 180), 60)
    status_text = worker_status_label(status)
    task_text = worker_detail_summary(detail or {})
    action = (
        "Worker снова подтверждает работу. Повторный аварийный сигнал придёт только при новой потере heartbeat."
        if recovered
        else "Heartbeat отсутствует дольше допустимого времени. Проверь состояние сервиса и Deploy Logs в Railway."
    )
    return (
        f"<b>{heading}</b>\n"
        f"<code>{_html(worker_name)}</code>\n\n"
        "<b>Состояние</b>\n"
        f"├ Этап: <code>{_html(status_text)}</code>\n"
        f"├ Последнее подтверждение: <code>{age} назад</code>\n"
        f"├ Порог тревоги: <code>{_duration_seconds(stale_after)}</code>\n"
        f"└ Задача: {_html(task_text)}\n\n"
        f"<i>{action}</i>"
    )


def format_system_health(snapshot: SystemHealthSnapshot) -> str:
    overall_icon = "🟢" if snapshot.healthy else "🔴"
    overall = "СИСТЕМА ИСПРАВНА" if snapshot.healthy else "ТРЕБУЕТ ВНИМАНИЯ"
    mode = "PAPER · виртуальные средства" if snapshot.paper_trading else "LIVE · реальные ордера"
    worker_lines = [
        (
            f"{'🟢' if item.healthy else '🔴'} {_html(item.name)} · "
            f"<code>{_html(worker_status_label(item.status))}</code> · "
            f"{_duration_seconds(item.age_seconds)} назад"
            + (
                f"\n   {_html(worker_detail_summary(item.detail))}"
                if item.detail
                else ""
            )
        )
        for item in snapshot.workers
    ]
    if not worker_lines:
        worker_lines.append("🟡 Heartbeat-данных пока нет")
    pause_reason = _html(snapshot.pause_reason or "нет")
    return (
        f"<b>{overall_icon} ДИАГНОСТИКА · {overall}</b>\n"
        f"<code>{mode}</code>\n\n"
        "<b>Зависимости</b>\n"
        f"{_component_line(snapshot.database)}\n"
        f"{_component_line(snapshot.redis)}\n"
        f"{_component_line(snapshot.exchange)}\n\n"
        "<b>Торговая безопасность</b>\n"
        f"├ Новые входы: <code>{'ПАУЗА' if snapshot.trading_paused else 'РАЗРЕШЕНЫ'}</code>\n"
        f"└ Причина паузы: {pause_reason}\n\n"
        "<b>Очередь Telegram</b>\n"
        f"├ Ожидают отправки: <code>{snapshot.pending_notifications}</code>\n"
        f"└ Окончательно не доставлены: <code>{snapshot.failed_notifications}</code>\n\n"
        "<b>Worker heartbeat</b>\n"
        + "\n".join(worker_lines)
        + f"\n\n<i>Проверено {snapshot.generated_at:%d.%m.%Y %H:%M:%S} UTC</i>"
    )


def format_daily_report(snapshot: DailyReportSnapshot) -> str:
    result_icon = "🟢" if snapshot.pnl_day >= 0 else "🔴"
    mode = "PAPER · виртуальные средства" if snapshot.paper_trading else "LIVE · реальные ордера"
    worker_state = f"{snapshot.healthy_workers}/{snapshot.total_workers} штатно"
    if snapshot.unhealthy_workers:
        worker_state += f"; внимание: {', '.join(snapshot.unhealthy_workers)}"
    position_lines = [
        (
            f"{'🟢' if item.pnl >= 0 else '🔴'} <b>{_html(item.symbol)}</b> · "
            f"{_html(_side_label(item.side))}\n"
            f"   PnL <b>{item.pnl:+.2f} USDT</b> · цена <code>{_price(item.current_price)}</code>"
        )
        for item in snapshot.positions[:8]
    ]
    if not position_lines:
        position_lines.append("Открытых позиций нет.")
    if len(snapshot.positions) > 8:
        position_lines.append(f"…и ещё {len(snapshot.positions) - 8}")
    return (
        f"<b>{result_icon} ЕЖЕДНЕВНЫЙ ОТЧЁТ · CRYBOTHUNTER</b>\n"
        f"<code>{mode}</code>\n\n"
        "<b>Результат портфеля</b>\n"
        f"├ PnL за день: <b>{snapshot.pnl_day:+.2f} USDT</b>\n"
        f"├ PnL за 7 дней: <code>{snapshot.pnl_week:+.2f} USDT</code>\n"
        f"├ Общий PnL: <code>{snapshot.total_pnl:+.2f} USDT</code>\n"
        f"├ Открытый PnL: <code>{snapshot.open_pnl:+.2f} USDT</code>\n"
        f"├ Закрыто сегодня: <code>{snapshot.closed_today}</code>\n"
        f"├ Всего закрытых сделок: <code>{snapshot.trades_count}</code>\n"
        f"└ Доля прибыльных: <code>{snapshot.win_rate:.2f}%</code>\n\n"
        "<b>Открытые позиции</b>\n"
        + "\n".join(position_lines)
        + "\n\n<b>Обучение</b>\n"
        f"├ Правил из сделок: <code>{snapshot.learning_rules}</code>\n"
        f"├ Наблюдений: <code>{snapshot.learning_observations}</code>\n"
        f"└ Активных RL-моделей: <code>{snapshot.active_rl_models}</code>\n\n"
        "<b>Надёжность</b>\n"
        f"├ Воркеры: <code>{_html(worker_state)}</code>\n"
        f"├ Telegram в очереди: <code>{snapshot.pending_notifications}</code>\n"
        f"└ Ошибок доставки: <code>{snapshot.failed_notifications}</code>\n\n"
        f"<i>Снимок на {snapshot.generated_at:%d.%m.%Y %H:%M} UTC. "
        "PnL за день включает текущий плавающий результат открытых позиций.</i>"
    )


def _component_line(component: ComponentHealth) -> str:
    icon = "🟢" if component.ok else "🔴"
    latency = f"{component.latency_ms} мс" if component.latency_ms is not None else "нет данных"
    return (
        f"{icon} <b>{_html(component.name)}</b> · <code>{latency}</code>\n"
        f"   {_html(component.detail)}"
    )


def human_reason(reason: str) -> str:
    normalized = reason.strip()
    if normalized in DECISION_REASON_LABELS:
        return DECISION_REASON_LABELS[normalized]
    translated = normalized
    for source, target in DECISION_REASON_LABELS.items():
        translated = translated.replace(source, target)
    translated = translated.replace("risk accepted", "базовая проверка риска пройдена")
    translated = translated.replace(
        "paper exploration keeps hard risk and market-quality gates",
        "для исследовательского входа сохранены лимиты риска, ликвидности и экспозиции",
    )
    translated = translated.replace("paper exploration from WAIT", "исследовательский вход из сигнала WAIT")
    translated = translated.replace("recovery probe after", "пробный вход после паузы:")
    translated = translated.replace("recovery cooldown until", "режим восстановления начнётся после")
    translated = translated.replace("risk reduced to", "риск снижен до")
    return translated or "причина не указана"


def split_telegram_message(text: str, limit: int = 4000) -> list[str]:
    clean = text.strip()
    if not clean:
        return []
    chunks: list[str] = []
    current = ""
    for line in clean.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _decision_lines(decisions: Iterable[TradingDecision]) -> list[str]:
    lines: list[str] = []
    for decision in decisions:
        action = "ОТКРЫТА" if decision.action == "OPENED" else "ПРОПУЩЕНА"
        icon = "🟢" if decision.action == "OPENED" else "⏸"
        signal = {"BUY": "ПОКУПКА", "SELL": "ПРОДАЖА", "WAIT": "ОЖИДАНИЕ"}.get(
            decision.signal,
            decision.signal,
        )
        lines.append(
            f"{icon} <b>{_html(decision.symbol)}</b> · {_html(signal)} · <code>{decision.score}/100</code>\n"
            f"   {_html(action.capitalize())}: {_html(human_reason(decision.reason))}"
        )
    return lines


def _position_update_lines(updates: Iterable[PositionUpdateOut]) -> list[str]:
    lines: list[str] = []
    for item in updates:
        status = "закрыта" if item.status == "CLOSED" else "открыта"
        movement = _effective_percent(item.side, item.current_price, item.entry_price or item.previous_price)
        notional = float(item.entry_price or item.current_price) * float(item.volume)
        pnl_percent = float(item.pnl) / notional * 100 if notional > 0 else 0.0
        icon = "🟢" if float(item.pnl) >= 0 else "🔴"
        lines.append(
            f"{icon} <b>{_html(item.symbol)}</b> · {_html(_side_label(item.side))} · <code>#{item.id}</code>\n"
            f"   {status} · PnL <b>{item.pnl:+.2f} USDT</b> ({pnl_percent:+.2f}%) · движение {movement:+.2f}%\n"
            f"   Цена <code>{_price(item.current_price)}</code> · SL {_price(item.stop)} "
            f"({_percent_distance(item.current_price, item.stop):.2f}%) · TP {_price(item.take)} "
            f"({_percent_distance(item.current_price, item.take):.2f}%)"
        )
    return lines


def _side_label(side: str) -> str:
    return "LONG / покупка" if side.upper() == "LONG" else "SHORT / продажа"


def _html(value: object) -> str:
    return escape(str(value), quote=False)


def _price(value: float) -> str:
    number = float(value)
    if abs(number) >= 1000:
        return f"{number:,.2f}".replace(",", " ")
    if abs(number) >= 1:
        return f"{number:.4f}"
    return f"{number:.8f}"


def _percent_change(current: float, entry: float) -> float:
    base = float(entry)
    if base == 0:
        return 0.0
    return (float(current) / base - 1) * 100


def _effective_percent(side: str, current: float, entry: float) -> float:
    movement = _percent_change(current, entry)
    return movement if side.upper() == "LONG" else -movement


def _percent_distance(current: float, target: float) -> float:
    base = float(current)
    if base == 0:
        return 0.0
    return abs(float(target) / base - 1) * 100


def _positive_or_entry(value: float | None, entry: float) -> float:
    candidate = float(value or 0)
    return candidate if candidate > 0 else float(entry)


def _format_datetime(value: datetime | None) -> str:
    if not value:
        return "будет записано после сохранения позиции"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _entry_context_summary(context: dict) -> str:
    if not context:
        return "контекст старой позиции не сохранён"
    regime = str(context.get("regime") or "UNKNOWN")
    rating = context.get("rating", "—")
    rsi = context.get("rsi", "—")
    atr = context.get("atr_percent", "—")
    trend = str(context.get("trend_stack") or "—")
    macd = str(context.get("macd_direction") or "—")
    return f"режим {regime}, рейтинг {rating}/100, RSI {rsi}, ATR {atr}%, тренд {trend}, MACD {macd}"


def _entry_reason_summary(context: dict) -> str:
    reasons = context.get("reasons") if context else None
    if not isinstance(reasons, list) or not reasons:
        decision = str(context.get("decision_reason") or "") if context else ""
        return human_reason(decision) if decision else "для старой позиции подробные факторы не сохранены"
    return "; ".join(human_reason(str(reason)) for reason in reasons[:5])


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start:
        return "неизвестно"
    finish = end or datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if finish.tzinfo is None:
        finish = finish.replace(tzinfo=timezone.utc)
    seconds = max(int((finish - start).total_seconds()), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def _duration_seconds(seconds: int) -> str:
    minutes, remaining = divmod(max(int(seconds), 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {remaining} сек"
    return f"{remaining} сек"


def worker_status_label(status: str) -> str:
    labels = {
        "STARTING": "запускается",
        "RUNNING": "выполняет цикл",
        "TRAINING": "обучает RL-модель",
        "IDLE": "ожидает следующий цикл",
        "OK": "работает штатно",
        "PAUSED": "на паузе",
        "DISABLED": "отключён настройкой",
        "DEGRADED": "цикл завершён с ошибками",
        "ERROR": "ошибка",
    }
    normalized = str(status or "UNKNOWN").upper()
    return labels.get(normalized, normalized)


def worker_detail_summary(detail: dict) -> str:
    if not detail:
        return "дополнительных данных нет"
    stage_labels = {
        "startup": "инициализация процесса",
        "cycle_start": "подготовка нового цикла",
        "checking_model": "проверка свежести модели",
        "ppo_training": "PPO-обучение и validation",
        "publishing_decision": "расчёт свежего RL-решения",
        "cycle_complete": "цикл завершён",
        "cycle_failed": "цикл аварийно завершён",
    }
    parts: list[str] = []
    stage = str(detail.get("stage") or "")
    if stage:
        parts.append(stage_labels.get(stage, stage))
    if detail.get("pair"):
        parts.append(str(detail["pair"]))
    if detail.get("progress"):
        parts.append(f"прогресс {detail['progress']}")
    if stage == "cycle_complete":
        parts.append(
            "обучено {trained}, принято {promoted}, решений {decisions}, ошибок {errors}".format(
                trained=int(detail.get("trained", 0)),
                promoted=int(detail.get("promoted", 0)),
                decisions=int(detail.get("decisions", 0)),
                errors=int(detail.get("errors", 0)),
            )
        )
    if detail.get("error"):
        parts.append(f"ошибка {detail['error']}")
    return " · ".join(parts) or "служебный heartbeat"


def _close_explanation(side: str, exit_price: float, entry_price: float, reason: str) -> str:
    movement = _percent_change(exit_price, entry_price)
    effective = movement if side.upper() == "LONG" else -movement
    direction = "в пользу позиции" if effective > 0 else "против позиции" if effective < 0 else "без движения"
    trigger = EXIT_REASON_LABELS.get(reason, reason)
    return f"цена изменилась на {movement:+.2f}% ({direction}); {trigger}"
