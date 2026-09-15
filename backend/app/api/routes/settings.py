import httpx

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import get_settings
from app.core.security import decrypt_secret, encrypt_secret, mask_secret
from app.db.session import get_db
from app.models.entities import User, UserSettings
from app.schemas.dto import ActionMessage, SettingsIn, SettingsOut
from app.services.exchange import ExchangeClient, exchange_error_message
from app.services.telegram_bot import TelegramNotifier

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_model=SettingsOut)
async def get_user_settings(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> SettingsOut:
    settings = await _settings_for(user, db)
    return _serialize(settings)


@router.put("", response_model=SettingsOut)
async def update_user_settings(payload: SettingsIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> SettingsOut:
    settings = await _settings_for(user, db)
    settings.exchange = payload.exchange
    settings.risk_percent = payload.risk_percent
    settings.daily_risk_percent = payload.daily_risk_percent
    settings.max_positions = payload.max_positions
    settings.min_rating = payload.min_rating
    settings.scan_interval = payload.scan_interval
    settings.stop_loss_percent = payload.stop_loss_percent
    settings.take_profit_percent = payload.take_profit_percent
    settings.trailing_stop_percent = payload.trailing_stop_percent
    settings.atr_stop_multiplier = payload.atr_stop_multiplier
    settings.risk_reward_ratio = payload.risk_reward_ratio
    settings.breakeven_trigger_r = payload.breakeven_trigger_r
    settings.breakeven_offset_percent = payload.breakeven_offset_percent
    settings.partial_take_profit_r = payload.partial_take_profit_r
    settings.partial_close_percent = payload.partial_close_percent
    if payload.api_key:
        settings.api_key_encrypted = encrypt_secret(payload.api_key)
    if payload.secret_key:
        settings.secret_key_encrypted = encrypt_secret(payload.secret_key)
    if payload.passphrase:
        settings.passphrase_encrypted = encrypt_secret(payload.passphrase)
    await db.commit()
    await db.refresh(settings)
    return _serialize(settings)


@router.post("/exchange/test", response_model=ActionMessage)
async def test_exchange_connection(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> ActionMessage:
    settings = await _settings_for(user, db)
    runtime = get_settings()
    diagnostics = await _exchange_network_diagnostics()
    exchange = ExchangeClient.from_user_settings(settings)
    try:
        balance = await exchange.fetch_real_balance()
    except Exception as exc:
        return ActionMessage(
            ok=False,
            message=(
                exchange_error_message(
                    exc,
                    exchange=settings.exchange,
                    market_type=runtime.exchange_default_type,
                    sandbox=runtime.exchange_sandbox_enabled,
                )
                + f" {diagnostics}"
            ),
        )
    finally:
        await exchange.close()

    usdt = float(balance.get("USDT") or 0.0)
    mode = "sandbox" if runtime.exchange_sandbox_enabled else "real"
    return ActionMessage(
        ok=True,
        message=f"{settings.exchange} подключена ({runtime.exchange_default_type}, {mode}). Баланс USDT: {usdt:.2f}; активов: {len(balance)}. {diagnostics}",
    )


@router.post("/telegram/test", response_model=ActionMessage)
async def test_telegram(_: User = Depends(current_user)) -> ActionMessage:
    notifier = TelegramNotifier()
    if not notifier.enabled:
        return ActionMessage(ok=False, message="TELEGRAM_BOT_TOKEN is not configured")
    if not notifier.settings.telegram_allowed_chat_ids:
        return ActionMessage(ok=False, message="TELEGRAM_ALLOWED_CHAT_IDS is empty")
    delivered = await notifier.broadcast("CryBotHunter test notification: Telegram is connected.")
    if delivered == 0:
        return ActionMessage(ok=False, message="Telegram message was not delivered")
    return ActionMessage(ok=True, message=f"Telegram test notification sent to {delivered} chat(s)")


async def _settings_for(user: User, db: AsyncSession) -> UserSettings:
    settings = (await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))).scalar_one_or_none()
    if settings:
        return settings
    settings = UserSettings(user_id=user.id)
    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    return settings


async def _exchange_network_diagnostics() -> str:
    async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
        outbound_ip = await _diagnostic_get(client, "https://api.ipify.org?format=json", json_key="ip")
        binance_public = await _diagnostic_get(client, "https://api.binance.com/api/v3/time")
    return f"Outbound IP: {outbound_ip}; Binance public: {binance_public}."


async def _diagnostic_get(client: httpx.AsyncClient, url: str, json_key: str | None = None) -> str:
    try:
        response = await client.get(url)
        body = response.text.replace("\n", " ").strip()
        if response.is_success and json_key:
            try:
                return str(response.json().get(json_key) or body[:160])
            except ValueError:
                return body[:160] or f"HTTP {response.status_code}"
        if response.is_success:
            return f"HTTP {response.status_code}"
        return f"HTTP {response.status_code}: {body[:180]}"
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {str(exc)[:180]}"


def _serialize(settings: UserSettings) -> SettingsOut:
    return SettingsOut(
        exchange=settings.exchange,
        api_key_masked=mask_secret(decrypt_secret(settings.api_key_encrypted)),
        secret_key_masked=mask_secret(decrypt_secret(settings.secret_key_encrypted)),
        passphrase_masked=mask_secret(decrypt_secret(settings.passphrase_encrypted)),
        risk_percent=settings.risk_percent,
        daily_risk_percent=settings.daily_risk_percent,
        max_positions=settings.max_positions,
        min_rating=settings.min_rating,
        scan_interval=settings.scan_interval,
        stop_loss_percent=settings.stop_loss_percent,
        take_profit_percent=settings.take_profit_percent,
        trailing_stop_percent=settings.trailing_stop_percent,
        atr_stop_multiplier=settings.atr_stop_multiplier,
        risk_reward_ratio=settings.risk_reward_ratio,
        breakeven_trigger_r=settings.breakeven_trigger_r,
        breakeven_offset_percent=settings.breakeven_offset_percent,
        partial_take_profit_r=settings.partial_take_profit_r,
        partial_close_percent=settings.partial_close_percent,
    )
