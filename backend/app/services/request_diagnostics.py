"""Read-only Redis diagnostics: never contacts the exchange or returns secrets."""
import time
from datetime import datetime, timezone
from redis.exceptions import RedisError
from app.core.config import get_settings
from app.services.exchange_throttle import shared_store


def request_diagnostics():
    now = time.time()
    settings = get_settings()
    base = {"generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "public_budget": settings.binance_rest_public_budget,
            "total_budget": settings.binance_rest_total_budget,
            "stream_enabled": settings.binance_market_stream_enabled,
            "note": "Локальная оценка веса за 60 секунд, не счётчик Binance. Потоки показывают активность сборщика, а не свежесть каждой монеты."}
    try:
        store = shared_store()
        seconds = [str(s) for s in range(int(now)-59, int(now)+1)]
        pipe = store.pipeline(transaction=False)
        pipe.hmget('binance:rest:weight', seconds)
        pipe.mget(['binance:cooldown:until', 'binance:usdt:public:stream:health:market',
                   'binance:usdt:public:stream:health:public'])
        weights, health = pipe.execute()
        used = sum(float(v or 0) for v in weights)
        deadline = float(health[0] or 0)
        streams = []
        for name, timestamp in zip(('ticker', 'book'), health[1:]):
            age = now-float(timestamp) if timestamp else None
            streams.append({"name": name, "age_seconds": round(age, 1) if age is not None else None,
                            "fresh": age is not None and 0 <= age <= 5})
        return {**base, "available": True, "used_weight": used,
            "public_remaining": max(0, min(settings.binance_rest_public_budget, settings.binance_rest_total_budget)-used),
            "total_remaining": max(0, settings.binance_rest_total_budget-used),
            "cooldown_until": datetime.fromtimestamp(deadline, timezone.utc).isoformat() if deadline > now else None,
            "cooldown_seconds": max(0, int(deadline-now)+1) if deadline > now else 0,
            "streams": streams}
    except (RedisError, TypeError, ValueError, OverflowError):
        return {**base, "available": False, "reason": "Диагностика Redis недоступна; отсутствие данных не означает нулевой расход."}
