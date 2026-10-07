"""组合级敞口计算与规则判定引擎（纯函数，无 I/O）。

敞口口径：
    预计敞口 = 持仓市值 + 其他在途买单预留 + 本次买单名义金额

- 持仓市值随报价变化：数量 × 最新价；
- 在途买单按委托价的最坏成交代价预留（限价买最高即以委托价成交），
  与报价瞬时跳动无关，保证预算不会被行情上跳击穿；
- 卖单不增加多头敞口，incremental 记 0，结果仍逐规则留痕；
- 豁免在有效期内按 (维度, 目标) 叠加到限额上，决策中记录豁免明细。
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
import uuid

from app.risk.models import (
    Exemption,
    LedgerEntry,
    OpenOrderView,
    OrderIntent,
    PositionView,
    ResidualLine,
    ResidualReport,
    RuleCheckResult,
    RiskDecision,
    RiskDimension,
    RiskLimit,
    RuleSet,
)

SINGLE_TARGET = "*"
ZERO = Decimal("0")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass
class PortfolioSnapshot:
    """计算输入：持仓与在途订单在某一时刻的快照。"""

    positions: Sequence[PositionView]
    open_orders: Sequence[OpenOrderView]

    @classmethod
    def from_entries(
        cls,
        positions: Sequence[PositionView],
        entries: Iterable[LedgerEntry],
    ) -> "PortfolioSnapshot":
        return cls(
            positions=list(positions),
            open_orders=[e.to_open_view() for e in entries],
        )


class PortfolioRiskEngine:
    """无状态规则计算器：同输入必同输出，便于单测与审计复算。"""

    def __init__(self, lot_size: int = 100):
        self.lot_size = lot_size

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------

    @staticmethod
    def _bucket_matches(dimension: RiskDimension, target: str, industry: str, itype: str) -> bool:
        if dimension == RiskDimension.TOTAL:
            return True
        if dimension == RiskDimension.INDUSTRY:
            return industry == target
        if dimension == RiskDimension.INSTRUMENT:
            return itype == target
        return False

    def _position_used(self, dimension: RiskDimension, target: str, positions: Sequence[PositionView]) -> Decimal:
        return sum(
            (
                p.market_value
                for p in positions
                if self._bucket_matches(dimension, target, p.industry, p.instrument_type)
            ),
            ZERO,
        )

    def _pending_used(
        self,
        dimension: RiskDimension,
        target: str,
        open_orders: Sequence[OpenOrderView],
        exclude_order_id: Optional[str],
    ) -> Decimal:
        return sum(
            (
                o.reserved_amount
                for o in open_orders
                if o.order_id != exclude_order_id
                and self._bucket_matches(dimension, target, o.industry, o.instrument_type)
            ),
            ZERO,
        )

    @staticmethod
    def _exemptions_for(
        dimension: RiskDimension,
        target: str,
        exemptions: Sequence[Exemption],
        at: datetime,
    ) -> Tuple[Decimal, Tuple[str, ...]]:
        matched = tuple(
            e for e in exemptions
            if e.dimension == dimension and e.target == target and e.is_active(at)
        )
        amount = sum((e.extra_amount for e in matched), ZERO)
        return amount, tuple(f"{e.exemption_id}:v{e.version}" for e in matched)

    def _lot_quantity(self, residual: Decimal, price: Decimal) -> Optional[int]:
        if price <= 0:
            return None
        lots = int((residual / price) // self.lot_size)
        return max(lots, 0) * self.lot_size

    # ------------------------------------------------------------------
    # 单条规则评估
    # ------------------------------------------------------------------

    def _evaluate_limit(
        self,
        limit: RiskLimit,
        snapshot: PortfolioSnapshot,
        exemptions: Sequence[Exemption],
        at: datetime,
        intent: Optional[OrderIntent],
    ) -> RuleCheckResult:
        position_used = self._position_used(limit.dimension, limit.target, snapshot.positions)
        pending_used = self._pending_used(
            limit.dimension,
            limit.target,
            snapshot.open_orders,
            exclude_order_id=intent.order_id if intent else None,
        )
        exempted, exemption_ids = self._exemptions_for(
            limit.dimension, limit.target, exemptions, at
        )
        effective_limit = limit.max_exposure + exempted

        if intent is not None and intent.side == "buy":
            incremental = intent.notional
            touches = self._bucket_matches(
                limit.dimension, limit.target, intent.industry, intent.instrument_type
            )
            if not touches:
                incremental = ZERO
        else:
            incremental = ZERO

        projected = position_used + pending_used + incremental
        residual = effective_limit - position_used - pending_used
        passed = projected <= effective_limit

        if passed:
            if exempted > 0:
                reason = (
                    f"{limit.dimension.value}:{limit.target} 预计敞口 {projected} "
                    f"≤ 有效限额 {effective_limit}（规则 {limit.max_exposure} + "
                    f"豁免 {exempted}）"
                )
            else:
                reason = (
                    f"{limit.dimension.value}:{limit.target} 预计敞口 {projected} "
                    f"≤ 限额 {limit.max_exposure}，剩余 {residual}"
                )
        else:
            reason = (
                f"{limit.dimension.value}:{limit.target} 预计敞口 {projected} 超过"
                f"有效限额 {effective_limit}（持仓 {position_used} + 在途 {pending_used}"
                f" + 本次 {incremental}，规则限额 {limit.max_exposure}"
                + (f" + 豁免 {exempted}" if exempted > 0 else "")
                + f"，超额 {projected - effective_limit}）"
            )

        ref_price = intent.valuation_price if intent is not None and incremental > 0 else ZERO
        return RuleCheckResult(
            dimension=limit.dimension,
            target=limit.target,
            passed=passed,
            rule_limit=limit.max_exposure,
            exempted_amount=exempted,
            effective_limit=effective_limit,
            position_used=position_used,
            pending_used=pending_used,
            incremental=incremental,
            projected_exposure=projected,
            residual=residual,
            max_additional_quantity=self._lot_quantity(residual, ref_price) if ref_price > 0 else None,
            exemption_ids=exemption_ids,
            reason=reason,
        )

    def _evaluate_single_order(
        self,
        ruleset: RuleSet,
        exemptions: Sequence[Exemption],
        at: datetime,
        intent: OrderIntent,
    ) -> RuleCheckResult:
        exempted, exemption_ids = self._exemptions_for(
            RiskDimension.SINGLE, SINGLE_TARGET, exemptions, at
        )
        effective = ruleset.max_single_order_amount + exempted
        notional = intent.notional if intent.side == "buy" else ZERO
        passed = notional <= effective
        if passed:
            reason = f"单笔委托金额 {notional} ≤ 限额 {effective}"
        else:
            reason = (
                f"单笔委托金额 {notional} 超过限额 {effective}"
                f"（规则 {ruleset.max_single_order_amount}"
                + (f" + 豁免 {exempted}" if exempted > 0 else "")
                + "）"
            )
        return RuleCheckResult(
            dimension=RiskDimension.SINGLE,
            target=SINGLE_TARGET,
            passed=passed,
            rule_limit=ruleset.max_single_order_amount,
            exempted_amount=exempted,
            effective_limit=effective,
            position_used=ZERO,
            pending_used=ZERO,
            incremental=notional,
            projected_exposure=notional,
            residual=max(effective - notional, ZERO),
            max_additional_quantity=None,
            exemption_ids=exemption_ids,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 对外：完整判定
    # ------------------------------------------------------------------

    def evaluate(
        self,
        ruleset: RuleSet,
        snapshot: PortfolioSnapshot,
        intent: OrderIntent,
        exemptions: Sequence[Exemption] = (),
        at: Optional[datetime] = None,
        action: str = "PRE_CHECK",
        valuation: Optional[Dict[str, str]] = None,
    ) -> RiskDecision:
        """对一笔下单意图执行全量规则检查，输出可解释判定。"""
        moment = at or datetime.now()
        results = self._all_results(ruleset, snapshot, exemptions, moment, intent)
        passed = all(r.passed for r in results)
        reasons = tuple(r.reason for r in results if not r.passed)
        return RiskDecision(
            decision_id=_new_id("DEC"),
            order_id=intent.order_id,
            portfolio_id=ruleset.portfolio_id,
            action=action,
            passed=passed,
            evaluated_at=moment,
            rule_version=ruleset.version,
            rule_snapshot=ruleset.to_dict(),
            results=tuple(results),
            exemptions_applied=self._applied_exemptions(ruleset, exemptions, moment),
            valuation=valuation or {},
            reasons=reasons,
            intent=intent.to_dict(),
        )

    def state_decision(
        self,
        ruleset: RuleSet,
        snapshot: PortfolioSnapshot,
        order_id: str,
        action: str,
        exemptions: Sequence[Exemption] = (),
        at: Optional[datetime] = None,
        valuation: Optional[Dict[str, str]] = None,
    ) -> RiskDecision:
        """订单状态迁移（部分成交/全成/撤销）后的组合快照留痕。

        与 evaluate 的区别：没有"本次新增"，各口径只反映事件后的持仓 +
        仍开放的在途预留，因此 incremental 恒为 0，不会把释放中的数量
        误记为新增占用。
        """
        moment = at or datetime.now()
        results = self._all_results(ruleset, snapshot, exemptions, moment, None)
        return RiskDecision(
            decision_id=_new_id("DEC"),
            order_id=order_id,
            portfolio_id=ruleset.portfolio_id,
            action=action,
            passed=True,
            evaluated_at=moment,
            rule_version=ruleset.version,
            rule_snapshot=ruleset.to_dict(),
            results=tuple(results),
            exemptions_applied=self._applied_exemptions(ruleset, exemptions, moment),
            valuation=valuation or {},
            reasons=(),
            intent=None,
        )

    def _all_results(
        self,
        ruleset: RuleSet,
        snapshot: PortfolioSnapshot,
        exemptions: Sequence[Exemption],
        moment: datetime,
        intent: Optional[OrderIntent],
    ) -> List[RuleCheckResult]:
        results: List[RuleCheckResult] = [
            self._evaluate_limit(limit, snapshot, exemptions, moment, intent)
            for limit in ruleset.aggregation_limits()
        ]
        if intent is not None:
            results.append(self._evaluate_single_order(ruleset, exemptions, moment, intent))
        return results

    @staticmethod
    def _applied_exemptions(
        ruleset: RuleSet, exemptions: Sequence[Exemption], moment: datetime
    ) -> Tuple[Dict, ...]:
        """命中本次评估口径且在有效期的豁免，全部留痕。"""
        targets = {(limit.dimension, limit.target) for limit in ruleset.aggregation_limits()}
        targets.add((RiskDimension.SINGLE, SINGLE_TARGET))
        return tuple(
            e.to_dict()
            for e in exemptions
            if e.is_active(moment) and (e.dimension, e.target) in targets
        )

    # ------------------------------------------------------------------
    # 对外：剩余额度报告（报价变化时可随时重算）
    # ------------------------------------------------------------------

    def residual_report(
        self,
        ruleset: RuleSet,
        snapshot: PortfolioSnapshot,
        exemptions: Sequence[Exemption] = (),
        at: Optional[datetime] = None,
        valuation: Optional[Dict[str, str]] = None,
    ) -> ResidualReport:
        moment = at or datetime.now()
        lines: List[ResidualLine] = []
        for limit in ruleset.aggregation_limits():
            result = self._evaluate_limit(limit, snapshot, exemptions, moment, None)
            lines.append(
                ResidualLine(
                    dimension=limit.dimension,
                    target=limit.target,
                    rule_limit=limit.max_exposure,
                    exempted_amount=result.exempted_amount,
                    effective_limit=result.effective_limit,
                    position_used=result.position_used,
                    pending_used=result.pending_used,
                    residual=result.residual,
                    exemption_ids=result.exemption_ids,
                )
            )
        total_position = sum((p.market_value for p in snapshot.positions), ZERO)
        total_pending = sum((o.reserved_amount for o in snapshot.open_orders), ZERO)
        return ResidualReport(
            portfolio_id=ruleset.portfolio_id,
            evaluated_at=moment,
            rule_version=ruleset.version,
            total_position_value=total_position,
            total_pending=total_pending,
            lines=tuple(lines),
            valuation=valuation or {},
        )
