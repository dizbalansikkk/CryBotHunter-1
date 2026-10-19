# Closed, source-specific training candles

RL training now requests only completed ccxt candles. Optimizer history requests
completed candles from the configured source: ccxt for live market data (including
Paper execution using real prices), synthetic only in explicit synthetic mode.
Pretrade quality uses the same source distinction and retains its closed-candle
cutoff. Source/time predicates are applied in SQL before ordering and limiting,
so the forming bar does not consume a training slot. Ingestion still maintains
the forming bar for other consumers. RL inference was not changed by this patch.

No model is automatically replaced or old model provenance retroactively claimed.
Historical freshness/gaps, exchange-specific candle provenance, and funding remain
separate validation concerns. Existing data is not deleted or rewritten. Missing
history follows existing readiness/risk rules, not a fallback to another source.

Validation: 440 full-suite tests passed; regression checks inspect source and
closed-bar SQL predicates and verify optimizer/RL request closed real history.
This removes contamination paths but does not establish profitability.
