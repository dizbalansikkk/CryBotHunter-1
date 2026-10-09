from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from app.api.routes.logs import logs, EXIT_AUDIT_EVENTS


@pytest.mark.asyncio
async def test_exit_journal_filters_position_and_cursor_in_database():
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))))
    assert await logs(None, db, limit=50, category="exits", position_id=71, before_id=1000) == []
    statement = db.execute.call_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "SECOND_TAKE_PROFIT_FILLED" in sql and "BREAKEVEN_APPLIED" in sql
    assert "position_id" in sql and "'71'" in sql
    assert "logs.id < 1000" in sql and "ORDER BY logs.id DESC" in sql
    assert "LIMIT 50" in sql
    assert "PROTECTIVE_STOP_UNCONFIRMED" in EXIT_AUDIT_EVENTS
    assert "SCALE_OUT_CANCELLED_MIN_NOTIONAL" in EXIT_AUDIT_EVENTS
