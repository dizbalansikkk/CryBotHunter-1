"""Keep historical regression scenarios in their original policy mode.

Experiment tests explicitly opt into the new policy on isolated settings
copies, testing both modes without depending on the developer's environment.
"""
import pytest
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def historical_policy_baseline(monkeypatch):
    monkeypatch.setattr(get_settings(), "experimental_exit_limits_enabled", False)
