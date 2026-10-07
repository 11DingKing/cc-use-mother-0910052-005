"""组合级风控的数据访问层。

职责：
- 规则版本、豁免版本、订单台账、审计决策、证券分类的持久化；
- 以"每个组合一把进程内可重入锁 + 数据库事务"界定清晰的事务边界，
  把"检查剩余额度 + 写入预留"串行化，杜绝并发下单各自看到旧余额导致
  合计超预算；
- 所有版本数据 append-only：规则换版、豁免撤销都只追加新行，历史行
  永不修改，供审计按当时口径复核。
"""

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import create_engine, desc
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from app.entities.risk import (
    Base,
    RiskDecisionEntity,
    RiskExemptionEntity,
    RiskInstrumentEntity,
    RiskOrderEntity,
    RiskRuleVersionEntity,
)
from app.risk.models import (
    Exemption,
    ExemptionStatus,
    Instrument,
    LedgerEntry,
    LedgerStatus,
    RiskDecision,
    RiskDimension,
    RiskLimit,
    RuleCheckResult,
    RuleSet,
)

logger = logging.getLogger(__name__)


class RiskStoreError(Exception):
    """数据访问层错误（如台账中订单已存在、版本不存在）。"""


class DuplicateReservationError(RiskStoreError):
    """订单已在台账中预留：用于调用方实现幂等重试。"""

    def __init__(self, order_id: str, existing: LedgerEntry):
        super().__init__(f"订单 {order_id} 已存在风控台账记录")
        self.order_id = order_id
        self.existing = existing


def _now() -> datetime:
    return datetime.now()


class RiskStore:
    """风控持久化与事务边界。"""

    # 组合级串行锁：同一组合的预检/预留/状态变更在进程内互斥
    _portfolio_locks: Dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, engine: Optional[Engine] = None, portfolio_id: str = "default"):
        if engine is None:
            from app.config import get_engine
            engine = get_engine()
        self.engine = engine
        self.portfolio_id = portfolio_id
        self._session_factory = sessionmaker(
            bind=engine, autocommit=False, autoflush=False
        )
        self.init_schema()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        Base.metadata.create_all(bind=self.engine)

    @classmethod
    def in_memory(cls, portfolio_id: str = "default") -> "RiskStore":
        """供测试使用的共享内存 SQLite 存储。"""
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        return cls(engine=engine, portfolio_id=portfolio_id)

    def _lock_for(self, portfolio_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._portfolio_locks.get(portfolio_id)
            if lock is None:
                lock = threading.RLock()
                self._portfolio_locks[portfolio_id] = lock
            return lock

    @contextmanager
    def transaction(self, portfolio_id: Optional[str] = None):
        """开启一个串行化事务。

        同一组合内事务互斥执行；事务内先读余额再写预留，提交前锁不释放，
        因此并发下单不会出现双花。异常自动回滚（撤单/拒绝回滚的边界）。
        """
        pid = portfolio_id or self.portfolio_id
        lock = self._lock_for(pid)
        lock.acquire()
        session: Session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            lock.release()

    # ------------------------------------------------------------------
    # 证券分类
    # ------------------------------------------------------------------

    def upsert_instrument(
        self,
        stock_code: str,
        industry: str = "其他",
        instrument_type: str = "STOCK",
        portfolio_id: Optional[str] = None,
    ) -> Instrument:
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            entity = (
                session.query(RiskInstrumentEntity)
                .filter(
                    RiskInstrumentEntity.portfolio_id == pid,
                    RiskInstrumentEntity.stock_code == stock_code,
                )
                .first()
            )
            if entity is None:
                entity = RiskInstrumentEntity(
                    portfolio_id=pid,
                    stock_code=stock_code,
                    industry=industry,
                    instrument_type=instrument_type,
                )
                session.add(entity)
            else:
                entity.industry = industry
                entity.instrument_type = instrument_type
                entity.updated_at = _now()
            session.flush()
            return Instrument(stock_code, entity.industry, entity.instrument_type)

    def classify(self, stock_code: str, portfolio_id: Optional[str] = None) -> Instrument:
        """取证券分类；未登记时按默认桶（其他/STOCK）处理，不阻塞交易。"""
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            entity = (
                session.query(RiskInstrumentEntity)
                .filter(
                    RiskInstrumentEntity.portfolio_id == pid,
                    RiskInstrumentEntity.stock_code == stock_code,
                )
                .first()
            )
            if entity is None:
                return Instrument(stock_code=stock_code)
            return Instrument(
                stock_code=entity.stock_code,
                industry=entity.industry,
                instrument_type=entity.instrument_type,
            )

    def classify_many(
        self, stock_codes: List[str], portfolio_id: Optional[str] = None
    ) -> Dict[str, Instrument]:
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            return self._classify_many(session, stock_codes, pid)

    @staticmethod
    def _classify_many(
        session: Session, stock_codes: List[str], pid: Optional[str] = None
    ) -> Dict[str, Instrument]:
        result = {code: Instrument(stock_code=code) for code in stock_codes}
        query = session.query(RiskInstrumentEntity)
        if pid is not None:
            query = query.filter(RiskInstrumentEntity.portfolio_id == pid)
        entities = query.filter(RiskInstrumentEntity.stock_code.in_(stock_codes)).all()
        for entity in entities:
            result[entity.stock_code] = Instrument(
                stock_code=entity.stock_code,
                industry=entity.industry,
                instrument_type=entity.instrument_type,
            )
        return result

    # ------------------------------------------------------------------
    # 规则版本（append-only）
    # ------------------------------------------------------------------

    def publish_ruleset(
        self,
        limits: List[RiskLimit],
        max_single_order_amount: Decimal = Decimal("100000"),
        created_by: str = "system",
        note: str = "",
        portfolio_id: Optional[str] = None,
    ) -> RuleSet:
        """发布新版本规则集。版本号在事务内递增，旧版本保持只读。"""
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            latest = (
                session.query(RiskRuleVersionEntity)
                .filter(RiskRuleVersionEntity.portfolio_id == pid)
                .order_by(desc(RiskRuleVersionEntity.version))
                .first()
            )
            next_version = 1 if latest is None else latest.version + 1
            ruleset = RuleSet(
                version=next_version,
                limits=tuple(limits),
                max_single_order_amount=max_single_order_amount,
                portfolio_id=pid,
                published_at=_now(),
                created_by=created_by,
                note=note,
            )
            entity = RiskRuleVersionEntity(
                portfolio_id=pid,
                version=next_version,
                snapshot=json.loads(json.dumps(ruleset.to_dict())),
                published_at=ruleset.published_at,
                created_by=created_by,
                note=note,
            )
            session.add(entity)
            session.flush()
            logger.info("发布组合 %s 风控规则版本 v%d", pid, next_version)
            return ruleset

    def get_ruleset(
        self, version: Optional[int] = None, portfolio_id: Optional[str] = None
    ) -> Optional[RuleSet]:
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            return self._get_ruleset(session, pid, version)

    @staticmethod
    def _get_ruleset(
        session: Session, pid: str, version: Optional[int] = None
    ) -> Optional[RuleSet]:
        query = session.query(RiskRuleVersionEntity).filter(
            RiskRuleVersionEntity.portfolio_id == pid
        )
        if version is None:
            entity = query.order_by(desc(RiskRuleVersionEntity.version)).first()
        else:
            entity = query.filter(RiskRuleVersionEntity.version == version).first()
        return RiskStore._ruleset_from_entity(entity) if entity else None

    def list_rule_versions(self, portfolio_id: Optional[str] = None) -> List[RuleSet]:
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            entities = (
                session.query(RiskRuleVersionEntity)
                .filter(RiskRuleVersionEntity.portfolio_id == pid)
                .order_by(RiskRuleVersionEntity.version)
                .all()
            )
            return [self._ruleset_from_entity(e) for e in entities]

    @staticmethod
    def _ruleset_from_entity(entity: RiskRuleVersionEntity) -> RuleSet:
        snap = entity.snapshot
        return RuleSet(
            version=entity.version,
            limits=tuple(
                RiskLimit(
                    dimension=RiskDimension(item["dimension"]),
                    target=item["target"],
                    max_exposure=Decimal(item["max_exposure"]),
                    note=item.get("note", ""),
                )
                for item in snap.get("limits", [])
            ),
            max_single_order_amount=Decimal(snap.get("max_single_order_amount", "100000")),
            portfolio_id=entity.portfolio_id,
            published_at=entity.published_at,
            created_by=entity.created_by,
            note=entity.note or "",
        )

    # ------------------------------------------------------------------
    # 临时豁免（版本行 append-only）
    # ------------------------------------------------------------------

    def grant_exemption(
        self,
        dimension: RiskDimension,
        target: str,
        extra_amount: Decimal,
        valid_from: datetime,
        valid_to: datetime,
        reason: str,
        granted_by: str = "system",
        portfolio_id: Optional[str] = None,
    ) -> Exemption:
        if valid_to <= valid_from:
            raise RiskStoreError("豁免失效时间必须晚于生效时间")
        if extra_amount <= 0:
            raise RiskStoreError("豁免额度必须为正数")
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            exemption = Exemption(
                exemption_id=f"EXM_{uuid.uuid4().hex[:12]}",
                version=1,
                dimension=dimension,
                target=target,
                extra_amount=extra_amount,
                valid_from=valid_from,
                valid_to=valid_to,
                reason=reason,
                granted_by=granted_by,
                status=ExemptionStatus.ACTIVE,
                created_at=_now(),
            )
            session.add(self._exemption_to_entity(pid, exemption))
            session.flush()
            return exemption

    def revoke_exemption(
        self, exemption_id: str, portfolio_id: Optional[str] = None
    ) -> Exemption:
        """撤销豁免：不动旧行，追加一行 status=revoked 的新版本。"""
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            latest = (
                session.query(RiskExemptionEntity)
                .filter(
                    RiskExemptionEntity.portfolio_id == pid,
                    RiskExemptionEntity.exemption_id == exemption_id,
                )
                .order_by(desc(RiskExemptionEntity.version))
                .first()
            )
            if latest is None:
                raise RiskStoreError(f"豁免 {exemption_id} 不存在")
            if latest.status != ExemptionStatus.ACTIVE.value:
                raise RiskStoreError(f"豁免 {exemption_id} 已处于 {latest.status} 状态")
            revoked = Exemption(
                exemption_id=exemption_id,
                version=latest.version + 1,
                dimension=RiskDimension(latest.dimension),
                target=latest.target,
                extra_amount=Decimal(latest.extra_amount),
                valid_from=latest.valid_from,
                valid_to=latest.valid_to,
                reason=latest.reason,
                granted_by=latest.granted_by,
                status=ExemptionStatus.REVOKED,
                created_at=_now(),
            )
            session.add(self._exemption_to_entity(pid, revoked))
            session.flush()
            return revoked

    def active_exemptions(
        self, at: Optional[datetime] = None, portfolio_id: Optional[str] = None
    ) -> List[Exemption]:
        """当前生效豁免：每个 exemption_id 取最新版本行，且处于有效期。"""
        pid = portfolio_id or self.portfolio_id
        moment = at or _now()
        with self.transaction(pid) as session:
            return self._active_exemptions(session, pid, moment)

    @staticmethod
    def _active_exemptions(
        session: Session, pid: str, moment: datetime
    ) -> List[Exemption]:
        entities = (
            session.query(RiskExemptionEntity)
            .filter(RiskExemptionEntity.portfolio_id == pid)
            .order_by(RiskExemptionEntity.exemption_id, RiskExemptionEntity.version)
            .all()
        )
        latest_by_id: Dict[str, RiskExemptionEntity] = {}
        for entity in entities:
            latest_by_id[entity.exemption_id] = entity
        result = []
        for entity in latest_by_id.values():
            exemption = RiskStore._exemption_from_entity(entity)
            if exemption.is_active(moment):
                result.append(exemption)
        return result

    def list_exemptions(self, portfolio_id: Optional[str] = None) -> List[Exemption]:
        """全部豁免版本行（含已撤销），供审计。"""
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            entities = (
                session.query(RiskExemptionEntity)
                .filter(RiskExemptionEntity.portfolio_id == pid)
                .order_by(
                    RiskExemptionEntity.exemption_id, RiskExemptionEntity.version
                )
                .all()
            )
            return [self._exemption_from_entity(e) for e in entities]

    @staticmethod
    def _exemption_to_entity(pid: str, exemption: Exemption) -> RiskExemptionEntity:
        return RiskExemptionEntity(
            portfolio_id=pid,
            exemption_id=exemption.exemption_id,
            version=exemption.version,
            dimension=exemption.dimension.value,
            target=exemption.target,
            extra_amount=str(exemption.extra_amount),
            valid_from=exemption.valid_from,
            valid_to=exemption.valid_to,
            reason=exemption.reason,
            granted_by=exemption.granted_by,
            status=exemption.status.value,
            created_at=exemption.created_at,
        )

    @staticmethod
    def _exemption_from_entity(entity: RiskExemptionEntity) -> Exemption:
        return Exemption(
            exemption_id=entity.exemption_id,
            version=entity.version,
            dimension=RiskDimension(entity.dimension),
            target=entity.target,
            extra_amount=Decimal(entity.extra_amount),
            valid_from=entity.valid_from,
            valid_to=entity.valid_to,
            reason=entity.reason,
            granted_by=entity.granted_by,
            status=ExemptionStatus(entity.status),
            created_at=entity.created_at,
        )

    # ------------------------------------------------------------------
    # 订单台账（额度预留）
    # ------------------------------------------------------------------

    def reserve_order(
        self,
        entry: LedgerEntry,
        session: Optional[Session] = None,
    ) -> LedgerEntry:
        """在台账中写入买单预留。重复 order_id 抛出以支持调用方幂等。"""
        if session is None:
            with self.transaction(entry.portfolio_id) as owned:
                return self._insert_reservation(owned, entry)
        return self._insert_reservation(session, entry)

    @staticmethod
    def _insert_reservation(session: Session, entry: LedgerEntry) -> LedgerEntry:
        existing = (
            session.query(RiskOrderEntity)
            .filter(RiskOrderEntity.order_id == entry.order_id)
            .first()
        )
        if existing is not None:
            raise DuplicateReservationError(
                entry.order_id, RiskStore._entry_from_entity(existing)
            )
        session.add(RiskStore._entry_to_entity(entry))
        session.flush()
        return entry

    def get_order(self, order_id: str) -> Optional[LedgerEntry]:
        with self.transaction() as session:
            entity = (
                session.query(RiskOrderEntity)
                .filter(RiskOrderEntity.order_id == order_id)
                .first()
            )
            return self._entry_from_entity(entity) if entity else None

    def open_orders(self, portfolio_id: Optional[str] = None) -> List[LedgerEntry]:
        """仍占用预留的买单（待成交/部分成交）。"""
        pid = portfolio_id or self.portfolio_id
        open_statuses = [LedgerStatus.RESERVED.value, LedgerStatus.PARTIAL.value]
        with self.transaction(pid) as session:
            return self._open_orders(session, open_statuses)

    @staticmethod
    def _open_orders(
        session: Session, open_statuses: List[str], pid: Optional[str] = None
    ) -> List[LedgerEntry]:
        query = session.query(RiskOrderEntity).filter(
            RiskOrderEntity.status.in_(open_statuses)
        )
        if pid is not None:
            query = query.filter(RiskOrderEntity.portfolio_id == pid)
        return [RiskStore._entry_from_entity(e) for e in query.all()]

    def all_orders(self, portfolio_id: Optional[str] = None) -> List[LedgerEntry]:
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            entities = (
                session.query(RiskOrderEntity)
                .filter(RiskOrderEntity.portfolio_id == pid)
                .order_by(RiskOrderEntity.created_at)
                .all()
            )
            return [self._entry_from_entity(e) for e in entities]

    def update_order(
        self,
        order_id: str,
        status: LedgerStatus,
        filled_quantity: int,
        rule_version: Optional[int] = None,
        session: Optional[Session] = None,
    ) -> LedgerEntry:
        """更新台账状态（部分成交/全成/撤销/拒绝/失败）。

        部分成交按已成交量缩减剩余预留；全成时预留清零转为持仓敞口；
        撤销/拒绝/失败释放全部剩余预留。已处于终态的订单拒绝再次变更。
        """
        if session is None:
            with self.transaction() as owned:
                return self._update_reservation(owned, order_id, status, filled_quantity, rule_version)
        return self._update_reservation(session, order_id, status, filled_quantity, rule_version)

    @staticmethod
    def _update_reservation(
        session: Session,
        order_id: str,
        status: LedgerStatus,
        filled_quantity: int,
        rule_version: Optional[int] = None,
    ) -> LedgerEntry:
        entity = (
            session.query(RiskOrderEntity)
            .filter(RiskOrderEntity.order_id == order_id)
            .first()
        )
        if entity is None:
            raise RiskStoreError(f"订单 {order_id} 不在风控台账中")
        current = LedgerStatus(entity.status)
        if current in (
            LedgerStatus.FILLED,
            LedgerStatus.CANCELLED,
            LedgerStatus.REJECTED,
            LedgerStatus.FAILED,
        ):
            raise RiskStoreError(
                f"订单 {order_id} 已处于终态 {current.value}，不可变更为 {status.value}"
            )
        if filled_quantity < entity.filled_quantity:
            raise RiskStoreError("已成交量只能单调增加，不能回滚到更小值")
        if filled_quantity > entity.quantity:
            raise RiskStoreError("已成交量不能超过委托数量")
        if filled_quantity == entity.quantity and status.is_open:
            raise RiskStoreError("已全部成交的订单不能保持开放状态")
        entity.status = status.value
        entity.filled_quantity = filled_quantity
        entity.remaining_quantity = max(entity.quantity - filled_quantity, 0)
        entity.updated_at = _now()
        if rule_version is not None:
            entity.rule_version = rule_version
        session.flush()
        return RiskStore._entry_from_entity(entity)

    @staticmethod
    def _entry_to_entity(entry: LedgerEntry) -> RiskOrderEntity:
        return RiskOrderEntity(
            order_id=entry.order_id,
            portfolio_id=entry.portfolio_id,
            stock_code=entry.stock_code,
            side=entry.side,
            quantity=entry.quantity,
            filled_quantity=entry.filled_quantity,
            remaining_quantity=entry.remaining_quantity,
            price=str(entry.price),
            industry=entry.industry,
            instrument_type=entry.instrument_type,
            status=entry.status.value,
            rule_version=entry.rule_version,
            created_at=entry.created_at,
            updated_at=entry.updated_at,
        )

    @staticmethod
    def _entry_from_entity(entity: RiskOrderEntity) -> LedgerEntry:
        return LedgerEntry(
            order_id=entity.order_id,
            portfolio_id=entity.portfolio_id,
            stock_code=entity.stock_code,
            side=entity.side,
            quantity=entity.quantity,
            filled_quantity=entity.filled_quantity,
            remaining_quantity=entity.remaining_quantity,
            price=Decimal(entity.price),
            industry=entity.industry,
            instrument_type=entity.instrument_type,
            status=LedgerStatus(entity.status),
            rule_version=entity.rule_version,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )

    # ------------------------------------------------------------------
    # 审计决策
    # ------------------------------------------------------------------

    def insert_decision(
        self,
        decision: RiskDecision,
        session: Optional[Session] = None,
    ) -> RiskDecision:
        if session is None:
            with self.transaction(decision.portfolio_id) as owned:
                return self._insert_decision(owned, decision)
        return self._insert_decision(session, decision)

    @staticmethod
    def _insert_decision(session: Session, decision: RiskDecision) -> RiskDecision:
        payload = decision.to_dict()
        entity = RiskDecisionEntity(
            decision_id=decision.decision_id,
            portfolio_id=decision.portfolio_id,
            order_id=decision.order_id,
            action=decision.action,
            passed=decision.passed,
            rule_version=decision.rule_version,
            evaluated_at=decision.evaluated_at,
            rule_snapshot=json.loads(json.dumps(decision.rule_snapshot)),
            payload=json.loads(json.dumps(payload)),
            summary=decision.summary(),
        )
        session.add(entity)
        session.flush()
        return decision

    def get_decision(self, decision_id: str) -> Optional[RiskDecision]:
        with self.transaction() as session:
            entity = (
                session.query(RiskDecisionEntity)
                .filter(RiskDecisionEntity.decision_id == decision_id)
                .first()
            )
            return self._decision_from_entity(entity) if entity else None

    def list_decisions(
        self,
        order_id: Optional[str] = None,
        portfolio_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[RiskDecision]:
        pid = portfolio_id or self.portfolio_id
        with self.transaction(pid) as session:
            query = session.query(RiskDecisionEntity).filter(
                RiskDecisionEntity.portfolio_id == pid
            )
            if order_id:
                query = query.filter(RiskDecisionEntity.order_id == order_id)
            entities = query.order_by(
                desc(RiskDecisionEntity.evaluated_at)
            ).limit(limit).all()
            return [self._decision_from_entity(e) for e in entities]

    @staticmethod
    def _decision_from_entity(entity: RiskDecisionEntity) -> RiskDecision:
        return RiskStore.decision_from_dict(entity.payload)

    @staticmethod
    def decision_from_dict(payload: Dict) -> RiskDecision:
        return RiskDecision(
            decision_id=payload["decision_id"],
            order_id=payload["order_id"],
            portfolio_id=payload["portfolio_id"],
            action=payload["action"],
            passed=payload["passed"],
            evaluated_at=datetime.fromisoformat(payload["evaluated_at"]),
            rule_version=payload["rule_version"],
            rule_snapshot=payload["rule_snapshot"],
            results=tuple(
                RuleCheckResult(
                    dimension=RiskDimension(r["dimension"]),
                    target=r["target"],
                    passed=r["passed"],
                    rule_limit=Decimal(r["rule_limit"]),
                    exempted_amount=Decimal(r["exempted_amount"]),
                    effective_limit=Decimal(r["effective_limit"]),
                    position_used=Decimal(r["position_used"]),
                    pending_used=Decimal(r["pending_used"]),
                    incremental=Decimal(r["incremental"]),
                    projected_exposure=Decimal(r["projected_exposure"]),
                    residual=Decimal(r["residual"]),
                    max_additional_quantity=r["max_additional_quantity"],
                    exemption_ids=tuple(r["exemptions"]),
                    reason=r["reason"],
                )
                for r in payload["results"]
            ),
            exemptions_applied=tuple(payload.get("exemptions_applied", [])),
            # valuation 为说明性字符串字典（代码/价格/状态），保持原样
            valuation=dict(payload.get("valuation", {})),
            reasons=tuple(payload.get("reasons", [])),
            intent=payload.get("intent"),
        )
