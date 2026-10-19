# Venue inventory and protective-stop safety

Before new LIVE derivatives entries, compare open local positions with a fresh
venue position snapshot. Compare LONG and SHORT separately, normalise settled
symbols, convert contracts through contractSize, and use a small quantity
rounding tolerance. Missing, additional, invalid or unavailable inventory blocks
new entries and sets the existing persistent panic state. Emit a single incident
on transition. Paper execution does not query live inventory. This is admission
control, not automatic modification of exchange positions or historical fills.
The local ledger uses base-asset quantities; venue integrations must preserve
that convention. Existing protective stops remain on the exchange.

Protective stop intent and client order ID are committed before submission.
An uncertain response remains NEW with reconciliation_required, not FAILED.
Do not assume that a failed lookup proves rejection. Known venue rejections
remain failures. Late recovery still requires inventory review; conditional-order
lookup support varies by exchange and is not automatically guessed here.

Cancellation requires a cancelled status. If a fill raced with cancellation,
retain its actual quantity and price and pass the order to the existing exit
accounting path instead of issuing another market exit. Ambiguous cancellation
is not acknowledged as success.

Validation: 422 tests passed, including contract conversion, independent hedge
sides, orphan/missing/invalid positions, stop submission timeout, cancellation
races, and reuse of confirmed fills. No real orders were placed. The previous
credential-saving deployment was confirmed successful separately.

Remaining work: fully automatic inventory repair, comprehensive coverage checks
for every existing protective order, historical funding ingestion/simulation,
exit-rule experiments and expanded scheduled reports. Existing backtesting
already chooses the stop for ambiguous same-bar stop/target touches and models
adverse stop gaps. These safety fixes are not evidence of higher strategy returns.
