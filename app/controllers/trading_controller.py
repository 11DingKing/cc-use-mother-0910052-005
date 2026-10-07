"""业务模块说明。"""

from typing import Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.services.trading_service import TradingService
from app.risk.models import InstrumentInfo, RuleLimits
from decimal import Decimal

router = APIRouter(prefix="/api/trading", tags=["trading"])
trading_service = TradingService()


class ConnectRequest(BaseModel):
    """业务模块说明。"""
    adapter_type: str = "simulation"  # simulation, vnpy
    config: Optional[dict] = None


class BuyRequest(BaseModel):
    """业务模块说明。"""
    stock_code: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "limit"  # limit, market
    signal_type: Optional[str] = None
    signal_strength: float = 0.0


class SellRequest(BaseModel):
    """业务模块说明。"""
    stock_code: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "limit"
    signal_type: Optional[str] = None
    signal_strength: float = 0.0


class SignalTradeRequest(BaseModel):
    """业务模块说明。"""
    stock_code: str
    signal_type: str
    signal_strength: float
    price: float
    position_ratio: float = 0.1


@router.post("/connect")
async def connect(request: ConnectRequest):
    """业务模块说明。"""
    success = trading_service.connect(request.adapter_type, request.config)
    return {
        "success": success,
        "adapter_type": request.adapter_type,
        "message": "连接成功" if success else "连接失败",
    }


@router.post("/disconnect")
async def disconnect():
    """业务模块说明。"""
    trading_service.disconnect()
    return {"success": True, "message": "已断开连接"}


@router.get("/account")
async def get_account():
    """业务模块说明。"""
    return trading_service.get_account()


@router.get("/positions")
async def get_positions():
    """业务模块说明。"""
    return {"positions": trading_service.get_positions()}


@router.get("/positions/{stock_code}")
async def get_position(stock_code: str):
    """业务模块说明。"""
    position = trading_service.get_position(stock_code)
    if not position:
        return {"error": "未持有该股票"}
    return position


@router.post("/buy")
async def buy(request: BuyRequest):
    """业务模块说明。"""
    return trading_service.buy(
        stock_code=request.stock_code,
        quantity=request.quantity,
        price=request.price,
        order_type=request.order_type,
        signal_type=request.signal_type,
        signal_strength=request.signal_strength,
    )


@router.post("/sell")
async def sell(request: SellRequest):
    """业务模块说明。"""
    return trading_service.sell(
        stock_code=request.stock_code,
        quantity=request.quantity,
        price=request.price,
        order_type=request.order_type,
        signal_type=request.signal_type,
        signal_strength=request.signal_strength,
    )


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str):
    """业务模块说明。"""
    return trading_service.cancel_order(order_id)


@router.get("/orders/{order_id}")
async def get_order(order_id: str):
    """业务模块说明。"""
    return trading_service.get_order(order_id)


@router.get("/orders")
async def get_orders(
    stock_code: Optional[str] = Query(default=None, description="股票代码"),
    status: Optional[str] = Query(default=None, description="订单状态"),
):
    """业务模块说明。"""
    return {"orders": trading_service.get_orders(stock_code, status)}


@router.get("/quote/{stock_code}")
async def get_quote(stock_code: str):
    """业务模块说明。"""
    return trading_service.get_quote(stock_code)


@router.post("/signal-trade")
async def execute_signal_trade(request: SignalTradeRequest):
    """业务模块说明。"""
    result = trading_service.execute_signal(
        stock_code=request.stock_code,
        signal_type=request.signal_type,
        signal_strength=request.signal_strength,
        price=request.price,
        position_ratio=request.position_ratio,
    )
    
    if result:
        return result
    return {"message": "自动交易未启用或条件不满足"}


@router.post("/auto-trade/enable")
async def enable_auto_trade():
    """业务模块说明。"""
    trading_service.enable_auto_trade(True)
    return {"success": True, "message": "自动交易已启用"}


@router.post("/auto-trade/disable")
async def disable_auto_trade():
    """业务模块说明。"""
    trading_service.enable_auto_trade(False)
    return {"success": True, "message": "自动交易已禁用"}


@router.post("/check-stop-loss")
async def check_stop_loss():
    """业务模块说明。"""
    results = trading_service.check_stop_loss_take_profit()
    return {
        "triggered_count": len(results),
        "orders": results,
    }


# ---------------------------------------------------------------------------
# 组合级风险预算
# ---------------------------------------------------------------------------

class LimitsPayload(BaseModel):
    """新版规则额度。所有金额单位为元。"""
    max_total_long_amount: float = 1000000.0
    max_single_order_amount: float = 100000.0
    default_industry_amount: float = 300000.0
    industry_limits: dict[str, float] = {}
    default_instrument_amount: float = 500000.0
    instrument_limits: dict[str, float] = {}


class InstrumentPayload(BaseModel):
    """证券主数据。"""
    stock_code: str
    industry: str = "UNKNOWN"
    instrument_type: str = "stock"
    stock_name: Optional[str] = None


class ExemptionPayload(BaseModel):
    """临时豁免。"""
    dimension: str  # total / industry / instrument / order
    extra_amount: float
    valid_minutes: int = 60
    bucket: Optional[str] = None
    order_id: Optional[str] = None
    reason: str = ""


def _limits_from_payload(p: LimitsPayload) -> RuleLimits:
    return RuleLimits(
        max_total_long_amount=Decimal(str(p.max_total_long_amount)),
        max_single_order_amount=Decimal(str(p.max_single_order_amount)),
        default_industry_amount=Decimal(str(p.default_industry_amount)),
        industry_limits={k: Decimal(str(v)) for k, v in p.industry_limits.items()},
        default_instrument_amount=Decimal(str(p.default_instrument_amount)),
        instrument_limits={k: Decimal(str(v)) for k, v in p.instrument_limits.items()},
    )


@router.post("/risk/rules")
async def publish_rules(payload: LimitsPayload):
    """发布新版风险规则（版本号自动递增，在途订单仍按批准时版本追溯）。"""
    version = trading_service.publish_rules(_limits_from_payload(payload))
    return {"success": True, "version": version}


@router.get("/risk/rules")
async def get_rules():
    """当前生效规则版本与内容。"""
    return trading_service.risk_rules()


@router.post("/risk/instruments")
async def register_instrument(payload: InstrumentPayload):
    """登记证券行业/品种主数据。"""
    trading_service.register_instrument(
        InstrumentInfo(
            stock_code=payload.stock_code,
            industry=payload.industry,
            instrument_type=payload.instrument_type,
            stock_name=payload.stock_name,
        )
    )
    return {"success": True}


@router.post("/risk/exemptions")
async def grant_exemption(payload: ExemptionPayload):
    """授予临时豁免（按桶或按订单追加额度）。"""
    exemption_id = trading_service.grant_exemption(
        dimension=payload.dimension,
        extra_amount=payload.extra_amount,
        valid_minutes=payload.valid_minutes,
        bucket=payload.bucket,
        order_id=payload.order_id,
        reason=payload.reason,
    )
    return {"success": True, "exemption_id": exemption_id}


@router.get("/risk/exposure")
async def risk_exposure(
    stock_code: Optional[str] = Query(default=None, description="先刷新该证券行情再试算"),
):
    """各维度 limit/current/pending/projected/remaining 全量试算。"""
    return trading_service.risk_exposure(stock_code)


@router.get("/risk/open-orders")
async def risk_open_orders():
    """仍占用预算的待成交订单（含冻结价格与规则版本）。"""
    return {"orders": trading_service.risk_open_orders()}


@router.get("/risk/audit")
async def risk_audit(
    order_id: Optional[str] = Query(default=None),
):
    """决策审计链：放行/拒绝/成交/释放，附完整试算快照与版本。"""
    return {"decisions": trading_service.risk_audit(order_id)}
