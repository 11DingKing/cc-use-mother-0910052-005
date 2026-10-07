"""组合级风险预算服务：业务编排与事务边界。

把持仓、待成交订单（风控台账）、规则版本、临时豁免与证券分类连接起来：

- 下单前 ``precheck`` / ``check_and_reserve``：在"组合锁 + DB 事务"内
  读取最新规则、生效豁免、持仓与在途预留，一次算完总敞口/行业/品种/
  单笔四条口径；通过则同事务写入预留（原子的检查并预留），拒绝则不留
  任何痕迹（除审计决策本身）。
- 订单状态变化 ``on_order_update``：部分成交缩减剩余预留，全部成交
  释放预留（敞口转入持仓），撤销/拒绝/失败回滚释放全部预留。
- 报价变化时 ``residual_report`` 用最新价即时重算可解释剩余额度。
- 规则换版只追加新版本；历史决策内嵌完整快照，``get_decision`` 可按
  当时版本逐条复算复核。
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence

from app.entities.risk import RiskDecisionEntity, RiskOrderEntity
from app.middleware.exception_handler import AppException
from app.risk.engine import PortfolioRiskEngine, PortfolioSnapshot
from app.risk.models import (
    Exemption,
    Instrument,
    LedgerEntry,
    LedgerStatus,
    OrderIntent,
    PositionView,
    RiskDecision,
    RiskDimension,
    RiskLimit,
    RuleSet,
)
from app.risk.store import RiskStore, RiskStoreError

logger = logging.getLogger(__name__)


class RiskRejectedException(AppException):
    """组合级风控拒绝下单（HTTP 409：与当前预算状态冲突）。"""

    def __init__(self, decision: RiskDecision):
        self.decision = decision
        failures = [r.reason for r in decision.results if not r.passed]
        super().__init__(
            message="组合风险预算不足：" + "；".join(failures),
            code="RISK_REJECTED",
            status_code=409,
            details={
                "decision_id": decision.decision_id,
                "order_id": decision.order_id,
                "rule_version": decision.rule_version,
                "reasons": failures,
                "results": [r.to_dict() for r in decision.results],
            },
        )


# 订单域状态 -> 风控台账状态
_STATUS_MAP = {
    "pending": LedgerStatus.RESERVED,
    "submitted": LedgerStatus.RESERVED,
    "partial": LedgerStatus.PARTIAL,
    "filled": LedgerStatus.FILLED,
    "cancelled": LedgerStatus.CANCELLED,
    "rejected": LedgerStatus.REJECTED,
    "failed": LedgerStatus.FAILED,
}


class PortfolioRiskService:
    """组合风险预算的对外门面。"""

    def __init__(
        self,
        store: Optional[RiskStore] = None,
        portfolio_id: str = "default",
        engine: Optional[PortfolioRiskEngine] = None,
        position_provider: Optional[Callable[[], Sequence[Any]]] = None,
        quote_provider: Optional[Callable[[str], Optional[Dict]]] = None,
    ):
        self.portfolio_id = portfolio_id
        self.store = store or RiskStore(portfolio_id=portfolio_id)
        self.engine = engine or PortfolioRiskEngine()
        # 持仓/行情供给：默认返回空，接入交易适配器后由其提供
        self.position_provider = position_provider or (lambda: [])
        self.quote_provider = quote_provider or (lambda code: None)

    # ------------------------------------------------------------------
    # 规则版本管理
    # ------------------------------------------------------------------

    def publish_ruleset(
        self,
        limits: List[Dict[str, Any]],
        max_single_order_amount: Any = Decimal("100000"),
        created_by: str = "system",
        note: str = "",
    ) -> Dict[str, Any]:
        """发布新规则版本（append-only）。旧版本永不修改。"""
        parsed = [self._parse_limit(item) for item in limits]
        self._validate_limits(parsed)
        ruleset = self.store.publish_ruleset(
            limits=parsed,
            max_single_order_amount=Decimal(str(max_single_order_amount)),
            created_by=created_by,
            note=note,
            portfolio_id=self.portfolio_id,
        )
        return ruleset.to_dict()

    def ensure_default_ruleset(self) -> RuleSet:
        """若无任何版本，发布一版宽松默认规则，保证服务可用。"""
        ruleset = self.store.get_ruleset(portfolio_id=self.portfolio_id)
        if ruleset is None:
            ruleset = self.store.publish_ruleset(
                limits=[
                    RiskLimit(RiskDimension.TOTAL, "TOTAL", Decimal("100000000")),
                ],
                max_single_order_amount=Decimal("100000"),
                note="系统初始化默认规则",
                portfolio_id=self.portfolio_id,
            )
        return ruleset

    def current_ruleset(self) -> RuleSet:
        ruleset = self.store.get_ruleset(portfolio_id=self.portfolio_id)
        if ruleset is None:
            raise RiskStoreError("组合尚未发布任何风险规则版本")
        return ruleset

    def get_ruleset(self, version: Optional[int] = None) -> Dict[str, Any]:
        ruleset = self.store.get_ruleset(version=version, portfolio_id=self.portfolio_id)
        if ruleset is None:
            raise RiskStoreError(f"规则版本 {version} 不存在")
        return ruleset.to_dict()

    def list_rule_versions(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self.store.list_rule_versions(self.portfolio_id)]

    @staticmethod
    def _parse_limit(item: Dict[str, Any]) -> RiskLimit:
        try:
            dimension = RiskDimension(item["dimension"])
        except (KeyError, ValueError) as exc:
            raise RiskStoreError(
                f"dimension 必须是 {[d.value for d in RiskDimension]}"
            ) from exc
        target = item.get("target") or ("TOTAL" if dimension == RiskDimension.TOTAL else None)
        if not target:
            raise RiskStoreError(f"{dimension.value} 规则必须指定 target")
        if dimension == RiskDimension.TOTAL and target != "TOTAL":
            raise RiskStoreError("总敞口规则的 target 必须为 TOTAL")
        amount = Decimal(str(item["max_exposure"]))
        if amount <= 0:
            raise RiskStoreError("限额必须为正数")
        return RiskLimit(dimension, target, amount, item.get("note", ""))

    @staticmethod
    def _validate_limits(limits: List[RiskLimit]) -> None:
        seen = set()
        for limit in limits:
            if limit.key in seen:
                raise RiskStoreError(
                    f"重复规则：{limit.dimension.value}:{limit.target}"
                )
            seen.add(limit.key)

    # ------------------------------------------------------------------
    # 豁免管理（版本化、可撤销）
    # ------------------------------------------------------------------

    def grant_exemption(
        self,
        dimension: str,
        target: str,
        extra_amount: Any,
        valid_minutes: int = 60,
        reason: str = "",
        granted_by: str = "system",
        valid_from: Optional[datetime] = None,
        valid_to: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        start = valid_from or datetime.now()
        end = valid_to or (start + timedelta(minutes=valid_minutes))
        exemption = self.store.grant_exemption(
            dimension=RiskDimension(dimension),
            target=target,
            extra_amount=Decimal(str(extra_amount)),
            valid_from=start,
            valid_to=end,
            reason=reason,
            granted_by=granted_by,
            portfolio_id=self.portfolio_id,
        )
        logger.info(
            "授予豁免 %s v%d %s:%s +%s（%s）",
            exemption.exemption_id, exemption.version,
            dimension, target, exemption.extra_amount, reason,
        )
        return exemption.to_dict()

    def revoke_exemption(self, exemption_id: str) -> Dict[str, Any]:
        return self.store.revoke_exemption(
            exemption_id, portfolio_id=self.portfolio_id
        ).to_dict()

    def list_exemptions(self, include_history: bool = True) -> List[Dict[str, Any]]:
        exemptions = self.store.list_exemptions(self.portfolio_id)
        if not include_history:
            latest: Dict[str, Exemption] = {}
            for exemption in exemptions:
                latest[exemption.exemption_id] = exemption
            exemptions = list(latest.values())
        return [e.to_dict() for e in exemptions]

    # ------------------------------------------------------------------
    # 证券分类
    # ------------------------------------------------------------------

    def upsert_instrument(
        self, stock_code: str, industry: str = "其他", instrument_type: str = "STOCK"
    ) -> Dict[str, Any]:
        return self.store.upsert_instrument(
            stock_code, industry, instrument_type, self.portfolio_id
        ).to_dict()

    def classify(self, stock_codes: Sequence[str]) -> Dict[str, Instrument]:
        unique = list(dict.fromkeys(stock_codes))
        return self.store.classify_many(unique, self.portfolio_id)

    # ------------------------------------------------------------------
    # 快照构建
    # ------------------------------------------------------------------

    def _position_views(self, session, pid: str) -> List[PositionView]:
        """从交易适配器读取持仓，用最新行情价与风控分类构建估值快照。"""
        raw_positions = list(self.position_provider() or [])
        if not raw_positions:
            return []
        codes = [self._code_of(p) for p in raw_positions]
        classifications = self.store._classify_many(session, codes, pid)
        views = []
        for pos in raw_positions:
            code = self._code_of(pos)
            price = self._position_price(pos, code)
            qty = int(getattr(pos, "quantity", 0))
            if qty <= 0 or price <= 0:
                continue
            info = classifications[code]
            views.append(
                PositionView(
                    stock_code=code,
                    quantity=qty,
                    price=price,
                    industry=info.industry,
                    instrument_type=info.instrument_type,
                )
            )
        return views

    def _position_price(self, pos: Any, code: str) -> Decimal:
        """持仓估值价：优先实时行情最新价，回退到持仓上的当前价。"""
        quote = None
        try:
            quote = self.quote_provider(code)
        except Exception:  # 行情不可用时不应阻断风控
            logger.warning("获取 %s 行情失败，使用持仓现价", code)
        if quote and quote.get("last_price") is not None:
            return Decimal(str(quote["last_price"]))
        current = getattr(pos, "current_price", None)
        return Decimal(str(current)) if current is not None else Decimal("0")

    @staticmethod
    def _code_of(obj: Any) -> str:
        return getattr(obj, "stock_code")

    def _build_intent(
        self,
        order: Any,
        classifications: Dict[str, Instrument],
    ) -> OrderIntent:
        code = order.stock_code
        info = classifications.get(code) or Instrument(stock_code=code)
        price = self._valuation_price(order)
        return OrderIntent(
            order_id=order.order_id,
            stock_code=code,
            side=order.side.value if hasattr(order.side, "value") else str(order.side),
            quantity=int(order.quantity),
            valuation_price=price,
            industry=info.industry,
            instrument_type=info.instrument_type,
        )

    def _valuation_price(self, order: Any) -> Decimal:
        """委托占用预算的估值价：限价单用委托价；市价单用最新成交价。"""
        if getattr(order, "price", None) is not None:
            return Decimal(str(order.price))
        if order.order_type and getattr(order.order_type, "value", str(order.order_type)) == "market":
            quote = self.quote_provider(order.stock_code)
            if quote and quote.get("last_price") is not None:
                return Decimal(str(quote["last_price"]))
        # 兜底：无法估值时拒绝放行而不是按 0 放过
        raise RiskStoreError(
            f"订单 {order.order_id} 缺少估值价：限价单需委托价，市价单需最新行情"
        )

    # ------------------------------------------------------------------
    # 预检（只读，不预留）
    # ------------------------------------------------------------------

    def precheck(self, order: Any) -> RiskDecision:
        pid = self.portfolio_id
        self.ensure_default_ruleset()
        with self.store.transaction(pid) as session:
            ruleset = self.store._get_ruleset(session, pid)
            exemptions = self.store._active_exemptions(session, pid, datetime.now())
            positions = self._position_views(session, pid)
            entries = self.store._open_orders(
                session, [LedgerStatus.RESERVED.value, LedgerStatus.PARTIAL.value], pid
            )
            classifications = self.store._classify_many(
                session, [order.stock_code] + [p.stock_code for p in positions], pid
            )
            intent = self._build_intent(order, classifications)
            snapshot = PortfolioSnapshot.from_entries(positions, entries)
            return self.engine.evaluate(
                ruleset=ruleset,
                snapshot=snapshot,
                intent=intent,
                exemptions=exemptions,
                action="PRE_CHECK",
                valuation=self._valuation_note(intent),
            )

    # ------------------------------------------------------------------
    # 检查并预留（单事务，并发安全）
    # ------------------------------------------------------------------

    def check_and_reserve(self, order: Any) -> RiskDecision:
        """原子的"检查 + 预留"。

        返回放行决策并写入台账预留；预算不足时抛 RiskRejectedException，
        事务回滚，不产生任何预留。重复 order_id 重放同一决策（幂等）。
        """
        pid = self.portfolio_id
        # 默认规则版本在主事务之外发布，避免同连接嵌套事务
        self.ensure_default_ruleset()
        rejection: Optional[RiskRejectedException] = None
        with self.store.transaction(pid) as session:
            ruleset = self.store._get_ruleset(session, pid)
            now = datetime.now()
            exemptions = self.store._active_exemptions(session, pid, now)
            positions = self._position_views(session, pid)
            entries = self.store._open_orders(
                session, [LedgerStatus.RESERVED.value, LedgerStatus.PARTIAL.value], pid
            )

            # 幂等：同一订单已完成预留（如下单接口超时后的重试），
            # 直接重放既有放行决策，不重复占用预算。
            existing = next((e for e in entries if e.order_id == order.order_id), None)
            if existing is not None:
                logger.info("订单 %s 已预留，按幂等重放", order.order_id)
                return self._replay_decision(session, existing)

            classifications = self.store._classify_many(
                session, [order.stock_code] + [p.stock_code for p in positions], pid
            )
            intent = self._build_intent(order, classifications)
            snapshot = PortfolioSnapshot.from_entries(positions, entries)
            decision = self.engine.evaluate(
                ruleset=ruleset,
                snapshot=snapshot,
                intent=intent,
                exemptions=exemptions,
                at=now,
                action="RESERVE",
                valuation=self._valuation_note(intent),
            )

            if not decision.passed:
                # 拒绝也留审计：决策随事务提交（不写任何预留），提交后再抛异常
                self.store._insert_decision(session, decision)
                rejection = RiskRejectedException(decision)
            else:
                entry = LedgerEntry(
                    order_id=intent.order_id,
                    portfolio_id=pid,
                    stock_code=intent.stock_code,
                    side=intent.side,
                    quantity=intent.quantity,
                    filled_quantity=0,
                    remaining_quantity=intent.quantity,
                    price=intent.valuation_price,
                    industry=intent.industry,
                    instrument_type=intent.instrument_type,
                    status=LedgerStatus.RESERVED,
                    rule_version=ruleset.version,
                    created_at=now,
                    updated_at=now,
                )
                self.store._insert_reservation(session, entry)
                self.store._insert_decision(session, decision)

        # 事务已提交（拒绝决策已落审计），再向调用方抛出拒绝
        if rejection is not None:
            raise rejection
        return decision

    def _replay_decision(self, session, entry: LedgerEntry) -> RiskDecision:
        """以历史版本重算并返回一笔已预留订单的放行结论（幂等重放）。"""
        prior = (
            session.query(RiskDecisionEntity)
            .filter_by(order_id=entry.order_id, action="RESERVE")
            .order_by(RiskDecisionEntity.evaluated_at)
            .first()
        )
        if prior is not None:
            return RiskStore.decision_from_dict(prior.payload)
        # 无历史决策（理论上不会发生）：用当时版本重算一份
        pid = entry.portfolio_id
        ruleset = self.store._get_ruleset(session, pid, entry.rule_version)
        exemptions = self.store._active_exemptions(session, pid, entry.created_at)
        positions = self._position_views(session, pid)
        entries = self.store._open_orders(
            session, [LedgerStatus.RESERVED.value, LedgerStatus.PARTIAL.value], pid
        )
        intent = OrderIntent(
            order_id=entry.order_id,
            stock_code=entry.stock_code,
            side=entry.side,
            quantity=entry.quantity,
            valuation_price=entry.price,
            industry=entry.industry,
            instrument_type=entry.instrument_type,
        )
        return self.engine.evaluate(
            ruleset=ruleset,
            snapshot=PortfolioSnapshot.from_entries(positions, entries),
            intent=intent,
            exemptions=exemptions,
            at=entry.created_at,
            action="RESERVE",
        )

    # ------------------------------------------------------------------
    # 订单状态变更（部分成交 / 全成 / 撤销回滚）
    # ------------------------------------------------------------------

    def on_order_update(self, order: Any) -> Optional[RiskDecision]:
        """订单状态变化驱动台账迁移；返回释放/迁移决策（仅买单记账）。"""
        status = self._map_status(order)
        if status is None:
            return None
        pid = self.portfolio_id
        with self.store.transaction(pid) as session:
            entity = (
                session.query(RiskOrderEntity)
                .filter_by(order_id=order.order_id)
                .first()
            )
            if entity is None:
                # 卖单或未经风控通道的订单：不占预算，无需迁移
                return None
            current = LedgerStatus(entity.status)
            filled_qty = int(getattr(order, "filled_quantity", 0) or 0)
            if current == status and filled_qty == entity.filled_quantity:
                return None  # 重复事件，幂等忽略
            if current in (
                LedgerStatus.FILLED,
                LedgerStatus.CANCELLED,
                LedgerStatus.REJECTED,
                LedgerStatus.FAILED,
            ):
                logger.info(
                    "订单 %s 已在终态 %s，忽略状态事件 %s",
                    order.order_id, current.value, status.value,
                )
                return None

            updated = self.store._update_reservation(
                session, order.order_id, status, filled_qty
            )

            action = "RELEASE" if status.releases_reservation or status == LedgerStatus.FILLED else "UPDATE"
            # 状态迁移按订单批准时的规则版本追溯，保证口径一致
            ruleset = self.store._get_ruleset(session, pid, entity.rule_version)
            exemptions = self.store._active_exemptions(session, pid, entity.created_at)
            positions = self._position_views(session, pid)
            remaining_entries = self.store._open_orders(
                session, [LedgerStatus.RESERVED.value, LedgerStatus.PARTIAL.value], pid
            )
            decision = self.engine.state_decision(
                ruleset=ruleset,
                snapshot=PortfolioSnapshot.from_entries(positions, remaining_entries),
                order_id=updated.order_id,
                action=action,
                exemptions=exemptions,
                valuation={"event_status": status.value, "filled_quantity": str(filled_qty)},
            )
            self.store._insert_decision(session, decision)
            return decision

    @staticmethod
    def _map_status(order: Any) -> Optional[LedgerStatus]:
        status = getattr(order, "status", None)
        key = status.value if hasattr(status, "value") else str(status)
        return _STATUS_MAP.get(key)

    # ------------------------------------------------------------------
    # 剩余额度（报价变化时即时重算）
    # ------------------------------------------------------------------

    def residual_report(self) -> Dict[str, Any]:
        pid = self.portfolio_id
        self.ensure_default_ruleset()
        with self.store.transaction(pid) as session:
            ruleset = self.store._get_ruleset(session, pid)
            exemptions = self.store._active_exemptions(session, pid, datetime.now())
            positions = self._position_views(session, pid)
            entries = self.store._open_orders(
                session, [LedgerStatus.RESERVED.value, LedgerStatus.PARTIAL.value], pid
            )
            codes = [p.stock_code for p in positions] + [e.stock_code for e in entries]
            prices = self._latest_prices(codes)
            report = self.engine.residual_report(
                ruleset=ruleset,
                snapshot=PortfolioSnapshot.from_entries(positions, entries),
                exemptions=exemptions,
                valuation=prices,
            )
            return report.to_dict()

    def _latest_prices(self, codes: Sequence[str]) -> Dict[str, str]:
        prices: Dict[str, str] = {}
        for code in dict.fromkeys(codes):
            quote = None
            try:
                quote = self.quote_provider(code)
            except Exception:
                quote = None
            if quote and quote.get("last_price") is not None:
                prices[code] = str(quote["last_price"])
        return prices

    @staticmethod
    def _valuation_note(intent: OrderIntent) -> Dict[str, str]:
        return {
            "stock_code": intent.stock_code,
            "valuation_price": str(intent.valuation_price),
            "notional": str(intent.notional),
        }

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def list_decisions(
        self, order_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        return [
            d.to_dict()
            for d in self.store.list_decisions(
                order_id=order_id, portfolio_id=self.portfolio_id, limit=limit
            )
        ]

    def get_decision(self, decision_id: str) -> Dict[str, Any]:
        decision = self.store.get_decision(decision_id)
        if decision is None or decision.portfolio_id != self.portfolio_id:
            raise RiskStoreError(f"审计决策 {decision_id} 不存在")
        return decision.to_dict()

    def get_order_trail(self, order_id: str) -> Optional[Dict[str, Any]]:
        """订单台账 + 该订单全部决策，供审计按当时口径追溯。"""
        entry = self.store.get_order(order_id)
        if entry is None:
            return None
        return {
            "ledger": entry.to_dict(),
            "decisions": self.list_decisions(order_id=order_id, limit=100),
        }

    def list_ledger(self, include_closed: bool = False) -> List[Dict[str, Any]]:
        entries = self.store.all_orders(self.portfolio_id)
        if not include_closed:
            entries = [e for e in entries if e.status.is_open]
        return [e.to_dict() for e in entries]
