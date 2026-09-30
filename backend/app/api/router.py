from fastapi import APIRouter

from app.api.routes import agents, audit, auth, dashboard, logs, market, orders, positions, settings, strategy_lab, trading

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(audit.router)
api_router.include_router(dashboard.router)
api_router.include_router(market.router)
api_router.include_router(positions.router)
api_router.include_router(orders.router)
api_router.include_router(settings.router)
api_router.include_router(strategy_lab.router)
api_router.include_router(logs.router)
api_router.include_router(trading.router)
api_router.include_router(agents.router)
