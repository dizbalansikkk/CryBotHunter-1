from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from app.api.routes import settings as routes
from app.schemas.dto import ExchangeCredentialsIn
from app.core.security import decrypt_secret
from cryptography.fernet import Fernet

@pytest.mark.asyncio
async def test_credentials_saved_independently_of_legacy_risk_settings(monkeypatch):
    # Stable key for this test, never a production credential.
    key = Fernet.generate_key().decode()
    monkeypatch.setattr('app.core.security.get_settings', lambda: SimpleNamespace(encryption_key=key))
    stored = SimpleNamespace(exchange='binance', partial_close_percent=50, risk_percent=1)
    monkeypatch.setattr(routes, '_settings_for', AsyncMock(return_value=stored))
    monkeypatch.setattr(routes, '_serialize', lambda obj: obj)
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    payload = ExchangeCredentialsIn(exchange='binance', api_key=' test-api ', secret_key=' test-secret ')
    result = await routes.save_exchange_credentials(payload, SimpleNamespace(id=1), db)
    assert result.partial_close_percent == 50
    assert decrypt_secret(stored.api_key_encrypted) == 'test-api'
    assert decrypt_secret(stored.secret_key_encrypted) == 'test-secret'
    assert stored.api_key_encrypted != 'test-api'
    assert stored.passphrase_encrypted is None
    db.commit.assert_awaited_once()

@pytest.mark.asyncio
async def test_invalid_encryption_fails_before_mutating_credentials(monkeypatch):
    monkeypatch.setattr('app.core.security.get_settings', lambda: SimpleNamespace(encryption_key='invalid'))
    lookup = AsyncMock()
    monkeypatch.setattr(routes, '_settings_for', lookup)
    payload = ExchangeCredentialsIn(exchange='binance', api_key='test-api', secret_key='test-secret')
    with pytest.raises(HTTPException) as error:
        await routes.save_exchange_credentials(payload, SimpleNamespace(id=1), SimpleNamespace())
    assert error.value.status_code == 503
    assert 'test-secret' not in error.value.detail
    lookup.assert_not_awaited()

@pytest.mark.asyncio
@pytest.mark.parametrize('exchange,api_key,secret', [('binance',' ','secret'),('okx','api','secret'),('kucoin','api','secret')])
async def test_credentials_require_complete_pair_and_passphrase(exchange,api_key,secret):
    payload = ExchangeCredentialsIn(exchange=exchange, api_key=api_key, secret_key=secret)
    with pytest.raises(HTTPException) as error:
        await routes.save_exchange_credentials(payload, SimpleNamespace(id=1), SimpleNamespace())
    assert error.value.status_code == 422
