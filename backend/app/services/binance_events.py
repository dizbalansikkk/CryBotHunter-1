import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from app.core.config import get_settings
from app.schemas.dto import MarketCoin


logger = logging.getLogger(__name__)

EventStatus = Literal["UNKNOWN", "NORMAL", "PRE_EVENT", "ACTIVE", "ACTIVE_HIGH_IMPACT", "POST_EVENT"]
EventType = Literal["LAUNCHPOOL", "LAUNCHPAD"]

_ARTICLE_LIST_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
_ARTICLE_DETAIL_URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query"
_SOURCE_ROOT = "https://www.binance.com/en/support/announcement/detail/"
_DATE_PATTERN = r"(20\d{2}-\d{2}-\d{2}\s+\d{1,2}:\d{2})(?:\s*:\d{2})?\s*\(UTC\)"
_PERIOD_PATTERNS = (
    re.compile(rf"Farming\s+Period\s*:?\s*{_DATE_PATTERN}\s*(?:to|[-–—])\s*{_DATE_PATTERN}", re.I),
    re.compile(rf"(?:Launchpad\s+)?Subscription\s+Period\s*:?\s*{_DATE_PATTERN}\s*(?:to|[-–—])\s*{_DATE_PATTERN}", re.I),
    re.compile(rf"Token\s+Sale\s+Period\s*:?\s*{_DATE_PATTERN}\s*(?:to|[-–—])\s*{_DATE_PATTERN}", re.I),
)
_START_DURATION_PATTERN = re.compile(
    rf"(?:over|for)\s+(\d+)\s+days?.{{0,160}}?(?:farming|subscription)\s+starting\s+(?:from|at)\s+{_DATE_PATTERN}",
    re.I,
)


@dataclass(frozen=True)
class BinanceEventAssessment:
    status: EventStatus
    event_type: EventType | None = None
    bnb_required: bool = False
    event_start: datetime | None = None
    event_end: datetime | None = None
    time_to_start_seconds: int | None = None
    time_to_end_seconds: int | None = None
    event_score: int = 0
    checked_at: datetime | None = None
    source_url: str | None = None
    title: str | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "event_type": self.event_type,
            "bnb_required": self.bnb_required,
            "event_start": self.event_start.isoformat() if self.event_start else None,
            "event_end": self.event_end.isoformat() if self.event_end else None,
            "time_to_start_seconds": self.time_to_start_seconds,
            "time_to_end_seconds": self.time_to_end_seconds,
            "event_score": self.event_score,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "source_url": self.source_url,
            "title": self.title,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BnbPriorityDecision:
    assessment: BinanceEventAssessment
    strategy_score: int
    market_score: int
    final_score: int
    market_signals: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            **self.assessment.as_dict(),
            "strategy_score": self.strategy_score,
            "market_score": self.market_score,
            "final_score": self.final_score,
            "market_signals": list(self.market_signals),
        }


@dataclass(frozen=True)
class _EventCandidate:
    event_type: EventType
    title: str
    source_url: str
    bnb_required: bool
    start: datetime
    end: datetime


class BinanceEventPriorityService:
    """Fail-closed Launchpool/Launchpad priority data from Binance announcements.

    The CMS feed is used only for scheduling and ranking.  It never mutates a
    strategy signal, its direction, or any risk setting.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.settings = get_settings()
        self._client = client
        self._cache: BinanceEventAssessment | None = None
        self._cache_monotonic = 0.0

    async def assess(self, now: datetime | None = None) -> BinanceEventAssessment:
        now = self._utc(now or datetime.now(timezone.utc))
        loop_time = asyncio.get_running_loop().time()
        ttl = max(int(self.settings.binance_event_cache_seconds), 0)
        if self._cache is not None and loop_time - self._cache_monotonic <= ttl:
            if self._cache.event_start:
                refreshed = self._classify(self._candidate_from_assessment(self._cache), now)
                return replace(refreshed, checked_at=self._cache.checked_at)
            return self._cache
        try:
            articles = await self._fetch_recent_articles(now)
            candidates = await self._fetch_candidates(articles)
            result = self._select(candidates, now)
        except Exception as exc:
            logger.warning("Binance event check failed closed error=%s", type(exc).__name__)
            result = BinanceEventAssessment(
                status="UNKNOWN",
                event_score=0,
                checked_at=now,
                reason=f"official Binance event data unavailable ({type(exc).__name__})",
            )
        self._cache = result
        self._cache_monotonic = loop_time
        return result

    def priority_decision(
        self,
        assessment: BinanceEventAssessment,
        coin: MarketCoin,
        strategy_score: int,
    ) -> BnbPriorityDecision:
        market_score, signals = self.market_score(coin)
        effective = assessment
        if assessment.status == "ACTIVE" and self._has_high_impact(signals):
            effective = replace(
                assessment,
                status="ACTIVE_HIGH_IMPACT",
                event_score=20,
                reason=f"{assessment.reason}; confirmed BNB market activity: {', '.join(signals)}",
            )
        elif assessment.status == "POST_EVENT":
            effective = replace(
                assessment,
                event_score=10 if self._has_high_impact(signals) else 0,
                reason=(
                    f"{assessment.reason}; post-event momentum confirmed: {', '.join(signals)}"
                    if self._has_high_impact(signals)
                    else f"{assessment.reason}; no confirmed post-event momentum"
                ),
            )
        return BnbPriorityDecision(
            assessment=effective,
            strategy_score=int(strategy_score),
            market_score=market_score,
            final_score=int(strategy_score) + effective.event_score + market_score,
            market_signals=signals,
        )

    def market_score(self, coin: MarketCoin) -> tuple[int, tuple[str, ...]]:
        signals: list[str] = []
        score = 0
        average_volume = max(float(coin.volume_average_24h or 0), 0.0)
        volume_ratio = float(coin.volume_24h) / average_volume if average_volume > 0 else 0.0
        atr_percent = float(coin.atr) / max(float(coin.price), 1e-12) * 100
        momentum = abs(float(coin.price_change_percent))
        spread = max(float(coin.spread_bps or 0), 0.0)

        if volume_ratio >= float(self.settings.binance_event_high_impact_volume_ratio):
            score += 6
            signals.append(f"volume_ratio={volume_ratio:.2f}")
        if atr_percent >= float(self.settings.binance_event_high_impact_atr_percent):
            score += 5
            signals.append(f"atr_percent={atr_percent:.2f}")
        if momentum >= float(self.settings.binance_event_high_impact_price_change_percent):
            score += 5
            signals.append(f"price_impulse={momentum:.2f}%")
        if 0 < spread <= float(self.settings.market_quality_max_spread_bps):
            score += 2
            signals.append(f"liquid_spread={spread:.2f}bps")
        if float(coin.open_interest or 0) > 0:
            score += 1
            signals.append("open_interest_available")
        if abs(float(coin.funding_rate or 0)) > 0:
            score += 1
            signals.append("funding_available")
        return min(score, 20), tuple(signals)

    def _has_high_impact(self, signals: tuple[str, ...]) -> bool:
        primary = sum(
            signal.startswith(("volume_ratio=", "atr_percent=", "price_impulse="))
            for signal in signals
        )
        return primary >= 2

    async def _fetch_recent_articles(self, now: datetime) -> list[dict[str, Any]]:
        articles: list[dict[str, Any]] = []
        max_pages = max(int(self.settings.binance_event_max_feed_pages), 1)
        lookback = timedelta(days=max(int(self.settings.binance_event_announcement_lookback_days), 1))
        cutoff = now - lookback
        for page in range(1, max_pages + 1):
            payload = await self._get_json(
                _ARTICLE_LIST_URL,
                params={"type": 1, "catalogId": 48, "pageNo": page, "pageSize": 50},
            )
            catalogs = ((payload.get("data") or {}).get("catalogs") or [])
            if payload.get("code") != "000000" or not catalogs or not isinstance(catalogs[0].get("articles"), list):
                raise ValueError("unexpected Binance announcement feed schema")
            page_articles = catalogs[0]["articles"]
            articles.extend(item for item in page_articles if isinstance(item, dict))
            release_dates = [self._timestamp(item.get("releaseDate")) for item in page_articles]
            oldest = min((value for value in release_dates if value is not None), default=None)
            if not page_articles or (oldest is not None and oldest < cutoff):
                break
        return [
            item
            for item in articles
            if re.search(r"\bLaunch(?:pool|pad)\b", str(item.get("title") or ""), re.I)
            and (self._timestamp(item.get("releaseDate")) or now) >= cutoff
        ]

    async def _fetch_candidates(self, articles: list[dict[str, Any]]) -> list[_EventCandidate]:
        candidates: list[_EventCandidate] = []
        for article in articles:
            code = str(article.get("code") or "").strip()
            title = str(article.get("title") or "").strip()
            if not code or not title:
                continue
            payload = await self._get_json(_ARTICLE_DETAIL_URL, params={"articleCode": code})
            data = payload.get("data") or {}
            if payload.get("code") != "000000" or not data.get("body"):
                raise ValueError("unexpected Binance announcement detail schema")
            text = self._body_text(data["body"])
            period = self._extract_period(text)
            if period is None:
                raise ValueError(f"Binance event has incomplete dates code={code}")
            event_type: EventType = "LAUNCHPOOL" if re.search(r"\bLaunchpool\b", title, re.I) else "LAUNCHPAD"
            bnb_required = bool(
                re.search(
                    r"\b(?:locking|lock|stake|staking|subscribe|subscription|commit|committing|hold|holding|use|using)"
                    r"\b[^.!?]{0,120}\bBNB\b|\bBNB\b[^.!?]{0,120}"
                    r"\b(?:pool|subscribe|subscription|commit|holding|holdings|eligible|required)\b",
                    f"{title}. {text}",
                    re.I,
                )
            )
            candidates.append(
                _EventCandidate(
                    event_type=event_type,
                    title=title,
                    source_url=f"{_SOURCE_ROOT}{code}",
                    bnb_required=bnb_required,
                    start=period[0],
                    end=period[1],
                )
            )
        return candidates

    def _select(self, candidates: list[_EventCandidate], now: datetime) -> BinanceEventAssessment:
        relevant = [candidate for candidate in candidates if candidate.bnb_required]
        if not relevant:
            return BinanceEventAssessment(
                status="NORMAL",
                event_score=0,
                checked_at=now,
                reason="official Binance announcement feed has no current BNB Launchpool/Launchpad event",
            )
        schedules_by_event: dict[tuple[str, str], set[tuple[datetime, datetime]]] = {}
        for candidate in relevant:
            key = (candidate.event_type, " ".join(candidate.title.lower().split()))
            schedules_by_event.setdefault(key, set()).add((candidate.start, candidate.end))
        if any(len(schedules) > 1 for schedules in schedules_by_event.values()):
            return BinanceEventAssessment(
                status="UNKNOWN",
                event_score=0,
                checked_at=now,
                reason="conflicting official Binance event schedules",
            )
        assessments = [self._classify(candidate, now) for candidate in relevant]
        weight = {"ACTIVE": 5, "PRE_EVENT": 4, "POST_EVENT": 3, "NORMAL": 2, "UNKNOWN": 1, "ACTIVE_HIGH_IMPACT": 6}
        return max(assessments, key=lambda item: (weight[item.status], item.event_score, item.event_end or datetime.min.replace(tzinfo=timezone.utc)))

    def _classify(self, candidate: _EventCandidate | None, now: datetime) -> BinanceEventAssessment:
        if candidate is None:
            return BinanceEventAssessment(status="UNKNOWN", event_score=0, checked_at=now, reason="event details unavailable")
        to_start = int((candidate.start - now).total_seconds())
        to_end = int((candidate.end - now).total_seconds())
        pre_window = timedelta(hours=max(float(self.settings.binance_event_pre_event_hours), 0.0))
        post_window = timedelta(hours=max(float(self.settings.binance_event_post_event_hours), 0.0))
        if candidate.start <= now <= candidate.end:
            status: EventStatus = "ACTIVE"
            score = 15
        elif now < candidate.start and candidate.start - now <= pre_window:
            status = "PRE_EVENT"
            score = 10
        elif candidate.end < now and now - candidate.end <= post_window:
            status = "POST_EVENT"
            score = 0
        else:
            status = "NORMAL"
            score = 0
        return BinanceEventAssessment(
            status=status,
            event_type=candidate.event_type,
            bnb_required=candidate.bnb_required,
            event_start=candidate.start,
            event_end=candidate.end,
            time_to_start_seconds=to_start,
            time_to_end_seconds=to_end,
            event_score=score,
            checked_at=now,
            source_url=candidate.source_url,
            title=candidate.title,
            reason=f"confirmed Binance {candidate.event_type.lower()} schedule",
        )

    def _candidate_from_assessment(self, assessment: BinanceEventAssessment) -> _EventCandidate | None:
        if not assessment.event_type or not assessment.event_start or not assessment.event_end:
            return None
        return _EventCandidate(
            event_type=assessment.event_type,
            title=assessment.title or "",
            source_url=assessment.source_url or "",
            bnb_required=assessment.bnb_required,
            start=assessment.event_start,
            end=assessment.event_end,
        )

    async def _get_json(self, url: str, params: dict[str, object]) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "CryptoAiTrader/1.0 (Binance event priority; public data only)",
            "Referer": "https://www.binance.com/en/support/announcement/",
        }
        if self._client is not None:
            response = await self._client.get(url, params=params, headers=headers)
        else:
            timeout = max(float(self.settings.binance_event_request_timeout_seconds), 1.0)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Binance returned a non-object JSON response")
        return payload

    def _extract_period(self, text: str) -> tuple[datetime, datetime] | None:
        normalized = " ".join(html.unescape(text).replace("\xa0", " ").split())
        for pattern in _PERIOD_PATTERNS:
            match = pattern.search(normalized)
            if match:
                start = self._parse_date(match.group(1))
                end = self._parse_date(match.group(2))
                return (start, end) if end > start else None
        duration = _START_DURATION_PATTERN.search(normalized)
        if duration:
            start = self._parse_date(duration.group(2))
            return start, start + timedelta(days=int(duration.group(1)))
        return None

    def _body_text(self, body: object) -> str:
        parsed = json.loads(body) if isinstance(body, str) else body
        values: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                text = node.get("text")
                if isinstance(text, str):
                    values.append(text)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(parsed)
        if not values:
            raise ValueError("Binance announcement body has no readable text")
        return " ".join(values)

    def _parse_date(self, value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

    def _timestamp(self, value: object) -> datetime | None:
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def _utc(self, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
