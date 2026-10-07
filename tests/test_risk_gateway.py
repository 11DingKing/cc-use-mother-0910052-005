"""交易风险网关测试。

用一个可控的假适配器模拟：立即成交、挂在途、柜台拒单、抛异常、
异步成交回报等场景，验证风控预留与柜台状态机的衔接。
"""

from decimal import Decimal

import pytest

from app.risk.gateway import RiskBudgetExceeded, TradingRiskGateway
from app.risk.models import (
    InstrumentInfo,
    ReservationStatus,
    RuleLimits,
    Side,
)
from app.trading.base import (
    Account,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    TradingAdapter,
)


class FakeAdapter(TradingAdapter):
    """可编排回执的假柜台。"""

    def __init__(self, cash=Decimal("1000000")):
        super().__init__({})
        self._cash = cash
        self._orders = {}
        self._positions = {}
        self._quotes = {}
        self.mode = "fill"          # fill / hold / reject / explode
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False

    def get_account(self):
        return Account(
            account_id="FAKE", broker="fake",
            total_assets=self._cash, available_cash=self._cash,
            frozen_cash=Decimal("0"), market_value=Decimal("0"),
            profit_loss=Decimal("0"), profit_loss_ratio=0.0,
        )

    def get_positions(self):
        return list(self._positions.values())

    def get_position(self, stock_code):
        return self._positions.get(stock_code)

    def set_quote(self, code, price):
        self._quotes[code] = {"last_price": price}

    def get_quote(self, stock_code):
        return self._quotes.get(stock_code)

    def place_order(self, order: Order) -> Order:
        if self.mode == "explode":
            raise RuntimeError("柜台连接中断")
        if self.mode == "reject":
            order.status = OrderStatus.REJECTED
            order.error_message = "交易所拒绝"
            self._orders[order.order_id] = order
            self._emit("on_order", order)
            return order

        self._orders[order.order_id] = order
        if self.mode == "hold":
            order.status = OrderStatus.SUBMITTED
            self._emit("on_order", order)
            return order

        # fill
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.filled_price = order.price or Decimal("10")
        self._apply(order)
        self._emit("on_order", order)
        self._emit("on_trade", order)
        return order

    def _apply(self, order: Order):
        code = order.stock_code
        if order.side is OrderSide.BUY:
            pos = self._positions.get(code)
            if pos:
                total = pos.avg_cost * pos.quantity + order.filled_price * order.filled_quantity
                qty = pos.quantity + order.filled_quantity
                pos.quantity = qty
                pos.available_quantity = qty
                pos.avg_cost = total / qty
            else:
                self._positions[code] = Position(
                    stock_code=code, stock_name=code,
                    quantity=order.filled_quantity,
                    available_quantity=order.filled_quantity,
                    avg_cost=order.filled_price,
                    current_price=order.filled_price,
                    market_value=order.filled_price * order.filled_quantity,
                    profit_loss=Decimal("0"), profit_loss_ratio=0.0,
                )
            self._cash -= order.filled_price * order.filled_quantity
        else:
            pos = self._positions[code]
            pos.quantity -= order.filled_quantity
            pos.available_quantity = pos.quantity
            self._cash += order.filled_price * order.filled_quantity
            if pos.quantity == 0:
                del self._positions[code]

    def push_partial_fill(self, order_id, fill_qty, fill_price):
        """模拟柜台异步部分成交回报。"""
        order = self._orders[order_id]
        order.filled_quantity += fill_qty
        order.filled_price = fill_price
        order.status = (
            OrderStatus.FILLED if order.filled_quantity >= order.quantity
            else OrderStatus.PARTIAL_FILLED
        )
        self._apply_fill_only(fill_qty, fill_price, order)
        self._emit("on_order", order)
        self._emit("on_trade", order)

    def _apply_fill_only(self, qty, price, order):
        # 简化的账本变动（与 _apply 同方向）
        if order.side is OrderSide.BUY:
            self._cash -= price * qty
        else:
            self._cash += price * qty

    def cancel_order(self, order_id):
        order = self._orders.get(order_id)
        if order and order.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED,
                                      OrderStatus.PARTIAL_FILLED):
            order.status = OrderStatus.CANCELLED
            self._emit("on_order", order)
            return True
        return False

    def get_order(self, order_id):
        return self._orders.get(order_id)

    def get_orders(self, stock_code=None, status=None):
        return list(self._orders.values())


LIMITS = RuleLimits(
    max_total_long_amount=Decimal("300000"),
    max_single_order_amount=Decimal("200000"),
    default_industry_amount=Decimal("250000"),
)


@pytest.fixture
def gateway():
    adapter = FakeAdapter()
    adapter.connect()
    adapter.set_quote("000001", 100)
    gw = TradingRiskGateway(adapter, "P1", initial_limits=LIMITS)
    gw.initialize()
    gw.register_instrument(InstrumentInfo("000001", industry="BANK"))
    gw.register_instrument(InstrumentInfo("600000", industry="BANK"))
    return gw


class TestSubmitHappyPath:
    def test_filled_order_converts_reservation(self, gateway):
        result = gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        assert result["order"]["status"] == "filled"
        assert result["reservation"]["status"] == "filled"
        assert result["approval"]["action"] == "APPROVE"
        # 成交后不再占用在途额度
        assert gateway.open_orders() == []

    def test_order_carries_rule_version_for_audit(self, gateway):
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        decisions = gateway.decisions("O1")
        assert [d["action"] for d in decisions] == ["APPROVE", "FILL"]
        assert all(d["rule_version"] == 1 for d in decisions)


class TestBudgetRejection:
    def test_two_compliant_orders_exceed_total(self, gateway):
        gateway.submit("O1", "000001", Side.BUY, 2000, Decimal("100"))  # 20 万
        # 第二笔 20 万会使总敞口 40 万 > 30 万
        with pytest.raises(RiskBudgetExceeded) as exc:
            gateway.submit("O2", "600000", Side.BUY, 2000, Decimal("100"))
        assert exc.value.decision.action == "REJECT"
        dims = {v["dimension"] for v in exc.value.report.to_dict()["violations"]}
        assert "total" in dims
        # 被拒订单没有进入柜台，也没有占用额度
        assert gateway.adapter.get_order("O2") is None
        assert gateway.engine.get_reservation("O2") is None

    def test_rejection_report_has_limit_and_remaining(self, gateway):
        gateway.adapter.mode = "hold"
        gateway.submit("O1", "000001", Side.BUY, 2000, Decimal("100"))  # 在途 20 万
        with pytest.raises(RiskBudgetExceeded) as exc:
            gateway.submit("O2", "600000", Side.BUY, 2000, Decimal("100"))
        total = next(
            v for v in exc.value.report.to_dict()["violations"]
            if v["dimension"] == "total"
        )
        assert total["base_limit"] == "300000.00"
        assert total["pending"] == "200000.00"
        assert total["incoming"] == "200000.00"
        assert total["projected"] == "400000.00"

    def test_pending_orders_accumulate_then_block(self, gateway):
        gateway.adapter.mode = "hold"
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))  # 在途 10 万
        gateway.submit("O2", "600000", Side.BUY, 1000, Decimal("100"))  # 在途 20 万
        with pytest.raises(RiskBudgetExceeded):
            gateway.submit("O3", "000001", Side.BUY, 2000, Decimal("100"))  # +20 万 超限
        # 在途单都在 open 列表里
        open_orders = {o["order_id"] for o in gateway.open_orders()}
        assert open_orders == {"O1", "O2"}


class TestDownstreamRejection:
    def test_broker_rejection_rolls_back_reservation(self, gateway):
        # 风控放行后柜台拒单：submit 不抛预算异常，但预留被回滚
        gateway.adapter.mode = "reject"
        out = gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        assert out["order"]["status"] == "rejected"
        reservation = gateway.engine.get_reservation("O1")
        assert reservation.status is ReservationStatus.REJECTED
        assert gateway.open_orders() == []
        # 额度全额回来：20 万的新单可过（总限额 30 万）
        gateway.adapter.mode = "fill"
        from app.risk.models import OrderRequest
        assert gateway.engine.check(
            OrderRequest("O2", "P1", "600000", Side.BUY, 2000, Decimal("100"))
        ).approved


class TestAdapterException:
    def test_exception_after_approval_releases_reservation(self, gateway):
        gateway.adapter.mode = "explode"
        with pytest.raises(RuntimeError):
            gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        reservation = gateway.engine.get_reservation("O1")
        assert reservation.status is ReservationStatus.REJECTED
        assert gateway.open_orders() == []


class TestPartialFillAndCancel:
    def test_async_partial_fill_then_cancel(self, gateway):
        gateway.adapter.mode = "hold"
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))

        # 柜台异步部分成交 400
        gateway.adapter.push_partial_fill("O1", 400, Decimal("100"))
        r = gateway.engine.get_reservation("O1")
        assert r.status is ReservationStatus.PARTIAL
        assert r.open_quantity == 600

        # 撤销剩余 600
        out = gateway.cancel("O1", reason="不再需要")
        assert out["reservation"]["status"] == "released"
        assert out["decision"]["detail"]["released_quantity"] == 600

        actions = [d["action"] for d in gateway.decisions("O1")]
        assert actions == ["APPROVE", "FILL", "RELEASE"]

    def test_cancel_full_hold_releases_all(self, gateway):
        gateway.adapter.mode = "hold"
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        out = gateway.cancel("O1")
        assert out["reservation"]["status"] == "released"
        assert out["decision"]["detail"]["released_quantity"] == 1000
        assert gateway.open_orders() == []


class TestReconciliation:
    def test_exposure_reflects_holdings(self, gateway):
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        exposure = gateway.exposure()
        total = next(c for c in exposure["checks"] if c["dimension"] == "total")
        assert total["current"] == "100000.00"
        assert total["remaining_after"] == "200000.00"

    def test_refresh_quote_revalues(self, gateway):
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        gateway.adapter.set_quote("000001", 120)
        exposure = gateway.refresh_quote("000001").to_dict()
        total = next(c for c in exposure["checks"] if c["dimension"] == "total")
        assert total["current"] == "120000.00"


class TestSellPositionCheck:
    def test_sell_beyond_position_rejected_before_broker(self, gateway):
        gateway.submit("O1", "000001", Side.BUY, 1000, Decimal("100"))
        with pytest.raises(RiskBudgetExceeded) as exc:
            gateway.submit("S1", "000001", Side.SELL, 2000, Decimal("100"))
        v = exc.value.report.to_dict()["violations"]
        assert any(x["dimension"] == "position" for x in v)


class TestGatewayConcurrency:
    """并发下单经过网关串行：柜台账本与预算都不会超卖。"""

    def test_concurrent_submits_through_adapter(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        adapter = FakeAdapter()
        adapter.connect()
        adapter.set_quote("000001", 100)
        gw = TradingRiskGateway(adapter, "P1", initial_limits=RuleLimits(
            max_total_long_amount=Decimal("100000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("100000"),
        ))
        gw.initialize()
        gw.register_instrument(InstrumentInfo("000001", industry="BANK"))

        outcomes = {"ok": 0, "no": 0}
        lock = threading.Lock()

        def attempt(i):
            try:
                gw.submit(f"O{i}", "000001", Side.BUY, 200, Decimal("10"))
                with lock:
                    outcomes["ok"] += 1
            except RiskBudgetExceeded:
                with lock:
                    outcomes["no"] += 1

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(attempt, range(50)))

        # 每笔 2000，总额度 10 万：恰好 50 笔
        assert outcomes == {"ok": 50, "no": 0}
        # 柜台账本一致：现金扣减与成交总额相符
        assert adapter._cash == Decimal("1000000") - Decimal("100000")
        assert sum(
            p.quantity for p in adapter.get_positions()
        ) == 10000
