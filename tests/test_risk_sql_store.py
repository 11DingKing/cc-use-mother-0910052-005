"""SqlRiskStore 测试：真实事务、持久化恢复、审计 JSON 回放。"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.risk.engine import PortfolioRiskEngine, RiskRejected
from app.risk.models import (
    Dimension,
    InstrumentInfo,
    OrderRequest,
    Reservation,
    ReservationStatus,
    ExposureReport,
    RuleLimits,
    RuleVersion,
    Side,
)
from app.risk.sql_store import SqlRiskStore

T0 = datetime(2026, 10, 6, 10, 0, 0)


@pytest.fixture
def store():
    return SqlRiskStore("sqlite:///:memory:")


@pytest.fixture
def engine(store):
    eng = PortfolioRiskEngine(store=store)
    eng.ensure_portfolio(
        "P1",
        initial_cash=Decimal("1000000"),
        limits=RuleLimits(max_total_long_amount=Decimal("500000")),
    )
    eng.register_instrument(InstrumentInfo("000001", industry="BANK"))
    return eng


class TestRuleVersions:
    def test_version_persistence_and_current_pointer(self, store):
        eng = PortfolioRiskEngine(store=store)
        eng.ensure_portfolio("P1", limits=RuleLimits(max_total_long_amount=Decimal("500")))
        eng.publish_rules("P1", RuleLimits(max_total_long_amount=Decimal("800")))

        versions = store.list_rule_versions("P1")
        assert [v.version for v in versions] == [1, 2]
        assert store.get_current_rule_version("P1").version == 2
        # 历史版本内容原样保留
        assert versions[0].limits.max_total_long_amount == Decimal("500")
        assert versions[1].limits.max_total_long_amount == Decimal("800")

    def test_duplicate_version_rejected(self, store, engine):
        with pytest.raises(ValueError):
            store.add_rule_version(RuleVersion(
                portfolio_id="P1", version=1, limits=RuleLimits(),
                created_at=T0, effective_at=T0,
            ))

    def test_limits_json_roundtrip(self, store):
        limits = RuleLimits(
            max_total_long_amount=Decimal("123456.78"),
            industry_limits={"BANK": Decimal("111.11")},
            instrument_limits={"etf": Decimal("222.22")},
        )
        eng = PortfolioRiskEngine(store=store)
        eng.ensure_portfolio("P1", limits=limits)
        got = store.get_current_rule_version("P1").limits
        assert got.max_total_long_amount == Decimal("123456.78")
        assert got.industry_limits["BANK"] == Decimal("111.11")
        assert got.instrument_limits["etf"] == Decimal("222.22")


class TestTransactionRollback:
    def test_exception_inside_transaction_rolls_back_all(self, store):
        # 先有一个有效版本
        eng = PortfolioRiskEngine(store=store)
        eng.ensure_portfolio("P1")
        eng.register_instrument(InstrumentInfo("000001"))

        with pytest.raises(RuntimeError, match="模拟故障"):
            with store.transaction():
                store.put_reservation(Reservation(
                    order_id="BROKEN", portfolio_id="P1", stock_code="000001",
                    industry="UNKNOWN", instrument_type="stock", side=Side.BUY,
                    quantity=100, filled_quantity=0, frozen_price=Decimal("10"),
                    rule_version=1, status=ReservationStatus.HELD,
                    created_at=T0, updated_at=T0,
                ))
                raise RuntimeError("模拟故障")

        # 事务回滚后，预留不存在
        assert store.get_reservation("BROKEN") is None

    def test_approve_failure_leaves_no_partial_state(self, engine):
        with pytest.raises(RiskRejected):
            engine.approve(
                OrderRequest("O1", "P1", "000001", Side.BUY, 100000, Decimal("100")),
            )
        # 被拒订单没有任何预留行
        assert engine.get_reservation("O1") is None
        # 但 REJECT 审计已落库
        decisions = engine.store.list_decisions("P1", "O1")
        assert len(decisions) == 1 and decisions[0].action == "REJECT"


class TestPersistenceRecovery:
    def test_state_survives_store_reopen(self, tmp_path):
        db = tmp_path / "risk.db"
        url = f"sqlite:///{db}"

        store1 = SqlRiskStore(url)
        eng1 = PortfolioRiskEngine(store=store1)
        eng1.ensure_portfolio("P1", initial_cash=Decimal("1000000"),
                              limits=RuleLimits(max_total_long_amount=Decimal("500000")))
        eng1.register_instrument(InstrumentInfo("000001", industry="BANK"))
        eng1.approve(OrderRequest("O1", "P1", "000001", Side.BUY, 1000, Decimal("100")))
        eng1.report_fill("O1", 400, Decimal("100"))

        # 重新打开：规则指针、预留、豁免、审计全部可恢复
        store2 = SqlRiskStore(url)
        assert store2.get_current_rule_version("P1").version == 1
        r = store2.get_reservation("O1")
        assert r.status is ReservationStatus.PARTIAL
        assert r.filled_quantity == 400
        assert r.frozen_price == Decimal("100.0000")
        decisions = store2.list_decisions("P1", "O1")
        assert [d.action for d in decisions] == ["APPROVE", "FILL"]

    def test_audit_report_json_roundtrip(self, engine):
        with pytest.raises(RiskRejected):
            engine.approve(
                OrderRequest("O1", "P1", "000001", Side.BUY, 6000, Decimal("100")),
            )
        stored = engine.store.list_decisions("P1", "O1")[0]
        # 从 JSON 完整反序列化试算报告
        assert isinstance(stored.report, ExposureReport)
        assert stored.report.approved is False
        assert stored.report.violations[0].dimension is Dimension.TOTAL
        assert stored.report.violations[0].projected == Decimal("600000.00")


class TestExemptionPersistence:
    def test_consumed_flag_persists(self, engine):
        from datetime import timedelta

        ex = engine.grant_exemption(
            "P1", Dimension.ORDER, Decimal("5"),
            valid_from=T0, valid_until=T0 + timedelta(hours=1),
            order_id="O1", at=T0,
        )
        engine.store.consume_exemption(ex.exemption_id, "O1")
        fresh = engine.store.get_exemption(ex.exemption_id)
        assert fresh.consumed_by_order == "O1"

        # 不能被另一笔订单重复消费
        with pytest.raises(ValueError):
            engine.store.consume_exemption(ex.exemption_id, "O2")


class TestEngineOnSqlStore:
    """同一套生命周期在真实事务存储上端到端跑通。"""

    def test_approve_fill_cancel_lifecycle(self, engine):
        r, d = engine.approve(
            OrderRequest("O1", "P1", "000001", Side.BUY, 1000, Decimal("100"))
        )
        assert d.action == "APPROVE"
        engine.report_fill("O1", 400, Decimal("100"))
        r, d = engine.cancel("O1")
        assert r.status is ReservationStatus.RELEASED

        actions = [x.action for x in engine.store.list_decisions("P1")]
        assert actions == ["APPROVE", "FILL", "RELEASE"]
        # 每条审计都带规则版本
        assert all(x.rule_version == 1 for x in engine.store.list_decisions("P1"))
