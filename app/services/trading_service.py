"""业务模块说明。"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

from app.trading.base import (
    TradingAdapter,
    Order,
    OrderStatus,
    OrderType,
    OrderSide,
    Position,
    Account,
    RiskManager,
)
from app.trading.simulation_adapter import SimulationAdapter
from app.trading.vnpy_adapter import VnpyAdapter
from app.services.analysis_service import AnalysisService
from app.middleware.exception_handler import AppException
from app.risk.service import PortfolioRiskService
from app.risk.store import RiskStore

logger = logging.getLogger(__name__)


class TradingException(AppException):
    """业务模块说明。"""
    
    def __init__(
        self,
        message: str,
        order_id: Optional[str] = None,
        stock_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            message=message,
            code="TRADING_ERROR",
            status_code=400,
            details={
                "order_id": order_id,
                "stock_code": stock_code,
                **(details or {}),
            },
        )


class TradingService:
    """业务模块说明。"""
    
    def __init__(
        self,
        adapter: Optional[TradingAdapter] = None,
        risk_store: Optional[RiskStore] = None,
        portfolio_id: str = "default",
    ):
        self.adapter = adapter or SimulationAdapter()
        self.risk_manager = RiskManager()
        self.analysis_service = AnalysisService()
        self._auto_trade_enabled = False
        # 组合级风险预算服务：默认进程内内存存储，可注入持久化 RiskStore
        self.portfolio_id = portfolio_id
        self.portfolio_risk = PortfolioRiskService(
            store=risk_store or RiskStore.in_memory(portfolio_id),
            portfolio_id=portfolio_id,
            position_provider=lambda: self.adapter.get_positions(),
            quote_provider=lambda code: self._safe_quote(code),
        )
        self._register_risk_callback(self.adapter)

    def _register_risk_callback(self, adapter: TradingAdapter) -> None:
        """订单状态变化（挂单/部分成交/全成/撤销）驱动风控台账迁移。"""
        adapter.register_callback("on_order", self._handle_risk_order_event)

    def _handle_risk_order_event(self, order: Order) -> None:
        try:
            self.portfolio_risk.on_order_update(order)
        except Exception:
            logger.exception("同步订单 %s 状态到风控台账失败", getattr(order, "order_id", "?"))

    def _safe_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        try:
            return self.adapter.get_quote(stock_code)
        except Exception:
            return None

    def connect(self, adapter_type: str = "simulation", config: Optional[Dict] = None) -> bool:
        """业务模块说明。"""
        if adapter_type == "vnpy":
            self.adapter = VnpyAdapter(config or {})
        else:
            self.adapter = SimulationAdapter(config)

        self._register_risk_callback(self.adapter)
        return self.adapter.connect()
    
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
        
        order = Order(
            order_id=self.adapter._generate_order_id(),
            stock_code=stock_code,
            side=OrderSide.BUY,
            order_type=ot,
            quantity=quantity,
            price=Decimal(str(price)) if price else None,
            signal_type=signal_type,
            signal_strength=signal_strength,
        )

        # 组合级风控：在串行事务内完成"检查剩余额度 + 预留"。
        # 预算不足直接抛 RiskRejectedException（含逐条可解释原因），
        # 事务回滚，不产生预留。
        decision = self.portfolio_risk.check_and_reserve(order)

        # 预留已提交：调用柜台；柜台拒绝/异常时必须回滚释放预留
        try:
            result = self.adapter.place_order(order)
        except Exception:
            failed = self._mark_failed(order, "柜台调用异常")
            self.portfolio_risk.on_order_update(failed)
            raise

        if result.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
            # 回滚边界：预留释放，并留 RELEASE 审计
            self.portfolio_risk.on_order_update(result)
            raise TradingException(
                f"下单失败: {result.error_message}",
                order_id=result.order_id,
                stock_code=stock_code,
            )

        # 挂单/部分成交/全成：以柜台返回状态同步台账（回调也会驱动，幂等）
        self.portfolio_risk.on_order_update(result)

        # 记录交易金额（兼容旧口径）
        if result.status == OrderStatus.FILLED:
            self.risk_manager.record_trade(result.filled_price * result.filled_quantity)

        response = result.to_dict()
        response["risk_decision_id"] = decision.decision_id
        response["risk_rule_version"] = decision.rule_version
        return response

    @staticmethod
    def _mark_failed(order: Order, message: str) -> Order:
        order.status = OrderStatus.FAILED
        order.error_message = message
        return order
    
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
        
        order = Order(
            order_id=self.adapter._generate_order_id(),
            stock_code=stock_code,
            side=OrderSide.SELL,
            order_type=ot,
            quantity=quantity,
            price=Decimal(str(price)) if price else None,
            signal_type=signal_type,
            signal_strength=signal_strength,
        )
        
        # 执行下单
        result = self.adapter.place_order(order)
        
        if result.status in (OrderStatus.REJECTED, OrderStatus.FAILED):
            raise TradingException(
                f"下单失败: {result.error_message}",
                order_id=result.order_id,
                stock_code=stock_code,
            )
        
        return result.to_dict()
    
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """业务模块说明。"""
        order = self.adapter.get_order(order_id)
        if not order:
            raise TradingException("订单不存在", order_id=order_id)
        
        if order.status not in (
            OrderStatus.PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIAL_FILLED,
        ):
            raise TradingException(
                f"订单状态为 {order.status.value}，无法撤销",
                order_id=order_id,
            )
        
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
