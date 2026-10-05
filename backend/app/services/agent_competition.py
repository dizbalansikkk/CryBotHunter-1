from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AgentPerformance, LogEntry, Position


@dataclass(frozen=True)
class CompetitionProfile:
    agent_name: str
    role: str
    status: str
    observations: int = 0
    successful_predictions: int = 0
    failed_predictions: int = 0
    rating: float = 0.5
    ema_reward: float = 0.0

    @property
    def vote_weight(self) -> float:
        # A new challenger gets a neutral shadow score. Proven analysts can gain
        # influence, while a poor analyst never gets a zero weight overnight.
        return round(max(0.35, min(1.35, 0.35 + self.rating)), 4)


class AgentCompetitionService:
    """Learns analyst quality from closed trades and safely promotes challengers."""

    ROLE_MEMBERS = {
        "TREND": ("TrendAgent", "AdaptiveTrendAgent"),
        "MOMENTUM": ("MomentumAgent", "BreakoutAgent"),
    }
    DEFAULT_CHAMPIONS = {"TREND": "TrendAgent", "MOMENTUM": "MomentumAgent"}
    MIN_OBSERVATIONS = 20
    PROMOTION_MARGIN = 0.10
    PROMOTION_COOLDOWN_DAYS = 7
    POOR_RATING = 0.42
    EMA_ALPHA = 0.2

    async def profiles(self, db: AsyncSession) -> dict[str, CompetitionProfile]:
        rows = list((await db.execute(select(AgentPerformance))).scalars().all())
        stored = {row.agent_name: self._profile(row) for row in rows}
        result: dict[str, CompetitionProfile] = {}
        for role, members in self.ROLE_MEMBERS.items():
            champion = next((item for item in members if stored.get(item) and stored[item].status == "CHAMPION"), None)
            champion = champion or self.DEFAULT_CHAMPIONS[role]
            for name in members:
                profile = stored.get(name)
                if profile:
                    result[name] = CompetitionProfile(
                        agent_name=profile.agent_name,
                        role=profile.role,
                        status="CHAMPION" if name == champion else profile.status,
                        observations=profile.observations,
                        successful_predictions=profile.successful_predictions,
                        failed_predictions=profile.failed_predictions,
                        rating=profile.rating,
                        ema_reward=profile.ema_reward,
                    )
                else:
                    result[name] = CompetitionProfile(
                        agent_name=name,
                        role=role,
                        status="CHAMPION" if name == champion else "CHALLENGER",
                    )
        return result

    async def record_closed_position(self, db: AsyncSession, position: Position) -> list[dict[str, Any]]:
        context = position.entry_context if isinstance(position.entry_context, dict) else {}
        votes = context.get("agent_votes") if isinstance(context.get("agent_votes"), list) else []
        if not votes:
            return []
        pnl = float(position.pnl or 0.0)
        result_r = pnl / max(float(context.get("planned_risk") or 0.0), 1e-9)
        result_r = max(-2.0, min(2.0, result_r))
        summaries: list[dict[str, Any]] = []
        for vote in votes:
            if not isinstance(vote, dict):
                continue
            name = str(vote.get("agent") or "")
            role = str(vote.get("competition_role") or "")
            if name not in {member for members in self.ROLE_MEMBERS.values() for member in members} or not role:
                continue
            reward, success = self._reward(
                action=str(vote.get("action") or "WAIT"),
                position_side=position.side,
                pnl=pnl,
                result_r=result_r,
                confidence=float(vote.get("confidence") or 0.5),
            )
            row = await self._get_or_create(db, name, role)
            row.observations += 1
            row.successful_predictions += int(success)
            row.failed_predictions += int(not success)
            row.total_reward = round(float(row.total_reward or 0.0) + reward, 6)
            row.ema_reward = round(
                reward if row.observations == 1 else self.EMA_ALPHA * reward + (1 - self.EMA_ALPHA) * float(row.ema_reward or 0.0),
                6,
            )
            win_rate = row.successful_predictions / max(row.observations, 1)
            mean_reward = row.total_reward / max(row.observations, 1)
            row.rating = round(max(0.0, min(1.0, 0.55 * win_rate + 0.25 * ((row.ema_reward + 1) / 2) + 0.20 * ((mean_reward + 1) / 2))), 4)
            summaries.append({"agent": name, "role": role, "reward": reward, "success": success, "rating": row.rating, "observations": row.observations})

        changes = await self._reconcile_roles(db)
        db.add(
            LogEntry(
                level="INFO",
                message=(
                    f"Агенты учли результат сделки {position.symbol} #{position.id}: "
                    f"PnL={pnl:+.4f} USDT, оценено прогнозов={len(summaries)}, перестановок={len(changes)}."
                ),
                context={
                    "event": "AGENT_LEARNING_UPDATED",
                    "symbol": position.symbol,
                    "position_id": position.id,
                    "pnl": round(pnl, 4),
                    "evaluated_agents": len(summaries),
                    "agent_results": summaries,
                    "role_changes": changes,
                    "explanation": "Каждый теневой и основной аналитик получил награду по фактическому исходу сделки; рейтинг обновлён сглаженно.",
                },
            )
        )
        return summaries

    def _reward(
        self,
        *,
        action: str,
        position_side: str,
        pnl: float,
        result_r: float,
        confidence: float = 0.5,
    ) -> tuple[float, bool]:
        opened_action = "BUY" if position_side == "LONG" else "SELL"
        if action in {"BUY", "SELL"}:
            aligned = action == opened_action
            success = (aligned and pnl > 0) or (not aligned and pnl < 0)
        elif action in {"WAIT", "BLOCK"}:
            success = pnl < 0
        else:
            success = pnl > 0
        magnitude = min(abs(result_r), 1.0)
        calibrated_confidence = max(0.0, min(float(confidence), 1.0))
        conviction = 0.5 + 0.5 * calibrated_confidence
        reward = (0.5 + 0.5 * magnitude) * conviction * (1 if success else -1)
        return round(reward, 4), success

    async def _reconcile_roles(self, db: AsyncSession) -> list[dict[str, str]]:
        changes: list[dict[str, str]] = []
        for role, members in self.ROLE_MEMBERS.items():
            rows = [await self._get_or_create(db, name, role) for name in members]
            champion = next((row for row in rows if row.status == "CHAMPION"), None)
            if champion is None:
                champion = next(row for row in rows if row.agent_name == self.DEFAULT_CHAMPIONS[role])
                champion.status = "CHAMPION"
            eligible = [row for row in rows if row.observations >= self.MIN_OBSERVATIONS]
            best = max(eligible, key=lambda row: row.rating, default=None)
            promoted_at = champion.promoted_at
            if promoted_at is not None and promoted_at.tzinfo is None:
                promoted_at = promoted_at.replace(tzinfo=timezone.utc)
            promotion_cooldown = bool(
                promoted_at
                and datetime.now(timezone.utc) - promoted_at < timedelta(days=self.PROMOTION_COOLDOWN_DAYS)
            )
            if (
                best
                and best.agent_name != champion.agent_name
                and champion.observations >= self.MIN_OBSERVATIONS
                and best.rating >= champion.rating + self.PROMOTION_MARGIN
                and not promotion_cooldown
            ):
                previous = champion.agent_name
                champion.status = "POOR" if champion.rating < self.POOR_RATING else "CHALLENGER"
                best.status = "CHAMPION"
                best.promoted_at = datetime.now(timezone.utc)
                changes.append({"role": role, "previous": previous, "champion": best.agent_name})
            for row in rows:
                if row.status != "CHAMPION":
                    row.status = "POOR" if row.observations >= self.MIN_OBSERVATIONS and row.rating < self.POOR_RATING else "CHALLENGER"
        return changes

    async def _get_or_create(self, db: AsyncSession, name: str, role: str) -> AgentPerformance:
        row = (await db.execute(select(AgentPerformance).where(AgentPerformance.agent_name == name))).scalar_one_or_none()
        if row:
            return row
        row = AgentPerformance(
            agent_name=name,
            role=role,
            status="CHAMPION" if self.DEFAULT_CHAMPIONS.get(role) == name else "CHALLENGER",
        )
        db.add(row)
        await db.flush()
        return row

    def _profile(self, row: AgentPerformance) -> CompetitionProfile:
        return CompetitionProfile(
            agent_name=row.agent_name,
            role=row.role,
            status=row.status,
            observations=row.observations,
            successful_predictions=row.successful_predictions,
            failed_predictions=row.failed_predictions,
            rating=float(row.rating),
            ema_reward=float(row.ema_reward),
        )
