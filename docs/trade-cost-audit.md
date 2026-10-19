# Entry cost scenarios and execution evidence

Entry OPENED/REJECTED/SKIPPED events include sizing.cost_scenario with configured
entry and target exit taker fees, round-trip spread and adverse slippage, net
final-target proceeds and stop loss including costs. A candidate that reaches
this gate is rejected when estimated final-target proceeds before funding are
non-positive, including Paper exploration. Earlier rejection reasons are retained.

These are scenarios, not predicted profitability. Fees use the configured rate,
not the account VIP tier. Funding is a separately labelled stress scenario for one
adverse payment; next settlement and holding time are unknown. No funding income
is assumed or booked. The target scenario assumes the entire volume exits at the
final target; staged exits can realize less. Existing signal thresholds and exit
levels are unchanged. Live trading remains disabled by existing configuration.

Exit audit fill_confirmed requires FILLED/PARTIAL plus positive finite filled
quantity and average price. It includes fill price, confirmed quantity and fee.
An unconfirmed result has no executed_at. The recorded timestamp is acknowledgement
time, not a claimed exchange fill timestamp. Existing TP1/TP2/final and failed-exit
journal events retain their stage and order identifiers.

Validation: full suite 399 passed, including round-trip arithmetic, SHORT exit
fees, invalid inputs, costs exceeding target, and confirmation status/quantity/
price combinations. These tests verify accounting and evidence; they do not prove
improved trading returns. Further changes to entry/exit strategy require separate
historical and Paper comparisons.
