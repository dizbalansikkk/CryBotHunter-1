import json
import logging

import httpx
from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.dto import LlmAdvice, MarketCoin

logger = logging.getLogger(__name__)


class LlmAdvisorProvider:
    async def advise(self, coin: MarketCoin, market_context: dict) -> LlmAdvice | None:
        settings = get_settings()
        if settings.llm_provider.lower() != "openai" or not settings.openai_api_key:
            return None

        prompt = {
            "task": "Return conservative crypto trading advice as strict JSON only.",
            "allowed_actions": ["BUY", "SELL", "WAIT"],
            "risk_rule": "Prefer WAIT if confidence is below 0.70 or data is conflicting.",
            "symbol": coin.symbol,
            "market": {
                "price": coin.price,
                "rating": coin.rating,
                "rsi": coin.rsi,
                "ema20": coin.ema20,
                "ema50": coin.ema50,
                "ema200": coin.ema200,
                "macd": coin.macd,
                "atr": coin.atr,
                "volume_24h": coin.volume_24h,
                "price_change_percent": coin.price_change_percent,
                "funding_rate": coin.funding_rate,
                "open_interest": coin.open_interest,
            },
            "local_agent_context": market_context,
            "json_schema": {
                "action": "BUY | SELL | WAIT",
                "confidence": "number between 0 and 1",
                "rationale": "short explanation",
                "invalid_if": ["condition that invalidates the idea"],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={
                        "model": settings.llm_model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a conservative trading risk assistant. Return JSON only.",
                            },
                            {"role": "user", "content": json.dumps(prompt)},
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return LlmAdvice.model_validate_json(content)
        except (httpx.HTTPError, KeyError, json.JSONDecodeError, ValidationError):
            logger.exception("LLM-советник не смог подготовить рыночное заключение")
            return None
