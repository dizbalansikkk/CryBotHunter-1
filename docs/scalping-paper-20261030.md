# Order book / tape paper scalping — 2026-10-30

## Runtime

`SCALPING_ENABLED=true` selects a dedicated Binance USDT futures paper entry path.
It supersedes the ATR entry policy and excludes candle scanning, candle-based
committee votes, hourly RL votes, paper exploration, and candle post-mortems from
new scalp trades. Existing ordinary positions retain their original management.
Turning scalping off does not remove management of already-open scalp positions.
Real exchange order submission is not enabled by this change.

Public depth20 snapshots and aggTrade streams are shared through Redis. OBI uses
the top 10 levels, weighted by quote notional; tape pressure uses aggressive
buy/sell notional in the last 10 seconds. At least five prints in each of the
current and preceding 10-second windows are required. Bid/ask and tape must be
at most two seconds old. No REST candle/ticker fallback is permitted for scalp
decisions. Venue metadata preparation remains subject to the existing request
budget. Partial-exit metadata assessment also uses that budget/cache.

Visible walls are levels with at least three times the mean visible level size.
These are **not confirmed iceberg orders**. Spoofed/cancelled liquidity remains
a limitation; the strategy does not infer hidden liquidity.

## Entry and risk

* Require matching OBI and tape direction, magnitude at least 0.20 each.
* Require at least 10,000 USDT visible depth on each side and 5,000 USDT recent
  tape volume. Spread must not exceed one configured taker fee in price terms.
* Recent price standard deviation must be at least 2 bps and exceed the preceding
  window by 5%. These are experimental filters, not proven profitable settings.
* Place simulated post-only limits at best bid (LONG) or best ask (SHORT).
  Store queue state and consumed trade IDs in SQL. Touching the limit is
  insufficient: opposite aggressive volume at/through the limit must consume
  the initially visible queue **plus the complete requested size**. This is a
  conservative full-fill approximation, not a model of actual queue priority,
  cancellations, hidden orders, or partial entry fills.
* Cancel after 10 seconds, stale data, changed signal, pause, or risk veto.
  Reconciliation never sends these virtual limits to the exchange.
* Stop behind a visible wall, with a 1-bp buffer, only if distance is 15–30 bps;
  otherwise use a 20-bp stop. Stop is not widened. TP is 30 bps, shortened to the
  nearest opposing wall, and rejected if it cannot pay costs. No ATR fallback.
* Size from 0.25% of current paper equity divided by loss per unit including
  entry/exit costs. Venue normalization must not increase size; missing metadata
  blocks entry. Gross exposure cannot exceed configured permitted leverage.
  The saved user maximum-position limit applies.
* Require positive expected net TP after conservative taker round-trip costs
  and exit slippage; gross movement must exceed twice the taker fee. Simulated
  maker fee defaults to 0.02%, not a claim about an account's actual fee tier.
* Recheck equity, daily loss veto, gross exposure, position count and freshness
  before filling a waiting limit. Daily -2.5% latch persists across restarts.
  Unknown equity or missing quotes blocks entry without delaying exit management.

The older fixed-dollar experiment is not the active entry policy in scalping
mode. Health reports the actual selected policy and numerical parameters.

## Exits, accounting, observations

Local paper stop and TP are registered atomically with the position. Exits use
the production paper taker executor: adverse configured slippage and exit fee.
Close on stop, TP, reverse aggressive flow, or 75 seconds without ever reaching
positive liquidation PnL. All positions have an additional 180-second maximum
hold. Near an opposing wall, take half once if both portions satisfy minimum
sizes; otherwise retain the full-position exit. Existing exit-failure interlock
prevents blind retries and blocks new entries.

Reasons `SCALP_TIME_STOP` and `SCALP_TAPE_REVERSAL` are distinct from SL/TP in the
ledger and interface. Entry fee is paid once; partial fills and final exit use
the shared fill ledger. Scalp PnL is stored at eight decimal places. Initial
SL/TP, risk, fees, fill assumption and entry microstructure are retained.
MFE/MAE and giveback continue to be recorded. A bounded path of 150 observations
contains contemporaneous bid/ask, flow, OBI and net liquidation PnL.

`scalp_reward` is a versioned closed-trade label: net PnL divided by observed
drawdown with a risk-based floor; positive reward decreases with holding time;
overstaying and entry against strong OBI incur penalties. Fees already contained
in net PnL are not subtracted twice. The pure reward function is reusable for
offline training. **No new RL model has been trained or promoted on these data.**
Hourly models cannot vote on these trades; admitting a microstructure model still
requires a separately collected time-ordered dataset and forward validation.

## Timing and validation limits

Depth updates are 500 ms, the worker cycle defaults to two seconds plus processing
time. This is seconds-scale paper scalping, **not exchange-colocated HFT**. Stops
and timeouts execute on the next valid observed quote; missing quotes produce a
single persistent alarm and no invented fill. Gaps, latency and outages can
exceed planned risk/time. No profitability or loss guarantee is implied.

Stream routing follows the official Binance USD-M documentation:
https://developers.binance.info/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Mini-Tickers-Stream

Production-path tests in `backend/tests/test_scalping.py` replay book/tape data
through a real SQLite ledger and the real paper exit executor. They cover both
directions, SL/TP/time/flow exits, queue waiting/expiry, partial plus final PnL,
fee accounting, stale data, market regime filters, daily veto at submit and
fill, pause, failed-exit blocking, alternative entry-path blocking, and absence
of candle/live order calls. These validate execution invariants, not market edge.

Deployment checks: `/health/deep` exposes selected risk policy and public book /
tape heartbeat ages. Stream liveness alone does not establish symbol-specific
entry eligibility or a profitable forward sample.
