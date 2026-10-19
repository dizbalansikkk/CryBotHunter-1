# Exit checks and eight-hour reports

Before entry the engine checks TP1, TP2 and the residual quantity using venue
precision and minimums. An invalid leg selects MONOLITHIC without increasing
entry size. The entry context retains `exit_preflight`; a successful cascade is
checked again against the actual fill. Runtime minimum checks remain necessary
because prices and venue rules can change.

Unconfirmed final or partial exits persist `entry_context.exit_failure` and a
single POSITION_CLOSE_FAILED event. Entries are blocked globally; further exit
submissions for that symbol are blocked, including the manual API. Price/PnL
monitoring continues. Native stop orders already on the venue are not removed
by this hold. Every five minutes, LIVE mode reads the order (exchange ID or
client ID) and compares local inventory to venue positions. PAPER mode makes
no venue reconciliation requests.

Recovery is deliberately read-only: a recovered fill is not silently booked and
the hold is not automatically cleared. An operator must reconcile the actual
fills, fees, position quantity and protective orders before clearing the stored
review flag and resuming the panic pause. Merely pressing Resume cannot bypass
the persistent review flag. This prevents duplicating an exit whose outcome is
unknown. `exit_failure` is included in the position's exported entry context.

With TELEGRAM_DAILY_REPORT_ENABLED=true and TELEGRAM_TRADING_SLICES_ENABLED=true,
the scheduled daily notification is replaced by completed eight-hour windows.
Delivery is at 00:05, 08:05 and 16:05 in TELEGRAM_DAILY_REPORT_TIMEZONE (default
Europe/Simferopol). On startup the latest completed window is queued; missing
older windows are not automatically replayed. The outbox deduplication key is
the window end, so a restart does not create a second queued copy. The legacy
daily notification remains available by disabling TELEGRAM_TRADING_SLICES_ENABLED.

Reports separate exploration, strategy and unlabelled records. They contain
closed-position net PnL, average wins/losses, realized drawdown from a zero
baseline, planned-risk breaches and top-three entry/exit failure reasons.
Counts are events, not unique trades. The report does not infer missing funding
or floating-equity drawdown and does not subtract commissions a second time.
