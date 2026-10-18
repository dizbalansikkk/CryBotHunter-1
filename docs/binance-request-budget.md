# Binance request coordination

All Binance CCXT clients share a Redis rolling 60-second estimated request-weight
budget. Public calls default to 600 weight/minute; the total including authenticated
calls is 900. The difference reserves capacity for balances and order operations.
These are local safety budgets, not advertised exchange limits. Other applications
on the same outbound IP and Binance endpoint/order-count limits still apply.

`BINANCE_REST_PUBLIC_BUDGET`, `BINANCE_REST_TOTAL_BUDGET` and
`BINANCE_MARKET_STREAM_ENABLED` configure these defaults on every service.
All services must use the same Redis. A Redis failure stops REST admission.
Requests rejected locally do not create a Binance cooldown. Actual 418/429 responses
share a cooldown across workers and honor the ban deadline / Retry-After.

The candle worker holds a renewable Redis collector lease and connects to Binance
USD-M production public ticker and partial-depth streams, consistent with the
existing production public-data client used in Paper mode. Execution settings are
unchanged. Two routed connections cover configured scan/ingestion symbols (up to
100). The collector reconnects with exponential backoff; lease loss stops it and
the worker restarts it on its next cycle. Snapshots expire after five seconds and
consumers also check event timestamps. Ticker consumers require a fresh book for
bid/ask. Missing or stale streams fall back to budgeted REST, never synthetic data.

Public REST responses are cached briefly (ticker 2s, depth 1s, candles 30s,
metadata 30min); concurrent cache misses are coalesced across workers. Signed and
API-key responses are never cached. A concurrent reader retries through its normal
worker cycle. Cold start/backfill uses the regular budget, so it may take longer.

After initial candle ingestion, the candle worker skips already-seen intervals
and refreshes the prior forming candle plus recent bars when the interval advances.
A long outage refetches the configured recent history window. Explicit training
backfills retain their requested history depth.

Verification: unit tests cover cooldown sharing, private-response isolation,
execution reserve, local exhaustion without a false ban, stream freshness and
book validation, and incremental candle updates. Public WebSocket smoke checks
confirmed ticker and partial-depth events on both routed URLs. Production stream
health is stored in Redis under `binance:usdt:public:stream:health:market` and
`binance:usdt:public:stream:health:public` (60-second TTL). An HTTP health check alone
does not confirm that the collector is receiving events.
