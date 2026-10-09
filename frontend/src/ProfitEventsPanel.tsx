import React from "react";
import { api, LogEntry } from "./api/client";

const labels: Record<string, string> = {
  POSITION_PARTIALLY_CLOSED: "TP1 · первая фиксация",
  SECOND_TAKE_PROFIT_FILLED: "TP2 · средний уровень",
  DYNAMIC_TAKE_PROFIT_EXTENDED: "TP3 · часть зафиксирована, цель продлена",
  POSITION_EXIT_PARTIALLY_FILLED: "Выход исполнен частично",
  POSITION_CLOSED: "Финальное закрытие",
  BREAKEVEN_APPLIED: "Безубыток · стоп перенесён",
  PROTECTIVE_STOP_CONFIRMED: "Стоп подтверждён биржей",
  PROTECTIVE_STOP_UNCONFIRMED: "Стоп не подтверждён биржей",
  BREAKEVEN_STOP_CONFIRMATION_PENDING: "Ожидается подтверждение стопа",
  PARTIAL_TAKE_PROFIT_FAILED: "TP1 · исполнение не подтверждено",
  SECOND_TAKE_PROFIT_FAILED: "TP2 · исполнение не подтверждено",
  DYNAMIC_TAKE_PROFIT_FAILED: "TP3 · исполнение не подтверждено",
  POSITION_CLOSE_FAILED: "Закрытие не подтверждено",
  SCALE_OUT_CANCELLED_MIN_NOTIONAL: "Частичная фиксация отменена: малый объём",
};
const reason: Record<string, string> = { TAKE_PROFIT: "тейк-профит", STOP_LOSS: "стоп-лосс", BREAKEVEN_STOP: "безубыточный стоп", EMERGENCY_DRAWDOWN: "защита от просадки" };
const number = (v: unknown) => typeof v === "number" ? v.toLocaleString("ru-RU", {maximumFractionDigits: 8}) : "—";
const time = (v: unknown) => typeof v === "string" ? new Date(v).toLocaleString("ru-RU", {timeZone: "Europe/Simferopol"}) : "—";

export default function ProfitEventsPanel() {
  const [input, setInput] = React.useState("");
  const [position, setPosition] = React.useState("");
  const [cursor, setCursor] = React.useState<number | null>(null);
  const [rows, setRows] = React.useState<LogEntry[]>([]);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [refresh, setRefresh] = React.useState(0);
  React.useEffect(() => {
    const controller = new AbortController();
    setRows([]);
    const load = async () => {
      setLoading(true);
      try {
        const response = await api.get<LogEntry[]>("/logs", {signal: controller.signal,
          params: {category: "exits", limit: 50, position_id: position || undefined, before_id: cursor || undefined}});
        if (!controller.signal.aborted) { setRows(response.data); setError(""); }
      } catch (err) {
        if (!controller.signal.aborted) setError("Не удалось загрузить фиксации. Повторите обновление.");
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    const timer = cursor === null ? window.setInterval(load, 30000) : undefined;
    return () => { controller.abort(); if (timer) window.clearInterval(timer); };
  }, [position, cursor, refresh]);
  return <section className="space-y-3">
    <h2 className="text-xl font-bold">Фиксации прибыли и безубыток</h2>
    <p className="muted">Фактические исполнения и изменения защиты, новые сверху · время Симферополя. Перенос стопа в безубыток не означает закрытие или получение прибыли. Касание уровня не считается исполнением.</p>
    <form className="flex flex-wrap items-end gap-3" onSubmit={e => {e.preventDefault(); setPosition(input); setCursor(null); setRefresh(v => v + 1);}}>
      <label className="field">Номер позиции<input type="number" min="1" step="1" placeholder="Все позиции" value={input} onChange={e => setInput(e.target.value)} /></label>
      <button className="btn" type="submit">Показать историю</button>
      <button className="btn" type="button" onClick={() => {setInput(""); setPosition(""); setCursor(null); setRefresh(v => v + 1);}}>Все позиции</button>
      <button className="btn" type="button" disabled={loading} onClick={() => setRefresh(v => v + 1)}>Обновить</button>
    </form>
    {error && <p role="alert" className="text-danger">{error}</p>}
    <div className="table-wrap"><table>
      <thead><tr><th>Время подтверждения / записи</th><th>Позиция / режим</th><th>Этап и подтверждение</th><th>Цена / стоп</th><th>Исполнено / остаток</th><th>PnL этапа, USDT</th><th>Итог позиции, USDT</th><th>Ордер</th></tr></thead>
      <tbody>{rows.map(row => {
        const c = row.context ?? {}, event = String(c.event ?? "");
        const isFill = c.fill_confirmed === true;
        const failed = event.includes("FAILED") || event.includes("UNCONFIRMED") || event.includes("CANCELLED");
        return <tr key={row.id}>
          <td>{time(c.executed_at ?? row.created_at)}<div className="muted">Запись #{row.id}</div></td>
          <td><button type="button" className="underline" onClick={() => {const id = String(c.position_id ?? ""); setInput(id); setPosition(id); setCursor(null);}}>#{String(c.position_id ?? "—")}</button><div>{String(c.symbol ?? "—")} · {String(c.side ?? "")}</div><small>{String(c.mode ?? "Режим не записан")}</small></td>
          <td><strong>{labels[event] ?? event}</strong><div className={failed ? "text-danger" : "muted"}>{isFill ? (c.mode === "PAPER" ? "Исполнено в Paper" : "Исполнение подтверждено") : event === "BREAKEVEN_APPLIED" ? "Локальная защита обновлена; подтверждение биржи — отдельной записью" : "См. событие и исходные данные"}</div>{c.exit_reason != null && <small>{reason[String(c.exit_reason)] ?? String(c.exit_reason)}</small>}
            <details><summary>Исходная запись</summary><p>{row.message}</p><pre>{JSON.stringify(c, null, 2)}</pre></details></td>
          <td>{number(c.exit_price ?? c.stop_price ?? c.stop)}</td>
          <td>{number(c.filled_volume)} / {number(c.remaining_volume)}</td>
          <td>{number(c.partial_profit)}</td>
          <td>{event === "POSITION_CLOSED" ? number(c.pnl) : "—"}</td>
          <td>{c.order_id != null ? `#${c.order_id}` : "Не записан"}<div>{String(c.order_status ?? "")}</div></td>
        </tr>;
      })}{!rows.length && <tr><td colSpan={8}>{loading ? "Загрузка…" : error ? "Данные недоступны" : "Событий фиксации по этому фильтру пока нет. Пропущенные исторические поля не восстанавливаются догадками."}</td></tr>}</tbody>
    </table></div>
    <p className="muted">PnL этапа включает комиссию выхода; комиссия входа учитывается отдельно в общем результате позиции. Итог показан только после полного закрытия. Для старых записей отсутствующие значения обозначены «—».</p>
    <div className="flex gap-3"><button className="btn" disabled={cursor === null || loading} onClick={() => setCursor(null)}>К новым событиям</button><button className="btn" disabled={rows.length < 50 || loading} onClick={() => setCursor(rows[rows.length - 1].id)}>Более ранние</button></div>
  </section>;
}
