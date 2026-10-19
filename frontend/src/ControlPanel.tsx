import React from "react";
import { api } from "./api/client";

type Row = Record<string, any>;
type Column = { label: string; key: string; render?: (row: Row) => React.ReactNode };
const number = (v: unknown) => typeof v === "number" && Number.isFinite(v) ? v.toLocaleString("ru-RU", { maximumFractionDigits: 6 }) : "Нет данных";
const time = (v: unknown) => typeof v === "string" ? new Date(v).toLocaleString("ru-RU", {timeZone: "Europe/Simferopol"}) : "Нет данных";
const money = (key: string, label: string): Column => ({key, label, render: row => number(row[key])});
const gates: Record<string, string> = { PRETRADE_QUALITY: "Историческая проверка", MICROSTRUCTURE: "Стакан и поток сделок", PRICE_EXTENSION: "Цена далеко от EMA", RL_GATE: "Решение модели", PAPER_LEARNING: "Лимит учебных сделок", EXECUTION: "Исполнение", MAX_POSITIONS: "Лимит позиций", DAILY_RISK_BUDGET: "Дневной риск", STRATEGY_WAIT: "Нет сигнала", POSITION_ALREADY_OPEN: "Позиция уже открыта" };
const stages: Record<string, string> = {ENTRY_OPENED: "Вход", ENTRY_SKIPPED: "Пропуск", ENTRY_REJECTED: "Отказ исполнения", BREAKEVEN_APPLIED: "Стоп в безубытке", POSITION_PARTIALLY_CLOSED: "TP1: исполнен", SECOND_TAKE_PROFIT_FILLED: "TP2: исполнен", DYNAMIC_TAKE_PROFIT_EXTENDED: "Часть прибыли зафиксирована", POSITION_CLOSED: "Закрытие", PROTECTIVE_STOP_CONFIRMED: "Стоп подтверждён"};

function Table({rows, columns}: {rows: Row[]; columns: Column[]}) {
  return <div className="table-wrap"><table><thead><tr>{columns.map(c => <th key={c.key}>{c.label}</th>)}</tr></thead><tbody>
    {rows.map((row, i) => <tr key={row.id ?? i}>{columns.map(c => <td key={c.key}>{c.render ? c.render(row) : row[c.key] == null ? "Нет данных" : String(row[c.key])}</td>)}</tr>)}
    {!rows.length && <tr><td colSpan={columns.length}>Записей пока нет. Это не нулевой результат торговли.</td></tr>}
  </tbody></table></div>;
}

function Lifecycle({id}: {id: number}) {
  const [data, setData] = React.useState<Row | null>(null);
  const [error, setError] = React.useState("");
  React.useEffect(() => {
    const controller = new AbortController(); setData(null); setError("");
    api.get(`/audit/control/positions/${id}`, {signal: controller.signal}).then(r => {if (!controller.signal.aborted) setData(r.data);}).catch(() => {if (!controller.signal.aborted) setError("Не удалось получить историю сделки.");});
    return () => controller.abort();
  }, [id]);
  return <div className="panel-block space-y-3"><h3 className="table-title">История сделки #{id}</h3>
    {error && <p role="alert">{error}</p>}{!data && !error && <p>Загрузка…</p>}
    {data && <><p>{data.position.symbol} · {data.position.side} · {data.position.status} · вход {time(data.position.entered_at)}</p>
      <p className="text-sm text-muted">{data.note} Перенос стопа в безубыток сам по себе не фиксирует прибыль. Результат части учитывает комиссию выхода; итог позиции включает вход и предыдущие части.</p>
      {data.truncated && <p>Показаны первые 500 событий. Полный журнал доступен в экспорте аудита.</p>}
      <Table rows={data.events} columns={[
        {key: "at", label: "Записано", render: r => time(r.at)},
        {key: "event", label: "Событие", render: r => stages[r.event] ?? r.event ?? "Запись журнала"},
        money("exit_price", "Цена выхода"), money("filled_volume", "Исполненный объём"), money("remaining_volume", "Осталось"),
        money("partial_profit", "Результат части, USDT"), money("pnl", "PnL позиции"), money("exit_fee", "Комиссия выхода"),
        {key: "message", label: "Подробности"},
      ]}/></>}
  </div>;
}

export default function ControlPanel() {
  const [tab, setTab] = React.useState("entries");
  const [loaded, setLoaded] = React.useState<{tab: string; value: any} | null>(null);
  const data = loaded?.tab === tab ? loaded.value : null;
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [refresh, setRefresh] = React.useState(0);
  const [positionId, setPositionId] = React.useState<number | null>(null);
  React.useEffect(() => {
    const controller = new AbortController(); setLoaded(null); setError("");
    const load = async () => {
      setBusy(true);
      try { const r = await api.get(`/audit/control/${tab === "models" ? "learning" : tab}`, {signal: controller.signal});
        if (!controller.signal.aborted) {setLoaded({tab, value: r.data}); setError("");}
      } catch {if (!controller.signal.aborted) setError("Данные не загружены. Повторите запрос; предыдущие значения могут быть устаревшими.");}
      finally {if (!controller.signal.aborted) setBusy(false);}
    };
    void load();
    // No permanent polling for large audit/storage tables.
    const timer = tab === "entries" ? window.setInterval(() => {if (!document.hidden) void load();}, 60000) : undefined;
    return () => {controller.abort(); if (timer) clearInterval(timer);};
  }, [tab, refresh]);
  return <section className="space-y-4">
    <div className="panel-block"><h2 className="table-title">Контроль торговли</h2>
      <div className="flex flex-wrap gap-2">{[["requests", "Запросы и Binance"], ["entries", "Почему нет сделки"], ["positions", "Суммы и история"], ["learning", "Что изменилось"], ["models", "Проверка моделей"], ["storage", "Память базы"]].map(([key, label]) => <button key={key} className={`btn ${tab === key ? "primary" : ""}`} aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}
      <button className="btn" disabled={busy} onClick={() => setRefresh(v => v + 1)}>{busy ? "Загрузка…" : "Обновить"}</button></div>
      {error && <p role="alert" className="mt-3 text-danger">{error}</p>}
    </div>
    {data && tab === "requests" && <div className="panel-block space-y-3">
      <h3 className="table-title">Запросы и Binance</h3>
      <p>Срез на {time(data.generated_at)}. Обновляется кнопкой «Обновить», без запросов к бирже.</p>
      <p>{data.note}</p>
      {!data.available ? <p role="alert">{data.reason}</p> : <>
        <p>Расход: {number(data.used_weight)} единиц веса за минуту. Бюджет публичных запросов: {number(data.public_budget)}, общий: {number(data.total_budget)}.</p>
        <p>Остаток для публичных запросов: {number(data.public_remaining)}; общий остаток: {number(data.total_remaining)}.</p>
        <p>{data.cooldown_until ? `Пауза Binance до ${time(data.cooldown_until)} (UTC+3); на момент среза осталось ${number(data.cooldown_seconds)} сек.` : "Активной сохранённой паузы Binance нет. Это не проверка доступности биржи."}</p>
        <p>WebSocket: {data.stream_enabled ? "включён в настройках" : "отключён в настройках"}.</p>
        <Table rows={data.streams} columns={[
          {key:"name",label:"Поток",render:r=>r.name === "ticker" ? "Цены" : "Стакан"},
          {key:"fresh",label:"Активность",render:r=>r.fresh ? "События поступают" : "Нет свежего подтверждения"},
          money("age_seconds","Секунд после последнего события")
        ]}/>
      </>}
    </div>}
    {data && tab === "entries" && <>
      <p>Последнее решение по каждой монете за 24 часа · получено {time(data.generated_at)}. Автообновление раз в минуту. Старый результат не означает текущий сигнал.</p>
      <Table rows={data.entries} columns={[
        {key: "symbol", label: "Монета"}, {key: "checked_at", label: "Проверка", render: r => time(r.checked_at)},
        {key: "event", label: "Результат", render: r => stages[r.event] ?? r.event}, {key: "signal", label: "Сигнал"}, money("score", "Оценка"),
        {key: "gate", label: "Причина", render: r => gates[r.gate] ?? r.gate ?? "Допущен"},
        {key: "notional", label: "Расчёт входа, USDT", render: r => number(r.sizing?.notional)},
        {key: "stop_risk", label: "Риск до стопа, USDT", render: r => number(r.sizing?.stop_risk)},
        {key: "reason", label: "Объяснение"},
      ]}/><p className="text-sm text-muted">Расчёт объёма не является исполнением. Для старых записей объём может отсутствовать.</p>
    </>}
    {data && tab === "positions" && <>
      <p>Последние 50 позиций. Стоимость входа и комиссия — из первоначального исполнения; маржа расчётная. Риск до стопа не включает расходы закрытия. «Нет данных» не заменяется нулём.</p>
      <Table rows={data} columns={[
        {key: "id", label: "Сделка", render: r => <button className="btn" onClick={() => setPositionId(r.id)}>#{r.id} {r.symbol}</button>},
        {key: "status", label: "Статус"}, {key: "side", label: "Сторона"}, money("notional", "Вход, USDT"),
        money("estimated_margin", "Маржа ≈ USDT"), money("leverage", "Плечо"), {key: "margin_mode", label: "Маржа"},
        money("initial_stop_risk", "Начальный риск, USDT"), money("entry_fee", "Комиссия входа"), money("pnl", "PnL позиции"),
        {key: "virtual_subminimum", label: "Биржевой минимум", render: r => r.virtual_subminimum ? "Только виртуальное исполнение" : "См. ордер"},
      ]}/>{positionId && <Lifecycle id={positionId}/>}</>}
    {data && tab === "learning" && <>
      <h3 className="table-title">Обучение по закрытым сделкам · последние 30 событий</h3>
      <Table rows={data.updates} columns={[{key: "at", label: "Время", render: r => time(r.at)}, {key: "position_id", label: "Сделка"}, {key: "symbol", label: "Монета"}, money("pnl", "PnL"), money("outcome_r", "Результат в R"), money("rules_updated", "Обновлено правил"), money("penalty_delta", "Изменение штрафа до ограничений"), {key: "applied", label: "Записано", render: r => r.applied === true ? "Да" : "Нет подтверждения"}]}/>
      <p>{data.note}</p><h3 className="table-title">Последние обновления памяти · до 30 правил</h3>
      <Table rows={data.rules} columns={[{key: "symbol", label: "Область"}, {key: "feature", label: "Условие"}, money("observations", "Наблюдений"), money("wins", "Плюсовых"), money("losses", "Минусовых"), money("total_profit", "PnL выборки"), money("penalty", "Текущий штраф"), {key: "updated_at", label: "Обновлено", render: r => time(r.updated_at)}]}/>
      <h3 className="table-title">Изменения настроек между запусками версий</h3>
      {(data.changes as Row[]).map((c, i) => <div className="panel-block" key={i}><p>{String(c.version).slice(0, 8)} · {time(c.at)}</p><Table rows={c.parameters} columns={[{key: "name", label: "Параметр"}, {key: "before", label: "До"}, {key: "after", label: "После"}]}/></div>)}
      {!data.changes.length && <p>Недостаточно сохранённых версий для сравнения.</p>}
    </>}
    {data && tab === "models" && <>
      <p>История и виртуальная торговля проверяются отдельно. Прохождение порогов не гарантирует прибыль. Сравнение с основной моделью требует одинакового периода и достаточной выборки.</p>
      <Table rows={data.models} columns={[
        {key: "id", label: "Модель"}, {key: "symbol", label: "Монета"}, {key: "status", label: "Статус"},
        {key: "history", label: "История", render: r => r.historical?.backtest_passed === true ? "Проверка пройдена" : r.historical?.backtest_passed === false ? "Не пройдена" : "Нет данных"},
        {key: "historical_trades", label: "Сделок на истории", render: r => number(r.historical?.trades)},
        {key: "forward_status", label: "Paper-проверка"},
        {key: "forward_trades", label: "Закрыто Paper", render: r => number(r.forward?.closed_trades)},
        {key: "forward_pnl", label: "Paper PnL", render: r => number(r.forward?.total_pnl)},
        {key: "comparison", label: "Сравнение", render: r => r.comparison ? <details><summary>{r.comparison.reason}</summary><p>Период: {time(r.comparison.start)} — {time(r.comparison.end)}</p><p>Закрыто новая / основная: {number(r.comparison.candidate?.trades)} / {number(r.comparison.incumbent?.trades)}</p><p>Средняя доходность, %: {number(r.comparison.candidate?.mean_net_return_percent)} / {number(r.comparison.incumbent?.mean_net_return_percent)}</p><p>Просадка нормированной кривой: {number(r.comparison.candidate?.normalized_drawdown_points)} / {number(r.comparison.incumbent?.normalized_drawdown_points)}</p><p>{r.comparison.note}</p></details> : "Сравнение не записано; превосходство не подтверждено"},
      ]}/>
    </>}
    {data && tab === "storage" && <>
      <div className="panel-block"><h3>База: {number(data.database_bytes / 1024 / 1024)} МБ</h3><p>{data.note}</p>
        <p>Технические сводки: {data.technical_log_days} дней · очистка {data.cleanup_enabled ? "включена" : "выключена"}. Доставленные уведомления: {data.telegram_days} дней. Кэш Telegram на телефоне сюда не входит.</p></div>
      <Table rows={data.tables} columns={[{key: "name", label: "Таблица"}, money("estimated_rows", "Строк ≈"), {key: "bytes", label: "МБ с индексами", render: r => number(r.bytes / 1024 / 1024)}]}/>
    </>}
  </section>;
}
