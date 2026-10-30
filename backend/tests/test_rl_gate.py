from types import SimpleNamespace

import pytest

from app.services.rl_gate import RlDecisionGate


class ScalarResult:
    def __init__(self, decision) -> None:
        self.decision = decision

    def scalars(self):
        return self

    def first(self):
        return self.decision


class FakeDb:
    def __init__(self, decision) -> None:
        self.decision = decision

    async def execute(self, _statement):
        return ScalarResult(self.decision)


@pytest.mark.asyncio
async def test_rl_gate_blocks_fresh_confident_opposite_signal(monkeypatch):
    gate = RlDecisionGate()
    monkeypatch.setattr(gate.settings, "rl_gate_enabled", True)
    monkeypatch.setattr(gate.settings, "rl_gate_min_confidence", 0.55)

    result = await gate.assess(FakeDb(SimpleNamespace(action="SELL", confidence=0.8, context={"feature_schema_version": 3})), "BTC/USDT", "BUY")

    assert result.allowed is False
    assert result.risk_multiplier == 0


@pytest.mark.asyncio
async def test_rl_gate_reduces_risk_for_wait(monkeypatch):
    gate = RlDecisionGate()
    monkeypatch.setattr(gate.settings, "rl_gate_enabled", True)
    monkeypatch.setattr(gate.settings, "rl_gate_min_confidence", 0.55)
    monkeypatch.setattr(gate.settings, "rl_wait_risk_multiplier", 0.5)

    result = await gate.assess(FakeDb(SimpleNamespace(action="WAIT", confidence=0.8, context={"feature_schema_version": 3})), "BTC/USDT", "BUY")

    assert result.allowed is True
    assert result.risk_multiplier == 0.5


@pytest.mark.asyncio
async def test_rl_gate_does_not_block_without_promoted_decision(monkeypatch):
    gate = RlDecisionGate()
    monkeypatch.setattr(gate.settings, "rl_gate_enabled", True)

    result = await gate.assess(FakeDb(None), "BTC/USDT", "BUY")

    assert result.allowed is True
    assert result.risk_multiplier == 1.0
