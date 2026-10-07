"""业务模块说明。"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

from app.trading.base import (
    TradingAdapter,
    OrderStatus,
    OrderType,
    RiskManager,
)
from app.trading.simulation_adapter import SimulationAdapter
from app.trading.vnpy_adapter import VnpyAdapter
from app.risk.engine import PortfolioRiskEngine
from app.risk.gateway import RiskBudgetExceeded, TradingRiskGateway
from app.risk.models import Dimension, InstrumentInfo, RuleLimits, Side
from app.risk.store import InMemoryRiskStore
from app.services.analysis_service import AnalysisService
from app.middleware.exception_handler import AppException

logger = logging.getLogger(__name__)


class TradingException(AppException):
    """业务模块说明。"""

    def __init__(
        self,
        message: str,
        order_id: Optional[str] = None,
        stock_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        code: str = "TRADING_ERROR",
        status_code: int = 400,
    ):
        super().__init__(
            message=message,
            code=code,
            status_code=status_code,
            details={
                "order_id": order_id,
                "stock_code": stock_code,
                **(details or {}),
            },
        )


class TradingService:
    """业务模块说明。"""
    
    def __init__(self, adapter: Optional[TradingAdapter] = None):
        self.adapter = adapter or SimulationAdapter()
        self.risk_manager = RiskManager()
        self.analysis_service = AnalysisService()
        self._auto_trade_enabled = False
        self.portfolio_id = "default"
        self.gateway: Optional[TradingRiskGateway] = None
        self._risk_engine: Optional[PortfolioRiskEngine] = None
        self._risk_limits: Optional[RuleLimits] = None

    def connect(self, adapter_type: str = "simulation", config: Optional[Dict] = None) -> bool:
        """业务模块说明。"""
        if adapter_type == "vnpy":
            self.adapter = VnpyAdapter(config or {})
        else:
            self.adapter = SimulationAdapter(config)

        connected = self.adapter.connect()

        # 每个交易连接一套全新的组合风险账本（进程内存储，离线可跑）
        engine = PortfolioRiskEngine(store=InMemoryRiskStore())
        self._risk_engine = engine
        self.gateway = TradingRiskGateway(
            adapter=self.adapter,
            portfolio_id=self.portfolio_id,
            engine=engine,
            initial_limits=self._risk_limits,
        )
        self.gateway.initialize()
        return connected
    
    def disconnect(self) -> None:
        """业务模块说明。"""
        if self.adapter:
            self.adapter.disconnect()
    
    def get_account(self) -> Dict[str, Any]:
        """业务模块说明。"""
        account = self.adapter.get_account()
        if not account:
            raise TradingException("无法获取账户信息，请检查交易连接")
        return account.to_dict()
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """业务模块说明。"""
        positions = self.adapter.get_positions()
        return [p.to_dict() for p in positions]
    
    def get_position(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """业务模块说明。"""
        position = self.adapter.get_position(stock_code)
        return position.to_dict() if position else None
    
    def buy(
        self,
        stock_code: str,
        quantity: int,
        price: Optional[float] = None,
        order_type: str = "limit",
        signal_type: Optional[str] = None,
        signal_strength: float = 0.0,
    ) -> Dict[str, Any]:
        """业务模块说明。"""
        # 数量校验
        if quantity <= 0 or quantity % 100 != 0:
            raise TradingException(
                "买入数量必须是100的整数倍",
                stock_code=stock_code,
            )
        
        # 构建订单
        ot = OrderType.LIMIT if order_type == "limit" else OrderType.MARKET

        if ot == OrderType.LIMIT and price is None:
            raise TradingException("限价单必须指定价格", stock_code=stock_code)

        order_id = self.adapter._generate_order_id()
        result = self._submit_with_risk(
            order_id=order_id,
            stock_code=stock_code,
            side=Side.BUY,
            quantity=quantity,
            limit_price=Decimal(str(price)) if price is not None else None,
            order_extra={
                "signal_type": signal_type,
                "signal_strength": signal_strength,
            },
        )
        return result

    def sell(
        self,
        stock_code: str,
        quantity: int,
        price: Optional[float] = None,
        order_type: str = "limit",
        signal_type: Optional[str] = None,
        signal_strength: float = 0.0,
    ) -> Dict[str, Any]:
        """业务模块说明。"""
        # 持仓检查
        position = self.adapter.get_position(stock_code)
        if not position or position.available_quantity < quantity:
            available = position.available_quantity if position else 0
            raise TradingException(
                f"可用持仓不足，需要 {quantity}，可用 {available}",
                stock_code=stock_code,
            )

        # 构建订单
        ot = OrderType.LIMIT if order_type == "limit" else OrderType.MARKET

        if ot == OrderType.LIMIT and price is None:
            raise TradingException("限价单必须指定价格", stock_code=stock_code)

        order_id = self.adapter._generate_order_id()
        return self._submit_with_risk(
            order_id=order_id,
            stock_code=stock_code,
            side=Side.SELL,
            quantity=quantity,
            limit_price=Decimal(str(price)) if price is not None else None,
            order_extra={
                "signal_type": signal_type,
                "signal_strength": signal_strength,
            },
        )

    def _submit_with_risk(
        self,
        order_id: str,
        stock_code: str,
        side: Side,
        quantity: int,
        limit_price: Optional[Decimal],
        order_extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """经组合风险预算放行后下单，并把拒绝原因组织成业务异常。"""
        if self.gateway is None:
            self.connect("simulation")
        assert self.gateway is not None

        try:
            result = self.gateway.submit(
                order_id=order_id,
                stock_code=stock_code,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                order_extra=order_extra,
            )
        except RiskBudgetExceeded as exc:
            raise TradingException(
                f"组合风险预算不足: {exc.decision.reason}",
                order_id=order_id,
                stock_code=stock_code,
                code="RISK_REJECTED",
                status_code=403,
                details={
                    "decision_id": exc.decision.decision_id,
                    "rule_version": exc.decision.rule_version,
                    "report": exc.report.to_dict(),
                },
            )

        order_dict = result["order"]
        if order_dict.get("status") in (OrderStatus.REJECTED.value, OrderStatus.FAILED.value):
            raise TradingException(
                f"下单失败: {order_dict.get('error_message')}",
                order_id=order_id,
                stock_code=stock_code,
            )

        # 兼容旧返回结构，附带组合风控追溯信息
        order_dict["risk_rule_version"] = result["approval"]["rule_version"]
        order_dict["risk_decision_id"] = result["approval"]["decision_id"]
        if result.get("reservation"):
            order_dict["risk_reservation"] = result["reservation"]
        return order_dict
    
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """业务模块说明。"""
        order = self.adapter.get_order(order_id)
        if not order:
            raise TradingException("订单不存在", order_id=order_id)

        if order.status not in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            raise TradingException(
                f"订单状态为 {order.status.value}，无法撤销",
                order_id=order_id,
            )

        if self.gateway is not None:
            result = self.gateway.cancel(order_id)
            return result["order"]

        success = self.adapter.cancel_order(order_id)
        if not success:
            raise TradingException("撤单失败", order_id=order_id)

        order = self.adapter.get_order(order_id)
        return order.to_dict()
    
    def get_order(self, order_id: str) -> Dict[str, Any]:
        """业务模块说明。"""
        order = self.adapter.get_order(order_id)
        if not order:
            raise TradingException("订单不存在", order_id=order_id)
        return order.to_dict()
    
    def get_orders(
        self,
        stock_code: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """业务模块说明。"""
        order_status = OrderStatus(status) if status else None
        orders = self.adapter.get_orders(stock_code, order_status)
        return [o.to_dict() for o in orders]
    
    def get_quote(self, stock_code: str) -> Dict[str, Any]:
        """业务模块说明。"""
        quote = self.adapter.get_quote(stock_code)
        if not quote:
            raise TradingException("无法获取行情数据", stock_code=stock_code)
        return quote
    
    def execute_signal(
        self,
        stock_code: str,
        signal_type: str,
        signal_strength: float,
        price: float,
        position_ratio: float = 0.1,
    ) -> Optional[Dict[str, Any]]:
        """业务模块说明。"""
        if not self._auto_trade_enabled:
            logger.info(f"Auto trade disabled, signal ignored: {signal_type}")
            return None
        
        account = self.adapter.get_account()
        if not account:
            return None
        
        is_buy = signal_type.startswith("BUY")
        
        if is_buy:
            # 计算买入数量
            available = float(account.available_cash)
            buy_amount = available * position_ratio * signal_strength
            quantity = int(buy_amount / price / 100) * 100  # 100股整数倍
            
            if quantity >= 100:
                return self.buy(
                    stock_code=stock_code,
                    quantity=quantity,
                    price=price,
                    order_type="limit",
                    signal_type=signal_type,
                    signal_strength=signal_strength,
                )
        else:
            # 卖出
            position = self.adapter.get_position(stock_code)
            if position and position.available_quantity > 0:
                # 根据信号强度决定卖出比例
                sell_quantity = int(position.available_quantity * signal_strength / 100) * 100
                if sell_quantity >= 100:
                    return self.sell(
                        stock_code=stock_code,
                        quantity=sell_quantity,
                        price=price,
                        order_type="limit",
                        signal_type=signal_type,
                        signal_strength=signal_strength,
                    )
        
        return None
    
    def enable_auto_trade(self, enabled: bool = True) -> None:
        """业务模块说明。"""
        self._auto_trade_enabled = enabled
        logger.info(f"Auto trade {'enabled' if enabled else 'disabled'}")

    # ------------------------------------------------------------------
    # 组合风险预算
    # ------------------------------------------------------------------
    def _gateway(self) -> TradingRiskGateway:
        if self.gateway is None:
            self.connect("simulation")
        assert self.gateway is not None
        return self.gateway

    def configure_risk_limits(self, limits: RuleLimits) -> None:
        """设置下版规则的额度参数（连接前设置亦可，连接时生效）。"""
        self._risk_limits = limits
        if self._risk_engine is not None and self.gateway is not None:
            self._risk_engine.publish_rules(self.portfolio_id, limits)

    def publish_rules(self, limits: RuleLimits, note: str = "") -> int:
        """发布新版风险规则，返回新版本号。不影响在途订单冻结的旧版本。"""
        gateway = self._gateway()
        rule = gateway.engine.publish_rules(self.portfolio_id, limits, note=note)
        self._risk_limits = limits
        return rule.version

    def register_instrument(self, info: InstrumentInfo) -> None:
        """登记证券主数据（行业 / 品种），决定订单归入哪个额度桶。"""
        self._gateway().register_instrument(info)

    def grant_exemption(
        self,
        dimension: str,
        extra_amount: float,
        valid_minutes: int,
        bucket: Optional[str] = None,
        order_id: Optional[str] = None,
        reason: str = "",
    ) -> str:
        """授予临时豁免，返回豁免 ID。"""
        from datetime import timedelta

        gateway = self._gateway()
        now = datetime.now()
        exemption = gateway.engine.grant_exemption(
            portfolio_id=self.portfolio_id,
            dimension=Dimension(dimension),
            extra_amount=Decimal(str(extra_amount)),
            valid_from=now,
            valid_until=now + timedelta(minutes=valid_minutes),
            bucket=bucket,
            order_id=order_id,
            reason=reason,
        )
        return exemption.exemption_id

    def risk_exposure(self, stock_code: Optional[str] = None) -> Dict[str, Any]:
        """当前组合各维度剩余额度的可解释全景。"""
        gateway = self._gateway()
        if stock_code:
            gateway.refresh_quote(stock_code)
        return gateway.exposure()

    def risk_open_orders(self) -> List[Dict[str, Any]]:
        """仍占用预算的待成交订单。"""
        return self._gateway().open_orders()

    def risk_audit(
        self, order_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """放行/拒绝/成交/释放的完整审计链（含规则版本与试算快照）。"""
        return self._gateway().decisions(order_id)

    def risk_rules(self) -> Dict[str, Any]:
        """当前规则版本及其内容。"""
        gateway = self._gateway()
        rule = gateway.engine.current_rules(self.portfolio_id)
        return {
            "portfolio_id": rule.portfolio_id,
            "version": rule.version,
            "published_by": rule.published_by,
            "effective_at": rule.effective_at.isoformat(),
            "note": rule.note,
            "limits": rule.limits.to_dict(),
        }
    
    def check_stop_loss_take_profit(self) -> List[Dict[str, Any]]:
        """业务模块说明。"""
        results = []
        positions = self.adapter.get_positions()
        
        for pos in positions:
            if self.risk_manager.check_stop_loss(pos):
                # 触发止损
                quote = self.adapter.get_quote(pos.stock_code)
                if quote:
                    try:
                        result = self.sell(
                            stock_code=pos.stock_code,
                            quantity=pos.available_quantity,
                            price=quote["bid_price_1"],
                            order_type="limit",
                            signal_type="STOP_LOSS",
                        )
                        result["trigger"] = "stop_loss"
                        results.append(result)
                    except TradingException as e:
                        logger.error(f"Stop loss failed: {e}")
            
            elif self.risk_manager.check_take_profit(pos):
                # 触发止盈
                quote = self.adapter.get_quote(pos.stock_code)
                if quote:
                    try:
                        result = self.sell(
                            stock_code=pos.stock_code,
                            quantity=pos.available_quantity,
                            price=quote["bid_price_1"],
                            order_type="limit",
                            signal_type="TAKE_PROFIT",
                        )
                        result["trigger"] = "take_profit"
                        results.append(result)
                    except TradingException as e:
                        logger.error(f"Take profit failed: {e}")
        
        return results
