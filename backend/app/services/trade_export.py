import csv
import io
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from zipfile import ZIP_DEFLATED, ZipFile

from app.models.entities import LogEntry, Order, Position, Trade, TradePostMortem


class TradeAuditExportService:
    def build_archive(
        self,
        *,
        positions: Iterable[Position],
        trades: Iterable[Trade],
        orders: Iterable[Order],
        post_mortems: Iterable[TradePostMortem],
        events: Iterable[LogEntry],
        generated_at: datetime | None = None,
    ) -> bytes:
        generated_at = generated_at or datetime.now(timezone.utc)
        position_rows = list(positions)
        trade_rows = list(trades)
        order_rows = list(orders)
        post_mortem_rows = list(post_mortems)
        event_rows = list(events)
        post_mortems_by_position = {int(item.position_id): item for item in post_mortem_rows}

        files = {
            "trade-journal.csv": self._csv(self._journal_rows(position_rows, post_mortems_by_position)),
            "trade-fills.csv": self._csv(self._trade_rows(trade_rows)),
            "orders.csv": self._csv(self._order_rows(order_rows)),
            "post-mortems.csv": self._csv(self._post_mortem_rows(post_mortem_rows)),
            "trade-events.csv": self._csv(self._event_rows(event_rows)),
            "summary.json": json.dumps(
                {
                    "generated_at": self._date(generated_at),
                    "positions": len(position_rows),
                    "closed_positions": sum(1 for item in position_rows if item.status == "CLOSED"),
                    "open_positions": sum(1 for item in position_rows if item.status == "OPEN"),
                    "fills": len(trade_rows),
                    "orders": len(order_rows),
                    "post_mortems": len(post_mortem_rows),
                    "trade_events": len(event_rows),
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
            "README.txt": (
                "CryBotHunter — полный аудит сделок\n\n"
                "trade-journal.csv: одна строка на позицию, включая причину входа, оценки, голоса агентов и итог.\n"
                "trade-fills.csv: внутренние записи частичных и финальных исполнений.\n"
                "orders.csv: ордера биржи, комиссии и проскальзывание.\n"
                "post-mortems.csv: разбор ошибок и уроки после закрытия сделки.\n"
                "trade-events.csv: системные события жизненного цикла позиций.\n"
                "summary.json: контрольные количества записей в архиве.\n"
            ).encode("utf-8"),
        }

        output = io.BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for filename, content in files.items():
                archive.writestr(filename, content)
        return output.getvalue()

    def _journal_rows(
        self,
        positions: list[Position],
        post_mortems: dict[int, TradePostMortem],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for position in positions:
            context = position.entry_context or {}
            execution = context.get("entry_execution") if isinstance(context.get("entry_execution"), dict) else {}
            post_mortem = post_mortems.get(int(position.id or 0))
            rows.append(
                {
                    "position_id": position.id,
                    "symbol": position.symbol,
                    "side": position.side,
                    "status": position.status,
                    "entered_at": self._date(position.entered_at),
                    "closed_at": self._date(position.closed_at),
                    "duration_minutes": self._duration(position.entered_at, position.closed_at),
                    "entry_price": position.entry_price,
                    "exit_or_current_price": position.current_price,
                    "volume": position.volume,
                    "stop": position.stop,
                    "take": position.take,
                    "pnl": position.pnl,
                    "exit_reason": position.exit_reason or "",
                    "signal": context.get("signal", ""),
                    "signal_score": context.get("signal_score", ""),
                    "entry_confidence": context.get("entry_confidence", ""),
                    "committee_consensus": context.get("committee_consensus", ""),
                    "committee_confidence": context.get("committee_confidence", ""),
                    "committee_action": context.get("committee_action", ""),
                    "paper_exploration": bool(context.get("paper_exploration", False)),
                    "learning_lane": context.get("learning_lane", ""),
                    "risk_percent": context.get("risk_percent", ""),
                    "planned_risk": context.get("planned_risk", ""),
                    "planned_reward": context.get("planned_reward", ""),
                    "risk_reward_ratio": context.get("risk_reward_ratio", ""),
                    "entry_fee": execution.get("fee", ""),
                    "entry_slippage": execution.get("slippage", ""),
                    "decision_reason": context.get("decision_reason", ""),
                    "entry_reasons": self._json(context.get("reasons", [])),
                    "agent_votes": self._json(context.get("agent_votes", [])),
                    "market_context": self._json(context.get("market_context", {})),
                    "entry_microstructure": self._json(context.get("microstructure", {})),
                    "position_microstructure": self._json(context.get("position_microstructure", [])),
                    "post_mortem_label": post_mortem.primary_label if post_mortem else "",
                    "post_mortem_reward": post_mortem.shaped_reward if post_mortem else "",
                    "post_mortem_lessons": self._json(post_mortem.lessons if post_mortem else []),
                    "entry_context_json": self._json(context),
                }
            )
        return rows

    def _trade_rows(self, trades: list[Trade]) -> list[dict[str, object]]:
        return [
            {
                "trade_id": item.id,
                "position_id": item.position_id or "",
                "symbol": item.symbol,
                "side": item.side,
                "entry_price": item.entry_price,
                "exit_price": item.exit_price if item.exit_price is not None else "",
                "profit": item.profit,
                "created_at": self._date(item.created_at),
            }
            for item in trades
        ]

    def _order_rows(self, orders: list[Order]) -> list[dict[str, object]]:
        return [
            {
                "order_id": item.id,
                "exchange_order_id": item.exchange_order_id or "",
                "symbol": item.symbol,
                "side": item.side,
                "order_type": item.order_type,
                "status": item.status,
                "requested_amount": item.requested_amount,
                "filled_amount": item.filled_amount,
                "requested_price": item.requested_price if item.requested_price is not None else "",
                "average_price": item.average_price if item.average_price is not None else "",
                "fee": item.fee,
                "slippage": item.slippage,
                "created_at": self._date(item.created_at),
                "updated_at": self._date(item.updated_at),
            }
            for item in orders
        ]

    def _post_mortem_rows(self, items: list[TradePostMortem]) -> list[dict[str, object]]:
        return [
            {
                "post_mortem_id": item.id,
                "position_id": item.position_id,
                "symbol": item.symbol,
                "side": item.side,
                "pnl": item.pnl,
                "planned_risk": item.planned_risk,
                "result_r": item.result_r,
                "shaped_reward": item.shaped_reward,
                "priority": item.priority,
                "primary_label": item.primary_label,
                "behavior_labels": self._json(item.behavior_labels),
                "strategy_followed": item.strategy_followed,
                "lessons": self._json(item.lessons),
                "reward_components": self._json(item.reward_components),
                "market_snapshot": self._json(item.market_snapshot),
                "execution_snapshot": self._json(item.execution_snapshot),
                "entered_at": self._date(item.entered_at),
                "closed_at": self._date(item.closed_at),
                "replay_count": item.replay_count,
                "created_at": self._date(item.created_at),
            }
            for item in items
        ]

    def _event_rows(self, events: list[LogEntry]) -> list[dict[str, object]]:
        return [
            {
                "event_id": item.id,
                "created_at": self._date(item.created_at),
                "level": item.level,
                "message": item.message,
            }
            for item in events
        ]

    def _csv(self, rows: list[dict[str, object]]) -> bytes:
        if not rows:
            return b""
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode("utf-8-sig")

    def _json(self, value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

    def _date(self, value: datetime | None) -> str:
        return value.isoformat() if value else ""

    def _duration(self, entered_at: datetime | None, closed_at: datetime | None) -> int | str:
        if not entered_at or not closed_at:
            return ""
        start = entered_at if entered_at.tzinfo else entered_at.replace(tzinfo=timezone.utc)
        end = closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=timezone.utc)
        return max(int((end - start).total_seconds() // 60), 0)
