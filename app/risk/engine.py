"""组合级风险预算引擎。

职责
----
把四类输入连接起来：

* 组合现状：持仓数量/成本、行情标记价格、可用资金；
* 待成交订单：``Reservation`` 中仍未成交的数量对额度的占用；
* 风险规则：当前生效的不可变 ``RuleVersion``；
* 临时豁免：有效期内、可按桶或按订单追加额度的 ``Exemption``。

每次评估输出每个维度桶的 ``limit / current / pending / incoming /
projected / remaining`` 全量试算（``ExposureReport``），放行或拒绝都
把这份试算写入 append-only 的决策审计。

事务与并发
----------
* 每个组合一把可重入锁，approve / fill / cancel / publish 等状态
  迁移串行化，“评估 + 预留 + 审计”在同一把锁、同一个存储事务内完成，
  并发下单不可能同时看到同一份剩余额度；
* 存储事务失败时不提交任何预留或审计（见 ``RiskStore.transaction``）。

版本与追溯
----------
* 规则换版只新增版本，绝不修改旧版本；
* 订单批准时把规则版本号冻结进 ``Reservation``，之后部分成交、撤销、
  审计复核均带该版本号，可按当时口径回放；
* 待成交订单以批准时冻结价格计价，行情波动只影响现实持仓的市值
  重估，不改变在途单的预留金额。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from app.risk.models import (
    BucketCheck,
    Dimension,
    Exemption,
    ExposureReport,
    InstrumentInfo,
    OrderRequest,
    Reservation,
    ReservationStatus,
    RiskDecision,
    RuleLimits,
    RuleVersion,
    Side,
    Violation,
    advance,
)
from app.risk.store import InMemoryRiskStore, RiskStore, now_local

_D = Decimal


def _money(value: Decimal) -> Decimal:
    """金额口径：保留两位小数。"""
    return value.quantize(Decimal("0.01"))


@dataclass
class Holding:
    """组合内的一条持仓。"""

    stock_code: str
    quantity: int
    avg_cost: Decimal
    mark_price: Decimal

    @property
    def market_value(self) -> Decimal:
        return _money(self.mark_price * self.quantity)


class RiskRejected(Exception):
    """订单被组合风险预算拒绝。携带完整试算报告与审计决策。"""

    def __init__(self, report: ExposureReport, decision: RiskDecision):
        self.report = report
        self.decision = decision
        super().__init__(
            "; ".join(v.message for v in report.violations) or "风控拒绝"
        )


class PortfolioRiskEngine:
    """组合风险预算引擎。"""

    def __init__(
        self,
        store: Optional[RiskStore] = None,
        initial_cash: Decimal = _D("1000000"),
    ) -> None:
        self.store = store or InMemoryRiskStore()
        self._initial_cash = _money(initial_cash)
        self._locks_guard = threading.Lock()
        self._locks: Dict[str, threading.RLock] = {}
        self._holdings: Dict[str, Dict[str, Holding]] = {}
        self._quotes: Dict[str, Dict[str, Decimal]] = {}
        self._cash: Dict[str, Decimal] = {}

    # ------------------------------------------------------------------
    # 组合管理
    # ------------------------------------------------------------------
    def _lock_for(self, portfolio_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(portfolio_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[portfolio_id] = lock
            return lock

    def ensure_portfolio(
        self,
        portfolio_id: str,
        initial_cash: Optional[Decimal] = None,
        limits: Optional[RuleLimits] = None,
        published_by: str = "system",
    ) -> RuleVersion:
        """确保组合已初始化（资金、持仓账本、首版规则）。幂等。"""
        with self._lock_for(portfolio_id), self.store.transaction():
            self._holdings.setdefault(portfolio_id, {})
            self._quotes.setdefault(portfolio_id, {})
            self._cash.setdefault(
                portfolio_id,
                self._initial_cash if initial_cash is None else _money(initial_cash),
            )
            rule = self.store.get_current_rule_version(portfolio_id)
            if rule is None:
                rule = self._publish_locked(
                    portfolio_id,
                    limits or RuleLimits(),
                    published_by=published_by,
                    note="自动初始化默认规则",
                    at=now_local(),
                )
            return rule

    def sync_cash(self, portfolio_id: str, available_cash: Decimal) -> None:
        """与外部账户对账，覆盖可用资金（如柜台推送）。"""
        with self._lock_for(portfolio_id):
            self.ensure_portfolio(portfolio_id)
            self._cash[portfolio_id] = _money(available_cash)

    def sync_holding(
        self,
        portfolio_id: str,
        stock_code: str,
        quantity: int,
        avg_cost: Decimal,
        mark_price: Optional[Decimal] = None,
    ) -> None:
        """与外部持仓对账：覆盖某只证券的持仓数量与成本。"""
        with self._lock_for(portfolio_id):
            self.ensure_portfolio(portfolio_id)
            marks = self._quotes[portfolio_id]
            mark = marks.get(stock_code, avg_cost if mark_price is None else mark_price)
            if quantity <= 0:
                self._holdings[portfolio_id].pop(stock_code, None)
            else:
                self._holdings[portfolio_id][stock_code] = Holding(
                    stock_code=stock_code,
                    quantity=quantity,
                    avg_cost=_D(str(avg_cost)),
                    mark_price=_D(str(mark)),
                )

    # ------------------------------------------------------------------
    # 证券主数据
    # ------------------------------------------------------------------
    def register_instrument(self, info: InstrumentInfo) -> None:
        self.store.upsert_instrument(info)

    def _instrument_locked(self, portfolio_id: str, stock_code: str) -> InstrumentInfo:
        info = self.store.get_instrument(stock_code)
        if info is None:
            # 未登记主数据时放入显式的 UNKNOWN 桶，绝不静默归入某个行业
            info = InstrumentInfo(stock_code=stock_code)
            self.store.upsert_instrument(info)
        return info

    # ------------------------------------------------------------------
    # 规则版本
    # ------------------------------------------------------------------
    def publish_rules(
        self,
        portfolio_id: str,
        limits: RuleLimits,
        published_by: str = "risk_manager",
        note: str = "",
        at: Optional[datetime] = None,
    ) -> RuleVersion:
        """发布新版规则。版本号单调递增；不影响在途订单冻结的旧版本。"""
        with self._lock_for(portfolio_id), self.store.transaction():
            self.ensure_portfolio(portfolio_id)
            return self._publish_locked(
                portfolio_id,
                limits,
                published_by=published_by,
                note=note,
                at=at or now_local(),
            )

    def _publish_locked(
        self,
        portfolio_id: str,
        limits: RuleLimits,
        published_by: str,
        note: str,
        at: datetime,
    ) -> RuleVersion:
        versions = self.store.list_rule_versions(portfolio_id)
        next_version = (versions[-1].version + 1) if versions else 1
        rule = RuleVersion(
            portfolio_id=portfolio_id,
            version=next_version,
            limits=limits,
            created_at=at,
            effective_at=at,
            published_by=published_by,
            note=note,
        )
        self.store.add_rule_version(rule)
        return rule

    def current_rules(self, portfolio_id: str) -> RuleVersion:
        self.ensure_portfolio(portfolio_id)
        rule = self.store.get_current_rule_version(portfolio_id)
        assert rule is not None
        return rule

    def rules_at(self, portfolio_id: str, version: int) -> RuleVersion:
        """按版本号取回历史规则，用于审计回放。"""
        rule = self.store.get_rule_version(portfolio_id, version)
        if rule is None:
            raise ValueError(f"规则版本不存在: {portfolio_id} v{version}")
        return rule

    # ------------------------------------------------------------------
    # 豁免
    # ------------------------------------------------------------------
    def grant_exemption(
        self,
        portfolio_id: str,
        dimension: Dimension,
        extra_amount: Decimal,
        valid_until: datetime,
        bucket: Optional[str] = None,
        order_id: Optional[str] = None,
        reason: str = "",
        approved_by: str = "risk_manager",
        valid_from: Optional[datetime] = None,
        exemption_id: Optional[str] = None,
        at: Optional[datetime] = None,
    ) -> Exemption:
        """授予临时豁免。"""
        now = at or now_local()
        exemption = Exemption(
            exemption_id=exemption_id or f"EX_{uuid.uuid4().hex[:12]}",
            portfolio_id=portfolio_id,
            dimension=dimension,
            extra_amount=_D(str(extra_amount)),
            created_at=now,
            valid_from=valid_from or now,
            valid_until=valid_until,
            bucket=bucket,
            order_id=order_id,
            reason=reason,
            approved_by=approved_by,
        )
        with self._lock_for(portfolio_id), self.store.transaction():
            self.ensure_portfolio(portfolio_id)
            self.store.add_exemption(exemption)
        return exemption

    def _active_exemptions(
        self,
        portfolio_id: str,
        dimension: Dimension,
        bucket: str,
        order_id: Optional[str],
        now: datetime,
    ) -> List[Exemption]:
        result = []
        for ex in self.store.list_exemptions(portfolio_id):
            if not ex.is_active(now) or ex.dimension is not dimension:
                continue
            if ex.bucket is not None and ex.bucket != bucket:
                continue
            if ex.order_id is not None and ex.order_id != order_id:
                continue
            result.append(ex)
        return result

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------
    def update_quote(
        self,
        portfolio_id: str,
        stock_code: str,
        price: Decimal,
        at: Optional[datetime] = None,
    ) -> ExposureReport:
        """行情变化：更新标记价格并立即重估剩余额度。"""
        with self._lock_for(portfolio_id):
            self.ensure_portfolio(portfolio_id)
            price = _D(str(price))
            self._quotes[portfolio_id][stock_code] = price
            holding = self._holdings[portfolio_id].get(stock_code)
            if holding is not None:
                self._holdings[portfolio_id][stock_code] = Holding(
                    stock_code,
                    holding.quantity,
                    holding.avg_cost,
                    price,
                )
            return self._build_report(portfolio_id, None, now=at or now_local())

    # ------------------------------------------------------------------
    # 评估（只读试算）
    # ------------------------------------------------------------------
    def check(
        self, order: OrderRequest, at: Optional[datetime] = None
    ) -> ExposureReport:
        """只读试算：不占用额度、不写审计。"""
        with self._lock_for(order.portfolio_id):
            self.ensure_portfolio(order.portfolio_id)
            return self._build_report(
                order.portfolio_id, order, now=at or now_local()
            )

    def exposure(
        self, portfolio_id: str, at: Optional[datetime] = None
    ) -> ExposureReport:
        """当前组合在所有维度上的剩余额度全景。"""
        with self._lock_for(portfolio_id):
            self.ensure_portfolio(portfolio_id)
            return self._build_report(portfolio_id, None, now=at or now_local())

    # ------------------------------------------------------------------
    # 批准（评估 + 预留 + 审计，原子完成）
    # ------------------------------------------------------------------
    def approve(
        self, order: OrderRequest, at: Optional[datetime] = None
    ) -> Tuple[Reservation, RiskDecision]:
        """批准订单并预留额度。

        拒绝时同样写入 REJECT 审计，并抛出 ``RiskRejected``（内含试算
        报告与审计记录），调用方可直接用于驳回应答。
        """
        portfolio_id = order.portfolio_id
        with self._lock_for(portfolio_id):
            self.ensure_portfolio(portfolio_id)
            now = at or now_local()
            if self.store.get_reservation(order.order_id) is not None:
                raise ValueError(f"订单已评估过，不能重复批准: {order.order_id}")

            report = self._build_report(portfolio_id, order, now=now)
            if not report.approved:
                decision = self._write_decision(
                    portfolio_id,
                    order.order_id,
                    action="REJECT",
                    approved=False,
                    rule_version=report.rule_version,
                    report=report,
                    reason="; ".join(v.message for v in report.violations),
                    at=now,
                )
                raise RiskRejected(report, decision)

            rule = self.store.get_rule_version(portfolio_id, report.rule_version)
            assert rule is not None
            info = self._instrument_locked(portfolio_id, order.stock_code)
            price = _D(str(report.valuations[order.stock_code]))
            # 只记录本单实际占用桶上的豁免（incoming>0），避免把无关
            # 行业桶上恰好生效的豁免记到这笔订单名下。
            exemption_ids = sorted(
                {
                    eid
                    for c in report.checks
                    if c.incoming > 0
                    for eid in c.exemption_ids
                }
            )

            reservation = Reservation(
                order_id=order.order_id,
                portfolio_id=portfolio_id,
                stock_code=order.stock_code,
                industry=info.industry,
                instrument_type=info.instrument_type,
                side=order.side,
                quantity=order.quantity,
                filled_quantity=0,
                frozen_price=price,
                rule_version=rule.version,
                status=ReservationStatus.HELD,
                created_at=now,
                updated_at=now,
                exemption_ids=exemption_ids,
            )
            with self.store.transaction():
                self.store.put_reservation(reservation)
                # 单订单豁免在批准时一次性消费
                for ex in self.store.list_exemptions(portfolio_id):
                    if (
                        ex.order_id == order.order_id
                        and ex.is_active(now)
                        and ex.consumed_by_order is None
                    ):
                        self.store.consume_exemption(ex.exemption_id, order.order_id)
                decision = self._write_decision(
                    portfolio_id,
                    order.order_id,
                    action="APPROVE",
                    approved=True,
                    rule_version=rule.version,
                    report=report,
                    exemption_ids=exemption_ids,
                    detail={
                        "frozen_price": str(price),
                        "reserved_amount": str(
                            _money(price * order.quantity)
                        ),
                    },
                    reason="OK",
                    at=now,
                )
            return reservation, decision

    # ------------------------------------------------------------------
    # 成交 / 部分成交
    # ------------------------------------------------------------------
    def report_fill(
        self,
        order_id: str,
        fill_quantity: int,
        fill_price: Optional[Decimal] = None,
        at: Optional[datetime] = None,
    ) -> Tuple[Reservation, RiskDecision]:
        """上报成交（支持部分成交）。

        预留中成交部分转为现实持仓，未成交部分继续占用；订单始终按
        批准时冻结的 ``rule_version`` 追溯。
        """
        reservation = self._require_reservation(order_id)
        portfolio_id = reservation.portfolio_id
        with self._lock_for(portfolio_id):
            now = at or now_local()
            if reservation.status not in (
                ReservationStatus.HELD,
                ReservationStatus.PARTIAL,
            ):
                raise ValueError(
                    f"订单状态 {reservation.status.value} 不可再成交: {order_id}"
                )
            if fill_quantity <= 0 or fill_quantity > reservation.open_quantity:
                raise ValueError(
                    f"成交数量非法: {fill_quantity}, 剩余可成交 "
                    f"{reservation.open_quantity}"
                )

            price = (
                reservation.frozen_price
                if fill_price is None
                else _D(str(fill_price))
            )
            new_filled = reservation.filled_quantity + fill_quantity
            new_status = (
                ReservationStatus.FILLED
                if new_filled >= reservation.quantity
                else ReservationStatus.PARTIAL
            )
            with self.store.transaction():
                self._apply_fill_to_book(reservation, fill_quantity, price)
                reservation = advance(
                    reservation,
                    filled_quantity=new_filled,
                    status=new_status,
                    updated_at=now,
                )
                self.store.put_reservation(reservation)
                # 快照按订单冻结版本构建：历史事件完全可按当时口径回放
                frozen_rule = self.store.get_rule_version(
                    portfolio_id, reservation.rule_version
                )
                report = self._build_report(
                    portfolio_id, None, now=now, rule_override=frozen_rule
                )
                decision = self._write_decision(
                    portfolio_id,
                    order_id,
                    action="FILL",
                    approved=True,
                    rule_version=reservation.rule_version,
                    report=report,
                    exemption_ids=reservation.exemption_ids,
                    detail={
                        "fill_quantity": fill_quantity,
                        "fill_price": str(price),
                        "filled_quantity": new_filled,
                        "remaining_quantity": reservation.open_quantity,
                    },
                    reason="OK",
                    at=now,
                )
            return reservation, decision

    def _apply_fill_to_book(
        self, reservation: Reservation, fill_quantity: int, price: Decimal
    ) -> None:
        portfolio_id = reservation.portfolio_id
        code = reservation.stock_code
        holdings = self._holdings[portfolio_id]
        cash = self._cash[portfolio_id]
        amount = _money(price * fill_quantity)

        if reservation.side is Side.BUY:
            existing = holdings.get(code)
            if existing is None:
                holdings[code] = Holding(code, fill_quantity, price, price)
            else:
                total_cost = existing.avg_cost * existing.quantity + price * fill_quantity
                qty = existing.quantity + fill_quantity
                holdings[code] = Holding(
                    code, qty, total_cost / qty, existing.mark_price
                )
            cash -= amount
        else:
            existing = holdings.get(code)
            if existing is None or existing.quantity < fill_quantity:
                raise ValueError(
                    f"卖出成交超过持仓: {code} 需要 {fill_quantity}"
                )
            left = existing.quantity - fill_quantity
            if left == 0:
                holdings.pop(code, None)
            else:
                holdings[code] = Holding(
                    code, left, existing.avg_cost, existing.mark_price
                )
            cash += amount
        self._cash[portfolio_id] = cash

    # ------------------------------------------------------------------
    # 撤销 / 拒单 / 失效 —— 额度回滚
    # ------------------------------------------------------------------
    def cancel(
        self, order_id: str, reason: str = "用户撤单", at: Optional[datetime] = None
    ) -> Tuple[Reservation, RiskDecision]:
        """撤销未成交完毕的订单：剩余占用一次性释放并审计。"""
        return self._release(order_id, "RELEASE", ReservationStatus.RELEASED, reason, at)

    def report_downstream_rejection(
        self, order_id: str, reason: str, at: Optional[datetime] = None
    ) -> Tuple[Reservation, RiskDecision]:
        """已批准订单被柜台/交易所拒绝：回滚全部剩余占用。"""
        return self._release(order_id, "REJECT_DOWNSTREAM", ReservationStatus.REJECTED, reason, at)

    def report_expired(
        self, order_id: str, reason: str = "订单超时失效", at: Optional[datetime] = None
    ) -> Tuple[Reservation, RiskDecision]:
        """订单超时失效：回滚剩余占用。"""
        return self._release(order_id, "EXPIRE", ReservationStatus.EXPIRED, reason, at)

    def _release(
        self,
        order_id: str,
        action: str,
        new_status: ReservationStatus,
        reason: str,
        at: Optional[datetime],
    ) -> Tuple[Reservation, RiskDecision]:
        reservation = self._require_reservation(order_id)
        portfolio_id = reservation.portfolio_id
        with self._lock_for(portfolio_id):
            now = at or now_local()
            if reservation.status not in (
                ReservationStatus.HELD,
                ReservationStatus.PARTIAL,
            ):
                raise ValueError(
                    f"订单状态 {reservation.status.value}，无需释放: {order_id}"
                )
            released_qty = reservation.open_quantity
            with self.store.transaction():
                reservation = advance(
                    reservation, status=new_status, updated_at=now
                )
                self.store.put_reservation(reservation)
                frozen_rule = self.store.get_rule_version(
                    portfolio_id, reservation.rule_version
                )
                report = self._build_report(
                    portfolio_id, None, now=now, rule_override=frozen_rule
                )
                decision = self._write_decision(
                    portfolio_id,
                    order_id,
                    action=action,
                    approved=True,
                    rule_version=reservation.rule_version,
                    report=report,
                    exemption_ids=reservation.exemption_ids,
                    detail={
                        "released_quantity": released_qty,
                        "released_amount": str(
                            _money(reservation.frozen_price * released_qty)
                        ),
                        "already_filled_quantity": reservation.filled_quantity,
                    },
                    reason=reason,
                    at=now,
                )
            return reservation, decision

    # ------------------------------------------------------------------
    # 审计查询
    # ------------------------------------------------------------------
    def get_reservation(self, order_id: str) -> Optional[Reservation]:
        return self.store.get_reservation(order_id)

    def open_reservations(self, portfolio_id: str) -> List[Reservation]:
        return self.store.list_reservations(portfolio_id, open_only=True)

    def decisions(
        self,
        portfolio_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> List[RiskDecision]:
        return self.store.list_decisions(portfolio_id, order_id)

    # ------------------------------------------------------------------
    # 内部：试算
    # ------------------------------------------------------------------
    def _require_reservation(self, order_id: str) -> Reservation:
        reservation = self.store.get_reservation(order_id)
        if reservation is None:
            raise ValueError(f"订单不存在: {order_id}")
        return reservation

    def _build_report(
        self,
        portfolio_id: str,
        order: Optional[OrderRequest],
        now: datetime,
        rule_override: Optional[RuleVersion] = None,
    ) -> ExposureReport:
        rule = rule_override or self.store.get_current_rule_version(portfolio_id)
        assert rule is not None, "组合尚未发布规则版本"
        limits = rule.limits
        holdings = self._holdings.get(portfolio_id, {})
        open_rsvs = self.store.list_reservations(portfolio_id, open_only=True)

        # 全量计价表：持仓标记价 + 在途单冻结价 + 候选单价格
        valuations: Dict[str, str] = {}
        for code, h in holdings.items():
            valuations[code] = str(h.mark_price)
        for r in open_rsvs:
            valuations.setdefault(r.stock_code, str(r.frozen_price))

        incoming_price: Optional[Decimal] = None
        incoming_amount = _D("0")
        incoming_info: Optional[InstrumentInfo] = None
        if order is not None:
            incoming_info = self._instrument_locked(portfolio_id, order.stock_code)
            quote = self._quotes.get(portfolio_id, {}).get(order.stock_code)
            if order.limit_price is not None:
                incoming_price = _D(str(order.limit_price))
            elif quote is not None:
                incoming_price = quote
            elif order.side is Side.BUY:
                # 买单必须能计价：在途买单要冻结资金
                raise ValueError(
                    f"市价买单缺少行情，无法计价: {order.stock_code}"
                )
            # 市价卖单没有价格时，不参与金额类桶（卖单只校验可交割数量）
            if incoming_price is not None:
                valuations[order.stock_code] = str(incoming_price)
                incoming_amount = _money(incoming_price * order.quantity)

        # 当前持仓按桶归集（用最新行情重估）
        cur_total = _D("0")
        cur_industry: Dict[str, Decimal] = {}
        cur_instrument: Dict[str, Decimal] = {}
        for code, h in holdings.items():
            info = self._instrument_locked(portfolio_id, code)
            mv = h.market_value
            cur_total += mv
            cur_industry[info.industry] = cur_industry.get(info.industry, _D("0")) + mv
            cur_instrument[info.instrument_type] = (
                cur_instrument.get(info.instrument_type, _D("0")) + mv
            )

        # 待成交买单按桶归集（在途冻结价；卖单不计入多头敞口）
        pend_total = _D("0")
        pend_industry: Dict[str, Decimal] = {}
        pend_instrument: Dict[str, Decimal] = {}
        pend_cash = _D("0")
        pend_sell_qty: Dict[str, int] = {}
        for r in open_rsvs:
            if r.side is Side.BUY:
                amt = r.open_amount
                pend_total += amt
                pend_industry[r.industry] = (
                    pend_industry.get(r.industry, _D("0")) + amt
                )
                pend_instrument[r.instrument_type] = (
                    pend_instrument.get(r.instrument_type, _D("0")) + amt
                )
                pend_cash += amt
            else:
                pend_sell_qty[r.stock_code] = (
                    pend_sell_qty.get(r.stock_code, 0) + r.open_quantity
                )

        checks: List[BucketCheck] = []
        violations: List[Violation] = []

        def add_bucket(
            dimension: Dimension,
            bucket: str,
            base_limit: Decimal,
            current: Decimal,
            pending: Decimal,
            incoming: Decimal,
            unit: str = "元",
        ) -> None:
            # 金额类额度统一到两位小数口径（数量类 POSITION 桶保持整数）
            if dimension is not Dimension.POSITION:
                base_limit = _money(base_limit)
                current = _money(current)
                pending = _money(pending)
                incoming = _money(incoming)
            active = self._active_exemptions(
                portfolio_id, dimension, bucket,
                order.order_id if order else None, now,
            )
            extra = sum((e.extra_amount for e in active), _D("0"))
            effective = base_limit + (extra if dimension is Dimension.POSITION else _money(extra))
            projected = current + pending + incoming
            remaining_before = effective - current - pending
            remaining_after = effective - projected
            passed = projected <= effective
            ex_ids = sorted(e.exemption_id for e in active)

            if passed:
                if dimension is Dimension.ORDER:
                    message = f"单笔金额 {incoming} {unit}，限额 {effective} {unit}"
                elif dimension is Dimension.CASH:
                    message = (
                        f"在途买单冻结 {pending} {unit}，本单 {incoming} {unit}，"
                        f"可用资金 {effective} {unit}"
                    )
                elif dimension is Dimension.POSITION:
                    message = (
                        f"在途卖出 {pending} 股，本单 {incoming} 股，"
                        f"可交割 {effective} 股"
                    )
                else:
                    message = (
                        f"{dimension.value}:{bucket} 现值 {current}，在途 {pending}，"
                        f"本单 {incoming}，上限 {effective}（剩余 {remaining_after}）"
                    )
            else:
                message = (
                    f"{dimension.value}:{bucket} 试算 {projected} {unit} 超过上限 "
                    f"{effective} {unit}（基础限额 {base_limit}"
                    + (f"，豁免追加 {extra}，豁免 {','.join(ex_ids)}" if extra else "")
                    + f"；现值 {current}，在途 {pending}，本单 {incoming}，"
                    f"当前剩余 {remaining_before}）"
                )

            check = BucketCheck(
                dimension=dimension,
                bucket=bucket,
                base_limit=base_limit,
                effective_limit=effective,
                current=current,
                pending=pending,
                incoming=incoming,
                projected=projected,
                remaining_before=remaining_before,
                remaining_after=remaining_after,
                exemption_ids=ex_ids,
                passed=passed,
                message=message,
            )
            checks.append(check)
            if not passed:
                violations.append(
                    Violation(
                        dimension=dimension,
                        bucket=bucket,
                        message=message,
                        base_limit=base_limit,
                        effective_limit=effective,
                        current=current,
                        pending=pending,
                        incoming=incoming,
                        projected=projected,
                        remaining_before=remaining_before,
                        exemption_ids=ex_ids,
                    )
                )

        # ---- 总敞口 / 行业 / 品种：始终输出全量桶（无候选单时也可解释） ----
        industries = set(cur_industry) | set(pend_industry)
        instruments = set(cur_instrument) | set(pend_instrument)
        inc_industry = inc_instrument = None
        if order is not None and incoming_info is not None:
            industries.add(incoming_info.industry)
            instruments.add(incoming_info.instrument_type)
            inc_industry = incoming_info.industry
            inc_instrument = incoming_info.instrument_type

        add_bucket(
            Dimension.TOTAL,
            "portfolio",
            limits.max_total_long_amount,
            cur_total,
            pend_total,
            incoming_amount if order and order.side is Side.BUY else _D("0"),
        )
        for industry in sorted(industries):
            add_bucket(
                Dimension.INDUSTRY,
                industry,
                limits.industry_limit(industry),
                cur_industry.get(industry, _D("0")),
                pend_industry.get(industry, _D("0")),
                incoming_amount
                if order and order.side is Side.BUY and inc_industry == industry
                else _D("0"),
            )
        for itype in sorted(instruments):
            add_bucket(
                Dimension.INSTRUMENT,
                itype,
                limits.instrument_limit(itype),
                cur_instrument.get(itype, _D("0")),
                pend_instrument.get(itype, _D("0")),
                incoming_amount
                if order and order.side is Side.BUY and inc_instrument == itype
                else _D("0"),
            )

        # ---- 单笔订单桶（仅候选单） ----
        if order is not None:
            add_bucket(
                Dimension.ORDER,
                order.stock_code,
                limits.max_single_order_amount,
                _D("0"),
                _D("0"),
                incoming_amount,
            )

        # ---- 资金/持仓桶 ----
        if order is None:
            # 全景视图：展示剩余买力（可用资金相对在途买单冻结）
            add_bucket(
                Dimension.CASH,
                "available_cash",
                self._cash[portfolio_id],
                _D("0"),
                pend_cash,
                _D("0"),
            )
        elif order.side is Side.BUY:
            add_bucket(
                Dimension.CASH,
                "available_cash",
                self._cash[portfolio_id],
                _D("0"),
                pend_cash,
                incoming_amount,
            )
        else:
            holding = holdings.get(order.stock_code)
            available_qty = Decimal(holding.quantity if holding else 0)
            pending_qty = Decimal(pend_sell_qty.get(order.stock_code, 0))
            add_bucket(
                Dimension.POSITION,
                order.stock_code,
                available_qty,
                _D("0"),
                pending_qty,
                Decimal(order.quantity),
                unit="股",
            )

        return ExposureReport(
            portfolio_id=portfolio_id,
            rule_version=rule.version,
            evaluated_at=now,
            order_id=order.order_id if order else None,
            valuations=valuations,
            checks=checks,
            violations=violations,
            approved=not violations,
        )

    # ------------------------------------------------------------------
    # 内部：审计写入
    # ------------------------------------------------------------------
    def _write_decision(
        self,
        portfolio_id: str,
        order_id: str,
        action: str,
        approved: bool,
        rule_version: int,
        at: datetime,
        report: Optional[ExposureReport] = None,
        exemption_ids: Optional[List[str]] = None,
        detail: Optional[Dict] = None,
        reason: str = "",
    ) -> RiskDecision:
        decision = RiskDecision(
            decision_id=f"DEC_{uuid.uuid4().hex[:16]}",
            portfolio_id=portfolio_id,
            order_id=order_id,
            action=action,
            approved=approved,
            rule_version=rule_version,
            at=at,
            report=report,
            exemption_ids=exemption_ids or [],
            detail=detail or {},
            reason=reason,
        )
        self.store.append_decision(decision)
        return decision
