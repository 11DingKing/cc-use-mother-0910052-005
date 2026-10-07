"""组合级风控 API 集成测试。"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        # 使用独立代码，避免与其他模块的全局单例状态耦合
        c.post("/api/trading/connect", json={
            "adapter_type": "simulation", "config": {"initial_cash": 1_000_000},
        })
        yield c


def _publish(client, total="60000", single="100000", industries=(("银行", "60000"),)):
    limits = [{"dimension": "total", "target": "TOTAL", "max_exposure": total}]
    if industries:
        for name, amount in industries:
            limits.append({"dimension": "industry", "target": name, "max_exposure": amount})
    resp = client.post("/api/risk/rules", json={
        "limits": limits,
        "max_single_order_amount": single,
        "note": "api-test",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["version"]


class TestRiskAPI:

    def test_01_publish_and_list_versions(self, client):
        v = _publish(client)
        resp = client.get("/api/risk/rules")
        versions = [x["version"] for x in resp.json()["versions"]]
        assert v in versions

        resp = client.get(f"/api/risk/rules/{v}")
        assert resp.status_code == 200
        assert resp.json()["version"] == v

    def test_02_instrument_classification(self, client):
        resp = client.put("/api/risk/instruments", json={
            "stock_code": "600111", "industry": "银行", "instrument_type": "STOCK",
        })
        assert resp.status_code == 200
        assert resp.json()["industry"] == "银行"

    def test_03_combined_orders_second_rejected_with_reasons(self, client):
        # 第一笔 40000 放行成交
        resp = client.post("/api/trading/buy", json={
            "stock_code": "600111", "quantity": 4000, "price": 10.0,
        })
        assert resp.status_code == 200, resp.text
        first = resp.json()
        assert first["status"] == "filled"
        assert first["risk_rule_version"] >= 1

        # 第二笔 40000 → 合计 80000 > 60000
        resp = client.post("/api/trading/buy", json={
            "stock_code": "600111", "quantity": 4000, "price": 10.0,
        })
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"]["code"] == "RISK_REJECTED"
        reasons = body["error"]["details"]["reasons"]
        assert any("total:TOTAL" in r and "超过" in r for r in reasons)

    def test_04_residual_report_explains_numbers(self, client):
        resp = client.get("/api/risk/residual")
        assert resp.status_code == 200
        data = resp.json()
        total = next(l for l in data["lines"] if l["target"] == "TOTAL")
        assert Decimal(total["position_used"]) == Decimal("40000")
        assert Decimal(total["pending_used"]) == Decimal("0")
        assert Decimal(total["residual"]) == Decimal("20000")

    def test_05_precheck_does_not_reserve(self, client):
        # 银行桶已用 40000，再买 30000 会超 60000
        resp = client.post("/api/risk/precheck", json={
            "stock_code": "600111", "quantity": 3000, "price": 10.0,
        })
        assert resp.status_code == 200
        decision = resp.json()
        assert decision["action"] == "PRE_CHECK"
        assert decision["passed"] is False
        # 试算不产生预留
        ledger = client.get("/api/risk/orders").json()["orders"]
        assert all(o["order_id"] != decision["order_id"] for o in ledger)

    def test_06_exemption_allows_then_revoke_blocks(self, client):
        grant = client.post("/api/risk/exemptions", json={
            "dimension": "total", "target": "TOTAL", "extra_amount": "30000",
            "reason": "临时提额",
        })
        assert grant.status_code == 200
        ex = grant.json()
        assert ex["status"] == "active"
        # 银行桶也需豁免：持仓 40000 + 新买 40000 > 60000
        client.post("/api/risk/exemptions", json={
            "dimension": "industry", "target": "银行", "extra_amount": "30000",
            "reason": "行业临时提额",
        })

        # 40000 持仓 + 40000 新买 = 80000 ≤ 60000 + 30000 豁免
        resp = client.post("/api/trading/buy", json={
            "stock_code": "600111", "quantity": 4000, "price": 10.0,
        })
        assert resp.status_code == 200, resp.text

        # 审计决策记录了豁免
        decision_id = resp.json()["risk_decision_id"]
        d = client.get(f"/api/risk/decisions/{decision_id}").json()
        total_result = next(r for r in d["results"] if r["dimension"] == "total")
        assert Decimal(total_result["exempted_amount"]) == Decimal("30000")
        assert len(total_result["exemptions"]) == 1

        # 撤销豁免 → 追加版本行
        revoke = client.post(f"/api/risk/exemptions/{ex['exemption_id']}/revoke")
        assert revoke.status_code == 200
        assert revoke.json()["version"] == 2
        assert revoke.json()["status"] == "revoked"

    def test_07_order_trail_is_auditable(self, client):
        # 找一笔已成交订单
        orders = client.get("/api/trading/orders").json()["orders"]
        oid = orders[0]["order_id"]
        trail = client.get(f"/api/risk/orders/{oid}/trail")
        assert trail.status_code == 200
        data = trail.json()
        assert data["ledger"]["order_id"] == oid
        actions = {d["action"] for d in data["decisions"]}
        assert "RESERVE" in actions
        # 决策内嵌规则快照
        reserve = next(d for d in data["decisions"] if d["action"] == "RESERVE")
        assert "rule_snapshot" in reserve and reserve["rule_snapshot"]["limits"]

    def test_08_invalid_rules_rejected(self, client):
        resp = client.post("/api/risk/rules", json={
            "limits": [{"dimension": "bogus", "target": "x", "max_exposure": "1"}],
        })
        assert resp.status_code == 400
