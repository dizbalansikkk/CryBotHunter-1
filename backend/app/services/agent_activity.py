from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.models.entities import AgentDecision, AgentPerformance
from app.schemas.dto import AgentActivityItemOut, AgentActivityOut


class AgentActivityService:
    def summarize(
        self,
        decisions: list[AgentDecision],
        now: datetime | None = None,
        performances: list[AgentPerformance] | None = None,
    ) -> AgentActivityOut:
        current_time = self._aware(now or datetime.now(timezone.utc))
        cutoff = current_time - timedelta(hours=24)
        grouped: dict[str, list[AgentDecision]] = defaultdict(list)
        for decision in decisions:
            grouped[decision.agent_name].append(decision)

        items: list[AgentActivityItemOut] = []
        performance_by_name = {row.agent_name: row for row in performances or []}
        for name, rows in grouped.items():
            rows.sort(key=lambda row: self._aware(row.created_at), reverse=True)
            latest = rows[0]
            decisions_24h = sum(1 for row in rows if self._aware(row.created_at) >= cutoff)
            performance = performance_by_name.get(name)
            items.append(
                AgentActivityItemOut(
                    agent_name=name,
                    decisions=len(rows),
                    decisions_24h=decisions_24h,
                    average_confidence=round(sum(float(row.confidence or 0) for row in rows) / len(rows), 4),
                    directional_votes=sum(1 for row in rows if row.action in {"BUY", "SELL"}),
                    approvals=sum(1 for row in rows if row.action in {"ALLOW", "REDUCE_SIZE"}),
                    blocks=sum(1 for row in rows if row.action == "BLOCK"),
                    waits=sum(1 for row in rows if row.action == "WAIT"),
                    last_action=latest.action,
                    last_symbol=latest.symbol,
                    last_seen_at=latest.created_at,
                    competition_role=performance.role if performance else None,
                    competition_status=performance.status if performance else None,
                    performance_rating=round(float(performance.rating), 4) if performance else None,
                    performance_observations=int(performance.observations) if performance else 0,
                    performance_win_rate=(
                        round(performance.successful_predictions / performance.observations, 4)
                        if performance and performance.observations
                        else None
                    ),
                )
            )
        items.sort(key=lambda item: (item.decisions_24h, item.decisions), reverse=True)
        newest = max((self._aware(row.created_at) for row in decisions), default=None)
        return AgentActivityOut(
            total_decisions=len(decisions),
            decisions_24h=sum(1 for row in decisions if self._aware(row.created_at) >= cutoff),
            active_agents=len(grouped),
            committee_approvals=sum(
                1
                for row in decisions
                if row.agent_name == "TradeCommittee" and row.action in {"BUY", "SELL"}
            ),
            last_decision_at=newest,
            agents=items,
        )

    def _aware(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
