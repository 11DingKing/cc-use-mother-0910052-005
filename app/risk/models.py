"""组合级风控的领域模型。

本模块只包含不可变的值对象与纯函数所需的数据结构，不依赖数据库或
FastAPI，便于在测试中以内存替身直接验证口径。

口径约定：
- 敞口 = 持仓市值（数量 × 最新价）+ 待成交买单的在途预留
  （剩余未成交数量 × 委托估值价）。
- 卖单降低多头敞口，不占用预算，仅做留痕。
- 所有金额使用 Decimal，避免浮点误差。
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import AbstractSet, Dict, Optional, Tuple


class RiskDimension(str, Enum):
    """风险聚合维度。"""

    TOTAL = "total"            # 组合总敞口
    INDUSTRY = "industry"      # 行业敞口
    INSTRUMENT = "instrument"  # 品种敞口
    SINGLE = "single"          # 单笔委托金额

    @property
    def is_aggregation(self) -> bool:
        return self in (RiskDimension.TOTAL, RiskDimension.INDUSTRY, RiskDimension.INSTRUMENT)


# 总敞口维度使用的固定 target
TOTAL_TARGET = "TOTAL"


class LedgerStatus(str, Enum):
    """订单在风控台账中的预留状态。"""

    RESERVED = "reserved"      # 已通过预检并预留额度（待成交/部分成交）
    PARTIAL = "partial"        # 部分成交，剩余部分继续占用预留
    FILLED = "filled"          # 全部成交，预留转为实际持仓敞口
    CANCELLED = "cancelled"    # 已撤销，预留释放
    REJECTED = "rejected"      # 被柜台拒绝，预留释放
    FAILED = "failed"          # 失败，预留释放

    @property
    def is_open(self) -> bool:
        return self in (LedgerStatus.RESERVED, LedgerStatus.PARTIAL)

    @property
    def releases_reservation(self) -> bool:
        return self in (LedgerStatus.CANCELLED, LedgerStatus.REJECTED, LedgerStatus.FAILED)


# 终态集合：进入后不可再变更
TERMINAL_STATUSES: AbstractSet[LedgerStatus] = frozenset({
    LedgerStatus.FILLED,
    LedgerStatus.CANCELLED,
    LedgerStatus.REJECTED,
    LedgerStatus.FAILED,
})


@dataclass(frozen=True)
class Instrument:
    """证券分类：决定订单/持仓归属哪个行业与品种桶。"""

    stock_code: str
    industry: str = "其他"
    instrument_type: str = "STOCK"

    def to_dict(self) -> Dict:
        return {
            "stock_code": self.stock_code,
            "industry": self.industry,
            "instrument_type": self.instrument_type,
        }


@dataclass(frozen=True)
class RiskLimit:
    """单条预算规则。target 对总敞口为 TOTAL，其余为行业名/品种名。"""

    dimension: RiskDimension
    target: str
    max_exposure: Decimal
    note: str = ""

    def to_dict(self) -> Dict:
        return {
            "dimension": self.dimension.value,
            "target": self.target,
            "max_exposure": str(self.max_exposure),
            "note": self.note,
        }

    @property
    def key(self) -> Tuple[RiskDimension, str]:
        return self.dimension, self.target


@dataclass(frozen=True)
class RuleSet:
    """某一版本的完整规则集。

    版本一经发布即为不可变快照（append-only）：后续换版只能发布新版本，
    决策记录会永久绑定版本号与完整快照，历史订单始终可按当时口径复核。
    """

    version: int
    limits: Tuple[RiskLimit, ...]
    max_single_order_amount: Decimal = Decimal("100000")
    portfolio_id: str = "default"
    published_at: datetime = field(default_factory=datetime.now)
    created_by: str = "system"
    note: str = ""

    def limit_for(self, dimension: RiskDimension, target: str) -> Optional[RiskLimit]:
        for limit in self.limits:
            if limit.dimension == dimension and limit.target == target:
                return limit
        return None

    def aggregation_limits(self) -> Tuple[RiskLimit, ...]:
        return tuple(limit for limit in self.limits if limit.dimension.is_aggregation)

    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "portfolio_id": self.portfolio_id,
            "published_at": self.published_at.isoformat(),
            "created_by": self.created_by,
            "note": self.note,
            "max_single_order_amount": str(self.max_single_order_amount),
            "limits": [limit.to_dict() for limit in self.limits],
        }


class ExemptionStatus(str, Enum):
    """豁免状态。撤销通过追加新版本行实现，历史行保留。"""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Exemption:
    """临时豁免：在有效期内为某条规则追加预算额度。

    豁免自身也有版本：修改/撤销产生新的不可变版本行，决策时绑定具体
    的 exemption_id + version，审计可还原当时究竟豁免了多少。
    """

    exemption_id: str
    version: int
    dimension: RiskDimension
    target: str
    extra_amount: Decimal
    valid_from: datetime
    valid_to: datetime
    reason: str
    granted_by: str = "system"
    status: ExemptionStatus = ExemptionStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.now)

    def is_active(self, at: datetime) -> bool:
        return (
            self.status == ExemptionStatus.ACTIVE
            and self.valid_from <= at < self.valid_to
        )

    @property
    def key(self) -> Tuple[RiskDimension, str]:
        return self.dimension, self.target

    def to_dict(self) -> Dict:
        return {
            "exemption_id": self.exemption_id,
            "version": self.version,
            "dimension": self.dimension.value,
            "target": self.target,
            "extra_amount": str(self.extra_amount),
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "reason": self.reason,
            "granted_by": self.granted_by,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class PositionView:
    """持仓快照（按某一时刻行情估值）。"""

    stock_code: str
    quantity: int
    price: Decimal
    industry: str = "其他"
    instrument_type: str = "STOCK"

    @property
    def market_value(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True)
class OpenOrderView:
    """待成交买单的在途预留快照。"""

    order_id: str
    stock_code: str
    remaining_quantity: int
    price: Decimal
    industry: str = "其他"
    instrument_type: str = "STOCK"

    @property
    def reserved_amount(self) -> Decimal:
        return self.price * self.remaining_quantity


@dataclass(frozen=True)
class OrderIntent:
    """待评估的下单意图。

    valuation_price 为该笔委托占用预算时使用的估值价：限价单用委托价，
    市价单用最新成交价（保守口径）。
    """

    order_id: str
    stock_code: str
    side: str  # "buy" / "sell"
    quantity: int
    valuation_price: Decimal
    industry: str = "其他"
    instrument_type: str = "STOCK"

    @property
    def notional(self) -> Decimal:
        return self.valuation_price * self.quantity

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "stock_code": self.stock_code,
            "side": self.side,
            "quantity": self.quantity,
            "valuation_price": str(self.valuation_price),
            "industry": self.industry,
            "instrument_type": self.instrument_type,
            "notional": str(self.notional),
        }


@dataclass(frozen=True)
class RuleCheckResult:
    """单条规则的可解释检查结果。"""

    dimension: RiskDimension
    target: str
    passed: bool
    rule_limit: Decimal                  # 规则本身的限额
    exempted_amount: Decimal             # 生效豁免追加的额度
    effective_limit: Decimal             # 限额 + 豁免
    position_used: Decimal               # 已用：持仓市值
    pending_used: Decimal                # 在途：其他待成交买单预留
    incremental: Decimal                 # 本次新增（买单名义金额）
    projected_exposure: Decimal          # 成交后预计敞口
    residual: Decimal                    # 剩余额度（不含本次新增）
    max_additional_quantity: Optional[int]  # 按参考价折算的最多可买股数
    exemption_ids: Tuple[str, ...]
    reason: str

    def to_dict(self) -> Dict:
        return {
            "dimension": self.dimension.value,
            "target": self.target,
            "passed": self.passed,
            "rule_limit": str(self.rule_limit),
            "exempted_amount": str(self.exempted_amount),
            "effective_limit": str(self.effective_limit),
            "position_used": str(self.position_used),
            "pending_used": str(self.pending_used),
            "incremental": str(self.incremental),
            "projected_exposure": str(self.projected_exposure),
            "residual": str(self.residual),
            "max_additional_quantity": self.max_additional_quantity,
            "exemptions": list(self.exemption_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RiskDecision:
    """一次完整的风控判定（预检/预留/状态变更/释放），落审计表。"""

    decision_id: str
    order_id: str
    portfolio_id: str
    action: str            # PRE_CHECK / RESERVE / UPDATE / RELEASE
    passed: bool
    evaluated_at: datetime
    rule_version: int
    rule_snapshot: Dict
    results: Tuple[RuleCheckResult, ...]
    exemptions_applied: Tuple[Dict, ...]
    valuation: Dict[str, str]
    reasons: Tuple[str, ...]
    intent: Optional[Dict] = None

    @property
    def failures(self) -> Tuple[RuleCheckResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    def summary(self) -> str:
        if self.passed:
            return "放行：所有组合级风险规则均通过"
        return "拒绝：" + "；".join(r.reason for r in self.failures)

    def to_dict(self) -> Dict:
        return {
            "decision_id": self.decision_id,
            "order_id": self.order_id,
            "portfolio_id": self.portfolio_id,
            "action": self.action,
            "passed": self.passed,
            "evaluated_at": self.evaluated_at.isoformat(),
            "rule_version": self.rule_version,
            "rule_snapshot": self.rule_snapshot,
            "results": [r.to_dict() for r in self.results],
            "exemptions_applied": list(self.exemptions_applied),
            "valuation": dict(self.valuation),
            "reasons": list(self.reasons),
            "summary": self.summary(),
            "intent": self.intent,
        }


@dataclass(frozen=True)
class ResidualLine:
    """剩余额度报告中的一行。"""

    dimension: RiskDimension
    target: str
    rule_limit: Decimal
    exempted_amount: Decimal
    effective_limit: Decimal
    position_used: Decimal
    pending_used: Decimal
    residual: Decimal
    exemption_ids: Tuple[str, ...]

    def to_dict(self) -> Dict:
        return {
            "dimension": self.dimension.value,
            "target": self.target,
            "rule_limit": str(self.rule_limit),
            "exempted_amount": str(self.exempted_amount),
            "effective_limit": str(self.effective_limit),
            "position_used": str(self.position_used),
            "pending_used": str(self.pending_used),
            "residual": str(self.residual),
            "exemptions": list(self.exemption_ids),
        }


@dataclass(frozen=True)
class ResidualReport:
    """某一时刻组合在全部规则下的剩余额度快照。"""

    portfolio_id: str
    evaluated_at: datetime
    rule_version: int
    total_position_value: Decimal
    total_pending: Decimal
    lines: Tuple[ResidualLine, ...]
    valuation: Dict[str, str]

    def to_dict(self) -> Dict:
        return {
            "portfolio_id": self.portfolio_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "rule_version": self.rule_version,
            "total_position_value": str(self.total_position_value),
            "total_pending": str(self.total_pending),
            "valuation": dict(self.valuation),
            "lines": [line.to_dict() for line in self.lines],
        }


@dataclass(frozen=True)
class LedgerEntry:
    """风控台账：一笔买单占用的组合预算及其生命周期。"""

    order_id: str
    portfolio_id: str
    stock_code: str
    side: str
    quantity: int
    filled_quantity: int
    remaining_quantity: int
    price: Decimal
    industry: str
    instrument_type: str
    status: LedgerStatus
    rule_version: int
    created_at: datetime
    updated_at: datetime

    def to_open_view(self) -> OpenOrderView:
        return OpenOrderView(
            order_id=self.order_id,
            stock_code=self.stock_code,
            remaining_quantity=self.remaining_quantity,
            price=self.price,
            industry=self.industry,
            instrument_type=self.instrument_type,
        )

    def with_update(
        self,
        status: LedgerStatus,
        filled_quantity: int,
        updated_at: datetime,
    ) -> "LedgerEntry":
        remaining = max(self.quantity - filled_quantity, 0)
        return replace(
            self,
            status=status,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining,
            updated_at=updated_at,
        )

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "portfolio_id": self.portfolio_id,
            "stock_code": self.stock_code,
            "side": self.side,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "price": str(self.price),
            "industry": self.industry,
            "instrument_type": self.instrument_type,
            "status": self.status.value,
            "rule_version": self.rule_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
