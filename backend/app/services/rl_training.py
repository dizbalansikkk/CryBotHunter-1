from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.env_checker import check_env

from app.core.config import get_settings
from app.models.entities import AgentDecision, RlModel, ShadowTrade
from app.services.history import HistoricalDataService
from app.services.post_mortem import BadExperienceReplay
from app.services.rl_environment import FEATURE_NAMES, CryptoTradingEnv, build_feature_frame, latest_observation
from app.services.shadow_trading import ShadowTradingService


ACTION_NAMES = ("WAIT", "BUY", "SELL")


class RlTrainingInterrupted(RuntimeError):
    pass


class ShutdownCallback(BaseCallback):
    def __init__(self, stop_requested: Callable[[], bool]) -> None:
        super().__init__(verbose=0)
        self.stop_requested = stop_requested

    def _on_step(self) -> bool:
        return not self.stop_requested()


class RlTrainingService:
    def __init__(self, stop_requested: Callable[[], bool] | None = None) -> None:
        self.settings = get_settings()
        self.history = HistoricalDataService()
        self.bad_replay = BadExperienceReplay()
        self.shadow_trading = ShadowTradingService()
        self.stop_requested = stop_requested or (lambda: False)

    async def close(self) -> None:
        await self.history.exchange.close()

    async def retire_excluded_symbols(self, db: AsyncSession, symbols: list[str]) -> dict[str, int]:
        excluded = list(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if symbol))
        if not excluded:
            return {"models_retired": 0, "shadow_trades_closed": 0}
        now = datetime.now(timezone.utc)
        model_result = await db.execute(
            update(RlModel)
            .where(
                RlModel.symbol.in_(excluded),
                RlModel.status.in_(("ACTIVE", "SHADOW", "CANDIDATE")),
            )
            .values(status="RETIRED", is_active=False)
        )
        shadow_result = await db.execute(
            update(ShadowTrade)
            .where(ShadowTrade.symbol.in_(excluded), ShadowTrade.status == "OPEN")
            .values(status="CLOSED", exit_reason="SYMBOL_EXCLUDED", closed_at=now)
        )
        await db.flush()
        return {
            "models_retired": max(int(getattr(model_result, "rowcount", 0) or 0), 0),
            "shadow_trades_closed": max(int(getattr(shadow_result, "rowcount", 0) or 0), 0),
        }

    async def needs_refresh(self, db: AsyncSession, symbol: str, timeframe: str) -> bool:
        latest = await self.latest_for(db, symbol, timeframe)
        if not latest or not latest.created_at:
            return True
        created_at = latest.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        retry_hours = (
            self.settings.rl_rejected_retry_hours
            if latest.status in {"REJECTED", "SHADOW"}
            else self.settings.rl_refresh_hours
        )
        if latest.status == "SHADOW" and bool((getattr(latest, "metrics", {}) or {}).get("backtest_passed")):
            retry_hours = max(float(getattr(self.settings, "shadow_forward_max_trial_days", 7.0)) * 24, 1.0)
        return datetime.now(timezone.utc) - created_at >= timedelta(hours=max(float(retry_hours), 1.0))

    async def latest_for(self, db: AsyncSession, symbol: str, timeframe: str) -> RlModel | None:
        return (
            await db.execute(
                select(RlModel)
                .where(RlModel.symbol == symbol, RlModel.timeframe == timeframe)
                .order_by(RlModel.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def active_for(self, db: AsyncSession, symbol: str, timeframe: str) -> RlModel | None:
        return (
            await db.execute(
                select(RlModel)
                .where(RlModel.symbol == symbol, RlModel.timeframe == timeframe, RlModel.is_active.is_(True))
                .order_by(RlModel.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def shadow_for(self, db: AsyncSession, symbol: str, timeframe: str) -> RlModel | None:
        return (
            await db.execute(
                select(RlModel)
                .where(
                    RlModel.symbol == symbol,
                    RlModel.timeframe == timeframe,
                    RlModel.status == "SHADOW",
                    RlModel.is_active.is_(False),
                )
                .order_by(RlModel.created_at.desc())
                .limit(1)
            )
        ).scalars().first()

    async def train_symbol(self, db: AsyncSession, symbol: str, timeframe: str = "1h") -> RlModel:
        if not self.settings.uses_live_market_data:
            raise RuntimeError("RL training refuses synthetic market data; use MARKET_DATA_MODE=ccxt")
        limit = max(self.settings.rl_training_limit, self.settings.rl_min_training_candles)
        candles = await self.history.load(db, symbol, timeframe, limit=limit, source="ccxt")
        if len(candles) < self.settings.rl_min_training_candles:
            await self.history.ingest(db, symbol, timeframe, limit=limit)
            candles = await self.history.load(db, symbol, timeframe, limit=limit, source="ccxt")
        if len(candles) < self.settings.rl_min_training_candles:
            raise RuntimeError(
                f"not enough real candles for RL: {len(candles)}/{self.settings.rl_min_training_candles}"
            )

        frame = build_feature_frame(candles)
        replay_metrics = {
            "bad_experiences_seen": 0,
            "replay_weighted_candles": 0,
            "max_replay_weight": 1.0,
        }
        if isinstance(frame, pd.DataFrame):
            frame, replay_metrics = await self.bad_replay.apply(db, symbol, frame)
        train_frame, validation_frame = self._split(frame)
        # Stable Baselines3/PyTorch are CPU-bound and synchronous. Keeping this
        # work off the asyncio event loop lets the heartbeat, shutdown handling,
        # and other database work continue while a model is learning.
        best_model, metrics = await asyncio.to_thread(
            self._train_candidates,
            train_frame,
            validation_frame,
        )
        metrics["market_data_source"] = "ccxt"
        metrics.update(replay_metrics)
        metrics["passed"] = self._passes_promotion(metrics)
        metrics["backtest_passed"] = bool(metrics["passed"])
        metrics["forward_status"] = "PENDING" if metrics["backtest_passed"] else "NOT_ELIGIBLE"
        metrics["promotion_reason"] = self._promotion_reason(metrics)
        artifact = self._serialize(best_model)

        await db.execute(
            update(RlModel)
            .where(RlModel.symbol == symbol, RlModel.timeframe == timeframe, RlModel.status == "SHADOW")
            .values(is_active=False, status="REJECTED")
        )
        record = RlModel(
            symbol=symbol,
            timeframe=timeframe,
            algorithm="PPO",
            status="SHADOW",
            is_active=False,
            training_candles=len(train_frame),
            validation_candles=len(validation_frame),
            metrics=metrics,
            feature_schema={"features": FEATURE_NAMES, "actions": list(ACTION_NAMES), "version": 2},
            artifact=artifact,
        )
        db.add(record)
        await db.flush()
        decision = self._store_decision(
            db,
            record,
            best_model,
            frame,
            agent_name="rl_shadow",
        )
        await db.flush()
        if decision is not None and isinstance(frame, pd.DataFrame):
            await self.shadow_trading.process(db, record, decision, float(frame.iloc[-1]["close"]))
        await db.commit()
        await db.refresh(record)
        return record

    def _train_candidates(self, train_frame, validation_frame) -> tuple[PPO, dict]:
        started = perf_counter()
        check_env(self._environment(train_frame), warn=True)
        candidates: list[tuple[float, PPO, dict]] = []
        candidate_summaries: list[dict] = []
        for seed in self.settings.rl_training_seeds:
            self._raise_if_stopping()
            curriculum = self._curriculum_frames(train_frame)
            model = PPO(
                "MlpPolicy",
                self._environment(curriculum[0][1]),
                learning_rate=3e-4,
                n_steps=512,
                batch_size=64,
                gamma=0.995,
                gae_lambda=0.95,
                ent_coef=0.005,
                policy_kwargs={"net_arch": [64, 64]},
                seed=seed,
                device="cpu",
                verbose=0,
            )
            total_timesteps = max(self.settings.rl_training_timesteps, 1_000)
            allocations = self._curriculum_allocations(total_timesteps, len(curriculum))
            for (stage_name, stage_frame), stage_timesteps in zip(curriculum, allocations):
                self._raise_if_stopping()
                model.set_env(self._environment(stage_frame))
                model.learn(
                    total_timesteps=stage_timesteps,
                    progress_bar=False,
                    callback=ShutdownCallback(self.stop_requested),
                    reset_num_timesteps=False,
                )
            self._raise_if_stopping()
            candidate_metrics = self._evaluate(model, validation_frame)
            candidate_metrics["seed"] = seed
            score = (
                float(candidate_metrics["return_percent"])
                - float(candidate_metrics["max_drawdown_percent"]) * 0.5
                + min(float(candidate_metrics["profit_factor"]), 5.0)
            )
            candidate_summaries.append(
                {
                    "seed": int(seed),
                    "score": round(float(score), 4),
                    "return_percent": float(candidate_metrics["return_percent"]),
                    "max_drawdown_percent": float(candidate_metrics["max_drawdown_percent"]),
                    "profit_factor": float(candidate_metrics["profit_factor"]),
                    "trades": int(candidate_metrics["trades"]),
                    "curriculum": [
                        {"stage": name, "candles": len(stage_frame), "timesteps": timesteps}
                        for (name, stage_frame), timesteps in zip(curriculum, allocations)
                    ],
                }
            )
            candidates.append((score, model, candidate_metrics))

        selected_score, best_model, selected_metrics = max(candidates, key=lambda item: item[0])
        metrics = dict(selected_metrics)
        buy_hold_return = self._buy_hold_return(validation_frame)
        metrics.update(
            {
                "buy_hold_return_percent": buy_hold_return,
                "excess_return_percent": round(float(metrics["return_percent"]) - buy_hold_return, 4),
                "selection_score": round(float(selected_score), 4),
                "training_seconds": round(perf_counter() - started, 2),
                "timesteps_per_seed": max(int(self.settings.rl_training_timesteps), 1_000),
                "seeds_evaluated": len(candidate_summaries),
                "profitable_seed_ratio": round(
                    sum(1 for item in candidate_summaries if float(item["return_percent"]) > 0)
                    / max(len(candidate_summaries), 1),
                    4,
                ),
                "candidates": candidate_summaries,
                "curriculum_enabled": bool(getattr(self.settings, "rl_curriculum_enabled", True)),
                "curriculum_stages": candidate_summaries[0].get("curriculum", []) if candidate_summaries else [],
            }
        )
        return best_model, metrics

    def _curriculum_frames(self, frame: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
        if not bool(getattr(self.settings, "rl_curriculum_enabled", True)) or not isinstance(frame, pd.DataFrame):
            return [("full_market", frame)]
        trend_mask = (
            frame["ema_gap"].abs().ge(0.025)
            & frame["rsi"].abs().ge(0.1)
            & frame["atr_percent"].between(0.01, 0.35)
        )
        stable_mask = frame["atr_percent"].le(frame["atr_percent"].quantile(0.8))
        stages: list[tuple[str, pd.DataFrame]] = []
        clean_trend = self._longest_window(frame, trend_mask)
        stable_market = self._longest_window(frame, stable_mask)
        if len(clean_trend) >= 100:
            stages.append(("clean_trend", clean_trend))
        if len(stable_market) >= 100 and len(stable_market) != len(clean_trend):
            stages.append(("range_and_normal_volatility", stable_market))
        stages.append(("full_market_with_high_volatility", frame.reset_index(drop=True)))
        return stages

    def _longest_window(self, frame: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
        best_start = 0
        best_length = 0
        current_start = 0
        current_length = 0
        for index, allowed in enumerate(mask.fillna(False).tolist()):
            if allowed:
                if current_length == 0:
                    current_start = index
                current_length += 1
                if current_length > best_length:
                    best_start, best_length = current_start, current_length
            else:
                current_length = 0
        return frame.iloc[best_start : best_start + best_length].reset_index(drop=True)

    def _curriculum_allocations(self, total_timesteps: int, stage_count: int) -> list[int]:
        if stage_count <= 1:
            return [max(total_timesteps, 1_000)]
        weights = [0.25, 0.25, 0.5] if stage_count == 3 else [0.35, 0.65]
        allocations = [max(int(total_timesteps * weight), 1_000) for weight in weights]
        allocations[-1] += max(total_timesteps - sum(allocations), 0)
        return allocations

    def _raise_if_stopping(self) -> None:
        if self.stop_requested():
            raise RlTrainingInterrupted("RL training interrupted by shutdown request")

    async def evaluate_shadow_promotion(self, db: AsyncSession, symbol: str, timeframe: str) -> str:
        record = await self.shadow_for(db, symbol, timeframe)
        if not record or not bool((record.metrics or {}).get("backtest_passed")):
            return "NONE"
        report = await self.shadow_trading.report(db, record.id)
        metrics = {
            **(record.metrics or {}),
            "forward_status": report.status,
            "forward": self.shadow_trading._report_dict(report),
        }
        record.metrics = metrics
        created_at = record.created_at or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        max_age = timedelta(days=max(float(getattr(self.settings, "shadow_forward_max_trial_days", 7.0)), 0.25))
        if report.status == "PENDING" and datetime.now(timezone.utc) - created_at <= max_age:
            return "PENDING"
        if report.status != "PASSED":
            record.status = "REJECTED"
            record.is_active = False
            record.metrics = {**metrics, "forward_status": "FAILED", "forward_rejection_reason": report.reason}
            await db.flush()
            return "REJECTED"
        await db.execute(
            update(RlModel)
            .where(
                RlModel.symbol == symbol,
                RlModel.timeframe == timeframe,
                RlModel.is_active.is_(True),
                RlModel.id != record.id,
            )
            .values(is_active=False, status="RETIRED")
        )
        record.status = "ACTIVE"
        record.is_active = True
        record.metrics = {
            **metrics,
            "forward_status": "PASSED",
            "promoted_after_forward_test": True,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
        }
        await db.flush()
        return "PROMOTED"

    async def publish_active_decision(self, db: AsyncSession, symbol: str, timeframe: str = "1h") -> AgentDecision | None:
        active, _ = await self.publish_decisions(db, symbol, timeframe)
        return active

    async def publish_decisions(
        self,
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1h",
    ) -> tuple[AgentDecision | None, AgentDecision | None]:
        active_record = await self.active_for(db, symbol, timeframe)
        shadow_record = await self.shadow_for(db, symbol, timeframe)
        records = [record for record in (active_record, shadow_record) if record and record.artifact]
        if not records:
            return None, None
        await self.history.ingest(db, symbol, timeframe, limit=300)
        candles = await self.history.load(db, symbol, timeframe, limit=300, source="ccxt")
        frame = build_feature_frame(candles)
        if len(frame) < 100:
            return None, None
        decisions: dict[str, AgentDecision] = {}
        for record in records:
            agent_name = "rl_policy" if record.is_active else "rl_shadow"
            model = self._deserialize(record.artifact)
            decisions[agent_name] = self._store_decision(db, record, model, frame, agent_name=agent_name)
            if agent_name == "rl_shadow":
                await db.flush()
                await self.shadow_trading.process(
                    db,
                    record,
                    decisions[agent_name],
                    float(frame.iloc[-1]["close"]),
                )
        await db.commit()
        return decisions.get("rl_policy"), decisions.get("rl_shadow")

    def _split(self, frame):
        percent = min(max(self.settings.rl_validation_percent, 10.0), 40.0)
        validation_size = max(300, int(len(frame) * percent / 100))
        train_size = len(frame) - validation_size
        if train_size < 1_000 or validation_size < 300:
            raise RuntimeError(f"RL split is too small: train={train_size}, validation={validation_size}")
        return frame.iloc[:train_size].reset_index(drop=True), frame.iloc[train_size:].reset_index(drop=True)

    def _environment(self, frame) -> CryptoTradingEnv:
        return CryptoTradingEnv(
            frame,
            fee_rate=self.settings.paper_fee_rate,
            slippage_bps=self.settings.paper_slippage_bps,
            latency_ms=int(getattr(self.settings, "execution_latency_ms", 250)),
            market_impact_bps=float(getattr(self.settings, "execution_market_impact_bps", 1.5)),
            behavior_penalty=float(getattr(self.settings, "rl_behavior_penalty", 0.35)),
            strategy_adherence_bonus=float(getattr(self.settings, "rl_strategy_adherence_bonus", 0.03)),
        )

    def _evaluate(self, model: PPO, frame) -> dict:
        env = self._environment(frame)
        observation, _ = env.reset()
        terminated = False
        truncated = False
        info = env.metrics()
        while not (terminated or truncated):
            self._raise_if_stopping()
            action, _ = model.predict(observation, deterministic=True)
            observation, _, terminated, truncated, info = env.step(int(action))
        return dict(info)

    def _passes_promotion(self, metrics: dict) -> bool:
        return (
            float(metrics["return_percent"]) >= self.settings.rl_min_validation_return_percent
            and float(metrics.get("excess_return_percent", 0.0)) >= self.settings.rl_min_excess_return_percent
            and float(metrics.get("profitable_seed_ratio", 0.0)) >= self.settings.rl_min_profitable_seed_ratio
            and float(metrics["profit_factor"]) >= self.settings.rl_min_validation_profit_factor
            and int(metrics["trades"]) >= self.settings.rl_min_validation_trades
            and float(metrics["max_drawdown_percent"]) <= self.settings.rl_max_validation_drawdown_percent
        )

    def _promotion_reason(self, metrics: dict) -> str:
        reasons: list[str] = []
        if float(metrics["return_percent"]) < self.settings.rl_min_validation_return_percent:
            reasons.append("validation return below threshold")
        if float(metrics.get("excess_return_percent", 0.0)) < self.settings.rl_min_excess_return_percent:
            reasons.append("validation return underperformed buy-and-hold")
        if float(metrics.get("profitable_seed_ratio", 0.0)) < self.settings.rl_min_profitable_seed_ratio:
            reasons.append("too few profitable training seeds")
        if float(metrics["profit_factor"]) < self.settings.rl_min_validation_profit_factor:
            reasons.append("validation profit factor below threshold")
        if int(metrics["trades"]) < self.settings.rl_min_validation_trades:
            reasons.append("too few validation trades")
        if float(metrics["max_drawdown_percent"]) > self.settings.rl_max_validation_drawdown_percent:
            reasons.append("validation drawdown above threshold")
        return "passed" if not reasons else "; ".join(reasons)

    def _buy_hold_return(self, frame) -> float:
        first = max(float(frame.iloc[0]["close"]), 1e-12)
        last = max(float(frame.iloc[-1]["close"]), 1e-12)
        return round((last / first - 1) * 100, 4)

    def _store_decision(
        self,
        db: AsyncSession,
        record: RlModel,
        model: PPO,
        frame,
        *,
        agent_name: str = "rl_policy",
    ) -> AgentDecision:
        observation = latest_observation(frame)
        action, _ = model.predict(observation, deterministic=True)
        action_index = int(action)
        confidence = self._confidence(model, observation, action_index)
        decision = AgentDecision(
            agent_name=agent_name,
            symbol=record.symbol,
            action=ACTION_NAMES[action_index],
            confidence=confidence,
            rationale=(
                f"Promoted PPO model #{record.id} evaluated current real-market features"
                if agent_name == "rl_policy"
                else f"Shadow PPO model #{record.id} evaluated features without trading authority"
            ),
            context={
                "model_id": record.id,
                "algorithm": record.algorithm,
                "timeframe": record.timeframe,
                "market_data_source": "ccxt",
                "shadow": agent_name == "rl_shadow",
                "trading_authority": agent_name == "rl_policy",
                "validation": record.metrics,
            },
        )
        db.add(decision)
        return decision

    def _confidence(self, model: PPO, observation: np.ndarray, action_index: int) -> float:
        observation_tensor, _ = model.policy.obs_to_tensor(observation)
        distribution = model.policy.get_distribution(observation_tensor)
        probabilities = distribution.distribution.probs.detach().cpu().numpy()[0]
        return round(float(probabilities[action_index]), 4)

    def _serialize(self, model: PPO) -> bytes:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "model.zip"
            model.save(path)
            return path.read_bytes()

    def _deserialize(self, artifact: bytes) -> PPO:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "model.zip"
            path.write_bytes(artifact)
            return PPO.load(path, device="cpu")
