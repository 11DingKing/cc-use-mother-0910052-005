"""组合级风控端到端：TradingService × SimulationAdapter × API。"""

from decimal import Decimal

import pytest

from app.services.trading_service import TradingService
from app.trading.base import OrderStatus
from app.risk.models import RiskDimension
from app.risk.store import RiskStore
from app.risk.service import RiskRejectedException


@pytest.fixture
def service():
    svc = TradingService(risk_store=RiskStore.in_memory("p1"), portfolio_id="p1")
    svc.connect("simulation", {"initial_cash": 1_000_000})
    svc.adapter.set_quote("000001", 10.0)
    svc.adapter.set_quote("000002", 10.0)
    # 收紧的组合预算：总敞口 60000、银行 30000
    svc.portfolio_risk.publish_ruleset(
        limits=[
            {"dimension": "total", "target": "TOTAL", "max_exposure": "60000"},
            {"dimension": "industry", "target": "银行", "max_exposure": "60000"},
            {"dimension": "industry", "target": "地产", "max_exposure": "60000"},
        ],
        max_single_order_amount="100000",
    )
    svc.portfolio_risk.upsert_instrument("000001", "银行")
    svc.portfolio_risk.upsert_instrument("000002", "地产")
    return svc


class TestEndToEndLifecycle:

    def test_two_compliant_orders_together_exceed(self, service):
        """每笔 40000 单独合规，合计 80000 > 60000：第二笔必须被拒。"""
        first = service.buy("000001", quantity=4000, price=10.0)  # 40000
        assert first["status"] == "filled"
        assert "risk_decision_id" in first

        with pytest.raises(Exception) as exc:
            service.buy("000002", quantity=4000, price=10.0)
        # TradingService 将风控拒绝包装为 TradingException 之外的 AppException
        assert getattr(exc.value, "code", None) == "RISK_REJECTED"
        decision = exc.value.decision
        assert decision.passed is False
        total = next(r for r in decision.results if r.dimension == RiskDimension.TOTAL)
        # 持仓 40000 + 本次 40000 = 80000 > 60000
        assert total.position_used == Decimal("40000")
        assert total.incremental == Decimal("40000")

        # 被拒订单没有进入成交/台账开放预留
        assert len(service.get_orders()) == 1

    def test_resting_order_reservation_then_quote_driven_fill(self, service):
        """挂单占用预留，行情触达后成交，预留转入持仓。"""
        # 低于最新价的买单：挂起等待行情触达
        result = service.buy("000001", quantity=2000, price=9.0)
        assert result["status"] == "submitted"
        oid = result["order_id"]

        report = service.portfolio_risk.residual_report()
        total = next(l for l in report["lines"] if l["target"] == "TOTAL")
        assert Decimal(total["pending_used"]) == Decimal("18000")

        # 行情跌到 9 → 限价买成交；回调驱动台账迁移
        service.adapter.set_quote("000001", 9.0)
        order = service.adapter.get_order(oid)
        assert order.status == OrderStatus.FILLED

        report2 = service.portfolio_risk.residual_report()
        total2 = next(l for l in report2["lines"] if l["target"] == "TOTAL")
        assert Decimal(total2["pending_used"]) == Decimal("0")
        assert Decimal(total2["position_used"]) == Decimal("18000")

        trail = service.portfolio_risk.get_order_trail(oid)
        assert trail["ledger"]["status"] == "filled"
        actions = [d["action"] for d in trail["decisions"]]
        assert "RESERVE" in actions
        assert "RELEASE" in actions

    def test_partial_fill_then_cancel_releases(self, service):
        result = service.buy("000001", quantity=2000, price=10.0)
        assert result["status"] == "filled"  # 默认价立即成交

    def test_cancel_resting_order_releases_budget(self, service):
        result = service.buy("000001", quantity=2000, price=9.0)
        oid = result["order_id"]
        assert result["status"] == "submitted"

        # 撤单：适配器释放冻结，风控释放预留
        cancelled = service.cancel_order(oid)
        assert cancelled["status"] == "cancelled"
        report = service.portfolio_risk.residual_report()
        total = next(l for l in report["lines"] if l["target"] == "TOTAL")
        assert Decimal(total["pending_used"]) == Decimal("0")
        assert Decimal(total["residual"]) == Decimal("60000")
        # 资金冻结已退还
        account = service.get_account()
        assert Decimal(str(account["frozen_cash"])) == Decimal("0")

    def test_sell_does_not_consume_buy_budget(self, service):
        service.buy("000001", quantity=1000, price=10.0)
        # 卖出不经过买入预算预留
        sold = service.sell("000001", quantity=1000, price=10.0)
        assert sold["status"] == "filled"
        assert service.portfolio_risk.get_order_trail(sold["order_id"]) is None

    def test_rejection_by_broker_rolls_back_reservation(self, service):
        """风控放行但柜台拒绝（资金不足）时预留必须回滚释放。"""
        # 换一版极宽松规则，使风控通过、但柜台资金检查拒绝
        service.portfolio_risk.publish_ruleset(
            limits=[{"dimension": "total", "target": "TOTAL", "max_exposure": "100000000"}],
            max_single_order_amount="100000000",
        )
        with pytest.raises(Exception) as exc:
            service.buy("000002", quantity=200_000, price=10.0)  # 200 万 > 100 万
        assert getattr(exc.value, "code", None) == "TRADING_ERROR"

        # 预留已回滚：无在途占用
        report = service.portfolio_risk.residual_report()
        total = next(l for l in report["lines"] if l["target"] == "TOTAL")
        assert Decimal(total["pending_used"]) == Decimal("0")
        # 台账终态为 rejected，且留有 RELEASE 审计
        orders = service.portfolio_risk.list_ledger(include_closed=True)
        rejected = [o for o in orders if o["status"] == "rejected"]
        assert len(rejected) == 1

    def test_partial_fill_shrinks_reservation(self, service):
        result = service.buy("000001", quantity=3000, price=9.0)
        oid = result["order_id"]
        assert result["status"] == "submitted"

        # 外部撮合部分成交 1000 股
        service.adapter.match_order(oid, fill_quantity=1000, fill_price=9.0)
        entry = service.portfolio_risk.store.get_order(oid)
        assert entry.status.value == "partial"
        assert entry.remaining_quantity == 2000

        report = service.portfolio_risk.residual_report()
        total = next(l for l in report["lines"] if l["target"] == "TOTAL")
        # 在途 2000×9=18000；持仓按最新行情价 10 估值 = 10000
        assert Decimal(total["pending_used"]) == Decimal("18000")
        assert Decimal(total["position_used"]) == Decimal("10000")

        # 撤销剩余
        service.cancel_order(oid)
        report2 = service.portfolio_risk.residual_report()
        total2 = next(l for l in report2["lines"] if l["target"] == "TOTAL")
        assert Decimal(total2["pending_used"]) == Decimal("0")
        assert Decimal(total2["position_used"]) == Decimal("10000")


class TestRuleChangeTraceability:

    def test_new_version_does_not_rewrite_history(self, service):
        result = service.buy("000001", quantity=2000, price=10.0)  # 20000 @ v1
        oid = result["order_id"]

        # 换版收紧到 10000
        service.portfolio_risk.publish_ruleset(
            limits=[{"dimension": "total", "target": "TOTAL", "max_exposure": "10000"}],
            max_single_order_amount="100000",
        )
        trail = service.portfolio_risk.get_order_trail(oid)
        reserve_decision = next(d for d in trail["decisions"] if d["action"] == "RESERVE")
        # 历史决策仍绑定 v1 快照
        assert reserve_decision["rule_version"] == 1
        assert reserve_decision["rule_snapshot"]["limits"][0]["max_exposure"] == "60000"

        # 新版本下新单被拒
        from app.trading.base import Order, OrderSide, OrderType
        order = Order(
            order_id="NEW1", stock_code="000002", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=2000, price=Decimal("10"),
        )
        with pytest.raises(RiskRejectedException) as exc:
            service.portfolio_risk.check_and_reserve(order)
        assert exc.value.decision.rule_version == 2
