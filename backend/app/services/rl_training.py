from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Callable

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.env_checker import check_env

from app.core.config import get_settings
from app.models.entities import AgentDecision, RlModel
from app.services.history import HistoricalDataService
from app.services.rl_environment import FEATURE_NAMES, CryptoTradingEnv, build_feature_frame, latest_observation


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
        self.stop_requested = stop_requested or (lambda: False)

    async def close(self) -> None:
        await self.history.exchange.close()

    async def needs_refresh(self, db: AsyncSession, symbol: str, timeframe: str) -> bool:
        latest = await self.latest_for(db, symbol, timeframe)
        if not latest or not latest.created_at:
            return True
        created_at = latest.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        retry_hours = (
            self.settings.rl_rejected_retry_hours
            if latest.status == "REJECTED"
            else self.settings.rl_refresh_hours
        )
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
        metrics["passed"] = self._passes_promotion(metrics)
        metrics["promotion_reason"] = self._promotion_reason(metrics)
        artifact = self._serialize(best_model)

        promoted = bool(metrics["passed"])
        if promoted:
            await db.execute(
                update(RlModel)
                .where(RlModel.symbol == symbol, RlModel.timeframe == timeframe, RlModel.is_active.is_(True))
                .values(is_active=False, status="RETIRED")
            )
        record = RlModel(
            symbol=symbol,
            timeframe=timeframe,
            algorithm="PPO",
            status="ACTIVE" if promoted else "REJECTED",
            is_active=promoted,
            training_candles=len(train_frame),
            validation_candles=len(validation_frame),
            metrics=metrics,
            feature_schema={"features": FEATURE_NAMES, "actions": list(ACTION_NAMES), "version": 1},
            artifact=artifact,
        )
        db.add(record)
        await db.flush()
        if promoted:
            self._store_decision(db, record, best_model, frame)
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
            model = PPO(
                "MlpPolicy",
                self._environment(train_frame),
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
            model.learn(
                total_timesteps=max(self.settings.rl_training_timesteps, 1_000),
                progress_bar=False,
                callback=ShutdownCallback(self.stop_requested),
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
                "candidates": candidate_summaries,
            }
        )
        return best_model, metrics

    def _raise_if_stopping(self) -> None:
        if self.stop_requested():
            raise RlTrainingInterrupted("RL training interrupted by shutdown request")

    async def publish_active_decision(self, db: AsyncSession, symbol: str, timeframe: str = "1h") -> AgentDecision | None:
        model_record = await self.active_for(db, symbol, timeframe)
        if not model_record or not model_record.artifact:
            return None
        await self.history.ingest(db, symbol, timeframe, limit=300)
        candles = await self.history.load(db, symbol, timeframe, limit=300, source="ccxt")
        frame = build_feature_frame(candles)
        if len(frame) < 100:
            return None
        model = self._deserialize(model_record.artifact)
        decision = self._store_decision(db, model_record, model, frame)
        await db.commit()
        return decision

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
            and float(metrics["profit_factor"]) >= self.settings.rl_min_validation_profit_factor
            and int(metrics["trades"]) >= self.settings.rl_min_validation_trades
            and float(metrics["max_drawdown_percent"]) <= self.settings.rl_max_validation_drawdown_percent
        )

    def _promotion_reason(self, metrics: dict) -> str:
        reasons: list[str] = []
        if float(metrics["return_percent"]) < self.settings.rl_min_validation_return_percent:
            reasons.append("validation return below threshold")
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

    def _store_decision(self, db: AsyncSession, record: RlModel, model: PPO, frame) -> AgentDecision:
        observation = latest_observation(frame)
        action, _ = model.predict(observation, deterministic=True)
        action_index = int(action)
        confidence = self._confidence(model, observation, action_index)
        decision = AgentDecision(
            agent_name="rl_policy",
            symbol=record.symbol,
            action=ACTION_NAMES[action_index],
            confidence=confidence,
            rationale=f"Promoted PPO model #{record.id} evaluated current real-market features",
            context={
                "model_id": record.id,
                "algorithm": record.algorithm,
                "timeframe": record.timeframe,
                "market_data_source": "ccxt",
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
