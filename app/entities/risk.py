"""组合风险预算的持久化表定义。

表设计与 ``app.risk.models`` 中的领域对象一一对应。规则版本与审计
决策只增不改；``risk_current_rule`` 单独保存每个组合的当前版本
指针，换版只移动指针，历史版本行永远保留。
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base

RiskBase = declarative_base()


class RiskInstrumentRow(RiskBase):
    """证券主数据：代码 -> 行业 / 品种。"""

    __tablename__ = "risk_instruments"

    stock_code = Column(String(32), primary_key=True)
    industry = Column(String(64), nullable=False, default="UNKNOWN")
    instrument_type = Column(String(32), nullable=False, default="stock")
    stock_name = Column(String(128), nullable=True)


class RiskRuleVersionRow(RiskBase):
    """不可变规则版本（limits 以 JSON 整体快照存储）。"""

    __tablename__ = "risk_rule_versions"

    portfolio_id = Column(String(64), primary_key=True)
    version = Column(Integer, primary_key=True)
    limits_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False)
    effective_at = Column(DateTime, nullable=False)
    published_by = Column(String(64), nullable=False, default="system")
    note = Column(Text, nullable=False, default="")


class RiskCurrentRuleRow(RiskBase):
    """每个组合当前生效的规则版本指针。"""

    __tablename__ = "risk_current_rule"

    portfolio_id = Column(String(64), primary_key=True)
    current_version = Column(Integer, nullable=False)


class RiskExemptionRow(RiskBase):
    """临时豁免。"""

    __tablename__ = "risk_exemptions"

    exemption_id = Column(String(40), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    dimension = Column(String(16), nullable=False)
    extra_amount = Column(Numeric(20, 2), nullable=False)
    created_at = Column(DateTime, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    bucket = Column(String(64), nullable=True)
    order_id = Column(String(64), nullable=True)
    reason = Column(Text, nullable=False, default="")
    approved_by = Column(String(64), nullable=False, default="risk_manager")
    consumed_by_order = Column(String(64), nullable=True)


class RiskReservationRow(RiskBase):
    """订单对预算的占用（最新状态；状态迁移历史见决策表）。"""

    __tablename__ = "risk_reservations"

    order_id = Column(String(64), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    stock_code = Column(String(32), nullable=False)
    industry = Column(String(64), nullable=False)
    instrument_type = Column(String(32), nullable=False)
    side = Column(String(8), nullable=False)
    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer, nullable=False, default=0)
    frozen_price = Column(Numeric(20, 4), nullable=False)
    rule_version = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    exemption_ids_json = Column(Text, nullable=False, default="[]")

    __table_args__ = (
        Index("ix_risk_reservations_portfolio_status", "portfolio_id", "status"),
    )


class RiskDecisionRow(RiskBase):
    """append-only 决策审计：放行/拒绝/成交/释放，含完整试算快照。"""

    __tablename__ = "risk_decisions"

    decision_id = Column(String(40), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    order_id = Column(String(64), nullable=False, index=True)
    action = Column(String(24), nullable=False)
    approved = Column(Boolean, nullable=False)
    rule_version = Column(Integer, nullable=False)
    at = Column(DateTime, nullable=False, index=True)
    report_json = Column(Text, nullable=True)
    exemption_ids_json = Column(Text, nullable=False, default="[]")
    detail_json = Column(Text, nullable=False, default="{}")
    reason = Column(Text, nullable=False, default="")
