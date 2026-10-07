"""组合级风险预算引擎测试。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.risk.engine import PortfolioRiskEngine, RiskRejected
from app.risk.models import (
    Dimension,
    InstrumentInfo,
    OrderRequest,
    ReservationStatus,
    RuleLimits,
    Side,
)

T0 = datetime(2026, 10, 6, 10, 0, 0)


def make_engine(cash="1000000", limits=None, portfolio="P1"):
    eng = PortfolioRiskEngine(initial_cash=Decimal(cash))
    eng.ensure_portfolio(
        portfolio,
        initial_cash=Decimal(cash),
        limits=limits
        or RuleLimits(
            max_total_long_amount=Decimal("500000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("200000"),
            industry_limits={"BANK": Decimal("150000")},
            default_instrument_amount=Decimal("400000"),
            instrument_limits={"etf": Decimal("100000")},
        ),
    )
    return eng


def bank_engine():
    eng = make_engine()
    eng.register_instrument(InstrumentInfo("000001", industry="BANK"))
    eng.register_instrument(InstrumentInfo("600000", industry="BANK"))
    eng.register_instrument(InstrumentInfo("510300", industry="IDX", instrument_type="etf"))
    return eng


def buy(oid, code, qty, price=Decimal("100"), portfolio="P1"):
    return OrderRequest(oid, portfolio, code, Side.BUY, qty, price)


class TestApproveDimensions:
    """各维度的放行与拒绝。"""

    def test_total_exposure_blocks_second_order(self):
        """两笔各自合规的订单合起来超总敞口：第二笔必须被拒。"""
        eng = make_engine(cash="500000", limits=RuleLimits(
            max_total_long_amount=Decimal("150000"),
            max_single_order_amount=Decimal("100000"),
        ))
        eng.register_instrument(InstrumentInfo("000001", industry="BANK"))
        eng.register_instrument(InstrumentInfo("600000", industry="BANK"))

        eng.approve(buy("O1", "000001", 1000, Decimal("100")))  # 10 万
        with pytest.raises(RiskRejected) as exc:
            eng.approve(buy("O2", "600000", 1000, Decimal("100")))  # 再加 10 万

        dims = {v.dimension for v in exc.value.report.violations}
        assert Dimension.TOTAL in dims
        total_v = next(v for v in exc.value.report.violations if v.dimension is Dimension.TOTAL)
        assert total_v.current == Decimal("0.00")
        assert total_v.pending == Decimal("100000.00")
        assert total_v.incoming == Decimal("100000.00")
        assert total_v.projected == Decimal("200000.00")

    def test_industry_bucket_accumulates_pending(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))  # 银行 10 万
        with pytest.raises(RiskRejected) as exc:
            eng.approve(buy("O2", "600000", 600, Decimal("100")))  # 银行再加 6 万 > 15 万
        v = next(v for v in exc.value.report.violations if v.dimension is Dimension.INDUSTRY)
        assert v.bucket == "BANK"
        assert v.pending == Decimal("100000.00")
        assert v.projected == Decimal("160000.00")

    def test_instrument_bucket(self):
        eng = bank_engine()
        with pytest.raises(RiskRejected) as exc:
            eng.approve(buy("O1", "510300", 2000, Decimal("100")))  # ETF 20 万 > 10 万
        v = next(v for v in exc.value.report.violations if v.dimension is Dimension.INSTRUMENT)
        assert v.bucket == "etf"

    def test_single_order_limit(self):
        eng = bank_engine()
        with pytest.raises(RiskRejected) as exc:
            eng.approve(buy("O1", "000001", 2000, Decimal("100")))  # 单笔 20 万 > 10 万
        assert any(v.dimension is Dimension.ORDER for v in exc.value.report.violations)

    def test_cash_includes_pending_buys(self):
        eng = make_engine(cash="120000")
        eng.register_instrument(InstrumentInfo("000001", industry="BANK"))
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))  # 冻结 10 万
        with pytest.raises(RiskRejected) as exc:
            eng.approve(buy("O2", "000001", 500, Decimal("100")))  # 再要 5 万 > 12 万
        v = next(v for v in exc.value.report.violations if v.dimension is Dimension.CASH)
        assert v.effective_limit == Decimal("120000.00")
        assert v.pending == Decimal("100000.00")

    def test_sell_position_bucket_includes_pending_sells(self):
        eng = bank_engine()
        # 先买入成交建立 1000 股持仓
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 1000, Decimal("100"))

        # 市价卖单（无价格）也必须校验可交割数量
        eng.approve(OrderRequest("S1", "P1", "000001", Side.SELL, 800))
        # 在途卖出 800 + 本单 300 > 持仓 1000
        with pytest.raises(RiskRejected) as exc:
            eng.approve(OrderRequest("S2", "P1", "000001", Side.SELL, 300))
        v = next(v for v in exc.value.report.violations if v.dimension is Dimension.POSITION)
        assert v.pending == Decimal("800")
        assert v.incoming == Decimal("300")

    def test_sell_does_not_consume_long_buckets(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 1000, Decimal("100"))
        # 卖单即使价格很高也不触发多头敞口维度（单笔金额另算）
        report = eng.check(OrderRequest("S1", "P1", "000001", Side.SELL, 1000, Decimal("100")))
        assert report.approved
        assert all(c.incoming == 0 for c in report.checks if c.dimension in
                   (Dimension.TOTAL, Dimension.INDUSTRY, Dimension.INSTRUMENT))

    def test_unknown_instrument_goes_to_explicit_unknown_bucket(self):
        eng = make_engine()
        report = eng.check(buy("O1", "999999", 100, Decimal("10")))
        industries = {c.bucket for c in report.checks if c.dimension is Dimension.INDUSTRY}
        assert "UNKNOWN" in industries


class TestExplainability:
    """剩余额度试算可解释。"""

    def test_report_has_full_breakdown(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        report = eng.check(buy("O2", "600000", 400, Decimal("100")))

        bank = next(c for c in report.checks
                    if c.dimension is Dimension.INDUSTRY and c.bucket == "BANK")
        assert bank.base_limit == Decimal("150000.00")
        assert bank.pending == Decimal("100000.00")
        assert bank.incoming == Decimal("40000.00")
        assert bank.projected == Decimal("140000.00")
        assert bank.remaining_before == Decimal("50000.00")
        assert bank.remaining_after == Decimal("10000.00")
        assert bank.passed is True

    def test_exposure_view_without_candidate_lists_all_buckets(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        view = eng.exposure("P1")
        assert view.order_id is None
        buckets = {(c.dimension, c.bucket) for c in view.checks}
        assert (Dimension.TOTAL, "portfolio") in buckets
        assert (Dimension.INDUSTRY, "BANK") in buckets

    def test_valuation_limit_takes_precedence(self):
        eng = bank_engine()
        eng.update_quote("P1", "000001", Decimal("88"))
        report = eng.check(buy("O1", "000001", 100, Decimal("100")))
        assert report.valuations["000001"] == "100"

    def test_market_order_requires_quote(self):
        eng = bank_engine()
        with pytest.raises(ValueError):
            eng.check(OrderRequest("O9", "P1", "000001", Side.BUY, 100, None))

    def test_market_order_uses_quote(self):
        eng = bank_engine()
        eng.update_quote("P1", "000001", Decimal("77"))
        report = eng.check(OrderRequest("O9", "P1", "000001", Side.BUY, 100, None))
        assert report.valuations["000001"] == "77"


class TestRuleVersioning:
    """规则换版与历史追溯。"""

    def test_publish_increments_version(self):
        eng = bank_engine()
        v1 = eng.current_rules("P1")
        v2 = eng.publish_rules("P1", RuleLimits(max_total_long_amount=Decimal("300000")))
        assert v2.version == v1.version + 1
        assert eng.current_rules("P1").version == v2.version

    def test_inflight_order_keeps_approved_version(self):
        eng = bank_engine()
        res, _ = eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        assert res.rule_version == 1
        eng.publish_rules("P1", RuleLimits(
            max_total_long_amount=Decimal("10"),  # 新版几乎为零
            max_single_order_amount=Decimal("10"),
        ))
        # 在途订单仍按 v1 口径成交、撤销
        res, fill_dec = eng.report_fill("O1", 400, Decimal("100"))
        assert fill_dec.rule_version == 1
        assert fill_dec.report.rule_version == 1
        res, cancel_dec = eng.cancel("O1")
        assert cancel_dec.rule_version == 1
        assert cancel_dec.report.rule_version == 1
        assert res.rule_version == 1

    def test_new_order_uses_new_version(self):
        eng = bank_engine()
        eng.publish_rules("P1", RuleLimits(
            max_total_long_amount=Decimal("50"),
            max_single_order_amount=Decimal("100000"),
        ))
        with pytest.raises(RiskRejected) as exc:
            eng.approve(buy("O2", "000001", 100, Decimal("100")))
        assert exc.value.report.rule_version == 2

    def test_historical_rule_can_be_retrieved(self):
        eng = bank_engine()
        eng.publish_rules("P1", RuleLimits(max_total_long_amount=Decimal("333")))
        old = eng.rules_at("P1", 1)
        assert old.limits.max_total_long_amount == Decimal("500000")

    def test_cannot_republish_same_version(self):
        eng = bank_engine()
        with pytest.raises(ValueError):
            # 直接调存储塞重复版本
            from app.risk.models import RuleVersion
            eng.store.add_rule_version(RuleVersion(
                portfolio_id="P1", version=1, limits=RuleLimits(),
                created_at=T0, effective_at=T0))


class TestExemptions:
    """临时豁免。"""

    def _engine(self):
        # 单笔限额放宽到 20 万，使行业桶（15 万）成为唯一约束
        eng = make_engine(limits=RuleLimits(
            max_total_long_amount=Decimal("500000"),
            max_single_order_amount=Decimal("200000"),
            default_industry_amount=Decimal("200000"),
            industry_limits={"BANK": Decimal("150000")},
        ))
        eng.register_instrument(InstrumentInfo("600000", industry="BANK"))
        return eng

    def test_bucket_exemption_raises_effective_limit(self):
        eng = self._engine()
        with pytest.raises(RiskRejected):
            eng.approve(buy("O1", "600000", 1600, Decimal("100")))  # 16 万 > 15 万
        eng.grant_exemption(
            "P1", Dimension.INDUSTRY, Decimal("20000"),
            valid_from=T0, valid_until=T0 + timedelta(hours=1),
            bucket="BANK", reason="季末申购窗口", at=T0,
        )
        res, dec = eng.approve(buy("O1", "600000", 1600, Decimal("100")), at=T0)
        assert res.status is ReservationStatus.HELD
        assert dec.exemption_ids  # 决策记录了用到的豁免
        bank = next(c for c in dec.report.checks
                    if c.dimension is Dimension.INDUSTRY and c.bucket == "BANK")
        assert bank.base_limit == Decimal("150000.00")
        assert bank.effective_limit == Decimal("170000.00")

    def test_expired_exemption_not_applied(self):
        eng = self._engine()
        eng.grant_exemption(
            "P1", Dimension.INDUSTRY, Decimal("20000"),
            valid_from=T0 - timedelta(hours=2), valid_until=T0 - timedelta(minutes=1),
            bucket="BANK", at=T0 - timedelta(hours=2),
        )
        with pytest.raises(RiskRejected):
            eng.approve(buy("O1", "600000", 1600, Decimal("100")), at=T0)

    def test_order_specific_exemption_consumed_once(self):
        eng = self._engine()
        ex = eng.grant_exemption(
            "P1", Dimension.INDUSTRY, Decimal("20000"),
            valid_from=T0, valid_until=T0 + timedelta(hours=1),
            bucket="BANK", order_id="O1", at=T0,
        )
        eng.approve(buy("O1", "600000", 1600, Decimal("100")), at=T0)
        assert eng.store.get_exemption(ex.exemption_id).consumed_by_order == "O1"
        # 换另一笔同规模订单，豁免不可复用
        with pytest.raises(RiskRejected):
            eng.approve(buy("O2", "600000", 1600, Decimal("100")), at=T0)

    def test_exemption_other_bucket_not_used(self):
        eng = self._engine()
        eng.grant_exemption(
            "P1", Dimension.INDUSTRY, Decimal("20000"),
            valid_from=T0, valid_until=T0 + timedelta(hours=1),
            bucket="REALTY", at=T0,
        )
        with pytest.raises(RiskRejected):
            eng.approve(buy("O1", "600000", 1600, Decimal("100")), at=T0)


class TestFillsAndReleases:
    """部分成交、撤销、拒单、失效。"""

    def test_partial_fill_shrinks_reservation(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        res, _ = eng.report_fill("O1", 400, Decimal("100"))
        assert res.status is ReservationStatus.PARTIAL
        assert res.open_quantity == 600
        assert res.open_amount == Decimal("60000.00")

        # 持仓已增加 400 股，在途仍冻结 600 股的钱
        view = eng.exposure("P1")
        total = next(c for c in view.checks if c.dimension is Dimension.TOTAL)
        assert total.current == Decimal("40000.00")
        assert total.pending == Decimal("60000.00")

    def test_full_fill_converts_reservation_to_holding(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        res, _ = eng.report_fill("O1", 1000, Decimal("100"))
        assert res.status is ReservationStatus.FILLED
        assert res.open_quantity == 0
        view = eng.exposure("P1")
        total = next(c for c in view.checks if c.dimension is Dimension.TOTAL)
        assert total.current == Decimal("100000.00")
        assert total.pending == Decimal("0.00")

    def test_cancel_releases_remaining_keeps_filled(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 400, Decimal("100"))
        res, dec = eng.cancel("O1", reason="用户改单")
        assert res.status is ReservationStatus.RELEASED
        assert dec.detail["released_quantity"] == 600
        assert dec.detail["already_filled_quantity"] == 400
        # 释放后同行业额度回来：4 万持仓 + 10 万新单 <= 15 万银行桶
        report = eng.check(buy("O2", "600000", 1000, Decimal("100")))
        assert report.approved

    def test_downstream_rejection_rolls_back_all(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        res, dec = eng.report_downstream_rejection("O1", reason="交易所拒单")
        assert res.status is ReservationStatus.REJECTED
        assert dec.action == "REJECT_DOWNSTREAM"
        assert eng.open_reservations("P1") == []
        # 全额回滚后，预算允许等额的新单
        assert eng.check(buy("O2", "000001", 1000, Decimal("100"))).approved

    def test_expiry_releases(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        res, _ = eng.report_expired("O1")
        assert res.status is ReservationStatus.EXPIRED
        assert eng.open_reservations("P1") == []

    def test_invalid_transitions_rejected(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 1000, Decimal("100"))
        with pytest.raises(ValueError):
            eng.report_fill("O1", 1, Decimal("100"))  # 已终结
        with pytest.raises(ValueError):
            eng.cancel("O1")
        with pytest.raises(ValueError):
            eng.report_fill("O404", 1)  # 不存在

    def test_duplicate_approve_rejected(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        with pytest.raises(ValueError):
            eng.approve(buy("O1", "000001", 1000, Decimal("100")))

    def test_fill_cannot_exceed_open_quantity(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        with pytest.raises(ValueError):
            eng.report_fill("O1", 1001, Decimal("100"))

    def test_sell_fill_reduces_holding_and_returns_cash_proceeds(self):
        eng = bank_engine()
        cash_before = eng._cash["P1"]
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 1000, Decimal("100"))
        eng.approve(OrderRequest("S1", "P1", "000001", Side.SELL, 400, Decimal("110")))
        eng.report_fill("S1", 400, Decimal("110"))
        # 卖出现金回笼 4.4 万
        assert eng._cash["P1"] == cash_before - Decimal("100000") + Decimal("44000")
        holdings = eng._holdings["P1"]
        assert holdings["000001"].quantity == 600


class TestQuoteUpdates:
    """报价变化重估。"""

    def test_quote_movement_revalues_holdings_but_not_pending(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 400, Decimal("100"))
        # 第二笔在途买单冻结价 100（行业桶：4 万持仓 + 6 万在途 + 5 万本单 = 15 万）
        eng.approve(buy("O2", "000001", 500, Decimal("100")))

        eng.update_quote("P1", "000001", Decimal("120"))
        view = eng.exposure("P1")
        total = next(c for c in view.checks if c.dimension is Dimension.TOTAL)
        # 持仓 400 股按新价 120 重估 = 4.8 万；在途 600+500 股仍按冻结价 100 = 11 万
        assert total.current == Decimal("48000.00")
        assert total.pending == Decimal("110000.00")
        assert total.projected == Decimal("158000.00")


class TestAudit:
    """审计链。"""

    def test_rejection_is_audited_with_report(self):
        eng = bank_engine()
        with pytest.raises(RiskRejected):
            eng.approve(buy("O1", "600000", 2000, Decimal("100")))
        decisions = eng.decisions("P1", "O1")
        assert len(decisions) == 1
        assert decisions[0].action == "REJECT"
        assert decisions[0].approved is False
        assert decisions[0].report.violations  # 试算快照留底

    def test_full_lifecycle_audit_chain(self):
        eng = bank_engine()
        eng.approve(buy("O1", "000001", 1000, Decimal("100")))
        eng.report_fill("O1", 400, Decimal("100"))
        eng.cancel("O1")
        actions = [(d.action, d.rule_version) for d in eng.decisions("P1", "O1")]
        assert actions == [("APPROVE", 1), ("FILL", 1), ("RELEASE", 1)]


class TestConcurrency:
    """并发下单：串行化评估，绝不超卖预算。"""

    def test_concurrent_approves_never_oversell_budget(self):
        eng = make_engine(cash="100000", limits=RuleLimits(
            max_total_long_amount=Decimal("100000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("100000"),
        ))
        eng.register_instrument(InstrumentInfo("000001", industry="BANK"))

        results = {"approved": 0, "rejected": 0}
        lock = threading.Lock()

        def attempt(i):
            try:
                eng.approve(buy(f"O{i}", "000001", 200, Decimal("10")))  # 每笔 2000
                with lock:
                    results["approved"] += 1
            except RiskRejected:
                with lock:
                    results["rejected"] += 1

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(attempt, range(50)))

        # 总额度 10 万 / 每笔 2000 = 恰好 50 笔可过
        assert results == {"approved": 50, "rejected": 0}
        total_held = sum(r.open_amount for r in eng.open_reservations("P1"))
        assert total_held == Decimal("100000.00")

    def test_concurrent_approves_partial_capacity(self):
        eng = make_engine(cash="100000", limits=RuleLimits(
            max_total_long_amount=Decimal("100000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("100000"),
        ))
        eng.register_instrument(InstrumentInfo("000001", industry="BANK"))

        outcomes = []

        def attempt(i):
            try:
                eng.approve(buy(f"O{i}", "000001", 300, Decimal("10")))  # 每笔 3000
                outcomes.append("ok")
            except RiskRejected:
                outcomes.append("no")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(attempt, range(40)))

        assert outcomes.count("ok") == 33  # floor(100000/3000)
        assert sum(r.open_amount for r in eng.open_reservations("P1")) <= Decimal("100000")
