"""组合级风险预算：引擎、版本、豁免、预留/回滚与审计测试。"""

import threading
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.risk.engine import PortfolioRiskEngine, PortfolioSnapshot
from app.risk.models import (
    Exemption,
    ExemptionStatus,
    LedgerEntry,
    LedgerStatus,
    OpenOrderView,
    OrderIntent,
    PositionView,
    RiskDimension,
    RiskLimit,
    RuleSet,
)
from app.risk.service import PortfolioRiskService, RiskRejectedException
from app.risk.store import RiskStore, RiskStoreError


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def make_ruleset(
    total=None,
    industries=None,
    instruments=None,
    version=1,
    single="100000",
):
    limits = []
    if total is not None:
        limits.append(RiskLimit(RiskDimension.TOTAL, "TOTAL", Decimal(str(total))))
    for name, amount in (industries or {}).items():
        limits.append(RiskLimit(RiskDimension.INDUSTRY, name, Decimal(str(amount))))
    for name, amount in (instruments or {}).items():
        limits.append(RiskLimit(RiskDimension.INSTRUMENT, name, Decimal(str(amount))))
    return RuleSet(
        version=version,
        limits=tuple(limits),
        max_single_order_amount=Decimal(str(single)),
        portfolio_id="t",
    )


def pos(code, qty, price, industry="银行", itype="STOCK"):
    return PositionView(code, qty, Decimal(str(price)), industry, itype)


def pending(oid, code, qty, price, industry="银行", itype="STOCK"):
    return OpenOrderView(oid, code, qty, Decimal(str(price)), industry, itype)


def intent(oid, code, qty, price, side="buy", industry="银行", itype="STOCK"):
    return OrderIntent(
        oid, code, side, qty, Decimal(str(price)), industry, itype
    )


def exemption(dim, target, amount, minutes=60, status=ExemptionStatus.ACTIVE):
    now = datetime.now()
    return Exemption(
        exemption_id=f"EXM_{dim.value}_{target}",
        version=1,
        dimension=dim,
        target=target,
        extra_amount=Decimal(str(amount)),
        valid_from=now - timedelta(minutes=1),
        valid_to=now + timedelta(minutes=minutes),
        reason="测试豁免",
        status=status,
    )


# ---------------------------------------------------------------------------
# 引擎：聚合口径
# ---------------------------------------------------------------------------

class TestEngineAggregation:

    def setup_method(self):
        self.engine = PortfolioRiskEngine()

    def test_total_exposure_includes_positions_and_pending(self):
        ruleset = make_ruleset(total="100000", industries={"银行": "60000"})
        snapshot = PortfolioSnapshot(
            positions=[pos("000001", 1000, "10")],            # 10000
            open_orders=[pending("O1", "000002", 1000, "20")],  # 20000
        )
        decision = self.engine.evaluate(
            ruleset, snapshot, intent("O2", "000001", 1000, "30")
        )
        total = next(r for r in decision.results if r.dimension == RiskDimension.TOTAL)
        # 持仓 10000 + 在途 20000 + 本次 30000 = 60000
        assert total.position_used == Decimal("10000")
        assert total.pending_used == Decimal("20000")
        assert total.incremental == Decimal("30000")
        assert total.projected_exposure == Decimal("60000")
        assert total.residual == Decimal("70000")
        assert total.passed is True
        bank = next(r for r in decision.results if r.target == "银行")
        assert bank.projected_exposure == Decimal("60000")

    def test_two_individually_fine_orders_together_exceed_budget(self):
        """核心需求：各自合规的订单合在一起必须能拦住第二笔。"""
        ruleset = make_ruleset(total="50000")
        snapshot_first = PortfolioSnapshot(positions=[], open_orders=[])
        d1 = self.engine.evaluate(ruleset, snapshot_first, intent("O1", "A", 1000, "20"))
        assert d1.passed

        snapshot_second = PortfolioSnapshot(
            positions=[], open_orders=[pending("O1", "A", 1000, "20")]
        )
        d2 = self.engine.evaluate(ruleset, snapshot_second, intent("O2", "A", 2000, "20"))
        assert d2.passed is False
        total = d2.failures[0]
        assert total.projected_exposure == Decimal("60000")
        assert total.effective_limit == Decimal("50000")
        assert "超过" in total.reason

    def test_industry_and_instrument_buckets(self):
        ruleset = make_ruleset(
            total="1000000",
            industries={"银行": "15000", "地产": "30000"},
            instruments={"STOCK": "1000000", "ETF": "5000"},
        )
        snapshot = PortfolioSnapshot(
            positions=[pos("000001", 1000, "10", industry="银行")],
            open_orders=[],
        )
        # 银行桶：10000 持仓 + 10000 新买 = 20000 > 15000
        d = self.engine.evaluate(ruleset, snapshot, intent("O1", "000001", 1000, "10"))
        assert d.passed is False
        failure = next(r for r in d.results if r.target == "银行")
        assert failure.projected_exposure == Decimal("20000")

        # ETF 桶单独受限
        snapshot2 = PortfolioSnapshot(positions=[], open_orders=[])
        d2 = self.engine.evaluate(
            ruleset, snapshot2,
            intent("O2", "510300", 1000, "10", industry="指数", itype="ETF"),
        )
        etf = next(r for r in d2.results if r.target == "ETF")
        assert etf.projected_exposure == Decimal("10000")
        assert etf.passed is False

    def test_sell_order_does_not_consume_budget(self):
        ruleset = make_ruleset(total="50000")
        snapshot = PortfolioSnapshot(
            positions=[pos("000001", 1000, "40")], open_orders=[]
        )
        d = self.engine.evaluate(
            ruleset, snapshot, intent("O1", "000001", 1000, "40", side="sell")
        )
        total = next(r for r in d.results if r.dimension == RiskDimension.TOTAL)
        assert total.incremental == 0
        assert total.passed is True

    def test_single_order_limit(self):
        ruleset = make_ruleset(total="1000000", single="50000")
        d = PortfolioRiskEngine().evaluate(
            ruleset, PortfolioSnapshot([], []), intent("O1", "A", 10000, "10")
        )
        single = next(r for r in d.results if r.dimension == RiskDimension.SINGLE)
        assert single.passed is False
        assert "单笔" in single.reason

    def test_exemption_extends_limit_within_window(self):
        ruleset = make_ruleset(total="50000")
        ex = exemption(RiskDimension.TOTAL, "TOTAL", "20000")
        snapshot = PortfolioSnapshot(
            positions=[], open_orders=[pending("O1", "A", 1000, "20")]
        )
        d = self.engine.evaluate(
            ruleset, snapshot, intent("O2", "A", 2000, "20"), exemptions=[ex]
        )
        total = next(r for r in d.results if r.dimension == RiskDimension.TOTAL)
        assert total.effective_limit == Decimal("70000")
        assert total.passed is True
        assert total.exempted_amount == Decimal("20000")
        assert any("EXM_total_TOTAL" in x for x in total.exemption_ids)

    def test_expired_exemption_ignored(self):
        ruleset = make_ruleset(total="50000")
        ex = exemption(RiskDimension.TOTAL, "TOTAL", "20000", minutes=-10)
        # valid_to 已过：直接构造过期豁免
        now = datetime.now()
        ex = Exemption(
            exemption_id="EXM_X", version=1, dimension=RiskDimension.TOTAL,
            target="TOTAL", extra_amount=Decimal("20000"),
            valid_from=now - timedelta(hours=2), valid_to=now - timedelta(hours=1),
            reason="过期",
        )
        snapshot = PortfolioSnapshot(
            positions=[], open_orders=[pending("O1", "A", 1000, "20")]
        )
        d = self.engine.evaluate(
            ruleset, snapshot, intent("O2", "A", 2000, "20"), exemptions=[ex]
        )
        assert d.passed is False

    def test_quote_change_reprices_positions(self):
        ruleset = make_ruleset(total="15000")
        snapshot_low = PortfolioSnapshot(positions=[pos("A", 1000, "10")], open_orders=[])
        snapshot_high = PortfolioSnapshot(positions=[pos("A", 1000, "20")], open_orders=[])
        d_low = PortfolioRiskEngine().evaluate(
            ruleset, snapshot_low, intent("O1", "A", 100, "10")
        )
        d_high = PortfolioRiskEngine().evaluate(
            ruleset, snapshot_high, intent("O1", "A", 100, "10")
        )
        assert d_low.passed is True
        assert d_high.passed is False  # 报价上跳后持仓已 20000 超限

    def test_residual_report_and_max_quantity(self):
        ruleset = make_ruleset(total="100000", industries={"银行": "50000"})
        snapshot = PortfolioSnapshot(
            positions=[pos("A", 1000, "10")],
            open_orders=[pending("O1", "A", 1000, "10")],
        )
        report = PortfolioRiskEngine().residual_report(ruleset, snapshot)
        assert report.total_position_value == Decimal("10000")
        assert report.total_pending == Decimal("10000")
        bank = next(l for l in report.lines if l.target == "银行")
        assert bank.residual == Decimal("30000")

        d = PortfolioRiskEngine().evaluate(
            ruleset, snapshot, intent("O2", "A", 100, "10")
        )
        bank = next(r for r in d.results if r.target == "银行")
        # 剩余 30000，价 10，最多 3000 股（100 整手）
        assert bank.max_additional_quantity == 3000


# ---------------------------------------------------------------------------
# 服务层：版本、豁免、事务、回滚、幂等
# ---------------------------------------------------------------------------

class StubAdapter:
    """内存持仓/行情桩。"""

    def __init__(self):
        self.positions = {}
        self.quotes = {}

    def set_position(self, code, qty, price, industry="其他", itype="STOCK"):
        from app.trading.base import Position
        self.positions[code] = Position(
            stock_code=code, stock_name=code, quantity=qty,
            available_quantity=qty, avg_cost=Decimal(str(price)),
            current_price=Decimal(str(price)),
            market_value=Decimal(str(price)) * qty,
            profit_loss=Decimal("0"), profit_loss_ratio=0.0,
        )
        self.quotes[code] = {"last_price": float(price)}

    def position_provider(self):
        return list(self.positions.values())

    def quote_provider(self, code):
        return self.quotes.get(code)


@pytest.fixture
def service():
    store = RiskStore.in_memory("p1")
    stub = StubAdapter()
    svc = PortfolioRiskService(
        store=store,
        portfolio_id="p1",
        position_provider=stub.position_provider,
        quote_provider=stub.quote_provider,
    )
    svc.publish_ruleset(
        limits=[
            {"dimension": "total", "target": "TOTAL", "max_exposure": "100000"},
            {"dimension": "industry", "target": "银行", "max_exposure": "40000"},
            {"dimension": "instrument", "target": "ETF", "max_exposure": "20000"},
        ],
        max_single_order_amount="50000",
        note="v1",
    )
    return svc, stub


def make_order(oid, code, qty, price, industry="其他", itype="STOCK", side="buy"):
    from app.trading.base import Order, OrderSide, OrderType
    return Order(
        order_id=oid, stock_code=code,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=qty, price=Decimal(str(price)),
    ), industry, itype


def reserve(svc, oid, code, qty, price, industry="其他", itype="STOCK"):
    order, _, _ = make_order(oid, code, qty, price)
    svc.upsert_instrument(code, industry, itype)
    return svc.check_and_reserve(order)


class TestServiceReserve:

    def test_reserve_success_and_ledger(self, service):
        svc, stub = service
        d = reserve(svc, "O1", "000001", 1000, "10", industry="银行")
        assert d.passed
        assert d.rule_version == 1
        ledger = svc.list_ledger()
        assert len(ledger) == 1
        assert ledger[0]["order_id"] == "O1"
        assert ledger[0]["remaining_quantity"] == 1000

    def test_combined_orders_exempt_first_then_reject(self, service):
        svc, _ = service
        reserve(svc, "O1", "A", 2000, "20", industry="银行")  # 40000，银行桶用满
        # 第二笔同行业：银行 40000 + 20000 > 40000
        order, _, _ = make_order("O2", "A", 1000, "20")
        svc.upsert_instrument("A", "银行")
        with pytest.raises(RiskRejectedException) as exc:
            svc.check_and_reserve(order)
        assert exc.value.status_code == 409
        assert exc.value.decision.rule_version == 1
        # 拒绝不留台账预留
        assert {o["order_id"] for o in svc.list_ledger()} == {"O1"}
        # 但拒绝原因可审计
        decisions = svc.list_decisions(order_id="O2")
        assert decisions and decisions[0]["passed"] is False
        assert any("银行" in r for r in decisions[0]["reasons"])

    def test_rejection_is_atomic_and_audited(self, service):
        svc, _ = service
        reserve(svc, "O1", "A", 4000, "10")  # 40000
        order, _, _ = make_order("O2", "A", 7000, "10")  # 再 70000 → 总额超
        with pytest.raises(RiskRejectedException):
            svc.check_and_reserve(order)
        # 台账只有 O1
        open_orders = svc.store.open_orders("p1")
        assert [o.order_id for o in open_orders] == ["O1"]
        # 拒绝决策已落库
        rejected = [d for d in svc.store.list_decisions(order_id="O2")]
        assert len(rejected) == 1 and rejected[0].passed is False

    def test_idempotent_reserve_replays_decision(self, service):
        svc, _ = service
        order, _, _ = make_order("O1", "A", 100, "10")
        d1 = svc.check_and_reserve(order)
        d2 = svc.check_and_reserve(order)  # 同对象重试
        assert d2.decision_id == d1.decision_id
        assert len(svc.list_ledger()) == 1

    def test_concurrent_orders_never_oversubscribe(self, service):
        """并发：每笔 40000 单独可行，三笔合计 120000 > 100000，恰好放行两笔。"""
        svc, _ = service
        results = {"accepted": [], "rejected": []}
        lock_results = threading.Lock()

        def worker(oid, barrier):
            barrier.wait()
            order, _, _ = make_order(oid, f"C{oid}", 2000, "20")  # 40000
            svc.upsert_instrument(order.stock_code, "其他")
            try:
                d = svc.check_and_reserve(order)
                with lock_results:
                    results["accepted"].append(d.order_id)
            except RiskRejectedException:
                with lock_results:
                    results["rejected"].append(oid)

        barrier = threading.Barrier(3)
        threads = [
            threading.Thread(target=worker, args=("O1", barrier)),
            threading.Thread(target=worker, args=("O2", barrier)),
            threading.Thread(target=worker, args=("O3", barrier)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results["accepted"]) == 2
        assert len(results["rejected"]) == 1
        total_pending = sum(
            Decimal(o["price"]) * o["remaining_quantity"]
            for o in svc.list_ledger()
        )
        assert total_pending <= Decimal("100000")

    def test_partial_fill_shrinks_and_cancel_releases(self, service):
        svc, _ = service
        reserve(svc, "O1", "A", 4000, "10")  # 40000 预留
        from app.trading.base import Order, OrderSide, OrderType, OrderStatus

        order = Order(
            order_id="O1", stock_code="A", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=4000, price=Decimal("10"),
            status=OrderStatus.PARTIAL_FILLED, filled_quantity=1500,
        )
        svc.on_order_update(order)
        entry = svc.store.get_order("O1")
        assert entry.status == LedgerStatus.PARTIAL
        assert entry.remaining_quantity == 2500

        report = svc.residual_report()
        total = next(l for l in report["lines"] if l["target"] == "TOTAL")
        # 在途只剩 25000
        assert total["pending_used"] == "25000"

        # 撤单回滚：剩余预留全部释放
        order.status = OrderStatus.CANCELLED
        svc.on_order_update(order)
        assert svc.store.get_order("O1").status == LedgerStatus.CANCELLED
        report2 = svc.residual_report()
        total2 = next(l for l in report2["lines"] if l["target"] == "TOTAL")
        assert total2["pending_used"] == "0"

    def test_filled_releases_reservation_into_position(self, service):
        svc, stub = service
        reserve(svc, "O1", "A", 2000, "10")  # 20000 在途
        from app.trading.base import Order, OrderSide, OrderType, OrderStatus

        # 全成：台账释放，持仓供给反映出 20000 市值
        order = Order(
            order_id="O1", stock_code="A", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=2000, price=Decimal("10"),
            status=OrderStatus.FILLED, filled_quantity=2000,
        )
        svc.on_order_update(order)
        assert svc.store.get_order("O1").status == LedgerStatus.FILLED
        # 此时持仓还没更新（桩），在途应为 0
        report = svc.residual_report()
        total = next(l for l in report["lines"] if l["target"] == "TOTAL")
        assert total["pending_used"] == "0"

    def test_terminal_state_transitions_rejected(self, service):
        svc, _ = service
        reserve(svc, "O1", "A", 1000, "10")
        from app.trading.base import Order, OrderSide, OrderType, OrderStatus

        cancel = Order(
            order_id="O1", stock_code="A", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
            status=OrderStatus.CANCELLED,
        )
        svc.on_order_update(cancel)
        # 终态后再来事件，台账不变
        filled = Order(
            order_id="O1", stock_code="A", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=1000, price=Decimal("10"),
            status=OrderStatus.FILLED, filled_quantity=1000,
        )
        svc.on_order_update(filled)
        assert svc.store.get_order("O1").status == LedgerStatus.CANCELLED


class TestRuleVersioningAndExemptions:

    def test_ruleset_append_only_and_decision_binds_version(self, service):
        svc, _ = service
        # v2：收紧总敞口到 30000
        svc.publish_ruleset(
            limits=[{"dimension": "total", "target": "TOTAL", "max_exposure": "30000"}],
            max_single_order_amount="50000", note="v2 收紧",
        )
        versions = svc.list_rule_versions()
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[0]["note"] == "v1"  # 旧版本不变

        order, _, _ = make_order("O1", "A", 4000, "10")  # 40000
        with pytest.raises(RiskRejectedException) as exc:
            svc.check_and_reserve(order)
        assert exc.value.decision.rule_version == 2
        # 决策内嵌完整 v2 快照，可独立复核
        snap = exc.value.decision.rule_snapshot
        assert snap["version"] == 2
        assert snap["limits"][0]["max_exposure"] == "30000"

    def test_historical_decision_recomputes_under_its_version(self, service):
        svc, _ = service
        # v1 下批准 O1（40000）
        reserve(svc, "O1", "A", 4000, "10")
        # 换 v2 收紧
        svc.publish_ruleset(
            limits=[{"dimension": "total", "target": "TOTAL", "max_exposure": "30000"}],
            max_single_order_amount="50000",
        )
        trail = svc.get_order_trail("O1")
        # 历史批准决策仍按 v1 口径
        reserve_decisions = [d for d in trail["decisions"] if d["action"] == "RESERVE"]
        assert reserve_decisions[0]["rule_version"] == 1
        assert reserve_decisions[0]["rule_snapshot"]["limits"][0]["max_exposure"] == "100000"
        # 台账记录批准时版本
        assert trail["ledger"]["rule_version"] == 1

    def test_exemption_grant_revoke_versioning(self, service):
        svc, _ = service
        ex = svc.grant_exemption("total", "TOTAL", "60000", reason="临时提额")
        assert ex["version"] == 1
        # 120000 的买单同时超过单笔限额 50000，再授予单笔口径豁免
        svc.grant_exemption("single", "*", "100000", reason="单笔提额")
        order, _, _ = make_order("O1", "A", 6000, "20")
        d = svc.check_and_reserve(order)
        assert d.passed
        total = next(r for r in d.results if r.dimension == RiskDimension.TOTAL)
        assert total.exempted_amount == Decimal("60000")
        single = next(r for r in d.results if r.dimension == RiskDimension.SINGLE)
        assert single.exempted_amount == Decimal("100000")

        # 撤销：追加 v2 revoked 行
        revoked = svc.revoke_exemption(ex["exemption_id"])
        assert revoked["version"] == 2
        assert revoked["status"] == "revoked"
        history = svc.list_exemptions(include_history=True)
        # 两个豁免（total 两行版本 + single 一行），历史全部保留
        assert len(history) == 3
        total_rows = [h for h in history if h["exemption_id"] == ex["exemption_id"]]
        assert [h["version"] for h in total_rows] == [1, 2]

        # 撤销后新买单不再享受豁免（先把 O1 释放掉避免占额）
        from app.trading.base import OrderStatus
        o1 = svc.store.get_order("O1")
        cancel_order, _, _ = make_order("O1", "A", 1, "1")
        cancel_order.status = OrderStatus.CANCELLED
        svc.on_order_update(cancel_order)

        order2, _, _ = make_order("O2", "A", 6000, "20")
        with pytest.raises(RiskRejectedException):
            svc.check_and_reserve(order2)

    def test_exemption_validation(self, service):
        svc, _ = service
        now = datetime.now()
        with pytest.raises(RiskStoreError):
            svc.grant_exemption(
                "total", "TOTAL", "100",
                valid_from=now, valid_to=now - timedelta(minutes=1),
            )


class TestResidualReport:

    def test_report_lines_explain_budget(self, service):
        svc, stub = service
        svc.upsert_instrument("000001", "银行", "STOCK")
        svc.upsert_instrument("510300", "指数", "ETF")
        stub.set_position("000001", 1000, 10, "银行")
        reserve(svc, "O1", "510300", 1000, "10", industry="指数", itype="ETF")

        report = svc.residual_report()
        assert report["rule_version"] == 1
        lines = {(l["dimension"], l["target"]): l for l in report["lines"]}
        assert Decimal(lines[("total", "TOTAL")]["position_used"]) == Decimal("10000")
        assert Decimal(lines[("total", "TOTAL")]["pending_used"]) == Decimal("10000")
        assert Decimal(lines[("total", "TOTAL")]["residual"]) == Decimal("80000")
        assert Decimal(lines[("industry", "银行")]["residual"]) == Decimal("30000")
        assert Decimal(lines[("instrument", "ETF")]["residual"]) == Decimal("10000")
        assert Decimal(report["valuation"]["000001"]) == Decimal("10")

    def test_quote_change_updates_residual(self, service):
        svc, stub = service
        stub.set_position("A", 1000, 10)
        svc.upsert_instrument("A", "银行")
        r1 = svc.residual_report()
        bank1 = next(l for l in r1["lines"] if l["target"] == "银行")
        assert Decimal(bank1["position_used"]) == Decimal("10000")

        stub.quotes["A"] = {"last_price": 30.0}  # 报价变化
        r2 = svc.residual_report()
        bank2 = next(l for l in r2["lines"] if l["target"] == "银行")
        assert Decimal(bank2["position_used"]) == Decimal("30000")
        assert Decimal(bank2["residual"]) == Decimal("10000")
