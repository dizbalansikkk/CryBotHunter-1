from types import SimpleNamespace
import pytest
from app.services.profit_excursion import record_excursion, excursion_summary


def test_realized_plus_unrealized_observations_preserve_original_risk():
    position = SimpleNamespace(entry_context={"planned_risk": 2})
    record_excursion(position, -.1, from_entry=True)
    record_excursion(position, 3)
    # Partial close changes volume and risk settings, not the original R basis.
    position.entry_context["planned_risk"] = .5
    record_excursion(position, 2.9)
    record_excursion(position, -1, final=True)
    state = position.entry_context["profit_excursion"]
    assert state["max_profit_usdt"] == 3 and state["max_loss_usdt"] == 1
    assert state["giveback_usdt"] == 4
    assert state["final_pnl_r"] == -.5 and state["max_profit_r"] == 1.5
    assert state["giveback_r"] == 2 and state["risk_basis_usdt"] == 2
    assert state["coverage"] == "FROM_ENTRY"
    assert "3.0000 USDT / 1.50R" in excursion_summary(position.entry_context)


def test_legacy_position_does_not_invent_prior_extrema_or_missing_risk():
    position = SimpleNamespace(entry_context={})
    record_excursion(position, -.2)
    record_excursion(position, -.1, final=True)
    state = position.entry_context["profit_excursion"]
    assert state["coverage"] == "SINCE_FIRST_OBSERVATION"
    assert state["max_profit_usdt"] == 0 and state["giveback_usdt"] == 0
    assert state["final_pnl_r"] is None
    assert state["max_loss_usdt"] == .2


def test_nonfinite_value_does_not_corrupt_persisted_extrema():
    position = SimpleNamespace(entry_context={})
    record_excursion(position, float("nan"))
    assert position.entry_context == {}


def test_export_exposes_observed_extrema():
    from app.models.entities import Position
    from app.services.trade_export import TradeAuditExportService
    position = Position(id=1, symbol="ETH/USDT", side="LONG", volume=1,
        entry_price=100, current_price=101, stop=98, take=104, status="CLOSED",
        entry_context={"planned_risk": 2})
    record_excursion(position, 2, from_entry=True)
    record_excursion(position, 1, final=True)
    # Use the public archive path so column names and serialization are tested.
    import zipfile, io, csv
    from datetime import datetime, timezone
    blob = TradeAuditExportService().build_archive(positions=[position], trades=[], orders=[],
        post_mortems=[], events=[], generated_at=datetime.now(timezone.utc))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        row = next(csv.DictReader(io.StringIO(archive.read("trade-journal.csv").decode("utf-8-sig"))))
    assert float(row["giveback_usdt"]) == 1
    assert float(row["final_pnl_r"]) == .5
