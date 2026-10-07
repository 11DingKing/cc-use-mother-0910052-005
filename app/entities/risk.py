"""组合级风控的持久化实体。

四张核心表：
- risk_rule_versions：规则集版本快照（append-only，不可更新/删除）
- risk_exemptions：临时豁免的版本行（追加新行实现换版/撤销）
- risk_orders：订单风控台账（额度预留与生命周期）
- risk_decisions：每次风控判定的审计记录（含规则快照，可按当时口径复核）
- risk_instruments：证券行业/品种分类
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.types import JSON

Base = declarative_base()


class RiskRuleVersionEntity(Base):
    """规则集版本：发布后只读，换版通过追加新版本行实现。"""

    __tablename__ = "risk_rule_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    # 完整规则快照（limits、单笔限额等），决策表也会冗余一份，双保险
    snapshot = Column(JSON, nullable=False)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_by = Column(String(64), nullable=False, default="system")
    note = Column(String(500), nullable=True, default="")

    __table_args__ = (
        UniqueConstraint("portfolio_id", "version", name="uix_risk_rule_portfolio_version"),
    )

    def __repr__(self):
        return f"<RiskRuleVersion(portfolio={self.portfolio_id}, version={self.version})>"


class RiskExemptionEntity(Base):
    """临时豁免版本行；同一豁免的换版/撤销以新行追加。"""

    __tablename__ = "risk_exemptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    exemption_id = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    dimension = Column(String(16), nullable=False)
    target = Column(String(128), nullable=False)
    extra_amount = Column(String(32), nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=False)
    reason = Column(String(500), nullable=False, default="")
    granted_by = Column(String(64), nullable=False, default="system")
    status = Column(String(16), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "exemption_id", "version",
            name="uix_risk_exemption_version",
        ),
        Index("ix_risk_exemption_rule", "portfolio_id", "dimension", "target"),
    )

    def __repr__(self):
        return (
            f"<RiskExemption(id={self.exemption_id}, v{self.version}, "
            f"{self.dimension}:{self.target}, status={self.status})>"
        )


class RiskOrderEntity(Base):
    """订单风控台账：买单从预留到成交/撤销/拒绝的完整生命周期。"""

    __tablename__ = "risk_orders"

    order_id = Column(String(64), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False, index=True)
    side = Column(String(8), nullable=False)
    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer, nullable=False, default=0)
    remaining_quantity = Column(Integer, nullable=False)
    price = Column(String(32), nullable=False)
    industry = Column(String(64), nullable=False, default="其他")
    instrument_type = Column(String(32), nullable=False, default="STOCK")
    status = Column(String(16), nullable=False, index=True)
    rule_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_risk_orders_portfolio_status", "portfolio_id", "status"),
    )

    def __repr__(self):
        return f"<RiskOrder({self.order_id}, status={self.status}, remain={self.remaining_quantity})>"


class RiskDecisionEntity(Base):
    """风控审计记录：放行/拒绝的全部输入、口径与逐条原因。"""

    __tablename__ = "risk_decisions"

    decision_id = Column(String(64), primary_key=True)
    portfolio_id = Column(String(64), nullable=False, index=True)
    order_id = Column(String(64), nullable=False, index=True)
    action = Column(String(16), nullable=False)  # PRE_CHECK/RESERVE/UPDATE/RELEASE
    passed = Column(Boolean, nullable=False)
    rule_version = Column(Integer, nullable=False)
    evaluated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    # 决策时使用的完整规则快照与豁免快照，保证历史决策可独立复核
    rule_snapshot = Column(JSON, nullable=False)
    payload = Column(JSON, nullable=False)
    summary = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_risk_decisions_order_time", "order_id", "evaluated_at"),
    )

    def __repr__(self):
        return f"<RiskDecision({self.decision_id}, order={self.order_id}, passed={self.passed})>"


class RiskInstrumentEntity(Base):
    """证券分类：行业与品种，决定敞口归属的桶。"""

    __tablename__ = "risk_instruments"

    portfolio_id = Column(String(64), primary_key=True, default="default")
    stock_code = Column(String(20), primary_key=True)
    industry = Column(String(64), nullable=False, default="其他")
    instrument_type = Column(String(32), nullable=False, default="STOCK")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<RiskInstrument({self.stock_code}, {self.industry}, {self.instrument_type})>"
