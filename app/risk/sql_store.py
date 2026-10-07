"""``RiskStore`` 的 SQLAlchemy 实现。

事务语义
--------
引擎在一次状态迁移内会连续调用多个存储方法（写预留、消费豁免、
追加审计）。这些调用必须落在同一个数据库事务里：``transaction()``
开启 Session 并绑定到线程局部，块内所有方法复用该 Session，正常
退出统一 commit，抛异常则 rollback —— 不会留下“预留已写但审计
缺失”的半成品状态。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.entities.risk import (
    RiskBase,
    RiskCurrentRuleRow,
    RiskDecisionRow,
    RiskExemptionRow,
    RiskInstrumentRow,
    RiskReservationRow,
    RiskRuleVersionRow,
)
from app.risk.models import (
    Dimension,
    Exemption,
    InstrumentInfo,
    Reservation,
    ReservationStatus,
    RiskDecision,
    ExposureReport,
    RuleLimits,
    RuleVersion,
    Side,
)
from app.risk.store import RiskStore


def init_risk_tables(engine) -> None:
    """在给定引擎上创建风险预算相关表。"""
    RiskBase.metadata.create_all(bind=engine)


class SqlRiskStore(RiskStore):
    """基于 SQLAlchemy 的风险状态存储（SQLite / Postgres 均可）。"""

    def __init__(self, url: str = "sqlite:///:memory:", engine=None) -> None:
        if engine is not None:
            self._engine = engine
        else:
            self._engine = create_engine(
                url,
                connect_args=(
                    {"check_same_thread": False} if "sqlite" in url else {}
                ),
            )
        init_risk_tables(self._engine)
        self._Session = sessionmaker(bind=self._engine, autoflush=False)
        self._local = threading.local()

    # -- 事务 ------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        existing = getattr(self._local, "session", None)
        if existing is not None:
            # 可重入（引擎按组合持锁，正常不会嵌套；保留语义对称）
            yield
            return
        session = self._Session()
        self._local.session = session
        try:
            yield
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            self._local.session = None

    def _session(self) -> Session:
        session = getattr(self._local, "session", None)
        if session is None:
            raise RuntimeError("存储操作必须在 _auto() 或 transaction() 上下文中")
        return session

    @contextmanager
    def _auto(self) -> Iterator[Session]:
        bound = getattr(self._local, "session", None)
        if bound is not None:
            # 已处于引擎开启的事务内：复用，提交/回滚由外层负责
            yield bound
            return
        session = self._Session()
        self._local.session = session
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            self._local.session = None

    # -- 证券主数据 ------------------------------------------------------
    def upsert_instrument(self, info: InstrumentInfo) -> None:
        with self._auto() as s:
            row = s.get(RiskInstrumentRow, info.stock_code)
            if row is None:
                s.add(
                    RiskInstrumentRow(
                        stock_code=info.stock_code,
                        industry=info.industry,
                        instrument_type=info.instrument_type,
                        stock_name=info.stock_name,
                    )
                )
            else:
                row.industry = info.industry
                row.instrument_type = info.instrument_type
                row.stock_name = info.stock_name

    def get_instrument(self, stock_code: str) -> Optional[InstrumentInfo]:
        with self._auto() as s:
            row = s.get(RiskInstrumentRow, stock_code)
            return None if row is None else self._to_instrument(row)

    @staticmethod
    def _to_instrument(row: RiskInstrumentRow) -> InstrumentInfo:
        return InstrumentInfo(
            stock_code=row.stock_code,
            industry=row.industry,
            instrument_type=row.instrument_type,
            stock_name=row.stock_name,
        )

    # -- 规则版本 --------------------------------------------------------
    def add_rule_version(self, rule: RuleVersion) -> None:
        with self._auto() as s:
            if s.get(RiskRuleVersionRow, (rule.portfolio_id, rule.version)):
                raise ValueError(
                    f"规则版本已存在: {rule.portfolio_id} v{rule.version}"
                )
            current = s.get(RiskCurrentRuleRow, rule.portfolio_id)
            if current is not None and rule.version <= current.current_version:
                raise ValueError("新版本号必须大于当前版本号")
            s.add(
                RiskRuleVersionRow(
                    portfolio_id=rule.portfolio_id,
                    version=rule.version,
                    limits_json=json.dumps(rule.limits.to_dict(), ensure_ascii=False),
                    created_at=rule.created_at,
                    effective_at=rule.effective_at,
                    published_by=rule.published_by,
                    note=rule.note,
                )
            )
            if current is None:
                s.add(
                    RiskCurrentRuleRow(
                        portfolio_id=rule.portfolio_id,
                        current_version=rule.version,
                    )
                )
            else:
                current.current_version = rule.version

    def get_current_rule_version(self, portfolio_id: str) -> Optional[RuleVersion]:
        with self._auto() as s:
            current = s.get(RiskCurrentRuleRow, portfolio_id)
            if current is None:
                return None
            row = s.get(
                RiskRuleVersionRow, (portfolio_id, current.current_version)
            )
            return None if row is None else self._to_rule(row)

    def get_rule_version(
        self, portfolio_id: str, version: int
    ) -> Optional[RuleVersion]:
        with self._auto() as s:
            row = s.get(RiskRuleVersionRow, (portfolio_id, version))
            return None if row is None else self._to_rule(row)

    def list_rule_versions(self, portfolio_id: str) -> List[RuleVersion]:
        with self._auto() as s:
            rows = (
                s.query(RiskRuleVersionRow)
                .filter_by(portfolio_id=portfolio_id)
                .order_by(RiskRuleVersionRow.version)
                .all()
            )
            return [self._to_rule(r) for r in rows]

    @staticmethod
    def _to_rule(row: RiskRuleVersionRow) -> RuleVersion:
        return RuleVersion(
            portfolio_id=row.portfolio_id,
            version=row.version,
            limits=RuleLimits.from_dict(json.loads(row.limits_json)),
            created_at=row.created_at,
            effective_at=row.effective_at,
            published_by=row.published_by,
            note=row.note,
        )

    # -- 豁免 ------------------------------------------------------------
    def add_exemption(self, exemption: Exemption) -> None:
        with self._auto() as s:
            if s.get(RiskExemptionRow, exemption.exemption_id):
                raise ValueError(f"豁免已存在: {exemption.exemption_id}")
            s.add(self._exemption_row(exemption))

    def get_exemption(self, exemption_id: str) -> Optional[Exemption]:
        with self._auto() as s:
            row = s.get(RiskExemptionRow, exemption_id)
            return None if row is None else self._to_exemption(row)

    def list_exemptions(self, portfolio_id: str) -> List[Exemption]:
        with self._auto() as s:
            rows = (
                s.query(RiskExemptionRow)
                .filter_by(portfolio_id=portfolio_id)
                .all()
            )
            return [self._to_exemption(r) for r in rows]

    def consume_exemption(self, exemption_id: str, order_id: str) -> None:
        with self._auto() as s:
            row = s.get(RiskExemptionRow, exemption_id)
            if row is None:
                raise ValueError(f"豁免不存在: {exemption_id}")
            if row.consumed_by_order not in (None, order_id):
                raise ValueError(
                    f"豁免 {exemption_id} 已被订单 {row.consumed_by_order} 消费"
                )
            row.consumed_by_order = order_id

    @staticmethod
    def _exemption_row(ex: Exemption) -> RiskExemptionRow:
        return RiskExemptionRow(
            exemption_id=ex.exemption_id,
            portfolio_id=ex.portfolio_id,
            dimension=ex.dimension.value,
            extra_amount=ex.extra_amount,
            created_at=ex.created_at,
            valid_from=ex.valid_from,
            valid_until=ex.valid_until,
            bucket=ex.bucket,
            order_id=ex.order_id,
            reason=ex.reason,
            approved_by=ex.approved_by,
            consumed_by_order=ex.consumed_by_order,
        )

    @staticmethod
    def _to_exemption(row: RiskExemptionRow) -> Exemption:
        return Exemption(
            exemption_id=row.exemption_id,
            portfolio_id=row.portfolio_id,
            dimension=Dimension(row.dimension),
            extra_amount=Decimal(row.extra_amount),
            created_at=row.created_at,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            bucket=row.bucket,
            order_id=row.order_id,
            reason=row.reason,
            approved_by=row.approved_by,
            consumed_by_order=row.consumed_by_order,
        )

    # -- 预留 ------------------------------------------------------------
    def put_reservation(self, reservation: Reservation) -> None:
        with self._auto() as s:
            row = s.get(RiskReservationRow, reservation.order_id)
            data = dict(
                portfolio_id=reservation.portfolio_id,
                stock_code=reservation.stock_code,
                industry=reservation.industry,
                instrument_type=reservation.instrument_type,
                side=reservation.side.value,
                quantity=reservation.quantity,
                filled_quantity=reservation.filled_quantity,
                frozen_price=reservation.frozen_price,
                rule_version=reservation.rule_version,
                status=reservation.status.value,
                created_at=reservation.created_at,
                updated_at=reservation.updated_at,
                exemption_ids_json=json.dumps(reservation.exemption_ids),
            )
            if row is None:
                s.add(RiskReservationRow(order_id=reservation.order_id, **data))
            else:
                for k, v in data.items():
                    setattr(row, k, v)

    def get_reservation(self, order_id: str) -> Optional[Reservation]:
        with self._auto() as s:
            row = s.get(RiskReservationRow, order_id)
            return None if row is None else self._to_reservation(row)

    def list_reservations(
        self, portfolio_id: str, open_only: bool = False
    ) -> List[Reservation]:
        with self._auto() as s:
            q = s.query(RiskReservationRow).filter_by(portfolio_id=portfolio_id)
            if open_only:
                q = q.filter(
                    RiskReservationRow.status.in_(
                        [ReservationStatus.HELD.value, ReservationStatus.PARTIAL.value]
                    )
                )
            rows = q.order_by(RiskReservationRow.created_at).all()
            return [self._to_reservation(r) for r in rows]

    @staticmethod
    def _to_reservation(row: RiskReservationRow) -> Reservation:
        return Reservation(
            order_id=row.order_id,
            portfolio_id=row.portfolio_id,
            stock_code=row.stock_code,
            industry=row.industry,
            instrument_type=row.instrument_type,
            side=Side(row.side),
            quantity=row.quantity,
            filled_quantity=row.filled_quantity,
            frozen_price=Decimal(str(row.frozen_price)),
            rule_version=row.rule_version,
            status=ReservationStatus(row.status),
            created_at=row.created_at,
            updated_at=row.updated_at,
            exemption_ids=json.loads(row.exemption_ids_json or "[]"),
        )

    # -- 审计决策 --------------------------------------------------------
    def append_decision(self, decision: RiskDecision) -> None:
        with self._auto() as s:
            s.add(
                RiskDecisionRow(
                    decision_id=decision.decision_id,
                    portfolio_id=decision.portfolio_id,
                    order_id=decision.order_id,
                    action=decision.action,
                    approved=decision.approved,
                    rule_version=decision.rule_version,
                    at=decision.at,
                    report_json=(
                        json.dumps(decision.report.to_dict(), ensure_ascii=False)
                        if decision.report is not None
                        else None
                    ),
                    exemption_ids_json=json.dumps(decision.exemption_ids),
                    detail_json=json.dumps(decision.detail, ensure_ascii=False, default=str),
                    reason=decision.reason,
                )
            )

    def list_decisions(
        self,
        portfolio_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> List[RiskDecision]:
        with self._auto() as s:
            q = s.query(RiskDecisionRow)
            if portfolio_id is not None:
                q = q.filter_by(portfolio_id=portfolio_id)
            if order_id is not None:
                q = q.filter_by(order_id=order_id)
            rows = q.order_by(RiskDecisionRow.at).all()
            return [
                RiskDecision(
                    decision_id=r.decision_id,
                    portfolio_id=r.portfolio_id,
                    order_id=r.order_id,
                    action=r.action,
                    approved=bool(r.approved),
                    rule_version=r.rule_version,
                    at=r.at,
                    report=(
                        ExposureReport.from_dict(json.loads(r.report_json))
                        if r.report_json
                        else None
                    ),
                    exemption_ids=json.loads(r.exemption_ids_json or "[]"),
                    detail=json.loads(r.detail_json or "{}"),
                    reason=r.reason,
                )
                for r in rows
            ]
