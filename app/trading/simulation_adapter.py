"""模拟交易适配器。

在原有即时撮合基础上补齐了组合风控所需的语义：
- 下单受理即冻结资金（买单）/可用持仓（卖单），待成交期间不会被重复占用；
- 限价单不满足成交价时保持 SUBMITTED 挂单，之后可由行情变化触发撮合，
  或通过 match_order 模拟部分成交；
- 撤单释放冻结；成交把冻结转为实际资金扣减/持仓变更；
- 订单状态变化（挂单/部分成交/全成/撤销）通过 on_order 回调对外广播，
  组合风控服务据此迁移额度预留。
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from app.trading.base import (
    TradingAdapter,
    Order,
    OrderStatus,
    OrderType,
    OrderSide,
    Position,
    Account,
)

logger = logging.getLogger(__name__)


class SimulationAdapter(TradingAdapter):
    """业务模块说明。"""

    def __init__(self, config: Optional[Dict] = None):
        super().__init__(config or {})

        # 初始资金
        initial_cash = Decimal(str(config.get("initial_cash", 1000000))) if config else Decimal("1000000")

        self._account = Account(
            account_id="SIM_" + datetime.now().strftime("%Y%m%d%H%M%S"),
            broker="模拟交易",
            total_assets=initial_cash,
            available_cash=initial_cash,
            frozen_cash=Decimal("0"),
            market_value=Decimal("0"),
            profit_loss=Decimal("0"),
            profit_loss_ratio=0.0,
        )

        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}
        # 卖单冻结的持仓数量
        self._frozen_position_qty: Dict[str, int] = {}

        # 交易成本配置
        self.commission_rate = Decimal(str(config.get("commission_rate", 0.0003))) if config else Decimal("0.0003")
        self.min_commission = Decimal(str(config.get("min_commission", 5))) if config else Decimal("5")
        self.stamp_tax_rate = Decimal(str(config.get("stamp_tax_rate", 0.001))) if config else Decimal("0.001")
        self.slippage_rate = config.get("slippage_rate", 0.001) if config else 0.001
        self.default_quote_price = Decimal(
            str(config.get("default_quote_price", 10)) if config else "10"
        )

        # 模拟行情
        self._quotes: Dict[str, Dict] = {}

    def connect(self) -> bool:
        """业务模块说明。"""
        self._connected = True
        logger.info("Simulation adapter connected")
        return True

    def disconnect(self) -> None:
        """业务模块说明。"""
        self._connected = False
        logger.info("Simulation adapter disconnected")

    def get_account(self) -> Optional[Account]:
        """业务模块说明。"""
        return self._account

    def get_positions(self) -> List[Position]:
        """业务模块说明。"""
        return list(self._positions.values())

    def get_position(self, stock_code: str) -> Optional[Position]:
        """业务模块说明。"""
        return self._positions.get(stock_code)

    # ------------------------------------------------------------------
    # 下单 / 撤单
    # ------------------------------------------------------------------

    def place_order(self, order: Order) -> Order:
        """业务模块说明。"""
        if not self._connected:
            order.status = OrderStatus.FAILED
            order.error_message = "交易连接已断开"
            return order

        # 获取行情
        quote = self.get_quote(order.stock_code)
        if not quote:
            order.status = OrderStatus.REJECTED
            order.error_message = "无法获取行情数据"
            return order

        current_price = Decimal(str(quote["last_price"]))
        valuation_price = order.price or current_price

        # 受理时的可成交性检查并冻结资源（挂单期间持续占用）
        if order.side == OrderSide.BUY:
            required_amount = valuation_price * order.quantity
            if required_amount > self._account.available_cash:
                order.status = OrderStatus.REJECTED
                order.error_message = f"可用资金不足，需要 {required_amount:.2f}，可用 {self._account.available_cash:.2f}"
                return order
            self._account.available_cash -= required_amount
            self._account.frozen_cash += required_amount
            order.reserved_price = valuation_price
        else:
            position = self._positions.get(order.stock_code)
            frozen = self._frozen_position_qty.get(order.stock_code, 0)
            available_now = (position.available_quantity - frozen) if position else 0
            if not position or available_now < order.quantity:
                order.status = OrderStatus.REJECTED
                order.error_message = f"可用持仓不足，需要 {order.quantity}，可用 {available_now}"
                return order
            self._frozen_position_qty[order.stock_code] = frozen + order.quantity

        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.now()
        self._orders[order.order_id] = order

        # 尝试即时撮合；不满足条件则保持挂单等待行情或人工撮合
        self._try_fill_order(order, current_price)

        self._emit("on_order", order)
        return order

    def cancel_order(self, order_id: str) -> bool:
        """业务模块说明。"""
        order = self._orders.get(order_id)
        if not order:
            return False

        if order.status in (OrderStatus.SUBMITTED, OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED):
            unfilled = order.quantity - order.filled_quantity
            self._release_frozen(order, unfilled)
            # 卖单撤单后恢复可用持仓数量
            if order.side == OrderSide.SELL:
                position = self._positions.get(order.stock_code)
                if position is not None:
                    position.available_quantity = (
                        position.quantity - self._frozen_position_qty.get(order.stock_code, 0)
                    )
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.now()
            self._emit("on_order", order)
            return True

        return False

    def _release_frozen(self, order: Order, quantity: int) -> None:
        """释放未成交部分在受理时冻结的资金/持仓。"""
        if quantity <= 0:
            return
        if order.side == OrderSide.BUY:
            amount = (order.reserved_price or order.price or self.default_quote_price) * quantity
            self._account.frozen_cash -= amount
            self._account.available_cash += amount
        else:
            frozen = self._frozen_position_qty.get(order.stock_code, 0)
            self._frozen_position_qty[order.stock_code] = max(frozen - quantity, 0)

    # ------------------------------------------------------------------
    # 撮合
    # ------------------------------------------------------------------

    def _try_fill_order(self, order: Order, current_price: Decimal) -> None:
        """按价格条件决定是否即时成交（满足即全成；部分成交走 match_order）。"""
        fill_price = None

        if order.order_type == OrderType.MARKET:
            # 市价单立即成交，加入滑点
            slippage = current_price * Decimal(str(self.slippage_rate))
            if order.side == OrderSide.BUY:
                fill_price = current_price + slippage
            else:
                fill_price = current_price - slippage

        elif order.order_type == OrderType.LIMIT:
            # 限价单检查是否可成交
            if order.side == OrderSide.BUY:
                if current_price <= order.price:
                    fill_price = order.price
            else:
                if current_price >= order.price:
                    fill_price = order.price

        if fill_price:
            self._execute_fill(order, order.quantity - order.filled_quantity, fill_price)

    def match_order(
        self,
        order_id: str,
        fill_quantity: Optional[int] = None,
        fill_price: Optional[float] = None,
    ) -> Optional[Order]:
        """模拟外部撮合：对挂单做（部分）成交，驱动订单状态变化。

        - fill_quantity 缺省表示全部剩余数量；
        - fill_price 缺省使用委托价（限价单）或最新价（市价单）。
        """
        order = self._orders.get(order_id)
        if order is None or order.status not in (
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIAL_FILLED,
        ):
            return None
        remaining = order.quantity - order.filled_quantity
        qty = fill_quantity or remaining
        qty = min(qty, remaining)
        if qty <= 0:
            return None
        if fill_price is None:
            price = order.price or Decimal(str(self.get_quote(order.stock_code)["last_price"]))
        else:
            price = Decimal(str(fill_price))
        self._execute_fill(order, qty, price)
        self._emit("on_order", order)
        return order

    def _execute_fill(self, order: Order, fill_quantity: int, fill_price: Decimal) -> None:
        """成交（可能是整笔或部分），逐笔更新冻结、持仓与账户。"""
        previous_filled = order.filled_quantity
        total_filled = previous_filled + fill_quantity

        # 加权平均成交价
        if previous_filled > 0 and order.filled_price is not None:
            avg_price = (
                order.filled_price * previous_filled + fill_price * fill_quantity
            ) / total_filled
        else:
            avg_price = fill_price

        order.filled_quantity = total_filled
        order.filled_price = avg_price
        order.status = (
            OrderStatus.FILLED if total_filled >= order.quantity else OrderStatus.PARTIAL_FILLED
        )
        order.updated_at = datetime.now()

        # 手续费与税费（按本笔成交金额）
        trade_amount = fill_price * fill_quantity
        commission = max(trade_amount * self.commission_rate, self.min_commission)
        if order.side == OrderSide.SELL:
            commission += trade_amount * self.stamp_tax_rate
        order.commission += commission

        # 资金/冻结结算：按受理预留价解冻，按实际成交价多退少补
        if order.side == OrderSide.BUY:
            reserved_amount = (order.reserved_price or fill_price) * fill_quantity
            self._account.frozen_cash -= reserved_amount
            self._account.available_cash -= trade_amount - reserved_amount
            self._account.available_cash -= commission
        else:
            frozen = self._frozen_position_qty.get(order.stock_code, 0)
            self._frozen_position_qty[order.stock_code] = max(frozen - fill_quantity, 0)

        self._update_position(order, fill_quantity, fill_price)
        self._update_account(order, fill_quantity, fill_price, commission)

        self._emit("on_trade", order)
        logger.info(
            f"Order filled: {order.order_id} {order.side.value} "
            f"{order.stock_code} {fill_quantity}@{fill_price} "
            f"({order.filled_quantity}/{order.quantity})"
        )

    def _update_position(self, order: Order, fill_quantity: int, fill_price: Decimal) -> None:
        """业务模块说明。"""
        stock_code = order.stock_code

        if order.side == OrderSide.BUY:
            if stock_code in self._positions:
                pos = self._positions[stock_code]
                total_cost = pos.avg_cost * pos.quantity + fill_price * fill_quantity
                new_qty = pos.quantity + fill_quantity
                pos.avg_cost = total_cost / new_qty
                pos.quantity = new_qty
                pos.available_quantity = new_qty - self._frozen_position_qty.get(stock_code, 0)
            else:
                self._positions[stock_code] = Position(
                    stock_code=stock_code,
                    stock_name=stock_code,
                    quantity=fill_quantity,
                    available_quantity=fill_quantity,
                    avg_cost=fill_price,
                    current_price=fill_price,
                    market_value=fill_price * fill_quantity,
                    profit_loss=Decimal("0"),
                    profit_loss_ratio=0.0,
                )
        else:
            pos = self._positions[stock_code]
            pos.quantity -= fill_quantity
            pos.available_quantity = pos.quantity - self._frozen_position_qty.get(stock_code, 0)

            if pos.quantity <= 0:
                del self._positions[stock_code]
                self._frozen_position_qty.pop(stock_code, None)

        # 更新持仓市值和盈亏
        for pos in self._positions.values():
            pos.market_value = pos.current_price * pos.quantity
            if pos.avg_cost > 0:
                pos.profit_loss = (pos.current_price - pos.avg_cost) * pos.quantity
                pos.profit_loss_ratio = float((pos.current_price - pos.avg_cost) / pos.avg_cost)
            pos.updated_at = datetime.now()

    def _update_account(
        self, order: Order, fill_quantity: int, fill_price: Decimal, commission: Decimal
    ) -> None:
        """业务模块说明。"""
        trade_amount = fill_price * fill_quantity

        if order.side == OrderSide.SELL:
            self._account.available_cash += trade_amount - commission

        self._account.market_value = sum(p.market_value for p in self._positions.values())
        self._account.total_assets = (
            self._account.available_cash + self._account.frozen_cash + self._account.market_value
        )
        self._account.profit_loss = sum(p.profit_loss for p in self._positions.values())

        cost_basis = self._account.total_assets - self._account.profit_loss
        if cost_basis > 0:
            self._account.profit_loss_ratio = float(
                self._account.profit_loss / cost_basis
            )

        self._account.updated_at = datetime.now()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_order(self, order_id: str) -> Optional[Order]:
        """业务模块说明。"""
        return self._orders.get(order_id)

    def get_orders(
        self,
        stock_code: Optional[str] = None,
        status: Optional[OrderStatus] = None,
    ) -> List[Order]:
        """业务模块说明。"""
        orders = list(self._orders.values())

        if stock_code:
            orders = [o for o in orders if o.stock_code == stock_code]

        if status:
            orders = [o for o in orders if o.status == status]

        return sorted(orders, key=lambda o: o.created_at, reverse=True)

    def get_pending_orders(self, stock_code: Optional[str] = None) -> List[Order]:
        """待成交（含部分成交）订单。"""
        return [
            o for o in self._orders.values()
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED, OrderStatus.PENDING)
            and (stock_code is None or o.stock_code == stock_code)
        ]

    def get_quote(self, stock_code: str) -> Optional[Dict]:
        """业务模块说明。"""
        if stock_code not in self._quotes:
            self.set_quote(stock_code, float(self.default_quote_price))

        quote = self._quotes[stock_code]
        quote["bid_price_1"] = quote["last_price"] * 0.999
        quote["ask_price_1"] = quote["last_price"] * 1.001
        quote["datetime"] = datetime.now().isoformat()

        # 更新持仓当前价格
        if stock_code in self._positions:
            self._positions[stock_code].current_price = Decimal(str(quote["last_price"]))
            self._update_position_pnl(stock_code)

        return quote

    def _update_position_pnl(self, stock_code: str) -> None:
        """业务模块说明。"""
        pos = self._positions.get(stock_code)
        if pos:
            pos.market_value = pos.current_price * pos.quantity
            if pos.avg_cost > 0:
                pos.profit_loss = (pos.current_price - pos.avg_cost) * pos.quantity
                pos.profit_loss_ratio = float((pos.current_price - pos.avg_cost) / pos.avg_cost)
            pos.updated_at = datetime.now()

    def set_quote(self, stock_code: str, price: float) -> None:
        """设置行情并尝试驱动挂单撮合。"""
        self._quotes[stock_code] = {
            "stock_code": stock_code,
            "last_price": price,
            "open": price,
            "high": price * 1.02,
            "low": price * 0.98,
            "close": price,
            "volume": 1000000,
            "bid_price_1": price * 0.999,
            "ask_price_1": price * 1.001,
            "bid_volume_1": 1000,
            "ask_volume_1": 1000,
            "datetime": datetime.now().isoformat(),
        }

        # 更新持仓估值
        if stock_code in self._positions:
            self._positions[stock_code].current_price = Decimal(str(price))
            self._update_position_pnl(stock_code)

        # 行情变化触发挂单撮合（复制列表避免撮合中变更）
        current_price = Decimal(str(price))
        resting = [
            o for o in list(self._orders.values())
            if o.stock_code == stock_code
            and o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED)
        ]
        for order in resting:
            prior_status = order.status
            prior_filled = order.filled_quantity
            self._try_fill_order(order, current_price)
            if order.status != prior_status or order.filled_quantity != prior_filled:
                self._emit("on_order", order)
