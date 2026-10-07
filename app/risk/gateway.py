"""交易网关：把交易适配器与组合风险引擎接成一条事务链。

下单链路（``submit``）：

1. 从适配器同步账户资金、持仓与行情，使引擎看到组合现状；
2. ``engine.approve`` —— 评估、预留、审计在引擎的同一把组合锁与
   存储事务内原子完成；拒绝时抛 ``RiskBudgetExceeded``，订单不会
   到达柜台；
3. 调用适配器 ``place_order``；
4. 按柜台回执推进预留：
   * 立即成交   -> ``report_fill``（预留转为持仓）；
   * 已报待成交 -> 保持 HELD，后续成交/撤销由回调或显式调用推进；
   * 柜台拒单   -> ``report_downstream_rejection``（预留全额回滚）。

并发：适配器回调可能在柜台线程触发。所有状态迁移都经过引擎的组合
锁，submit 与回调之间不会交叉记账。
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.risk.engine import PortfolioRiskEngine, RiskRejected
from app.risk.models import (
    ExposureReport,
    InstrumentInfo,
    OrderRequest,
    ReservationStatus,
    RiskDecision,
    RuleLimits,
    Side,
)
from app.risk.store import RiskStore
from app.trading.base import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingAdapter,
)

logger = logging.getLogger(__name__)

# 适配器订单状态 -> 引擎迁移时关心的类别
_TERMINAL_DOWNSTREAM = {OrderStatus.REJECTED, OrderStatus.FAILED}
_OPEN_DOWNSTREAM = {OrderStatus.PENDING, OrderStatus.SUBMITTED}
_FILL_DOWNSTREAM = {OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED}
_CANCELLED_DOWNSTREAM = {OrderStatus.CANCELLED}


class RiskBudgetExceeded(Exception):
    """订单被组合风险预算拒绝。"""

    def __init__(self, report: ExposureReport, decision: RiskDecision):
        self.report = report
        self.decision = decision
        super().__init__(decision.reason)


class TradingRiskGateway:
    """连接交易适配器与组合风险引擎的网关。"""

    def __init__(
        self,
        adapter: TradingAdapter,
        portfolio_id: str = "default",
        engine: Optional[PortfolioRiskEngine] = None,
        store: Optional[RiskStore] = None,
        initial_limits: Optional[RuleLimits] = None,
        initial_cash: Optional[Decimal] = None,
    ) -> None:
        self.adapter = adapter
        self.portfolio_id = portfolio_id
        self.engine = engine or PortfolioRiskEngine(
            store=store,
            initial_cash=initial_cash if initial_cash is not None else Decimal("0"),
        )
        self._limits = initial_limits
        self._initialized = False
        # 网关串行锁：把“对账→风险批准→柜台下单”连成一个原子区段。
        # 引擎锁只保护预算账本，柜台适配器通常不保证线程安全，因此
        # 并发下单必须在网关层也串行，避免同一份柜台状态被交叉修改。
        self._submit_lock = threading.RLock()
        adapter.register_callback("on_order", self._on_order_event)

    # ------------------------------------------------------------------
    # 初始化 / 对账
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """幂等初始化：建组合、发首版规则，并把适配器现状对账进引擎。"""
        if self._initialized:
            return
        self.engine.ensure_portfolio(
            self.portfolio_id, limits=self._limits or RuleLimits()
        )
        self.reconcile()
        self._initialized = True

    def reconcile(self) -> None:
        """从适配器全量同步资金、持仓、行情（可定时调用做对账）。

        口径约定：引擎自行统计在途买单的资金占用，因此从券商取数时要
        把券商侧对在途单的冻结资金加回（``available_cash + frozen_cash``），
        避免双重扣减；持仓同理使用 ``quantity``（含在途卖出冻结），
        可交割校验由引擎的 POSITION 桶完成。
        """
        account = self.adapter.get_account()
        if account is not None:
            self.engine.sync_cash(
                self.portfolio_id,
                account.available_cash + account.frozen_cash,
            )
        for pos in self.adapter.get_positions():
            self.engine.sync_holding(
                self.portfolio_id,
                pos.stock_code,
                pos.quantity,
                pos.avg_cost,
                pos.current_price,
            )

    def register_instrument(self, info: InstrumentInfo) -> None:
        self.engine.register_instrument(info)

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------
    def refresh_quote(self, stock_code: str) -> ExposureReport:
        """拉取最新行情并触发剩余额度重估。"""
        quote = self.adapter.get_quote(stock_code)
        if not quote:
            raise ValueError(f"无法获取行情: {stock_code}")
        return self.engine.update_quote(
            self.portfolio_id, stock_code, Decimal(str(quote["last_price"]))
        )

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------
    def submit(
        self,
        order_id: str,
        stock_code: str,
        side: Side,
        quantity: int,
        limit_price: Optional[Decimal] = None,
        order_extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """组合风控放行后下单（网关级串行，详见 ``_submit_locked``）。"""
        with self._submit_lock:
            return self._submit_locked(
                order_id, stock_code, side, quantity, limit_price, order_extra
            )

    def _submit_locked(
        self,
        order_id: str,
        stock_code: str,
        side: Side,
        quantity: int,
        limit_price: Optional[Decimal],
        order_extra: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """组合风控放行后下单，返回含预留、决策与适配器订单的结果。"""
        self.initialize()
        # 适配器是账户/持仓的事实来源：每次评估前全量对账，确保引擎
        # 看到的资金、持仓与柜台一致（含手续费造成的差异）。
        self.reconcile()
        # 仅市价单需要实时行情计价；限价单按委托价格冻结，避免拉到
        # 适配器的占位行情污染在途计价。
        if limit_price is None:
            quote = self.adapter.get_quote(stock_code)
            if not quote:
                raise ValueError(f"市价单缺少行情，无法计价: {stock_code}")
            self.engine.update_quote(
                self.portfolio_id,
                stock_code,
                Decimal(str(quote["last_price"])),
            )

        request = OrderRequest(
            order_id=order_id,
            portfolio_id=self.portfolio_id,
            stock_code=stock_code,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
        )
        try:
            reservation, decision = self.engine.approve(request)
        except RiskRejected as rejected:
            raise RiskBudgetExceeded(rejected.report, rejected.decision) from rejected

        # 放行后交给柜台；任何异常都回滚预留，避免额度泄漏
        try:
            adapter_order = self._place_adapter_order(
                order_id, stock_code, side, quantity, limit_price, order_extra
            )
        except Exception:
            logger.exception("适配器下单异常，回滚风险预留: %s", order_id)
            self.engine.report_downstream_rejection(order_id, reason="适配器异常")
            raise

        # 同步回执的模拟适配器可能已经在回调里推进过预留，这里幂等补齐
        self._advance_from_adapter_order(adapter_order)

        final = self.adapter.get_order(order_id) or adapter_order
        reservation_now = self.engine.get_reservation(order_id)
        return {
            "order": final.to_dict() if isinstance(final, Order) else final,
            "reservation": reservation_now.to_dict() if reservation_now else None,
            "approval": decision.to_dict(),
        }

    def _place_adapter_order(
        self,
        order_id: str,
        stock_code: str,
        side: Side,
        quantity: int,
        limit_price: Optional[Decimal],
        order_extra: Optional[Dict[str, Any]] = None,
    ) -> Order:
        fields: Dict[str, Any] = dict(order_extra or {})
        fields.pop("order_id", None)
        order = Order(
            order_id=order_id,
            stock_code=stock_code,
            side=OrderSide(side.value),
            order_type=OrderType.LIMIT if limit_price is not None else OrderType.MARKET,
            quantity=quantity,
            price=limit_price,
            **fields,
        )
        return self.adapter.place_order(order)

    def _advance_from_adapter_order(self, order: Order) -> None:
        """按适配器订单回执把预留推进到正确状态（幂等）。"""
        reservation = self.engine.get_reservation(order.order_id)
        if reservation is None:
            return

        if order.status in _OPEN_DOWNSTREAM:
            return

        # 回调可能已经把预留推进过：只对仍在途的预留做迁移，保证幂等
        if reservation.status not in (
            ReservationStatus.HELD,
            ReservationStatus.PARTIAL,
        ):
            return

        if order.status in _FILL_DOWNSTREAM:
            delta = order.filled_quantity - reservation.filled_quantity
            if delta > 0:
                self.engine.report_fill(
                    order.order_id,
                    delta,
                    fill_price=order.filled_price,
                )
            return

        if order.status in _TERMINAL_DOWNSTREAM:
            self.engine.report_downstream_rejection(
                order.order_id,
                reason=order.error_message or "柜台拒单",
            )
            return

        if order.status in _CANCELLED_DOWNSTREAM:
            # 柜台主动撤销（或撤单回执）：剩余占用回滚，已成交部分保留
            self._release_if_open(order.order_id, reason="订单已撤销")

    def _release_if_open(self, order_id: str, reason: str) -> Optional[RiskDecision]:
        """幂等释放：只有仍处于在途状态的预留才迁移，返回释放决策。"""
        reservation = self.engine.get_reservation(order_id)
        if reservation is None:
            return None
        if reservation.status in (ReservationStatus.HELD, ReservationStatus.PARTIAL):
            _, decision = self.engine.cancel(order_id, reason=reason)
            return decision
        return None

    # ------------------------------------------------------------------
    # 撤单
    # ------------------------------------------------------------------
    def cancel(self, order_id: str, reason: str = "用户撤单") -> Dict[str, Any]:
        """先撤柜台单，成功后释放剩余预留；柜台失败则预留不动。

        适配器 ``cancel_order`` 的回调可能已同步释放过预留，因此释放
        走幂等路径，不因重复释放报错。
        """
        with self._submit_lock:
            return self._cancel_locked(order_id, reason)

    def _cancel_locked(self, order_id: str, reason: str) -> Dict[str, Any]:
        self.initialize()
        reservation = self.engine.get_reservation(order_id)
        if reservation is None:
            raise ValueError(f"风控无此订单: {order_id}")

        if reservation.open_quantity > 0:
            success = self.adapter.cancel_order(order_id)
            if not success:
                raise RuntimeError(f"柜台撤单失败: {order_id}")
            # cancel_order 的回调可能已同步释放；未释放则在这里释放
            decision = self._release_if_open(order_id, reason=reason)
            if decision is None:
                # 释放已发生在回调里：取回该订单最近一条释放类审计
                for d in reversed(self.engine.decisions(self.portfolio_id, order_id)):
                    if d.action in ("RELEASE", "EXPIRE", "REJECT_DOWNSTREAM"):
                        decision = d
                        break
        else:
            decision = None

        reservation = self.engine.get_reservation(order_id)
        order = self.adapter.get_order(order_id)
        return {
            "order": order.to_dict() if order else None,
            "reservation": reservation.to_dict() if reservation else None,
            "decision": decision.to_dict() if decision else None,
        }

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def exposure(self) -> Dict[str, Any]:
        self.initialize()
        return self.engine.exposure(self.portfolio_id).to_dict()

    def open_orders(self) -> List[Dict[str, Any]]:
        self.initialize()
        return [r.to_dict() for r in self.engine.open_reservations(self.portfolio_id)]

    def decisions(self, order_id: Optional[str] = None) -> List[Dict[str, Any]]:
        self.initialize()
        return [
            d.to_dict()
            for d in self.engine.decisions(self.portfolio_id, order_id)
        ]

    # ------------------------------------------------------------------
    # 适配器回调：异步成交 / 撤单 / 拒单推进
    # ------------------------------------------------------------------
    def _on_order_event(self, order: Order) -> None:
        try:
            self._advance_from_adapter_order(order)
        except Exception:
            logger.exception("处理订单回调失败: %s", getattr(order, "order_id", "?"))
