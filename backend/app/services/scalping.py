"""Public-stream, paper-only scalping. No candle or RL directional fallback."""
import asyncio
import math
import time
from datetime import datetime, timezone
from statistics import pstdev
from sqlalchemy import select
from app.models.entities import Position, Order, Trade, LogEntry
from app.schemas.dto import TradingRunOut, TradingTickOut, TradingDecision
from app.services.market_stream import read_snapshots
from app.services.daily_risk import DailyRiskService
from app.services.exit_experiment import projection
from app.services.profit_excursion import record_excursion
from app.services.scalping_learning import profit_centric_reward


def scalp_features(book, tape, now_ms, settings):
    if any(not 0 <= now_ms - float(item.get("timestamp", 0)) <= 2000 for item in (book, tape)):
        raise ValueError("STALE_STREAM")
    bids, asks = book.get("bids", [])[:10], book.get("asks", [])[:10]
    if not bids or not asks or not all(math.isfinite(v) and v > 0 for levels in (bids, asks) for level in levels for v in level):
        raise ValueError("INVALID_BOOK")
    bid, ask = bids[0][0], asks[0][0]
    if bid >= ask or bids != sorted(bids, reverse=True) or asks != sorted(asks):
        raise ValueError("CROSSED_OR_UNSORTED_BOOK")
    mid = (bid + ask) / 2
    buy_depth, sell_depth = sum(p*q for p,q in bids), sum(p*q for p,q in asks)
    trades = [t for t in tape.get("trades", []) if 0 <= now_ms - t["timestamp"] <= 20000]
    if any(not all(math.isfinite(t[k]) and t[k] > 0 for k in ("price", "amount")) for t in trades):
        raise ValueError("INVALID_TAPE")
    recent = [t for t in trades if now_ms - t["timestamp"] <= 10000]
    prior = [t for t in trades if now_ms - t["timestamp"] > 10000]
    if len(recent) < 5 or len(prior) < 5:
        raise ValueError("TAPE_WARMUP")
    total = sum(t["price"]*t["amount"] for t in recent)
    flow = sum((1 if t["side"] == "buy" else -1)*t["price"]*t["amount"] for t in recent) / max(total, 1e-12)
    obi = (buy_depth - sell_depth)/(buy_depth + sell_depth)
    spread = (ask-bid)/mid*10000
    volatility = pstdev([t["price"] for t in recent])/mid*10000
    old_volatility = pstdev([t["price"] for t in prior])/mid*10000
    active = (min(buy_depth, sell_depth) >= settings.scalping_min_depth_usdt
        and total >= settings.scalping_min_tape_usdt and spread <= settings.paper_fee_rate*10000
        and volatility >= 2 and volatility > old_volatility*1.05)
    direction = 1 if obi >= .2 and flow >= .2 else -1 if obi <= -.2 and flow <= -.2 else 0
    def walls(levels):
        avg = sum(q for _, q in levels)/len(levels)
        return [p for p,q in levels if q >= avg*3]
    return {"bid": bid, "ask": ask, "mid": mid, "obi": obi, "flow": flow,
        "spread_bps": spread, "volatility_bps": volatility, "tape_notional": total,
        "bid_walls": walls(bids), "ask_walls": walls(asks), "active": active,
        "direction": direction if active else 0, "trades": trades,
        "wall_note": "visible density, not proof of iceberg"}


def scalp_levels(entry, direction, features, settings):
    wall_candidates = features["bid_walls" if direction == 1 else "ask_walls"]
    behind = [p for p in wall_candidates if (entry-p)*direction > 0]
    stop = (max(behind) if direction == 1 else min(behind)) if behind else entry*(1-direction*settings.scalping_stop_bps/10000)
    if behind:
        stop *= 1-direction*.0001  # buffer behind visible density
    distance = abs(entry-stop)/entry*10000
    if not 15 <= distance <= 30:
        stop = entry*(1-direction*settings.scalping_stop_bps/10000)
    if not 15 <= abs(entry-stop)/entry*10000 <= 30:
        raise ValueError("INVALID_MICRO_STOP")
    take = entry*(1+direction*.003)
    opposing = [p for p in features["ask_walls" if direction == 1 else "bid_walls"] if (p-entry)*direction > 0]
    if opposing:
        obstacle = min(opposing) if direction == 1 else max(opposing)
        take = min(take, obstacle) if direction == 1 else max(take, obstacle)
    return stop, take


def scalp_reward(net_pnl, drawdown, initial_risk, holding_seconds, entry_obi, direction,
                 fees=0.0, slippage=0.0, reason=""):
    """Compatibility wrapper around the strict closed-trade net-profit reward."""
    # PnL is net; reconstruct a pre-cost figure once so costs are charged once.
    gross = net_pnl + max(fees, 0) + max(slippage, 0)
    reward = profit_centric_reward(gross_pnl=gross, fees=fees, slippage=slippage,
        initial_risk=initial_risk, drawdown=drawdown, holding_seconds=holding_seconds,
        stop_or_loss=(reason == "STOP_LOSS" or net_pnl <= 0), timeout_or_flat=(reason == "SCALP_TIME_STOP"))
    # Explicitly penalize entering against the recorded book; it never turns a
    # losing reward positive and has no access to hidden model state.
    return reward - max(-direction*entry_obi-.2, 0) - max(holding_seconds - 75, 0) / 75 * .1


class StreamQuotes:
    def __init__(self, books):
        self.books = books

    async def fetch_tickers(self, symbols):
        return {s: {"bid": self.books[s]["bids"][0][0], "ask": self.books[s]["asks"][0][0]}
            for s in symbols if s in self.books and 0 <= time.time()*1000-self.books[s]["timestamp"] <= 2000}


class ScalpingEngine:
    def __init__(self, engine):
        self.engine, self.settings = engine, engine.settings

    async def snapshots(self, symbols):
        books, tapes = await asyncio.gather(asyncio.to_thread(read_snapshots, "book", symbols),
            asyncio.to_thread(read_snapshots, "tape", symbols))
        return books, tapes

    def event(self, db, name, **details):
        db.add(LogEntry(level="INFO", message=name, context={"event_type": name, **details}))

    async def risk_check(self, db, books):
        try:
            return await DailyRiskService(self.settings, StreamQuotes(books)).check(db)
        except ValueError:
            # Missing equity/quotes is an entry veto, not a reason to delay exits.
            return None

    async def manage(self, db):
        if not self.settings.paper_trading:
            return TradingTickOut(checked=0, closed=0, updated=[])
        positions = list((await db.execute(select(Position).where(Position.status == "OPEN"))).scalars().all())
        all_positions = positions
        positions = [p for p in positions if (p.entry_context or {}).get("scalping")]
        books, tapes = await self.snapshots([p.symbol for p in positions])
        closed = 0
        for p in positions:
            if (p.entry_context.get("exit_failure") or {}).get("review_required"):
                continue
            book = books.get(p.symbol, {})
            fresh = 0 <= time.time()*1000-book.get("timestamp", 0) <= 2000
            if not fresh:
                if not p.entry_context.get("scalp_data_alarm"):
                    p.entry_context = {**p.entry_context, "scalp_data_alarm": True}
                    self.event(db, "SCALP_EXIT_DATA_UNAVAILABLE", position_id=p.id, symbol=p.symbol)
                continue  # Never invent a fill at a stale price.
            state = dict(p.entry_context["scalping"])
            direction = 1 if p.side == "LONG" else -1
            price = book["bids" if direction == 1 else "asks"][0][0]
            p.current_price = price
            p.highest_price, p.lowest_price = max(p.highest_price, price), min(p.lowest_price, price)
            p.pnl = await self.engine._position_total_pnl(db, p, price)
            record_excursion(p, p.pnl)
            net = p.pnl-price*p.volume*(self.settings.paper_fee_rate+self.settings.paper_slippage_bps/10000)
            state["ever_positive"] = state.get("ever_positive", False) or net > 0
            state["peak_net"] = max(state.get("peak_net", 0), net)
            state["drawdown"] = max(state.get("drawdown", 0), state["peak_net"]-net)
            entered = p.entered_at.replace(tzinfo=timezone.utc) if p.entered_at.tzinfo is None else p.entered_at
            held = (datetime.now(timezone.utc)-entered).total_seconds()
            reason = None
            if direction*(price-p.stop) <= 0:
                reason = "STOP_LOSS"
            elif held >= self.settings.scalping_max_hold_seconds or (held >= self.settings.scalping_timeout_seconds and not state["ever_positive"]):
                reason = "SCALP_TIME_STOP"
            elif direction*(price-p.take) >= 0:
                reason = "TAKE_PROFIT"
            try:
                features = scalp_features(book, tapes.get(p.symbol, {}), time.time()*1000, self.settings)
            except ValueError:
                features = None
            path = list(p.entry_context.get("scalp_path", []))
            path.append({"timestamp": book["timestamp"], "bid": book["bids"][0][0], "ask": book["asks"][0][0],
                "net_liquidation_pnl": net, "obi": features["obi"] if features else None,
                "flow": features["flow"] if features else None,
                "volatility_bps": features["volatility_bps"] if features else None})
            replay = dict(p.entry_context.get("scalp_replay") or {})
            replay_tape = list(replay.get("tape") or [])
            last_id = int(replay.get("last_trade_id") or -1)
            new_tape = [item for item in (tapes.get(p.symbol, {}).get("trades") or []) if int(item.get("id", -1)) > last_id]
            replay_tape.extend({"timestamp": item["timestamp"], "price": item["price"], "amount": item["amount"], "side": item["side"]}
                for item in new_tape[-300:])
            if new_tape:
                replay["last_trade_id"] = int(new_tape[-1]["id"])
            replay["tape"] = replay_tape[-2000:]
            p.entry_context = {**p.entry_context, "scalp_path": path[-150:], "scalp_replay": replay}
            if not reason and features and direction*features["flow"] < -.2:
                reason = "SCALP_TAPE_REVERSAL"
            p.entry_context = {**p.entry_context, "scalping": state, "scalp_data_alarm": False}
            if reason:
                await self.engine._close_position(db, p, price, reason)
                if p.status == "CLOSED":
                    entry_execution = p.entry_context.get("entry_execution", {})
                    fills = p.entry_context.get("exit_fills", [])
                    fees = float(entry_execution.get("fee") or 0) + sum(float(item.get("fee") or 0) for item in fills)
                    slippage = float(entry_execution.get("slippage") or 0) * float(entry_execution.get("volume") or 0)
                    slippage += sum(float(item.get("slippage") or 0) * float(item.get("volume") or 0) for item in fills)
                    reward = scalp_reward(p.pnl, state["drawdown"], p.entry_context["planned_risk"], held, state["obi"], direction,
                        fees=fees, slippage=slippage, reason=reason)
                    p.entry_context = {**p.entry_context, "scalp_reward": reward, "scalp_reward_version": 2,
                        "scalp_holding_seconds": held, "scalp_reward_components": {"net_pnl": p.pnl, "fees": fees,
                            "slippage": slippage, "initial_risk": p.entry_context["planned_risk"], "drawdown": state["drawdown"],
                            "reason": reason, "entry_obi": state["obi"]}}
                    self.event(db, "SCALP_SETTLED", position_id=p.id, reason=reason, net_pnl=p.pnl, reward=reward, holding_seconds=held)
                    closed += 1
            elif not state.get("partial") and net > 0 and features:
                walls = features["ask_walls" if direction == 1 else "bid_walls"]
                near = any(0 <= direction*(wall-price)/price <= .0003 for wall in walls)
                if near:
                    amount = round(p.volume*.5, 8)
                    assessment = await self.engine.execution.assess_exit_size(p.symbol, amount, price)
                    if assessment.allowed and amount*price >= 5 and (p.volume-amount)*price >= 5:
                        order = await self.engine.execution.execute_market(db, p.symbol, "sell" if direction == 1 else "buy", amount, price, "PARTIAL_TAKE_PROFIT_SCALP")
                        if order.status == "FILLED":
                            await self.engine._record_partial_exit(db, p, exit_price=order.average_price, exit_fee=order.fee,
                                exit_volume=order.filled_amount, mark_price=price, execution_order=order)
                            state["partial"] = True
                            p.entry_context = {**p.entry_context, "scalping": state}
                            self.event(db, "SCALP_PARTIAL_FILLED", position_id=p.id, volume=order.filled_amount, fee=order.fee)
                        else:
                            await self.engine._hold_failed_exit(db, p, order, "SCALP_PARTIAL")
        # Never save a fresh-looking equity baseline using stale position marks.
        if len(all_positions) == len(positions) and all(
            0 <= time.time()*1000-books.get(p.symbol, {}).get("timestamp", 0) <= 2000 for p in positions
        ):
            await self.engine.portfolio_accounting.capture(db)
        # Pending paper limits must not survive a pause or a disabled strategy.
        paused, _ = await self.engine.control.is_paused()
        if paused or not self.settings.scalping_enabled:
            orders = (await db.execute(select(Order).where(Order.status == "NEW"))).scalars().all()
            for order in orders:
                if (order.raw or {}).get("scalp_pending"):
                    order.status = "CANCELLED"
                    self.event(db, "SCALP_MAKER_CANCELLED", symbol=order.symbol, reason="PAUSED_OR_DISABLED")
        await db.commit()
        return TradingTickOut(checked=len(positions), closed=closed, updated=[])

    async def enter(self, db, max_positions=3):
        if (not self.settings.paper_trading or self.engine.exchange.exchange != "binance"
                or not self.engine.exchange.is_derivatives_market()):
            return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
        paused, _ = await self.engine.control.is_paused()
        if paused:
            return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
        symbols = self.settings.market_scan_symbols
        positions = list((await db.execute(select(Position).where(Position.status == "OPEN"))).scalars().all())
        if any(((p.entry_context or {}).get("exit_failure") or {}).get("review_required") for p in positions):
            return TradingRunOut(scanned=0, opened=0, skipped=0, decisions=[])
        symbols = list(dict.fromkeys(symbols + [p.symbol for p in positions]))
        books, tapes = await self.snapshots(symbols)
        opened = 0
        decisions = []
        for symbol in symbols:
            decision = TradingDecision(symbol=symbol, signal="WAIT", score=0, action="SKIPPED", reason="SCALP_REGIME_FILTER")
            decisions.append(decision)
            pending = (await db.execute(select(Order).where(Order.symbol == symbol, Order.status.in_(["NEW", "PARTIAL"] )))).scalars().all()
            if any(not (o.raw or {}).get("scalp_pending") for o in pending):
                decision.reason = "SCALP_OTHER_ORDER_PENDING"
                continue
            pending = next((o for o in pending if (o.raw or {}).get("scalp_pending")), None)
            try:
                features = scalp_features(books.get(symbol, {}), tapes.get(symbol, {}), time.time()*1000, self.settings)
            except ValueError as exc:
                features = None
                decision.reason = "SCALP_" + str(exc)
            if features:
                decision.signal = "BUY" if features["direction"] == 1 else "SELL" if features["direction"] == -1 else "WAIT"
                decision.score = round((abs(features["obi"])+abs(features["flow"]))*50)
            if pending:
                decision.reason = "SCALP_MAKER_WAITING_FOR_TAPE"
                raw = dict(pending.raw)
                expired = time.time()-raw["created_seconds"] >= self.settings.scalping_maker_wait_seconds
                direction = 1 if pending.side == "buy" else -1
                if expired or not features or features["direction"] != direction:
                    pending.status = "CANCELLED"
                    decision.reason = "SCALP_MAKER_TTL_OR_SIGNAL_INVALID"
                    self.event(db, "SCALP_MAKER_CANCELLED", symbol=symbol, reason="TTL_OR_SIGNAL_INVALID")
                    continue
                new_trades = [t for t in features["trades"] if t["id"] > raw["last_trade_id"]]
                consumed = sum(t["amount"] for t in new_trades if t["side"] == ("sell" if direction == 1 else "buy")
                    and direction*(t["price"]-pending.requested_price) <= 0)
                raw["queue_remaining"] -= consumed
                raw["last_trade_id"] = max([raw["last_trade_id"]]+[t["id"] for t in new_trades])
                pending.raw = raw
                if raw["queue_remaining"] > 0:
                    continue
                day = await self.risk_check(db, books)
                check = projection("LONG" if direction == 1 else "SHORT", pending.requested_price, pending.requested_amount,
                    raw["stop"], raw["take"], self.settings.paper_fee_rate, self.settings.paper_slippage_bps,
                    entry_fee=pending.requested_price*pending.requested_amount*raw["maker_fee"])
                leverage = min(self.settings.futures_leverage, self.settings.futures_max_leverage)
                if (day is None or not day.allowed or check["loss"] > day.balance*self.settings.scalping_risk_percent/100
                        or check["profit"] <= 0 or day.gross_exposure + pending.requested_amount*pending.requested_price > day.balance*leverage):
                    pending.status = "CANCELLED"
                    decision.reason = "SCALP_RISK_VETO_AT_FILL"
                    continue
                if any(p.symbol == symbol for p in positions) or len(positions) >= max_positions:
                    pending.status = "CANCELLED"
                    decision.reason = "SCALP_POSITION_LIMIT"
                    continue
                if time.time()*1000-books[symbol]["timestamp"] > 2000:
                    pending.status = "CANCELLED"
                    continue
                pending.status, pending.average_price, pending.filled_amount = "FILLED", pending.requested_price, pending.requested_amount
                pending.fee = pending.average_price*pending.filled_amount*raw["maker_fee"]
                pending.slippage = 0
                pending.exchange_order_id = f"paper-scalp-{pending.id}"
                pending.raw = {**raw, "paper": True, "fill_assumption": "tape_consumed_visible_queue_plus_full_size"}
                p = Position(symbol=symbol, side="LONG" if direction == 1 else "SHORT", status="OPEN",
                    entry_price=pending.average_price, current_price=pending.average_price, volume=pending.filled_amount,
                    stop=raw["stop"], take=raw["take"], initial_risk=abs(raw["stop"]-pending.average_price),
                    highest_price=pending.average_price, lowest_price=pending.average_price, trailing_stop_percent=0,
                    partial_taken=False, breakeven_applied=False, pnl=-pending.fee,
                    entry_context={"scalping": {"obi": features["obi"], "flow": features["flow"], "maker": True,
                        "volatility_bps": features["volatility_bps"], "tape_notional": features["tape_notional"],
                        "bid_walls": features["bid_walls"], "ask_walls": features["ask_walls"]},
                        "execution_mode": "PAPER", "exit_fill_ledger_version": 1, "exit_fills": [],
                        "planned_risk": check["loss"], "planned_reward": check["profit"],
                        "exit_plan": {"initial_stop": raw["stop"], "initial_take": raw["take"], "initial_risk": abs(raw["stop"]-pending.average_price)},
                        "decision_reason": "SCALP_OBI_TAPE_CONSENSUS", "feature_schema": "SCALP_STREAM_V1",
                        "entry_execution": {"fee": pending.fee, "volume": pending.filled_amount, "slippage": 0},
                        "scalp_replay": {"side": pending.side.upper(), "entry_limit": pending.average_price,
                            "quantity": pending.filled_amount, "initial_visible_queue": raw.get("initial_visible_queue", 0),
                            "last_trade_id": raw.get("last_trade_id", 0), "tape": []},
                        "protective_stop": {"mode": "PAPER_LOCAL_SL_TP", "stop_price": raw["stop"]}})
                db.add(p)
                await db.flush()
                record_excursion(p, -pending.fee, from_entry=True)
                db.add(Trade(position_id=p.id, symbol=symbol, side=p.side, entry_price=p.entry_price, profit=-pending.fee))
                positions.append(p)
                opened += 1
                decision.action, decision.reason = "OPENED", "SCALP_MAKER_FILLED"
                self.event(db, "SCALP_MAKER_FILLED", symbol=symbol, position_id=p.id, stop=p.stop, take=p.take)
                continue
            if not features or not features["direction"] or len(positions) >= max_positions or any(p.symbol == symbol for p in positions):
                continue
            day = await self.risk_check(db, books)
            if day is None or not day.allowed:
                decision.reason = "SCALP_DAILY_RISK_VETO"
                break
            direction = features["direction"]
            entry = features["bid" if direction == 1 else "ask"]
            stop, take = scalp_levels(entry, direction, features, self.settings)
            # Require a profitable taker fallback even though entry defaults to maker.
            unit = projection("LONG" if direction == 1 else "SHORT", entry, 1, stop, take,
                self.settings.paper_fee_rate, self.settings.paper_slippage_bps)
            if unit["profit"] <= 0 or abs(take-entry)/entry <= 2*self.settings.paper_fee_rate:
                decision.reason = "SCALP_COST_FILTER"
                continue
            leverage = min(self.settings.futures_leverage, self.settings.futures_max_leverage)
            size = math.floor(min(day.balance*self.settings.scalping_risk_percent/100/unit["loss"],
                max(day.balance*leverage-day.gross_exposure, 0)/entry)*1e8)/1e8
            if size*entry < self.settings.min_entry_notional_usdt:
                decision.reason = "SCALP_SIZE_BELOW_MINIMUM"
                continue
            prepared = await self.engine.exchange.prepare_order(symbol, size, entry)
            if time.time()*1000-books[symbol]["timestamp"] > 2000:
                continue
            if not prepared.metadata_available or prepared.amount > size or prepared.amount*entry < self.settings.min_entry_notional_usdt:
                decision.reason = "SCALP_VENUE_SIZE_VETO"
                continue
            worst_fee = max(prepared.fee_rate, self.settings.paper_fee_rate)
            check = projection("LONG" if direction == 1 else "SHORT", entry, prepared.amount, stop, take,
                worst_fee, self.settings.paper_slippage_bps)
            if check["loss"] > day.balance*self.settings.scalping_risk_percent/100 or check["profit"] <= 0:
                decision.reason = "SCALP_COST_OR_RISK_VETO"
                continue
            queue = sum(q for p,q in books[symbol]["bids" if direction == 1 else "asks"] if p == entry)
            order = Order(symbol=symbol, side="buy" if direction == 1 else "sell", order_type="limit",
                status="NEW", requested_amount=prepared.amount, requested_price=entry,
                raw={"scalp_pending": True, "paper": True, "post_only": True, "stop": stop, "take": take,
                    "created_seconds": time.time(), "last_trade_id": max(t["id"] for t in features["trades"]),
                    "queue_remaining": queue+prepared.amount, "initial_visible_queue": queue,
                    "maker_fee": max(self.settings.scalping_maker_fee_rate, 0)})
            db.add(order)
            decision.reason = "SCALP_MAKER_SUBMITTED"
            self.event(db, "SCALP_MAKER_SUBMITTED", symbol=symbol, price=entry, amount=prepared.amount)
        await db.commit()
        return TradingRunOut(scanned=len(decisions), opened=opened, skipped=len(decisions)-opened, decisions=decisions)
