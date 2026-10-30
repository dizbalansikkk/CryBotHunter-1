# Scalp learning and reward engineering

The scalper's live decision path remains deterministic public order-book/tape
logic. This learning system is offline and paper-only. It cannot construct an
`Order`, call the exchange, or become active automatically. The legacy hourly
PPO worker is skipped while `SCALPING_ENABLED=true`, so a candle policy cannot
vote on a stream scalp.

## Reward

Closed-trade reward uses one accounting convention:

`gross directional PnL - maker/taker fees - slippage = net PnL`.

For a profitable trade, gross PnL must cover costs by more than 2.5 times.
Reward is then `net PnL / initial risk - holding-time penalty`. A losing or
stop trade receives `-beta * (drawdown / initial risk + 1.5)`. A time/flat exit
receives `-gamma`. Entry against the recorded OBI receives an additional
penalty. There is no reward for predicting direction, confidence, number of
orders, or unrealized price movement. PnL is never treated as both gross and
net: execution fees and slippage are stored as separate reward components.

## Replay model

Each new paper scalp preserves the initial visible queue, post-only limit, side,
and bounded public aggressive tape after entry. A replayed maker order fills
only after opposite aggressive volume at or through its price consumes visible
queue plus its whole quantity. If it does not, the replay uses a worse taker
price and taker fee plus configured slippage. It cannot award a maker fill
because price merely touched the limit.

Old positions without a complete queue/tape record are not training samples.
This deliberately postpones training instead of inventing historical order-book
execution. Recorded stream data can still be incomplete during a disconnect;
such samples are excluded.

## Curriculum and validation

Training uses expanding walk-forward folds: an initial chronological training
window followed by two untouched out-of-sample windows, never shuffled. It proceeds:

1. high OBI and high aggressive-flow impulse;
2. false-breakout cases;
3. noisy, risk-managed paper market.

Phase 1 must show Sharpe strictly above 1.5 and positive net expectancy before
later phases are accepted. Each later phase inherits that veto. A candidate also
needs positive phase-3 out-of-sample net expectancy and the configured minimum
out-of-sample sample count. Candidates are persisted as `SHADOW` only if all
conditions pass; otherwise they are `REJECTED`. They always have `is_active=false`.
Replacing an older candidate marks only the old candidate rejected; it cannot
modify any active model. This is the checkpoint/rollback boundary.

The inputs are public stream observations and executed paper ledger records.
Agents do not exchange opaque hidden state. The health endpoint exposes the
policy, its Sharpe threshold, full-replay requirement and the fact that automatic
execution authority is false.

For a confirmed flat agent state, a bounded `+0.01` reward is available only if
the book is toxic: spread exceeds the fee threshold or visible depth is below
the configured minimum. It is zero in a normal market and unavailable for an
open position, so repeated inactivity cannot accumulate a trading-like reward.

These controls validate accounting and sample separation. They do not guarantee
that a candidate will be profitable after fees, slippage, latency, gaps or
changing liquidity. A live rollout requires a separate reviewed decision.
