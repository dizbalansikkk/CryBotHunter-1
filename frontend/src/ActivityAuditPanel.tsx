import React from "react";
import { api } from "./api/client";

type Metrics = {
  opened: number; closed: number; realized_pnl: number; orders: number; failed_orders: number;
  log_records: number; errors: number; skipped_entries: number; failed_closes: number; snapshots: number;
  equity_first: number | null; equity_last: number | null; max_drawdown_percent: number | null;
  events: Array<{ event: string; count: number }>;
  failure_reasons: Array<{ reason: string; count: number }>;
  entry_blockers?: Array<{ gate: string; count: number }>;
};
type Snapshot = {
  captured_at: string; source: string; total_equity: number; free_equity: number; reserved_equity: number;
  realized_pnl: number; unrealized_pnl: number; open_stop_risk: number; gross_exposure: number;
  net_exposure: number; drawdown_percent: number;
};
type Activity = {
  date: string; timezone: string; mode: string; generated_at: string; complete: boolean;
  summary: Metrics & { open_at_end: number };
  periods: Array<Metrics & { label: string; status: string }>;
  coverage: { equity_collection_started_at: string | null; message: string; position_mode_note: string };
  equity_snapshots: Snapshot[];
  releases: Array<{ version: string; deployed_at: string; market: string | null; config_hash: string }>;
  saved_periods?: Array<{ start_hour: number; end_hour: number; captured_at: string; source: string; period: Metrics & { label: string } }>;
};

function localDate(offset = 0) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Simferopol", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const value = (key: string) => parts.find(p => p.type === key)!.value;
  const date = new Date(`${value("year")}-${value("month")}-${value("day")}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}
const number = (value: number | null | undefined) => value == null ? "Нет среза" : value.toLocaleString("ru-RU", { maximumFractionDigits: 4 });
const stamp = (value: string) => new Date(value).toLocaleString("ru-RU", { timeZone: "Europe/Simferopol" });

export function ActivityAuditPanel() {
  const [day, setDay] = React.useState(() => localDate(-1));
  const [data, setData] = React.useState<Activity | null>(null);
  const [error, setError] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [page, setPage] = React.useState(0);
  const [refresh, setRefresh] = React.useState(0);
  React.useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setData(null); setPage(0);
    async function load() {
      setLoading(true);
      try {
        const result = await api.get<Activity>("/audit/activity", { params: { day }, signal: controller.signal });
        if (active) { setData(result.data); setError(""); }
      } catch {
        if (active) setError("Не удалось загрузить дневной аудит. Данные не заменены нулями — повторите запрос.");
      } finally { if (active) setLoading(false); }
    }
    void load();
    const timer = window.setInterval(() => void load(), 60000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [day, refresh]);
  const rows = [...(data?.equity_snapshots ?? [])].reverse();
  const pageCount = Math.max(1, Math.ceil(rows.length / 20));
  const safePage = Math.min(page, pageCount - 1);
  const visible = rows.slice(safePage * 20, (safePage + 1) * 20);
  const values = (data?.equity_snapshots ?? []).map(s => s.total_equity);
  const low = Math.min(...values), high = Math.max(...values);
  const line = values.map((value, index) => `${10 + index * 780 / Math.max(values.length - 1, 1)},${110 - (value - low) * 90 / Math.max(high - low, 0.0001)}`).join(" ");
  return <div className="space-y-5">
    <div className="panel-block">
      <div className="table-title">Дневная статистика и срезы</div>
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn" onClick={() => setDay(localDate(-1))}>Вчера</button>
        <button className="btn" onClick={() => setDay(localDate())}>Сегодня</button>
        <label>Дата <input aria-label="Дата дневного аудита" type="date" value={day} max={localDate()} onChange={event => event.target.value && setDay(event.target.value)} /></label>
        <button className="btn" disabled={loading} onClick={() => setRefresh(value => value + 1)}>{loading ? "Загрузка…" : "Обновить"}</button>
      </div>
      <p className="mt-3 text-sm text-muted">Три плановых среза в сутки: 07:00, 14:00 и 00:00 · Europe/Simferopol. Подробный журнал капитала записывается чаще. Экран обновляется каждую минуту.</p>
      {error && <p role="alert" className="text-danger">{error}</p>}
    </div>
    {data && <>
      <div className="status-strip">
        <span>{data.date} · {data.complete ? "Полный день" : "День продолжается"} · {data.mode}</span>
        <span>Обновлено: {stamp(data.generated_at)}</span>
      </div>
      <div className="metric-grid">
        {[["Закрыто сделок", data.summary.closed], ["PnL закрытых, USDT", data.summary.realized_pnl], ["Открыто на конец периода", data.summary.open_at_end], ["Пропущено входов", data.summary.skipped_entries], ["Ошибок закрытия", data.summary.failed_closes], ["Срезов капитала", data.summary.snapshots]].map(([label, value]) => <div className="panel-block" key={String(label)}><div className="text-sm text-muted">{label}</div><strong className="text-xl">{number(Number(value))}</strong></div>)}
      </div>
      <div className="panel-block">
        <p>{data.coverage.message}</p>
        {data.coverage.equity_collection_started_at && <p className="text-sm mt-2">Начало записи капитала: {stamp(data.coverage.equity_collection_started_at)}</p>}
        <p className="text-sm text-muted mt-2">{data.coverage.position_mode_note}</p>
        {!data.summary.closed && <p className="mt-2">Закрытых сделок за день нет. Это не означает, что бот не работал: в журнале {data.summary.log_records} записей, создано {data.summary.orders} заявок на ордера.</p>}
      </div>
      <div className="table-wrap"><div className="table-title">Срезы дня: 00–07, 07–14, 14–24</div>
        <table><thead><tr><th>Период</th><th>Открыто</th><th>Закрыто</th><th>PnL закрытых</th><th>Пропущено входов</th><th>Ошибок ордеров</th><th>Срезов</th><th>Первый equity</th><th>Последний equity</th></tr></thead>
          <tbody>{data.periods.map(row => <tr key={row.label}><td>{row.label}{row.status === "FUTURE" ? " · ещё не наступил" : row.status === "IN_PROGRESS" ? " · идёт" : ""}</td><td>{row.opened}</td><td>{row.closed}</td><td>{number(row.realized_pnl)}</td><td>{row.skipped_entries}</td><td>{row.failed_orders}</td><td>{row.snapshots}</td><td>{number(row.equity_first)}</td><td>{number(row.equity_last)}</td></tr>)}</tbody></table>
      </div>
      {data.summary.failure_reasons.length > 0 && <div className="table-wrap"><div className="table-title">Почему не исполнились ордера</div><table><thead><tr><th>Записанная причина</th><th>Количество</th></tr></thead><tbody>{data.summary.failure_reasons.map(row => <tr key={row.reason}><td>{row.reason}</td><td>{row.count}</td></tr>)}</tbody></table></div>}
      <div className="table-wrap"><div className="table-title">Сохранённые плановые срезы · 3 в сутки</div><table><thead><tr><th>Период</th><th>Сохранён</th><th>Источник</th><th>Закрыто</th><th>PnL закрытых</th><th>Ошибки ордеров</th></tr></thead><tbody>{(data.saved_periods ?? []).map(row => <tr key={row.start_hour}><td>{row.period.label}</td><td>{stamp(row.captured_at)}</td><td>{row.source === "SCHEDULED" ? "По расписанию" : "По сохранённому журналу после запуска"}</td><td>{row.period.closed}</td><td>{number(row.period.realized_pnl)}</td><td>{row.period.failed_orders}</td></tr>)}{!data.saved_periods?.length && <tr><td colSpan={6}>Завершённые периоды будут сохранены рабочим процессом. Ещё не наступившие периоды не создаются заранее.</td></tr>}</tbody></table></div>
      {(data.summary.entry_blockers ?? []).length > 0 && <div className="table-wrap"><div className="table-title">Что препятствовало новым входам</div><table><thead><tr><th>Проверка</th><th>Отказов</th></tr></thead><tbody>{data.summary.entry_blockers!.map(row => <tr key={row.gate}><td>{({ PRICE_EXTENSION: "Цена слишком далеко от EMA20", POSITION_ALREADY_OPEN: "Позиция по паре уже открыта", MAX_POSITIONS: "Заняты все слоты позиций", VOLATILITY: "Неподходящая волатильность", COOLDOWN: "Пауза после убытков / входов", PRETRADE_QUALITY: "Историческая проверка стратегии", MARKET_REGIME: "Режим рынка", RL_GATE: "Несогласие RL со стратегией", VOLUME_CONFIRMATION: "Недостаточное подтверждение объёмом", MICROSTRUCTURE: "Стакан и поток сделок" } as Record<string, string>)[row.gate] ?? row.gate}</td><td>{row.count}</td></tr>)}</tbody></table></div>}
      {values.length > 0 && <div className="panel-block"><div className="table-title">Сохранённый капитал, USDT · {number(low)} — {number(high)}</div><svg viewBox="0 0 800 130" role="img" aria-label="Капитал по сохранённым срезам" className="w-full" style={{ maxHeight: 180 }}><polyline points={line} fill="none" stroke="#55d8bd" strokeWidth="2" /></svg><p className="text-sm text-muted">Линия соединяет сохранённые точки; промежутки между ними не являются измерениями.</p></div>}
      <div className="table-wrap"><div className="table-title">Журнал капитала · {rows.length} срезов</div><table><thead><tr><th>Время</th><th>Источник</th><th>Equity</th><th>Свободно</th><th>Резерв</th><th>Realized</th><th>Unrealized</th><th>Риск SL</th><th>Gross / Net</th><th>DD, %</th></tr></thead><tbody>
        {visible.map(row => <tr key={row.captured_at}><td>{stamp(row.captured_at)}</td><td>{row.source}</td><td>{number(row.total_equity)}</td><td>{number(row.free_equity)}</td><td>{number(row.reserved_equity)}</td><td>{number(row.realized_pnl)}</td><td>{number(row.unrealized_pnl)}</td><td>{number(row.open_stop_risk)}</td><td>{number(row.gross_exposure)} / {number(row.net_exposure)}</td><td>{number(row.drawdown_percent)}</td></tr>)}
        {!rows.length && <tr><td colSpan={10}>За этот день срезы не сохранены. Выберите «Сегодня», чтобы проверить текущую запись.</td></tr>}
      </tbody></table><div className="flex gap-3 p-3"><button className="btn" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>Назад</button><span>{safePage + 1} / {pageCount}</span><button className="btn" disabled={safePage + 1 >= pageCount} onClick={() => setPage(safePage + 1)}>Далее</button></div></div>
      <div className="table-wrap"><div className="table-title">Релизы, известные к концу выбранного дня</div><table><thead><tr><th>Версия</th><th>Время</th><th>Рынок</th></tr></thead><tbody>{data.releases.map(row => <tr key={`${row.version}-${row.config_hash}`}><td>{row.version.slice(0, 12)}</td><td>{stamp(row.deployed_at)}</td><td>{row.market ?? "Не записан"}</td></tr>)}{!data.releases.length && <tr><td colSpan={3}>К этой дате журнал релизов ещё не вёлся.</td></tr>}</tbody></table></div>
    </>}
  </div>;
}
