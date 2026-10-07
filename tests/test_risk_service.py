"""TradingService 风险预算接线测试 + 风险 API 端到端测试。"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.trading_service import TradingService, TradingException


def fresh_service(cash=1000000):
    service = TradingService()
    service.connect("simulation", {"initial_cash": cash})
    return service


class TestServiceBudgetEnforcement:
    def test_accumulated_orders_rejected_at_portfolio_level(self):
        service = fresh_service()
        from app.risk.models import RuleLimits
        service.publish_rules(RuleLimits(
            max_total_long_amount=Decimal("150000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("150000"),
        ))
        service.adapter.set_quote("000001", 100.0)
        service.adapter.set_quote("600000", 100.0)

        first = service.buy("000001", 1000, 100.0)
        assert first["status"] == "filled"
        assert first["risk_rule_version"] == 2

        with pytest.raises(TradingException) as exc:
            service.buy("600000", 1000, 100.0)
        assert exc.value.code == "RISK_REJECTED"
        assert exc.value.status_code == 403
        report = exc.value.details["report"]
        assert any(v["dimension"] == "total" for v in report["violations"])

    def test_pending_orders_share_budget(self):
        service = fresh_service()
        from app.risk.models import RuleLimits
        service.publish_rules(RuleLimits(
            max_total_long_amount=Decimal("150000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("150000"),
        ))
        # 行情 110 高于限价 100：买单挂在途不成交
        service.adapter.set_quote("000001", 110.0)
        service.buy("000001", 1000, 100.0)
        service.buy("000001", 500, 100.0)  # 在途累计 15 万

        open_orders = service.risk_open_orders()
        assert sum(o["open_quantity"] for o in open_orders) == 1500

        with pytest.raises(TradingException) as exc:
            service.buy("000001", 100, 100.0)  # 再加 1 万即超限
        assert exc.value.code == "RISK_REJECTED"

    def test_exemption_allows_then_is_consumed(self):
        service = fresh_service()
        from app.risk.models import Dimension, RuleLimits
        # 行业桶放宽，使总敞口成为唯一约束
        service.publish_rules(RuleLimits(
            max_total_long_amount=Decimal("150000"),
            max_single_order_amount=Decimal("100000"),
            default_industry_amount=Decimal("300000"),
        ))
        service.adapter.set_quote("000001", 100.0)
        service.adapter.set_quote("600000", 100.0)
        service.buy("000001", 1000, 100.0)

        # 无豁免被拒
        with pytest.raises(TradingException):
            service.buy("600000", 1000, 100.0)

        # 给总敞口追加 10 万豁免后放行
        service.grant_exemption(
            dimension=Dimension.TOTAL.value,
            extra_amount=100000,
            valid_minutes=60,
            bucket="portfolio",
            reason="打新预留",
        )
        result = service.buy("600000", 1000, 100.0)
        assert result["status"] == "filled"

    def test_audit_chain_records_every_decision(self):
        service = fresh_service()
        from app.risk.models import RuleLimits
        service.publish_rules(RuleLimits(
            max_total_long_amount=Decimal("150000"),
            max_single_order_amount=Decimal("100000"),
        ))
        service.adapter.set_quote("000001", 100.0)
        service.adapter.set_quote("600000", 100.0)
        service.buy("000001", 1000, 100.0)
        with pytest.raises(TradingException):
            service.buy("600000", 1000, 100.0)

        decisions = service.risk_audit()
        actions = [d["action"] for d in decisions]
        assert "APPROVE" in actions
        assert "REJECT" in actions
        # 每条审计都带规则版本
        assert all(d["rule_version"] in (1, 2) for d in decisions)
        rejected = next(d for d in decisions if d["action"] == "REJECT")
        assert rejected["report"]["violations"]

    def test_sell_beyond_holding_rejected(self):
        service = fresh_service()
        service.adapter.set_quote("000001", 100.0)
        service.buy("000001", 1000, 100.0)
        with pytest.raises(TradingException) as exc:
            service.sell("000001", 2000, 100.0)
        # 先被服务层的可用持仓校验拦截
        assert "持仓不足" in str(exc.value.message)

    def test_cancel_pending_releases_budget(self):
        service = fresh_service()
        from app.risk.models import RuleLimits
        service.publish_rules(RuleLimits(
            max_total_long_amount=Decimal("150000"),
            max_single_order_amount=Decimal("100000"),
        ))
        service.adapter.set_quote("000001", 110.0)
        order = service.buy("000001", 1000, 100.0)  # 挂在途
        assert order["status"] == "submitted"

        service.cancel_order(order["order_id"])
        # 释放后同等额度的新单可过
        service.adapter.set_quote("000001", 110.0)
        again = service.buy("000001", 1000, 100.0)
        assert again["status"] == "submitted"

    def test_rules_endpoint_view(self):
        service = fresh_service()
        info = service.risk_rules()
        assert info["version"] == 1
        assert "max_total_long_amount" in info["limits"]


@pytest.fixture(scope="module")
def client():
    from app.config import init_database
    init_database()
    with TestClient(app) as c:
        yield c


class TestRiskAPI:
    """风险预算 HTTP 接口端到端。"""

    def test_full_flow(self, client):
        # 连接模拟账户
        r = client.post("/api/trading/connect", json={
            "adapter_type": "simulation",
            "config": {"initial_cash": 500000},
        })
        assert r.status_code == 200

        # 登记证券行业
        r = client.post("/api/trading/risk/instruments", json={
            "stock_code": "000001", "industry": "BANK",
        })
        assert r.status_code == 200

        # 种子行情（模拟行情源推送）
        from app.controllers.trading_controller import trading_service
        trading_service.adapter.set_quote("000001", 100.0)

        # 收紧规则（行业桶放宽，使总敞口成为唯一约束）
        r = client.post("/api/trading/risk/rules", json={
            "max_total_long_amount": 150000,
            "max_single_order_amount": 100000,
            "default_industry_amount": 300000,
        })
        assert r.json()["version"] == 2

        # 查看额度全景
        r = client.get("/api/trading/risk/exposure")
        assert r.status_code == 200
        data = r.json()
        assert data["rule_version"] == 2
        total = next(c for c in data["checks"] if c["dimension"] == "total")
        assert total["remaining_after"] == "150000.00"

        # 第一笔放行
        r = client.post("/api/trading/buy", json={
            "stock_code": "000001", "quantity": 1000, "price": 100.0,
        })
        assert r.status_code == 200

        # 第二笔超总敞口被拒 403
        r = client.post("/api/trading/buy", json={
            "stock_code": "000001", "quantity": 1000, "price": 100.0,
        })
        assert r.status_code == 403
        body = r.json()
        assert body["error"]["code"] == "RISK_REJECTED"
        assert body["error"]["details"]["report"]

        # 授予豁免
        r = client.post("/api/trading/risk/exemptions", json={
            "dimension": "total", "extra_amount": 100000,
            "valid_minutes": 60, "bucket": "portfolio",
        })
        assert r.status_code == 200

        # 豁免后放行
        r = client.post("/api/trading/buy", json={
            "stock_code": "000001", "quantity": 1000, "price": 100.0,
        })
        assert r.status_code == 200

        # 审计链可查
        r = client.get("/api/trading/risk/audit")
        assert r.status_code == 200
        actions = [d["action"] for d in r.json()["decisions"]]
        assert "REJECT" in actions and "APPROVE" in actions
