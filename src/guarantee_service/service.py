"""担保风控应用服务：在分录账簿之上实现业务操作、幂等重放与可解释结果。

核心约定：
- 每个写操作先检查幂等索引（request_id + 请求指纹），命中则重放原分录的结果，
  重试不会扩大敞口；
- “检查可用担保 + 追加冻结分录”在账簿锁内完成，并发下单不会透支；
- 所有响应都是分录的纯函数，重放结果与首次完全一致。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .ledger import Ledger
from .models import (
    ORDER_OPEN_STATUSES,
    ZERO,
    CreditLineState,
    MarginBatchState,
    mstr,
    q2,
    to_decimal,
)
from .risk import DEFAULT_RULE_ID, compute_requirements, default_rule

# 调整原因 -> 允许的金额方向（+1 必须为正，-1 必须为负，0 任意非零）
CREDIT_ADJUST_REASONS = {"topup": 1, "regulatory_cut": -1, "correction": 0}
BATCH_ADJUST_REASONS = {"topup": 1, "withdrawal": -1, "correction": 0}


class ServiceError(Exception):
    """可直接映射为 HTTP 响应的业务错误。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _invalid(message: str, details: dict | None = None) -> ServiceError:
    return ServiceError(400, "INVALID_PARAMS", message, details)


def _not_found(message: str) -> ServiceError:
    return ServiceError(404, "NOT_FOUND", message)


def _conflict(code: str, message: str, details: dict | None = None) -> ServiceError:
    return ServiceError(409, code, message, details)


def _reason(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "details": details or {}}


def _require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"字段 {field} 必须是非空字符串")
    return value.strip()


def _parse_amount(value: Any, field: str) -> Decimal:
    try:
        return to_decimal(value)
    except (ValueError, ArithmeticError):
        raise _invalid(f"字段 {field} 不是合法金额：{value!r}") from None


def _parse_quantity(value: Any) -> int:
    if isinstance(value, bool):
        raise _invalid("数量必须是正整数")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value <= 0:
        raise _invalid("数量必须是正整数")
    return value


def _fingerprint(fields: dict) -> str:
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GuaranteeService:
    """担保风控应用服务。"""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        with ledger.lock:
            if DEFAULT_RULE_ID not in ledger.rules:
                ledger.append("risk_rule_registered", default_rule().to_entry_data())

    # ------------------------------------------------------------------
    # 登记：信用额度 / 保证金批次 / 风控规则
    # ------------------------------------------------------------------

    def register_credit_line(self, payload: dict) -> dict:
        line_id = _require_str(payload, "line_id")
        owner_id = _require_str(payload, "owner_id")
        limit = _parse_amount(payload.get("limit"), "limit")
        if limit <= 0:
            raise _invalid("信用额度必须为正数")
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"credit_line_registered"})
            if replayed is not None:
                return replayed
            if line_id in self.ledger.credit_lines:
                raise _conflict("DUPLICATE_ID", f"信用额度 {line_id} 已存在")
            entry = self.ledger.append(
                "credit_line_registered",
                {"line_id": line_id, "owner_id": owner_id, "limit": mstr(limit)},
                request_id=request_id,
            )
            return self._outcome(entry)

    def adjust_credit_line(self, line_id: str, payload: dict) -> dict:
        """追加担保（topup）或监管降额（regulatory_cut）等额度调整。"""
        delta = _parse_amount(payload.get("delta"), "delta")
        reason = _require_str(payload, "reason")
        self._check_adjust_reason(reason, delta, CREDIT_ADJUST_REASONS)
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"credit_line_adjusted"})
            if replayed is not None:
                return replayed
            line = self.ledger.credit_lines.get(line_id)
            if line is None:
                raise _not_found(f"信用额度 {line_id} 未登记")
            new_limit = line.limit + delta
            if new_limit < 0:
                raise _invalid(
                    "调整后限额不能为负数",
                    {"limit": mstr(line.limit), "delta": mstr(delta)},
                )
            entry = self.ledger.append(
                "credit_line_adjusted",
                {
                    "line_id": line_id,
                    "delta": mstr(delta),
                    "reason": reason,
                    "limit_after": mstr(new_limit),
                    "frozen_after": mstr(line.frozen),
                    "breached": line.frozen > new_limit,
                },
                request_id=request_id,
            )
            return self._outcome(entry)

    def register_margin_batch(self, payload: dict) -> dict:
        batch_id = _require_str(payload, "batch_id")
        owner_id = _require_str(payload, "owner_id")
        amount = _parse_amount(payload.get("amount"), "amount")
        if amount <= 0:
            raise _invalid("保证金金额必须为正数")
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"margin_batch_registered"})
            if replayed is not None:
                return replayed
            if batch_id in self.ledger.margin_batches:
                raise _conflict("DUPLICATE_ID", f"保证金批次 {batch_id} 已存在")
            entry = self.ledger.append(
                "margin_batch_registered",
                {"batch_id": batch_id, "owner_id": owner_id, "amount": mstr(amount)},
                request_id=request_id,
            )
            return self._outcome(entry)

    def adjust_margin_batch(self, batch_id: str, payload: dict) -> dict:
        """追加保证金（topup）或提取可用部分（withdrawal）。"""
        delta = _parse_amount(payload.get("delta"), "delta")
        reason = _require_str(payload, "reason")
        self._check_adjust_reason(reason, delta, BATCH_ADJUST_REASONS)
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"margin_batch_adjusted"})
            if replayed is not None:
                return replayed
            batch = self.ledger.margin_batches.get(batch_id)
            if batch is None:
                raise _not_found(f"保证金批次 {batch_id} 未登记")
            new_amount = batch.amount + delta
            if new_amount < 0:
                raise _invalid("调整后批次余额不能为负数")
            if new_amount < batch.frozen:
                raise _conflict(
                    "INSUFFICIENT_AVAILABLE",
                    "提取后余额将低于已冻结部分，只能提取可用部分",
                    {"amount": mstr(batch.amount), "frozen": mstr(batch.frozen), "delta": mstr(delta)},
                )
            entry = self.ledger.append(
                "margin_batch_adjusted",
                {
                    "batch_id": batch_id,
                    "delta": mstr(delta),
                    "reason": reason,
                    "amount_after": mstr(new_amount),
                    "frozen_after": mstr(batch.frozen),
                },
                request_id=request_id,
            )
            return self._outcome(entry)

    def register_rule(self, payload: dict) -> dict:
        rule_id = _require_str(payload, "rule_id")
        margin_rate = _parse_amount(payload.get("margin_rate"), "margin_rate")
        exposure_rate = _parse_amount(payload.get("exposure_rate"), "exposure_rate")
        max_order_notional = _parse_amount(payload.get("max_order_notional"), "max_order_notional")
        utilization = _parse_amount(payload.get("max_seller_utilization"), "max_seller_utilization")
        if margin_rate < 0 or exposure_rate <= 0:
            raise _invalid("保证金率不能为负、敞口系数必须为正")
        if max_order_notional <= 0:
            raise _invalid("单笔订单上限必须为正数")
        if utilization <= 0 or utilization > 1:
            raise _invalid("卖方额度使用率上限必须在 (0, 1] 区间")
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"risk_rule_registered"})
            if replayed is not None:
                return replayed
            current = self.ledger.rules.get(rule_id)
            version = current.version + 1 if current else 1
            entry = self.ledger.append(
                "risk_rule_registered",
                {
                    "rule_id": rule_id,
                    "version": version,
                    "margin_rate": str(margin_rate),
                    "exposure_rate": str(exposure_rate),
                    "max_order_notional": str(max_order_notional),
                    "max_seller_utilization": str(utilization),
                },
                request_id=request_id,
            )
            return self._outcome(entry)

    # ------------------------------------------------------------------
    # 交易：订单受理 / 交割 / 违约 / 撤销
    # ------------------------------------------------------------------

    def accept_order(self, payload: dict) -> dict:
        """受理订单：计算敞口与保证金，冻结可用担保；拒绝时记录结构化原因。

        同一 request_id 的重试重放首次结果，不会重复冻结、不会扩大敞口。
        """
        request_id = _require_str(payload, "request_id")
        seller_id = _require_str(payload, "seller_id")
        buyer_id = _require_str(payload, "buyer_id")
        quantity = _parse_quantity(payload.get("quantity"))
        price = _parse_amount(payload.get("price"), "price")
        if price <= 0:
            raise _invalid("价格必须为正数")
        rule_id = payload.get("rule_id") or DEFAULT_RULE_ID
        client_order_id = payload.get("order_id")
        if client_order_id is not None and (not isinstance(client_order_id, str) or not client_order_id.strip()):
            raise _invalid("字段 order_id 必须是非空字符串")
        fingerprint = _fingerprint(
            {
                "seller_id": seller_id,
                "buyer_id": buyer_id,
                "quantity": quantity,
                "price": mstr(price),
                "rule_id": rule_id,
                "client_order_id": client_order_id or "",
            }
        )
        with self.ledger.lock:
            existing = self.ledger.find_request(request_id)
            if existing is not None:
                if existing.kind not in ("order_accepted", "order_rejected"):
                    raise _conflict("REQUEST_ID_CONFLICT", f"请求号 {request_id} 已用于 {existing.kind}")
                if existing.data.get("request_fingerprint") != fingerprint:
                    raise _conflict(
                        "REQUEST_ID_CONFLICT",
                        f"请求号 {request_id} 对应的报文与本次不一致，拒绝重放",
                    )
                return self._outcome(existing)

            order_id = client_order_id or f"ORD-{uuid4().hex[:12].upper()}"
            if order_id in self.ledger.orders:
                raise _conflict("DUPLICATE_ORDER_ID", f"订单编号 {order_id} 已存在")
            rule = self.ledger.rules.get(rule_id)
            if rule is None:
                raise _invalid(f"风控规则 {rule_id} 未登记", {"rule_id": rule_id})

            notional, exposure, margin_required = compute_requirements(rule, quantity, price)
            seller_lines = [l for l in self.ledger.credit_lines.values() if l.owner_id == seller_id]
            buyer_batches = [b for b in self.ledger.margin_batches.values() if b.owner_id == buyer_id]

            reasons: list[dict] = []
            if not seller_lines:
                reasons.append(_reason("NO_CREDIT_LINE", f"卖方 {seller_id} 未登记任何信用额度"))
            if not buyer_batches:
                reasons.append(_reason("NO_MARGIN_BATCH", f"买方 {buyer_id} 未登记任何保证金批次"))
            if notional > rule.max_order_notional:
                reasons.append(
                    _reason(
                        "ORDER_NOTIONAL_EXCEEDS_LIMIT",
                        f"单笔订单名义金额 {mstr(notional)} 超过风控上限 {mstr(rule.max_order_notional)}",
                        {"notional": mstr(notional), "max_order_notional": mstr(rule.max_order_notional)},
                    )
                )
            breached = [l for l in seller_lines if l.breached]
            if breached:
                reasons.append(
                    _reason(
                        "CREDIT_LINE_BREACHED",
                        "卖方信用额度已被监管降额击穿，须先追加担保或等待交割释放",
                        {
                            "lines": [
                                {"line_id": l.line_id, "limit": mstr(l.limit), "frozen": mstr(l.frozen)}
                                for l in breached
                            ]
                        },
                    )
                )
            capacities = {
                l.line_id: max(ZERO, q2(l.limit * rule.max_seller_utilization) - l.frozen)
                for l in seller_lines
            }
            credit_available = sum(capacities.values(), ZERO)
            if seller_lines and credit_available < exposure:
                reasons.append(
                    _reason(
                        "INSUFFICIENT_CREDIT",
                        f"卖方可用信用额度不足：需要 {mstr(exposure)}，可用 {mstr(credit_available)}",
                        {
                            "required": mstr(exposure),
                            "available": mstr(credit_available),
                            "shortfall": mstr(exposure - credit_available),
                        },
                    )
                )
            margin_available = sum((b.available for b in buyer_batches), ZERO)
            if buyer_batches and margin_available < margin_required:
                reasons.append(
                    _reason(
                        "INSUFFICIENT_MARGIN",
                        f"买方可用保证金不足：需要 {mstr(margin_required)}，可用 {mstr(margin_available)}",
                        {
                            "required": mstr(margin_required),
                            "available": mstr(margin_available),
                            "shortfall": mstr(margin_required - margin_available),
                        },
                    )
                )

            base_data = {
                "order_id": order_id,
                "seller_id": seller_id,
                "buyer_id": buyer_id,
                "quantity": quantity,
                "price": mstr(price),
                "notional": mstr(notional),
                "exposure": mstr(exposure),
                "margin_required": mstr(margin_required),
                "rule": rule.snapshot(),
                "request_fingerprint": fingerprint,
            }
            if reasons:
                entry = self.ledger.append(
                    "order_rejected", {**base_data, "reasons": reasons}, request_id=request_id
                )
                return self._outcome(entry)

            allocations: list[dict] = []
            remaining = exposure
            for line in seller_lines:
                if remaining <= 0:
                    break
                take = min(remaining, capacities[line.line_id])
                if take > 0:
                    allocations.append(
                        {"source_kind": "credit_line", "source_id": line.line_id, "amount": mstr(take)}
                    )
                    remaining -= take
            remaining = margin_required
            for batch in buyer_batches:
                if remaining <= 0:
                    break
                take = min(remaining, batch.available)
                if take > 0:
                    allocations.append(
                        {"source_kind": "margin_batch", "source_id": batch.batch_id, "amount": mstr(take)}
                    )
                    remaining -= take
            entry = self.ledger.append(
                "order_accepted", {**base_data, "allocations": allocations}, request_id=request_id
            )
            return self._outcome(entry)

    def record_delivery(self, order_id: str, payload: dict) -> dict:
        """按交割进度释放担保：累计交割占比决定累计释放，收官时释放全部剩余。"""
        quantity = _parse_quantity(payload.get("quantity"))
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"delivery_recorded"}, order_id=order_id)
            if replayed is not None:
                return replayed
            order = self._open_order(order_id)
            if quantity > order.outstanding_qty:
                raise _invalid(
                    f"交割数量 {quantity} 超过未交割余额 {order.outstanding_qty}",
                    {"outstanding": order.outstanding_qty},
                )
            delivered_new = order.delivered + quantity
            closing = delivered_new + order.defaulted == order.quantity
            releases = []
            for alloc in order.allocations:
                if closing:
                    delta = alloc.outstanding
                else:
                    target = q2(alloc.amount * delivered_new / order.quantity)
                    delta = max(ZERO, target - alloc.released)
                if delta > 0:
                    releases.append(
                        {"source_kind": alloc.source_kind, "source_id": alloc.source_id, "amount": mstr(delta)}
                    )
            status_after = "settled" if closing else "delivering"
            entry = self.ledger.append(
                "delivery_recorded",
                {
                    "order_id": order_id,
                    "quantity": quantity,
                    "delivered_total": delivered_new,
                    "outstanding_after": order.quantity - delivered_new - order.defaulted,
                    "status_after": status_after,
                    "releases": releases,
                    "released_total": mstr(sum((to_decimal(r["amount"]) for r in releases), ZERO)),
                },
                request_id=request_id,
            )
            return self._outcome(entry)

    def record_default(self, order_id: str, payload: dict) -> dict:
        """登记部分违约：按违约数量占订单比例罚没冻结担保，剩余部分继续履约。"""
        quantity = _parse_quantity(payload.get("quantity"))
        reason_text = payload.get("reason")
        if reason_text is not None and not isinstance(reason_text, str):
            raise _invalid("字段 reason 必须是字符串")
        request_id = payload.get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"default_recorded"}, order_id=order_id)
            if replayed is not None:
                return replayed
            order = self._open_order(order_id)
            if quantity > order.outstanding_qty:
                raise _invalid(
                    f"违约数量 {quantity} 超过未交割余额 {order.outstanding_qty}",
                    {"outstanding": order.outstanding_qty},
                )
            defaulted_new = order.defaulted + quantity
            closing = order.delivered + defaulted_new == order.quantity
            seizures = []
            for alloc in order.allocations:
                if closing:
                    delta = alloc.outstanding
                else:
                    target = q2(alloc.amount * defaulted_new / order.quantity)
                    delta = max(ZERO, target - alloc.seized)
                if delta > 0:
                    seizures.append(
                        {"source_kind": alloc.source_kind, "source_id": alloc.source_id, "amount": mstr(delta)}
                    )
            status_after = "defaulted" if closing else order.status
            entry = self.ledger.append(
                "default_recorded",
                {
                    "order_id": order_id,
                    "quantity": quantity,
                    "defaulted_total": defaulted_new,
                    "outstanding_after": order.quantity - order.delivered - defaulted_new,
                    "status_after": status_after,
                    "reason": reason_text or "履约失败",
                    "seizures": seizures,
                    "seized_total": mstr(sum((to_decimal(s["amount"]) for s in seizures), ZERO)),
                },
                request_id=request_id,
            )
            return self._outcome(entry)

    def cancel_order(self, order_id: str, payload: dict) -> dict:
        """撤销订单未履约部分，释放全部剩余冻结。"""
        request_id = (payload or {}).get("request_id")
        with self.ledger.lock:
            replayed = self._replay(request_id, {"order_cancelled"}, order_id=order_id)
            if replayed is not None:
                return replayed
            order = self._open_order(order_id)
            releases = [
                {"source_kind": a.source_kind, "source_id": a.source_id, "amount": mstr(a.outstanding)}
                for a in order.allocations
                if a.outstanding > 0
            ]
            entry = self.ledger.append(
                "order_cancelled",
                {
                    "order_id": order_id,
                    "status_after": "cancelled",
                    "outstanding_after": 0,
                    "releases": releases,
                    "released_total": mstr(sum((to_decimal(r["amount"]) for r in releases), ZERO)),
                },
                request_id=request_id,
            )
            return self._outcome(entry)

    # ------------------------------------------------------------------
    # 查询与解释
    # ------------------------------------------------------------------

    def explain_order(self, order_id: str) -> dict:
        """解释订单：占用哪些额度、各释放/罚没多少、拒绝原因、关联分录。"""
        with self.ledger.lock:
            order = self.ledger.orders.get(order_id)
            if order is None:
                raise _not_found(f"订单 {order_id} 不存在")
            result = order.to_dict()
            result["events"] = [
                {"seq": e.seq, "entry_id": e.entry_id, "kind": e.kind, "ts": e.ts}
                for e in self.ledger.entries_for_order(order_id)
            ]
            if order.status == "rejected":
                result["explanation"] = "；".join(r["message"] for r in order.reject_reasons)
            else:
                result["explanation"] = self._allocation_explanation(result)
            return result

    def credit_line_view(self, line_id: str) -> dict:
        with self.ledger.lock:
            line = self.ledger.credit_lines.get(line_id)
            if line is None:
                raise _not_found(f"信用额度 {line_id} 未登记")
            return self._credit_line_view(line)

    def margin_batch_view(self, batch_id: str) -> dict:
        with self.ledger.lock:
            batch = self.ledger.margin_batches.get(batch_id)
            if batch is None:
                raise _not_found(f"保证金批次 {batch_id} 未登记")
            return self._margin_batch_view(batch)

    def list_orders(self, status: str | None = None, owner_id: str | None = None) -> list[dict]:
        with self.ledger.lock:
            orders = []
            for order in self.ledger.orders.values():
                if status and order.status != status:
                    continue
                if owner_id and owner_id not in (order.seller_id, order.buyer_id):
                    continue
                orders.append(
                    {
                        "order_id": order.order_id,
                        "status": order.status,
                        "seller_id": order.seller_id,
                        "buyer_id": order.buyer_id,
                        "notional": mstr(order.notional),
                        "frozen_outstanding": mstr(order.frozen_outstanding),
                    }
                )
            return orders

    def list_rules(self) -> list[dict]:
        with self.ledger.lock:
            return [rule.snapshot() for rule in self.ledger.rules.values()]

    def account_summary(self, owner_id: str) -> dict:
        with self.ledger.lock:
            lines = [l for l in self.ledger.credit_lines.values() if l.owner_id == owner_id]
            batches = [b for b in self.ledger.margin_batches.values() if b.owner_id == owner_id]
            open_orders = [
                o
                for o in self.ledger.orders.values()
                if o.status in ORDER_OPEN_STATUSES and owner_id in (o.seller_id, o.buyer_id)
            ]
            if not lines and not batches and not open_orders:
                raise _not_found(f"主体 {owner_id} 未登记任何担保账户")
            return {
                "owner_id": owner_id,
                "credit_lines": [self._credit_line_view(l) for l in lines],
                "margin_batches": [self._margin_batch_view(b) for b in batches],
                "open_orders": [
                    {
                        "order_id": o.order_id,
                        "role": "seller" if o.seller_id == owner_id else "buyer",
                        "status": o.status,
                        "frozen_outstanding": mstr(o.frozen_outstanding),
                        "outstanding_quantity": o.outstanding_qty,
                    }
                    for o in open_orders
                ],
                "totals": {
                    "credit_limit": mstr(sum((l.limit for l in lines), ZERO)),
                    "credit_frozen": mstr(sum((l.frozen for l in lines), ZERO)),
                    "credit_available": mstr(sum((l.available for l in lines), ZERO)),
                    "margin_amount": mstr(sum((b.amount for b in batches), ZERO)),
                    "margin_frozen": mstr(sum((b.frozen for b in batches), ZERO)),
                    "margin_available": mstr(sum((b.available for b in batches), ZERO)),
                },
            }

    def list_entries(self, after_seq: int = 0, limit: int = 100) -> list[dict]:
        with self.ledger.lock:
            return [
                e.to_dict()
                for e in self.ledger.entries()
                if e.seq > after_seq
            ][: max(1, min(limit, 1000))]

    # ------------------------------------------------------------------
    # 内部：幂等重放、结果构造、视图
    # ------------------------------------------------------------------

    def _replay(self, request_id: Any, kinds: set[str], order_id: str | None = None) -> dict | None:
        """request_id 命中幂等索引时重放原分录结果；类型或订单不符则报冲突。"""
        if request_id is None:
            return None
        if not isinstance(request_id, str) or not request_id.strip():
            raise _invalid("字段 request_id 必须是非空字符串")
        existing = self.ledger.find_request(request_id.strip())
        if existing is None:
            return None
        if existing.kind not in kinds:
            raise _conflict("REQUEST_ID_CONFLICT", f"请求号 {request_id} 已用于 {existing.kind}")
        if order_id is not None and existing.data.get("order_id") != order_id:
            raise _conflict("REQUEST_ID_CONFLICT", f"请求号 {request_id} 已用于其他订单")
        return self._outcome(existing)

    def _open_order(self, order_id: str):
        order = self.ledger.orders.get(order_id)
        if order is None:
            raise _not_found(f"订单 {order_id} 不存在")
        if order.status not in ORDER_OPEN_STATUSES:
            raise _conflict("ORDER_CLOSED", f"订单 {order_id} 已终结（{order.status}），不能继续操作")
        return order

    @staticmethod
    def _check_adjust_reason(reason: str, delta: Decimal, allowed: dict[str, int]) -> None:
        if reason not in allowed:
            raise _invalid(f"调整原因必须是：{'、'.join(sorted(allowed))}")
        sign = allowed[reason]
        if delta == 0:
            raise _invalid("调整金额不能为零")
        if sign > 0 and delta < 0:
            raise _invalid(f"调整原因 {reason} 要求金额为正")
        if sign < 0 and delta > 0:
            raise _invalid(f"调整原因 {reason} 要求金额为负")

    def _credit_line_view(self, line: CreditLineState) -> dict:
        frozen_by = []
        for order in self.ledger.orders.values():
            if order.status not in ORDER_OPEN_STATUSES:
                continue
            for alloc in order.allocations:
                if alloc.source_kind == "credit_line" and alloc.source_id == line.line_id and alloc.outstanding > 0:
                    frozen_by.append({"order_id": order.order_id, "outstanding": mstr(alloc.outstanding)})
        return {
            "line_id": line.line_id,
            "owner_id": line.owner_id,
            "limit": mstr(line.limit),
            "frozen": mstr(line.frozen),
            "available": mstr(line.available),
            "breached": line.breached,
            "breach_amount": mstr(line.frozen - line.limit) if line.breached else "0.00",
            "seized_total": mstr(line.seized_total),
            "frozen_by": frozen_by,
        }

    def _margin_batch_view(self, batch: MarginBatchState) -> dict:
        frozen_by = []
        for order in self.ledger.orders.values():
            if order.status not in ORDER_OPEN_STATUSES:
                continue
            for alloc in order.allocations:
                if alloc.source_kind == "margin_batch" and alloc.source_id == batch.batch_id and alloc.outstanding > 0:
                    frozen_by.append({"order_id": order.order_id, "outstanding": mstr(alloc.outstanding)})
        return {
            "batch_id": batch.batch_id,
            "owner_id": batch.owner_id,
            "amount": mstr(batch.amount),
            "frozen": mstr(batch.frozen),
            "available": mstr(batch.available),
            "seized_total": mstr(batch.seized_total),
            "frozen_by": frozen_by,
        }

    @staticmethod
    def _allocation_explanation(order_dict: dict) -> str:
        credits = [a for a in order_dict["allocations"] if a["source_kind"] == "credit_line"]
        margins = [a for a in order_dict["allocations"] if a["source_kind"] == "margin_batch"]
        credit_part = "、".join(f"{a['source_id']}（{a['amount']}）" for a in credits) or "无"
        margin_part = "、".join(f"{a['source_id']}（{a['amount']}）" for a in margins) or "无"
        return (
            f"订单 {order_dict['order_id']} 名义金额 {order_dict['notional']}："
            f"卖方敞口 {order_dict['exposure']} 占用信用额度 {credit_part}；"
            f"买方保证金 {order_dict['margin_required']} 占用批次 {margin_part}"
        )

    def _outcome(self, entry) -> dict:
        """把分录转换为接口响应；同一分录永远得到同一响应。"""
        kind = entry.kind
        d = entry.data
        if kind == "credit_line_registered":
            return {
                "line_id": d["line_id"],
                "owner_id": d["owner_id"],
                "limit": d["limit"],
                "frozen": "0.00",
                "available": d["limit"],
                "breached": False,
                "breach_amount": "0.00",
                "seized_total": "0.00",
                "frozen_by": [],
                "entry_id": entry.entry_id,
            }
        if kind == "credit_line_adjusted":
            limit = to_decimal(d["limit_after"])
            frozen = to_decimal(d["frozen_after"])
            return {
                "line_id": d["line_id"],
                "delta": d["delta"],
                "reason": d["reason"],
                "limit": d["limit_after"],
                "frozen": d["frozen_after"],
                "available": mstr(limit - frozen),
                "breached": d["breached"],
                "breach_amount": mstr(frozen - limit) if d["breached"] else "0.00",
                "entry_id": entry.entry_id,
            }
        if kind == "margin_batch_registered":
            return {
                "batch_id": d["batch_id"],
                "owner_id": d["owner_id"],
                "amount": d["amount"],
                "frozen": "0.00",
                "available": d["amount"],
                "seized_total": "0.00",
                "frozen_by": [],
                "entry_id": entry.entry_id,
            }
        if kind == "margin_batch_adjusted":
            amount = to_decimal(d["amount_after"])
            frozen = to_decimal(d["frozen_after"])
            return {
                "batch_id": d["batch_id"],
                "delta": d["delta"],
                "reason": d["reason"],
                "amount": d["amount_after"],
                "frozen": d["frozen_after"],
                "available": mstr(amount - frozen),
                "entry_id": entry.entry_id,
            }
        if kind == "risk_rule_registered":
            return {"rule": d, "entry_id": entry.entry_id}
        if kind == "order_accepted":
            order_dict = {
                "order_id": d["order_id"],
                "allocations": d["allocations"],
                "notional": d["notional"],
                "exposure": d["exposure"],
                "margin_required": d["margin_required"],
            }
            return {
                "order_id": d["order_id"],
                "request_id": entry.request_id,
                "status": "accepted",
                "seller_id": d["seller_id"],
                "buyer_id": d["buyer_id"],
                "quantity": d["quantity"],
                "price": d["price"],
                "notional": d["notional"],
                "exposure": d["exposure"],
                "margin_required": d["margin_required"],
                "rule": d["rule"],
                "allocations": d["allocations"],
                "explanation": self._allocation_explanation(order_dict),
                "entry_id": entry.entry_id,
            }
        if kind == "order_rejected":
            return {
                "order_id": d["order_id"],
                "request_id": entry.request_id,
                "status": "rejected",
                "seller_id": d["seller_id"],
                "buyer_id": d["buyer_id"],
                "quantity": d["quantity"],
                "price": d["price"],
                "notional": d["notional"],
                "rule": d["rule"],
                "reasons": d["reasons"],
                "explanation": "；".join(r["message"] for r in d["reasons"]),
                "entry_id": entry.entry_id,
            }
        if kind == "delivery_recorded":
            return {
                "order_id": d["order_id"],
                "status": d["status_after"],
                "delivered_total": d["delivered_total"],
                "outstanding_quantity": d["outstanding_after"],
                "releases": d["releases"],
                "released_total": d["released_total"],
                "explanation": f"交割 {d['quantity']}，释放担保 {d['released_total']}，剩余未交割 {d['outstanding_after']}",
                "entry_id": entry.entry_id,
            }
        if kind == "default_recorded":
            return {
                "order_id": d["order_id"],
                "status": d["status_after"],
                "defaulted_total": d["defaulted_total"],
                "outstanding_quantity": d["outstanding_after"],
                "reason": d["reason"],
                "seizures": d["seizures"],
                "seized_total": d["seized_total"],
                "explanation": f"违约 {d['quantity']}，罚没担保 {d['seized_total']}，剩余未交割 {d['outstanding_after']}",
                "entry_id": entry.entry_id,
            }
        if kind == "order_cancelled":
            return {
                "order_id": d["order_id"],
                "status": d["status_after"],
                "releases": d["releases"],
                "released_total": d["released_total"],
                "explanation": f"订单撤销，释放剩余担保 {d['released_total']}",
                "entry_id": entry.entry_id,
            }
        raise ValueError(f"未知分录类型：{kind}")
