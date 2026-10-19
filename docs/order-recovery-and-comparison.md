# Order uncertainty and comparable shadow trials

Live market execution commits an order intent and stable client order ID before
submission. A Binance network timeout triggers one read-only lookup by that ID,
never a second create-order request. Missing lookup results remain unknown; they
are not evidence of rejection. A per-symbol persisted reconciliation flag blocks
further market submissions until the result and local inventory are reconciled.
Known order rejections can clear the flag; missing execution details cannot.
Paper execution does not query this live guard or send venue orders.

Open and cancelled orders with positive finite quantity and price retain their
partial fills. An open remainder keeps the reconciliation flag. A later recovery
worker saves the venue snapshot and emits one inventory-review event. It does not
silently create a local position without the original strategy/stop context or
apply cumulative fills twice. Delayed recovery still requires inventory review;
fully automatic recovery of positions across process crashes is not implemented.
This change covers market orders, not the separate native protective-stop submit
path. Existing exchange-side protective stops are not cancelled by this guard.

Shadow trades freeze fees, slippage, notional, stop/take percentages and entry
confidence threshold at creation. Closing a virtual trade uses its saved fee and
slippage policy. Promotion comparison rejects missing or mixed policies instead
of attributing changed simulation assumptions to model skill. Historical trades
without policy metadata cannot validate a promotion. Comparison still requires
20+ closed trades per model and seven overlapping days, higher mean net return
and no worse normalized drawdown. This is not a statistical significance test,
funding simulation or proof of real profitability. No live trading is enabled.
