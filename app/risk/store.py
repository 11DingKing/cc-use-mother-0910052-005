"""风险预算的持久化端口。

引擎只依赖 ``RiskStore`` 接口，不关心底层是内存字典还是 SQL 表。
每次 approve/fill/release/publish 都在单个 ``transaction()`` 内完成
多张表的写入：事务内任何一步失败则整体回滚，不会出现“额度已预留
但审计缺失”或“预留写了一半”的中间状态。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterator, List, Optional

from app.risk.models import (
    Exemption,
    InstrumentInfo,
    Reservation,
    RiskDecision,
    RuleVersion,
)


class RiskStore(ABC):
    """风险状态存储接口。"""

    # -- 证券主数据 ------------------------------------------------------
    @abstractmethod
    def upsert_instrument(self, info: InstrumentInfo) -> None: ...

    @abstractmethod
    def get_instrument(self, stock_code: str) -> Optional[InstrumentInfo]: ...

    # -- 规则版本 --------------------------------------------------------
    @abstractmethod
    def add_rule_version(self, rule: RuleVersion) -> None:
        """写入新版本并将其置为该组合的当前版本。"""

    @abstractmethod
    def get_current_rule_version(self, portfolio_id: str) -> Optional[RuleVersion]: ...

    @abstractmethod
    def get_rule_version(
        self, portfolio_id: str, version: int
    ) -> Optional[RuleVersion]: ...

    @abstractmethod
    def list_rule_versions(self, portfolio_id: str) -> List[RuleVersion]: ...

    # -- 豁免 ------------------------------------------------------------
    @abstractmethod
    def add_exemption(self, exemption: Exemption) -> None: ...

    @abstractmethod
    def get_exemption(self, exemption_id: str) -> Optional[Exemption]: ...

    @abstractmethod
    def list_exemptions(self, portfolio_id: str) -> List[Exemption]: ...

    @abstractmethod
    def consume_exemption(self, exemption_id: str, order_id: str) -> None:
        """标记单订单豁免已被消费（幂等）。"""

    # -- 预留 ------------------------------------------------------------
    @abstractmethod
    def put_reservation(self, reservation: Reservation) -> None: ...

    @abstractmethod
    def get_reservation(self, order_id: str) -> Optional[Reservation]: ...

    @abstractmethod
    def list_reservations(
        self, portfolio_id: str, open_only: bool = False
    ) -> List[Reservation]: ...

    # -- 审计决策 --------------------------------------------------------
    @abstractmethod
    def append_decision(self, decision: RiskDecision) -> None: ...

    @abstractmethod
    def list_decisions(
        self,
        portfolio_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> List[RiskDecision]: ...

    # -- 事务 ------------------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """单事务边界。默认实现为空操作，由具体存储覆盖。"""
        yield


class InMemoryRiskStore(RiskStore):
    """进程内存储，用于单机运行与测试。自带可重入锁，保证并发安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instruments: Dict[str, InstrumentInfo] = {}
        self._rules: Dict[str, Dict[int, RuleVersion]] = {}
        self._current_rule: Dict[str, int] = {}
        self._exemptions: Dict[str, Exemption] = {}
        self._reservations: Dict[str, Reservation] = {}
        self._decisions: List[RiskDecision] = []

    @contextmanager
    def transaction(self) -> Iterator[None]:
        # 引擎本身已按组合加锁；这里再取存储锁，使事务内的多次写入
        # 对其他存储读者表现为一个原子批次。
        self._lock.acquire()
        try:
            yield
        except Exception:
            # 内存存储无法撤销已发生的字典变更；引擎在异常路径上不做
            # 部分写入（所有写操作集中在事务末尾），此处仅维持语义对称。
            raise
        finally:
            self._lock.release()

    # -- 证券主数据 ------------------------------------------------------
    def upsert_instrument(self, info: InstrumentInfo) -> None:
        with self._lock:
            self._instruments[info.stock_code] = info

    def get_instrument(self, stock_code: str) -> Optional[InstrumentInfo]:
        with self._lock:
            return self._instruments.get(stock_code)

    # -- 规则版本 --------------------------------------------------------
    def add_rule_version(self, rule: RuleVersion) -> None:
        with self._lock:
            bucket = self._rules.setdefault(rule.portfolio_id, {})
            if rule.version in bucket:
                raise ValueError(
                    f"规则版本已存在: {rule.portfolio_id} v{rule.version}"
                )
            current = self._current_rule.get(rule.portfolio_id)
            if current is not None and rule.version <= current:
                raise ValueError("新版本号必须大于当前版本号")
            bucket[rule.version] = rule
            self._current_rule[rule.portfolio_id] = rule.version

    def get_current_rule_version(self, portfolio_id: str) -> Optional[RuleVersion]:
        with self._lock:
            version = self._current_rule.get(portfolio_id)
            if version is None:
                return None
            return self._rules[portfolio_id][version]

    def get_rule_version(
        self, portfolio_id: str, version: int
    ) -> Optional[RuleVersion]:
        with self._lock:
            return self._rules.get(portfolio_id, {}).get(version)

    def list_rule_versions(self, portfolio_id: str) -> List[RuleVersion]:
        with self._lock:
            return [
                self._rules[portfolio_id][v]
                for v in sorted(self._rules.get(portfolio_id, {}))
            ]

    # -- 豁免 ------------------------------------------------------------
    def add_exemption(self, exemption: Exemption) -> None:
        with self._lock:
            if exemption.exemption_id in self._exemptions:
                raise ValueError(f"豁免已存在: {exemption.exemption_id}")
            self._exemptions[exemption.exemption_id] = exemption

    def get_exemption(self, exemption_id: str) -> Optional[Exemption]:
        with self._lock:
            return self._exemptions.get(exemption_id)

    def list_exemptions(self, portfolio_id: str) -> List[Exemption]:
        with self._lock:
            return [
                e
                for e in self._exemptions.values()
                if e.portfolio_id == portfolio_id
            ]

    def consume_exemption(self, exemption_id: str, order_id: str) -> None:
        with self._lock:
            ex = self._exemptions.get(exemption_id)
            if ex is None:
                raise ValueError(f"豁免不存在: {exemption_id}")
            if ex.consumed_by_order not in (None, order_id):
                raise ValueError(
                    f"豁免 {exemption_id} 已被订单 {ex.consumed_by_order} 消费"
                )
            if ex.consumed_by_order is None:
                from dataclasses import replace

                self._exemptions[exemption_id] = replace(
                    ex, consumed_by_order=order_id
                )

    # -- 预留 ------------------------------------------------------------
    def put_reservation(self, reservation: Reservation) -> None:
        with self._lock:
            self._reservations[reservation.order_id] = reservation

    def get_reservation(self, order_id: str) -> Optional[Reservation]:
        with self._lock:
            return self._reservations.get(order_id)

    def list_reservations(
        self, portfolio_id: str, open_only: bool = False
    ) -> List[Reservation]:
        with self._lock:
            from app.risk.models import ReservationStatus

            result = [
                r
                for r in self._reservations.values()
                if r.portfolio_id == portfolio_id
            ]
            if open_only:
                result = [
                    r
                    for r in result
                    if r.status
                    in (ReservationStatus.HELD, ReservationStatus.PARTIAL)
                ]
            return sorted(result, key=lambda r: r.created_at)

    # -- 审计决策 --------------------------------------------------------
    def append_decision(self, decision: RiskDecision) -> None:
        with self._lock:
            self._decisions.append(decision)

    def list_decisions(
        self,
        portfolio_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> List[RiskDecision]:
        with self._lock:
            result = list(self._decisions)
            if portfolio_id is not None:
                result = [d for d in result if d.portfolio_id == portfolio_id]
            if order_id is not None:
                result = [d for d in result if d.order_id == order_id]
            return result


def now_local() -> datetime:
    """统一时钟入口，便于测试替换。"""
    return datetime.now()


def to_decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
