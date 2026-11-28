"""回测只读端点(B2.4/KI-039)。

`POST /api/backtest/run` —— 需登录; 入参有界(标的数/资金/持有期), 无写操作。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.core.backtest.service import BacktestDateError, run_backtest
from src.web.api.auth import get_current_user
from src.web.models import User

router = APIRouter()


class BacktestIn(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=20)
    market: str = "CN"
    start_date: str = Field(
        "", pattern=r"^$|^[0-9]{4}-[0-9]{2}-[0-9]{2}$", description="有效的 YYYY-MM-DD 日期（留空不限）",
    )
    end_date: str = Field(
        "", pattern=r"^$|^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        description="有效的 YYYY-MM-DD 日期（含当日，显式边界不强制平仓）",
    )
    holding_days: int = Field(10, ge=1, le=60)
    stop_pct: float = Field(0.08, ge=0.0, le=0.5)
    target_pct: float = Field(0.15, ge=0.0, le=1.0)
    initial_capital: float = Field(1_000_000.0, gt=0, le=1e10)
    cash_per_trade: float = Field(100_000.0, gt=0, le=1e10)

    @field_validator("start_date", "end_date")
    @classmethod
    def _calendar_date(cls, value: str) -> str:
        """Reject impossible dates through the schema so 422 bodies keep one shape."""
        if value and date.fromisoformat(value).isoformat() != value:
            raise ValueError("必须是有效的 YYYY-MM-DD 日期")
        return value


@router.post("/run")
def run_backtest_endpoint(
    payload: BacktestIn,
    _user: User = Depends(get_current_user),
) -> dict:
    symbols = [s for s in (payload.symbols or []) if str(s).strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="symbols 不能为空")
    try:
        return run_backtest(
            symbols=symbols,
            market=payload.market,
            start_date=payload.start_date.strip() or None,
            end_date=payload.end_date.strip() or None,
            holding_days=payload.holding_days,
            stop_pct=payload.stop_pct,
            target_pct=payload.target_pct,
            initial_capital=payload.initial_capital,
            cash_per_trade=payload.cash_per_trade,
        )
    except BacktestDateError as exc:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", "date"], "msg": str(exc), "type": "value_error"}],
        ) from exc
