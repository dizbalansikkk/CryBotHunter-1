from __future__ import annotations

import hashlib
import json
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.entities import StrategyRelease


class StrategyReleaseAudit:
    """Persist a reproducible, secret-free snapshot of trading configuration."""

    PARAMETER_NAMES = (
        "primary_trading_market",
        "exchange_default_type",
        "paper_trading",
        "live_trading_enabled",
        "exchange_sandbox_enabled",
        "futures_margin_mode",
        "futures_leverage",
        "futures_max_leverage",
        "futures_min_liquidation_buffer_percent",
        "futures_require_native_stop",
        "spot_secondary_enabled",
        "max_drawdown_percent",
        "max_position_size_percent",
        "derivatives_max_gross_exposure_percent",
        "spot_max_gross_exposure_percent",
        "strategy_min_volume_ratio",
        "strategy_max_entry_distance_atr",
        "pretrade_quality_min_trades",
        "pretrade_quality_min_profit_factor",
        "pretrade_quality_min_win_rate",
        "strategy_optimizer_min_trades",
        "strategy_optimizer_min_validation_trades",
        "shadow_forward_min_trades",
        "extreme_funding_rate_abs",
        "counterfactual_horizon_minutes",
    )

    async def record(self, db: AsyncSession) -> StrategyRelease:
        settings = get_settings()
        parameters = {name: getattr(settings, name) for name in self.PARAMETER_NAMES}
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
        config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        commit_sha = (
            os.getenv("RAILWAY_GIT_COMMIT_SHA")
            or os.getenv("GIT_COMMIT_SHA")
            or os.getenv("SOURCE_VERSION")
        )
        environment = str(settings.environment)
        existing = (
            await db.execute(
                select(StrategyRelease)
                .where(
                    StrategyRelease.commit_sha == commit_sha,
                    StrategyRelease.config_hash == config_hash,
                    StrategyRelease.environment == environment,
                )
                .order_by(StrategyRelease.deployed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        version = (commit_sha or f"config-{config_hash}")[:64]
        release = StrategyRelease(
            version=version,
            commit_sha=commit_sha,
            environment=environment,
            config_hash=config_hash,
            parameters=parameters,
        )
        db.add(release)
        await db.flush()
        return release
