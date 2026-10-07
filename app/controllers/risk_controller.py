"""组合级风险预算接口。

复用 trading_controller 中的 TradingService 单例，确保规则、豁免、台账、
剩余额度与实际下单走的是同一个组合风控服务（同一份存储与行情供给）。
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.controllers.trading_controller import trading_service
from app.risk.store import RiskStoreError
from app.trading.base import Order, OrderSide, OrderType
from decimal import Decimal

router = APIRouter(prefix="/api/risk", tags=["risk"])


def _error(message: str, status_code: int = 400, details: Optional[dict] = None):
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": "RISK_ERROR", "message": message, "details": details or {}}},
    )


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class RiskLimitItem(BaseModel):
    """业务模块说明。"""
    dimension: str = Field(description="total / industry / instrument")
    target: str
    max_exposure: str
    note: Optional[str] = ""


class PublishRulesRequest(BaseModel):
    """业务模块说明。"""
    limits: List[RiskLimitItem]
    max_single_order_amount: str = "100000"
    note: Optional[str] = ""
    created_by: str = "system"


class ExemptionRequest(BaseModel):
    """业务模块说明。"""
    dimension: str
    target: str
    extra_amount: str
    valid_minutes: int = 60
    reason: str = ""
    granted_by: str = "system"
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None


class InstrumentRequest(BaseModel):
    """业务模块说明。"""
    stock_code: str
    industry: str = "其他"
    instrument_type: str = "STOCK"


class PreCheckRequest(BaseModel):
    """业务模块说明。"""
    stock_code: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "limit"
    side: str = "buy"


# ---------------------------------------------------------------------------
# 剩余额度
# ---------------------------------------------------------------------------

@router.get("/residual")
async def residual():
    """当前规则版本下总敞口/行业/品种的可解释剩余额度。"""
    try:
        return trading_service.portfolio_risk.residual_report()
    except RiskStoreError as exc:
        return _error(str(exc), 404)


@router.post("/precheck")
async def precheck(request: PreCheckRequest):
    """试算一笔委托（不预留），返回逐条规则的剩余与判定。"""
    try:
        order = Order(
            order_id=f"PRECHECK_{id(request)}",
            stock_code=request.stock_code,
            side=OrderSide.BUY if request.side == "buy" else OrderSide.SELL,
            order_type=OrderType.LIMIT if request.order_type == "limit" else OrderType.MARKET,
            quantity=request.quantity,
            price=Decimal(str(request.price)) if request.price is not None else None,
        )
        decision = trading_service.portfolio_risk.precheck(order)
        return decision.to_dict()
    except RiskStoreError as exc:
        return _error(str(exc))


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------

@router.get("/rules")
async def list_rules():
    """业务模块说明。"""
    return {"versions": trading_service.portfolio_risk.list_rule_versions()}


@router.get("/rules/current")
async def current_rules():
    """业务模块说明。"""
    try:
        return trading_service.portfolio_risk.get_ruleset()
    except RiskStoreError as exc:
        return _error(str(exc), 404)


@router.get("/rules/{version}")
async def get_rule_version(version: int):
    """业务模块说明。"""
    try:
        return trading_service.portfolio_risk.get_ruleset(version)
    except RiskStoreError as exc:
        return _error(str(exc), 404)


@router.post("/rules")
async def publish_rules(request: PublishRulesRequest):
    """发布新版本规则集（append-only，旧版本保持只读）。"""
    try:
        result = trading_service.portfolio_risk.publish_ruleset(
            limits=[item.model_dump() for item in request.limits],
            max_single_order_amount=request.max_single_order_amount,
            created_by=request.created_by,
            note=request.note or "",
        )
        return result
    except RiskStoreError as exc:
        return _error(f"规则发布失败: {exc}")


# ---------------------------------------------------------------------------
# 临时豁免
# ---------------------------------------------------------------------------

@router.get("/exemptions")
async def list_exemptions(include_history: bool = True):
    """业务模块说明。"""
    return {"exemptions": trading_service.portfolio_risk.list_exemptions(include_history)}


@router.post("/exemptions")
async def grant_exemption(request: ExemptionRequest):
    """授予临时豁免（版本化，到期自动失效）。"""
    try:
        return trading_service.portfolio_risk.grant_exemption(
            dimension=request.dimension,
            target=request.target,
            extra_amount=request.extra_amount,
            valid_minutes=request.valid_minutes,
            reason=request.reason,
            granted_by=request.granted_by,
            valid_from=request.valid_from,
            valid_to=request.valid_to,
        )
    except RiskStoreError as exc:
        return _error(f"豁免授予失败: {exc}")


@router.post("/exemptions/{exemption_id}/revoke")
async def revoke_exemption(exemption_id: str):
    """撤销豁免：追加 revoked 版本行，历史行保留可审。"""
    try:
        return trading_service.portfolio_risk.revoke_exemption(exemption_id)
    except RiskStoreError as exc:
        return _error(str(exc), 404)


# ---------------------------------------------------------------------------
# 证券分类
# ---------------------------------------------------------------------------

@router.put("/instruments")
async def upsert_instrument(request: InstrumentRequest):
    """登记/更新证券的行业与品种分类。"""
    return trading_service.portfolio_risk.upsert_instrument(
        stock_code=request.stock_code,
        industry=request.industry,
        instrument_type=request.instrument_type,
    )


# ---------------------------------------------------------------------------
# 台账与审计
# ---------------------------------------------------------------------------

@router.get("/orders")
async def list_ledger(include_closed: bool = False):
    """业务模块说明。"""
    return {"orders": trading_service.portfolio_risk.list_ledger(include_closed)}


@router.get("/orders/{order_id}/trail")
async def order_trail(order_id: str):
    """订单的台账记录与全部风控决策（按当时版本追溯）。"""
    trail = trading_service.portfolio_risk.get_order_trail(order_id)
    if trail is None:
        return _error(f"订单 {order_id} 无风控台账记录", 404)
    return trail


@router.get("/decisions")
async def list_decisions(order_id: Optional[str] = None, limit: int = 100):
    """业务模块说明。"""
    return {"decisions": trading_service.portfolio_risk.list_decisions(order_id, limit)}


@router.get("/decisions/{decision_id}")
async def get_decision(decision_id: str):
    """业务模块说明。"""
    try:
        return trading_service.portfolio_risk.get_decision(decision_id)
    except RiskStoreError as exc:
        return _error(str(exc), 404)
