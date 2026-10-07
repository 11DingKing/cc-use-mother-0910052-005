"""组合风险预算的领域模型。

所有金额均使用 ``Decimal``，避免浮点误差进入额度计算。领域对象本身
不可变（dataclass(frozen=True)）；状态变化通过引擎产生新的决策与
预留记录表达，历史记录不被覆盖，便于审计追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional


class Dimension(str, Enum):
    """风险维度。"""

    ORDER = "order"            # 单笔订单金额
    TOTAL = "total"            # 组合总敞口（多头市值）
    INDUSTRY = "industry"      # 行业敞口
    INSTRUMENT = "instrument"  # 品种（股票/ETF/债券…）敞口
    CASH = "cash"              # 可用资金对在途买单的承接能力
    POSITION = "position"      # 持仓数量对在途卖单的可交割能力


class ReservationStatus(str, Enum):
    """待成交订单对预算占用的生命周期状态。"""

    HELD = "held"            # 已批准，全额或部分额度被占用
    PARTIAL = "partial"      # 部分成交，剩余数量仍占用额度
    FILLED = "filled"        # 全部成交，占用已转为现实持仓
    RELEASED = "released"    # 撤销，剩余占用被释放
    REJECTED = "rejected"    # 拒绝（柜台拒单等），占用被释放
    EXPIRED = "expired"      # 超时等原因失效，占用被释放


class Side(str, Enum):
    """买卖方向。"""

    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


@dataclass(frozen=True)
class InstrumentInfo:
    """证券主数据：决定订单落入哪个行业 / 品种桶。"""

    stock_code: str
    industry: str = "UNKNOWN"
    instrument_type: str = "stock"  # stock / etf / bond / ...
    stock_name: Optional[str] = None


@dataclass(frozen=True)
class RuleLimits:
    """某一版规则下的全部额度上限（不可变快照）。

    金额口径均为组合记账货币的名义本金。行业/品种表中查不到的桶使用
    对应的 default 上限。
    """

    max_total_long_amount: Decimal = Decimal("1000000")
    max_single_order_amount: Decimal = Decimal("100000")
    default_industry_amount: Decimal = Decimal("300000")
    industry_limits: Dict[str, Decimal] = field(default_factory=dict)
    default_instrument_amount: Decimal = Decimal("500000")
    instrument_limits: Dict[str, Decimal] = field(default_factory=dict)

    def industry_limit(self, industry: str) -> Decimal:
        return self.industry_limits.get(industry, self.default_industry_amount)

    def instrument_limit(self, instrument_type: str) -> Decimal:
        return self.instrument_limits.get(
            instrument_type, self.default_instrument_amount
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_total_long_amount": str(self.max_total_long_amount),
            "max_single_order_amount": str(self.max_single_order_amount),
            "default_industry_amount": str(self.default_industry_amount),
            "industry_limits": {k: str(v) for k, v in self.industry_limits.items()},
            "default_instrument_amount": str(self.default_instrument_amount),
            "instrument_limits": {
                k: str(v) for k, v in self.instrument_limits.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuleLimits":
        return cls(
            max_total_long_amount=Decimal(data["max_total_long_amount"]),
            max_single_order_amount=Decimal(data["max_single_order_amount"]),
            default_industry_amount=Decimal(data["default_industry_amount"]),
            industry_limits={
                k: Decimal(v) for k, v in data.get("industry_limits", {}).items()
            },
            default_instrument_amount=Decimal(data["default_instrument_amount"]),
            instrument_limits={
                k: Decimal(v) for k, v in data.get("instrument_limits", {}).items()
            },
        )


@dataclass(frozen=True)
class RuleVersion:
    """规则版本。发布后不可修改；换版只能发新版本。

    已批准的订单在 ``Reservation.rule_version`` 中冻结其批准时版本，
    后续成交/撤单/审计均按当时口径追溯。
    """

    portfolio_id: str
    version: int
    limits: RuleLimits
    created_at: datetime
    effective_at: datetime
    published_by: str = "system"
    note: str = ""


@dataclass(frozen=True)
class Exemption:
    """临时豁免：在有效期内为某个（或某类）桶增加额度。

    - dimension+bucket 定位桶；bucket 为 None 表示该维度下所有桶通用；
    - extra_amount 为在基础上限之上追加的额度（可叠加，口径透明）；
    - order_id 不为空时为单订单豁免，批准时一次性消费；
    - valid_from/valid_until 以引擎时钟判定有效性。
    """

    exemption_id: str
    portfolio_id: str
    dimension: Dimension
    extra_amount: Decimal
    created_at: datetime
    valid_from: datetime
    valid_until: datetime
    bucket: Optional[str] = None
    order_id: Optional[str] = None
    reason: str = ""
    approved_by: str = "risk_manager"
    consumed_by_order: Optional[str] = None  # 单订单豁免被哪笔订单消费

    def is_active(self, now: datetime) -> bool:
        if self.valid_from > now or self.valid_until < now:
            return False
        if self.order_id is not None and self.consumed_by_order is not None:
            return False
        return True


@dataclass(frozen=True)
class OrderRequest:
    """进入风控评估的订单意图。"""

    order_id: str
    portfolio_id: str
    stock_code: str
    side: Side
    quantity: int
    limit_price: Optional[Decimal] = None  # 限价单价格；市价单传 None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("订单数量必须为正数")


@dataclass(frozen=True)
class OrderValuation:
    """订单在某一时刻的计价口径。"""

    price: Decimal
    amount: Decimal
    price_source: str  # "limit" / "quote"


@dataclass(frozen=True)
class Violation:
    """单个维度桶的超限说明。"""

    dimension: Dimension
    bucket: str
    message: str
    base_limit: Decimal
    effective_limit: Decimal
    current: Decimal
    pending: Decimal
    incoming: Decimal
    projected: Decimal
    remaining_before: Decimal
    exemption_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "bucket": self.bucket,
            "message": self.message,
            "base_limit": str(self.base_limit),
            "effective_limit": str(self.effective_limit),
            "current": str(self.current),
            "pending": str(self.pending),
            "incoming": str(self.incoming),
            "projected": str(self.projected),
            "remaining_before": str(self.remaining_before),
            "exemption_ids": list(self.exemption_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Violation":
        money_keys = (
            "base_limit", "effective_limit", "current", "pending",
            "incoming", "projected", "remaining_before",
        )
        return cls(
            dimension=Dimension(data["dimension"]),
            bucket=data["bucket"],
            message=data.get("message", ""),
            **{k: Decimal(data[k]) for k in money_keys},
            exemption_ids=list(data.get("exemption_ids", [])),
        )


@dataclass(frozen=True)
class BucketCheck:
    """单个维度桶的完整试算结果（放行时同样输出，用于解释剩余额度）。"""

    dimension: Dimension
    bucket: str
    base_limit: Decimal
    effective_limit: Decimal
    current: Decimal
    pending: Decimal
    incoming: Decimal
    projected: Decimal
    remaining_before: Decimal   # 不含本单的剩余额度
    remaining_after: Decimal    # 含本单后的剩余额度
    exemption_ids: List[str] = field(default_factory=list)
    passed: bool = True
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "bucket": self.bucket,
            "base_limit": str(self.base_limit),
            "effective_limit": str(self.effective_limit),
            "current": str(self.current),
            "pending": str(self.pending),
            "incoming": str(self.incoming),
            "projected": str(self.projected),
            "remaining_before": str(self.remaining_before),
            "remaining_after": str(self.remaining_after),
            "passed": self.passed,
            "message": self.message,
            "exemption_ids": list(self.exemption_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BucketCheck":
        money_keys = (
            "base_limit", "effective_limit", "current", "pending",
            "incoming", "projected", "remaining_before", "remaining_after",
        )
        return cls(
            dimension=Dimension(data["dimension"]),
            bucket=data["bucket"],
            **{k: Decimal(data[k]) for k in money_keys},
            exemption_ids=list(data.get("exemption_ids", [])),
            passed=bool(data["passed"]),
            message=data.get("message", ""),
        )


@dataclass(frozen=True)
class ExposureReport:
    """一次评估的可解释结果：每个桶的 limit/used/remaining 全量拆解。"""

    portfolio_id: str
    rule_version: int
    evaluated_at: datetime
    order_id: Optional[str]
    valuations: Dict[str, str]            # stock_code -> 计价价格
    checks: List[BucketCheck]
    violations: List[Violation]
    approved: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "rule_version": self.rule_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "order_id": self.order_id,
            "valuations": dict(self.valuations),
            "checks": [c.to_dict() for c in self.checks],
            "violations": [v.to_dict() for v in self.violations],
            "approved": self.approved,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExposureReport":
        return cls(
            portfolio_id=data["portfolio_id"],
            rule_version=data["rule_version"],
            evaluated_at=datetime.fromisoformat(data["evaluated_at"]),
            order_id=data.get("order_id"),
            valuations={k: str(v) for k, v in data.get("valuations", {}).items()},
            checks=[BucketCheck.from_dict(c) for c in data.get("checks", [])],
            violations=[Violation.from_dict(v) for v in data.get("violations", [])],
            approved=bool(data["approved"]),
        )


@dataclass(frozen=True)
class RiskDecision:
    """一次放行/拒绝/成交/释放的审计记录（append-only，永不修改）。"""

    decision_id: str
    portfolio_id: str
    order_id: str
    action: str                 # APPROVE / REJECT / FILL / RELEASE / EXPIRE / REJECT_DOWNSTREAM
    approved: bool
    rule_version: int
    at: datetime
    report: Optional[ExposureReport] = None
    exemption_ids: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "portfolio_id": self.portfolio_id,
            "order_id": self.order_id,
            "action": self.action,
            "approved": self.approved,
            "rule_version": self.rule_version,
            "at": self.at.isoformat(),
            "report": self.report.to_dict() if self.report else None,
            "exemption_ids": list(self.exemption_ids),
            "detail": self.detail,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Reservation:
    """订单对预算的占用。不可变；状态迁移通过引擎产生替换副本。

    成交时只迁移数量（filled_quantity），计价与规则版本保持批准时
    快照，实现“历史已批准订单按当时口径追溯”。
    """

    order_id: str
    portfolio_id: str
    stock_code: str
    industry: str
    instrument_type: str
    side: Side
    quantity: int
    filled_quantity: int
    frozen_price: Decimal                  # 批准时冻结的单位价格
    rule_version: int
    status: ReservationStatus
    created_at: datetime
    updated_at: datetime
    exemption_ids: List[str] = field(default_factory=list)

    @property
    def open_quantity(self) -> int:
        """仍待成交、仍占用额度的数量。"""
        if self.status in (
            ReservationStatus.HELD,
            ReservationStatus.PARTIAL,
        ):
            return self.quantity - self.filled_quantity
        return 0

    @property
    def open_amount(self) -> Decimal:
        return Decimal(self.open_quantity) * self.frozen_price

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "portfolio_id": self.portfolio_id,
            "stock_code": self.stock_code,
            "industry": self.industry,
            "instrument_type": self.instrument_type,
            "side": self.side.value,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "open_quantity": self.open_quantity,
            "frozen_price": str(self.frozen_price),
            "open_amount": str(self.open_amount),
            "rule_version": self.rule_version,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "exemption_ids": list(self.exemption_ids),
        }


def advance(reservation: Reservation, **changes: Any) -> Reservation:
    """以替换方式产生 Reservation 副本（updated_at 自动刷新）。"""
    return replace(reservation, **changes)
