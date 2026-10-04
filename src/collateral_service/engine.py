"""担保风控命令引擎。

所有写命令遵循同一流程：

1. ``BEGIN IMMEDIATE`` 取得写锁（并发下单在此串行）；
2. 命中幂等键 → 校验请求指纹一致后直接返回既有结果，**不写任何分录**；
3. 在事务内基于已提交状态做规则评估；
4. 评估结论与全部占用/释放明细在同一事务落账（受理 = submitted+accepted，
   拒绝 = submitted+rejected，原子可见）；
5. 提交成功后才把新分录应用进内存状态；事务回滚则内存状态毫发无损。

因此：重试绝不产生第二条冻结分录，敞口不会被重试放大。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from typing import Any

from .models import (
    D,
    DEFAULT_LINE,
    DomainError,
    Entry,
    EntryType,
    ErrorCode,
    Order,
    OrderStatus,
    PartyRole,
    ZERO,
    money,
)
from .rules import Proposal, evaluate, prorate_releases
from .state import LedgerState
from .store import EntryStore


class RiskConfig:
    def __init__(self, max_order_notional: str | Decimal = "10000000.00") -> None:
        self.max_order_notional = D(max_order_notional)


def _fingerprint(body: dict[str, Any]) -> str:
    canon = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class CollateralEngine:
    def __init__(self, store: EntryStore, config: RiskConfig | None = None) -> None:
        self.store = store
        self.config = config or RiskConfig()
        self.state = LedgerState()
        self.state.replay(store)

    # ================================================================ 主体登记

    def register_party(self, *, party_id: str, name: str, role: str,
                       idem_key: str) -> dict[str, Any]:
        body = {"party_id": party_id, "name": name, "role": role}
        def write(conn):
            if self.state.party(party_id) is not None:
                raise DomainError(ErrorCode.CONFLICT, 409, f"主体 {party_id} 已登记")
            try:
                prole = PartyRole(role)
            except ValueError:
                raise DomainError(ErrorCode.VALIDATION, 400,
                                  f"角色必须是 {[r.value for r in PartyRole]}")
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.PARTY_REGISTERED, actor=party_id,
                payload={"party_id": party_id, "name": name, "role": prole.value,
                         "fp": _fingerprint(body)},
                idem_key=idem_key)]
        self._commit(idem_key, body, write)
        return self.party_view(party_id)

    # ================================================================ 卖方额度

    def grant_credit(self, *, seller_id: str, amount: str, idem_key: str) -> dict[str, Any]:
        """授予/追加卖方授信总额（amount 为正数）。"""
        amt = _positive(amount, "授信金额")
        body = {"seller_id": seller_id, "amount": str(amt)}

        def write(conn):
            self._require_party(seller_id, PartyRole.SELLER)
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.CREDIT_GRANTED, actor=seller_id,
                payload={"seller_id": seller_id, "amount": str(amt),
                         "fp": _fingerprint(body)},
                idem_key=idem_key)]

        self._commit(idem_key, body, write)
        return self.credit_view(seller_id)

    def register_credit_line(self, *, seller_id: str, line_id: str, amount: str,
                             idem_key: str) -> dict[str, Any]:
        """登记/追加一条指定额度行。"""
        amt = _positive(amount, "额度行金额")
        body = {"seller_id": seller_id, "line_id": line_id, "amount": str(amt)}

        def write(conn):
            self._require_party(seller_id, PartyRole.SELLER)
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.CREDIT_LINE_GRANTED, actor=seller_id,
                payload={"seller_id": seller_id, "line_id": line_id,
                         "amount": str(amt), "fp": _fingerprint(body)},
                idem_key=idem_key)]

        self._commit(idem_key, body, write)
        return self.credit_view(seller_id)

    def set_credit_blocked(self, *, seller_id: str, blocked: bool,
                           reason: str, idem_key: str) -> dict[str, Any]:
        """监管冻结/解冻卖方信用账户。"""
        body = {"seller_id": seller_id, "blocked": blocked, "reason": reason}

        def write(conn):
            acc = self._require_credit(seller_id)
            if acc.blocked == blocked:
                raise DomainError(
                    ErrorCode.CONFLICT, 409,
                    f"账户已是{'冻结' if blocked else '正常'}状态，无需重复操作")
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.CREDIT_BLOCKED, actor="regulator",
                payload={"seller_id": seller_id, "blocked": blocked,
                         "reason": reason, "fp": _fingerprint(body)},
                idem_key=idem_key)]

        self._commit(idem_key, body, write)
        return self.credit_view(seller_id)

    def reduce_credit_limit(self, *, seller_id: str, reduction: str,
                            line_reductions: dict[str, str] | None = None,
                            reason: str, idem_key: str) -> dict[str, Any]:
        """监管降额（reduction 为正数，表示核减总额度）。

        历史冻结不可变：降额后占用超过新限额时账户标记 distressed，
        由监管/清算视图跟进，本方法不回改任何历史分录。
        """
        red = _positive(reduction, "降额幅度")
        line_deltas = {lid: D(v) for lid, v in (line_reductions or {}).items()}
        for v in line_deltas.values():
            if v < ZERO:
                raise DomainError(ErrorCode.VALIDATION, 400, "额度行调减金额必须为正")
        body = {"seller_id": seller_id, "reduction": str(red),
                "line_reductions": {k: str(v) for k, v in line_deltas.items()},
                "reason": reason}

        def write(conn):
            acc = self._require_credit(seller_id)
            if acc.total_limit - red < ZERO:
                raise DomainError(
                    ErrorCode.VALIDATION, 400,
                    f"降额后总额度为负：限额 {acc.total_limit}，申请核减 {red}")
            for lid, d in line_deltas.items():
                line = acc.lines.get(lid)
                if line is None:
                    raise DomainError(ErrorCode.NOT_FOUND, 404,
                                      f"额度行 {lid} 不存在")
                if line.limit - d < ZERO:
                    raise DomainError(
                        ErrorCode.VALIDATION, 400,
                        f"额度行 {lid} 降额后为负：限额 {line.limit}，核减 {d}")
            if sum(line_deltas.values(), ZERO) > red:
                raise DomainError(
                    ErrorCode.VALIDATION, 400,
                    "各额度行核减之和不能大于总额度核减幅度")
            # 未指明行的核减全部落在默认额度行，必须保证其不为负
            default_line = acc.lines.get(DEFAULT_LINE)
            residual = red - sum(line_deltas.values(), ZERO)
            if default_line is not None and default_line.limit - residual < ZERO:
                raise DomainError(
                    ErrorCode.VALIDATION, 400,
                    f"默认额度行余额 {default_line.limit} 不足以承受剩余核减 {residual}")
            lines_payload = [{"line_id": lid, "amount": str(-d)}
                             for lid, d in line_deltas.items()]
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.CREDIT_ADJUSTED, actor="regulator",
                payload={"seller_id": seller_id, "amount": str(-red),
                         "lines": lines_payload, "reason": reason,
                         "fp": _fingerprint(body)},
                idem_key=idem_key)]

        self._commit(idem_key, body, write)
        return self.credit_view(seller_id)

    # ================================================================ 买方保证金

    def deposit_margin(self, *, buyer_id: str, amount: str,
                       batch_id: str | None = None, idem_key: str) -> dict[str, Any]:
        """存入保证金批次；追加担保即新增一个独立批次。"""
        amt = _positive(amount, "保证金金额")
        batch_id = batch_id or _new_id("MB")
        body = {"buyer_id": buyer_id, "amount": str(amt), "batch_id": batch_id}

        def write(conn):
            self._require_party(buyer_id, PartyRole.BUYER)
            if batch_id in self.state.margin_batches:
                raise DomainError(ErrorCode.CONFLICT, 409,
                                  f"保证金批次 {batch_id} 已存在")
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.MARGIN_DEPOSITED, actor=buyer_id,
                payload={"buyer_id": buyer_id, "batch_id": batch_id,
                         "amount": str(amt), "fp": _fingerprint(body)},
                idem_key=idem_key)]

        self._commit(idem_key, body, write)
        return self.margin_view(buyer_id)

    # ================================================================ 订单受理

    def submit_order(self, *, seller_id: str, buyer_id: str, quantity: str,
                     unit_price: str, margin_rate: str,
                     client_token: str, preferred_lines: list[str] | None = None,
                     order_id: str | None = None, actor: str | None = None) -> dict[str, Any]:
        """受理订单：事务内评估并原子冻结担保；拒绝则只留拒绝分录。

        ``client_token`` 同时承担幂等键：同一 token 重试返回首张订单，
        不会第二次评估、不会产生第二组冻结。
        """
        qty = _positive(quantity, "数量")
        price = _nonnegative(unit_price, "单价")
        if price == ZERO:
            raise DomainError(ErrorCode.VALIDATION, 400, "单价必须为正")
        rate = D(margin_rate)
        if not (ZERO < rate <= Decimal("1")):
            raise DomainError(ErrorCode.VALIDATION, 400,
                              "保证金比例必须在 (0, 1] 区间")
        order_id = order_id or _new_id("ORD")
        # 指纹只覆盖客户端输入；order_id 可由服务端生成，否则同一请求重试
        # 会因随机 ID 不同被误判为指纹冲突
        fp_body = {"seller_id": seller_id, "buyer_id": buyer_id,
                   "quantity": str(qty), "unit_price": str(price),
                   "margin_rate": str(rate),
                   "preferred_lines": preferred_lines or []}
        actor = actor or buyer_id

        def write(conn):
            if self.state.order(order_id) is not None:
                raise DomainError(ErrorCode.CONFLICT, 409,
                                  f"订单 {order_id} 已存在")

            prop = Proposal(
                client_token=client_token, seller_id=seller_id, buyer_id=buyer_id,
                quantity=qty, unit_price=price, margin_rate=rate,
                order_id=order_id, preferred_lines=tuple(preferred_lines or ()))
            ev = evaluate(self.state, prop,
                          max_order_notional=self.config.max_order_notional,
                          alloc_id_prefix=order_id)

            common = {
                "order_id": order_id, "client_token": client_token,
                "seller_id": seller_id, "buyer_id": buyer_id,
                "quantity": str(qty), "unit_price": str(price),
                "margin_rate": str(rate),
                "notional": str(ev["notional"]),
                "required_margin": str(ev["required_margin"]),
                "preferred_lines": preferred_lines or [],
                "fp": _fingerprint(fp_body),
            }
            entries = [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.ORDER_SUBMITTED, actor=actor,
                payload=common, idem_key=client_token, order_id=order_id)]

            if ev["approved"]:
                allocs = [
                    {"allocation_id": aid, "source_type": "credit_line",
                     "source_id": lid, "amount": str(amt)}
                    for aid, lid, amt in ev["credit_plan"]
                ] + [
                    {"allocation_id": aid, "source_type": "margin_batch",
                     "source_id": bid, "amount": str(amt)}
                    for aid, bid, amt in ev["margin_plan"]
                ]
                entries.append(self.store.insert(
                    conn, entry_id=_new_id("E"),
                    entry_type=EntryType.ORDER_ACCEPTED, actor=actor,
                    payload={"order_id": order_id, "allocations": allocs},
                    order_id=order_id))
            else:
                entries.append(self.store.insert(
                    conn, entry_id=_new_id("E"),
                    entry_type=EntryType.ORDER_REJECTED, actor=actor,
                    payload={"order_id": order_id,
                             "reasons": list(ev["reject_reasons"])},
                    order_id=order_id))
            return entries

        entries, replay = self._commit(client_token, fp_body, write)
        if replay is not None:
            # 幂等命中：既有订单原样返回，不产生新分录、不扩大敞口
            view = self.order_view(replay.payload["order_id"])
            view["idempotent_replay"] = True
            return view

        view = self.order_view(order_id)
        view["risk_evaluation"] = self._last_evaluation(view)
        return view

    # ================================================================ 预检

    def preview_order(self, *, seller_id: str, buyer_id: str, quantity: str,
                      unit_price: str, margin_rate: str,
                      preferred_lines: list[str] | None = None) -> dict[str, Any]:
        """不落账的受理预检：返回逐规则结论、拟占用明细与拒绝原因。"""
        qty = _positive(quantity, "数量")
        price = _positive(unit_price, "单价")
        rate = D(margin_rate)
        if not (ZERO < rate <= Decimal("1")):
            raise DomainError(ErrorCode.VALIDATION, 400,
                              "保证金比例必须在 (0, 1] 区间")
        with self.store.lock:
            ev = evaluate(
                self.state,
                Proposal(client_token="(preview)", seller_id=seller_id,
                         buyer_id=buyer_id, quantity=qty, unit_price=price,
                         margin_rate=rate,
                         preferred_lines=tuple(preferred_lines or ())),
                max_order_notional=self.config.max_order_notional,
                alloc_id_prefix="(preview)")
        return {
            "approved": ev["approved"],
            "notional": money(ev["notional"]),
            "required_margin": money(ev["required_margin"]),
            "rule_results": [r.to_dict() for r in ev["rules"]],
            "planned_credit": [
                {"allocation_id": aid, "line_id": lid, "amount": money(amt)}
                for aid, lid, amt in ev["credit_plan"]],
            "planned_margin": [
                {"allocation_id": aid, "batch_id": bid, "amount": money(amt)}
                for aid, bid, amt in ev["margin_plan"]],
            "reject_reasons": list(ev["reject_reasons"]),
        }

    # ================================================================ 交割/违约

    def settle_delivery(self, *, order_id: str, delivered_qty: str,
                        defaulted_qty: str = "0", idem_key: str,
                        actor: str | None = None) -> dict[str, Any]:
        """按交割进度释放担保，违约部分罚没。同一事务出明细分录。"""
        dq = _nonnegative(delivered_qty, "交割数量")
        fq = _nonnegative(defaulted_qty, "违约数量")
        if dq + fq == ZERO:
            raise DomainError(ErrorCode.VALIDATION, 400, "交割与违约数量不能同时为 0")
        body = {"order_id": order_id, "delivered_qty": str(dq),
                "defaulted_qty": str(fq)}
        actor = actor or "operator"

        def write(conn):
            order = self._require_open_order(order_id)
            event_qty = dq + fq
            new_settled = order.settled_qty + event_qty
            if new_settled > order.quantity + Decimal("0.000001"):
                raise DomainError(
                    ErrorCode.VALIDATION, 400,
                    f"累计交割/违约数量超过订单数量：已处理 {order.settled_qty}，"
                    f"本次 {event_qty}，订单总量 {order.quantity}")

            releases = prorate_releases(order, dq, fq)
            payload = {"order_id": order_id,
                       "delivered_qty": str(dq), "defaulted_qty": str(fq),
                       "releases": [
                           {"allocation_id": aid, "released": str(rel),
                            "forfeited": str(forf)}
                           for aid, rel, forf in releases],
                       "fp": _fingerprint(body)}
            entries = [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.DELIVERY_SETTLED, actor=actor,
                payload=payload, idem_key=idem_key, order_id=order_id)]
            # 数量结清：结案分录落在同一事务，释放与结案原子可见
            if new_settled >= order.quantity - Decimal("0.000001"):
                entries.append(self.store.insert(
                    conn, entry_id=_new_id("E"),
                    entry_type=EntryType.ORDER_CLOSED, actor=actor,
                    payload={"order_id": order_id, "releases": [],
                             "reason": "delivery_complete"},
                    order_id=order_id))
            return entries

        self._commit(idem_key, body, write)
        # 幂等重放（retry）时订单视图与首次完全一致，不新增任何分录
        return self.order_view(order_id)

    def cancel_order(self, *, order_id: str, reason: str, idem_key: str,
                     actor: str | None = None) -> dict[str, Any]:
        """撤单：未释放的冻结担保全额退回（不计违约）。"""
        body = {"order_id": order_id, "reason": reason}
        actor = actor or "operator"

        def write(conn):
            order = self._require_open_order(order_id)
            releases = [
                {"allocation_id": a.alloc_id,
                 "released": str(a.outstanding), "forfeited": "0.00"}
                for a in order.allocations.values() if a.outstanding > ZERO
            ]
            return [self.store.insert(
                conn, entry_id=_new_id("E"),
                entry_type=EntryType.ORDER_CANCELLED, actor=actor,
                payload={"order_id": order_id, "reason": reason,
                         "releases": releases, "fp": _fingerprint(body)},
                idem_key=idem_key, order_id=order_id)]

        self._commit(idem_key, body, write)
        # 幂等重放（retry）时订单视图与首次完全一致，不新增任何分录
        return self.order_view(order_id)

    # ================================================================ 查询视图

    def order_view(self, order_id: str) -> dict[str, Any]:
        order = self.state.order(order_id)
        if order is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404, f"订单 {order_id} 不存在")
        acc = self.state.credit(order.seller_id)
        batch_index = self.state.margin_batches
        credit_lines, margin_used = [], []
        for a in order.allocations.values():
            if a.source_type == "credit_line":
                line = acc.lines.get(a.source_id) if acc else None
                credit_lines.append({
                    "allocation_id": a.alloc_id,
                    "line_id": a.source_id,
                    "frozen_amount": money(a.amount),
                    "released": money(a.released),
                    "forfeited": money(a.forfeited),
                    "outstanding": money(a.outstanding),
                    "status": a.status.value,
                    "line_limit": money(line.limit) if line else None,
                    "line_available_now": money(line.available) if line else None,
                })
            else:
                batch = batch_index.get(a.source_id)
                margin_used.append({
                    "allocation_id": a.alloc_id,
                    "batch_id": a.source_id,
                    "frozen_amount": money(a.amount),
                    "released": money(a.released),
                    "forfeited": money(a.forfeited),
                    "outstanding": money(a.outstanding),
                    "status": a.status.value,
                    "batch_amount": money(batch.amount) if batch else None,
                    "batch_available_now": money(batch.available) if batch else None,
                })
        return {
            "order_id": order.order_id,
            "client_token": order.client_token,
            "seller_id": order.seller_id,
            "buyer_id": order.buyer_id,
            "status": order.status.value,
            "closed": order.closed,
            "quantity": money(order.quantity),
            "unit_price": money(order.unit_price),
            "margin_rate": money(order.margin_rate),
            "notional": money(order.notional),
            "required_margin": money(order.required_margin),
            "delivered_qty": money(order.delivered_qty),
            "defaulted_qty": money(order.defaulted_qty),
            "settled_qty": money(order.settled_qty),
            "delivery_progress": _progress(order),
            "collateral": {
                "credit_occupied_total": money(order.credit_frozen),
                "margin_occupied_total": money(order.margin_frozen),
                "credit_outstanding": money(order.outstanding_credit),
                "margin_outstanding": money(order.outstanding_margin),
                "credit_lines": credit_lines,
                "margin_batches": margin_used,
            },
            "reject_reasons": order.reject_reasons,
            "created_at": order.created_at,
            "accepted_at": order.accepted_at,
            "entries": [e.entry_id for e in self.store.entries_for_order(order_id)],
        }

    def credit_view(self, seller_id: str) -> dict[str, Any]:
        acc = self.state.credit(seller_id)
        if acc is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404,
                              f"卖方 {seller_id} 没有信用账户")
        return {
            "seller_id": seller_id,
            "total_limit": money(acc.total_limit),
            "frozen": money(acc.frozen),
            "available": money(acc.available),
            "released_cum": money(acc.released_cum),
            "forfeited_cum": money(acc.forfeited_cum),
            "blocked": acc.blocked,
            "block_reason": acc.block_reason,
            "distressed": acc.distressed,
            "lines": {
                lid: {
                    "line_id": lid,
                    "limit": money(line.limit),
                    "frozen": money(line.frozen),
                    "available": money(line.available),
                    "released_cum": money(line.released_cum),
                    "forfeited_cum": money(line.forfeited_cum),
                    "distressed": line.distressed,
                }
                for lid, line in acc.lines.items()
            },
        }

    def margin_view(self, buyer_id: str) -> dict[str, Any]:
        if self.state.party(buyer_id) is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404, f"主体 {buyer_id} 不存在")
        batches = self.state.batches_for(buyer_id)
        total = sum((b.amount for b in batches), ZERO)
        frozen = sum((b.frozen for b in batches), ZERO)
        return {
            "buyer_id": buyer_id,
            "total_deposited": money(total),
            "total_frozen": money(frozen),
            "available": money(total - frozen),
            "forfeited_cum": money(sum((b.forfeited_cum for b in batches), ZERO)),
            "batches": [
                {"batch_id": b.batch_id, "amount": money(b.amount),
                 "frozen": money(b.frozen), "available": money(b.available),
                 "released_cum": money(b.released_cum),
                 "forfeited_cum": money(b.forfeited_cum),
                 "created_at": b.created_at}
                for b in batches],
        }

    def party_view(self, party_id: str) -> dict[str, Any]:
        p = self.state.party(party_id)
        if p is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404, f"主体 {party_id} 不存在")
        return {"party_id": p.party_id, "name": p.name, "role": p.role.value,
                "created_at": p.created_at}

    def ledger_view(self, *, limit: int = 100, order_id: str | None = None) -> dict[str, Any]:
        if order_id:
            entries = self.store.entries_for_order(order_id)
        else:
            entries = list(self.store.iter_entries())[-limit:]
        broken = self.store.verify_chain()
        return {
            "entry_count": self.store.count(),
            "chain_intact": not broken,
            "broken_seqs": broken,
            "entries": [e.to_public() for e in entries],
        }

    # ================================================================ 内部工具

    def _last_evaluation(self, view: dict[str, Any]) -> dict[str, Any]:
        """受理成功后从落账结果反推一份占用解释（与评估方案一致）。"""
        col = view["collateral"]
        return {
            "approved": view["status"] == OrderStatus.ACCEPTED.value,
            "notional": view["notional"],
            "required_margin": view["required_margin"],
            "planned_credit": [
                {"allocation_id": x["allocation_id"], "line_id": x["line_id"],
                 "amount": x["frozen_amount"]} for x in col["credit_lines"]],
            "planned_margin": [
                {"allocation_id": x["allocation_id"], "batch_id": x["batch_id"],
                 "amount": x["frozen_amount"]} for x in col["margin_batches"]],
            "reject_reasons": view["reject_reasons"],
        }

    def _require_party(self, party_id: str, role: PartyRole) -> None:
        p = self.state.party(party_id)
        if p is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404, f"主体 {party_id} 未登记")
        if p.role != role:
            raise DomainError(ErrorCode.VALIDATION, 400,
                              f"主体 {party_id} 角色为 {p.role.value}，需要 {role.value}")

    def _require_credit(self, seller_id: str):
        self._require_party(seller_id, PartyRole.SELLER)
        acc = self.state.credit(seller_id)
        if acc is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404,
                              f"卖方 {seller_id} 尚未授予信用额度")
        return acc

    def _require_open_order(self, order_id: str) -> Order:
        order = self.state.order(order_id)
        if order is None:
            raise DomainError(ErrorCode.NOT_FOUND, 404, f"订单 {order_id} 不存在")
        if order.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_SETTLED):
            raise DomainError(
                ErrorCode.RULE_STATE, 409,
                f"订单状态为 {order.status.value}，不允许交割/撤单")
        return order

    def _commit(self, idem_key: str | None, body: dict[str, Any],
                write_fn) -> tuple[list[Entry], Entry | None]:
        """统一事务模板。

        - ``write_fn(conn)`` 返回分录列表或 ``(entries, extra)``；
        - 幂等键命中且指纹一致：**write_fn 不会执行**，直接回传既有分录，
          调用方据此返回与首次相同的结果——重试不产生任何新分录；
        - 分录在 COMMIT 成功后才应用进内存状态，回滚不留痕。
        """
        with self.store.transaction() as conn:
            replay: Entry | None = None
            if idem_key is not None:
                existing = self.store.find_by_idem(idem_key)
                if existing is not None:
                    self._check_fingerprint(existing, body)
                    replay = existing
            if replay is None:
                result = write_fn(conn)
                if isinstance(result, tuple):
                    entries, _extra = result
                else:
                    entries = result
            else:
                entries = []
        for e in entries:
            self.state.apply(e)
        return entries, replay

    @staticmethod
    def _check_fingerprint(existing: Entry, body: dict[str, Any]) -> None:
        fp = existing.payload.get("fp")
        if fp is not None and fp != _fingerprint(body):
            raise DomainError(
                ErrorCode.IDEMPOTENCY_MISMATCH, 409,
                f"幂等键 {existing.idem_key} 已用于另一请求（分录 {existing.entry_id}），"
                "拒绝按新请求体重放")


def _positive(value: Any, label: str) -> Decimal:
    try:
        d = D(value)
    except Exception:
        raise DomainError(ErrorCode.VALIDATION, 400, f"{label}不是合法金额")
    if d <= ZERO:
        raise DomainError(ErrorCode.VALIDATION, 400, f"{label}必须为正数")
    return d


def _nonnegative(value: Any, label: str) -> Decimal:
    try:
        d = D(value)
    except Exception:
        raise DomainError(ErrorCode.VALIDATION, 400, f"{label}不是合法金额")
    if d < ZERO:
        raise DomainError(ErrorCode.VALIDATION, 400, f"{label}不能为负")
    return d


def _progress(order: Order) -> str:
    if order.quantity == ZERO:
        return "0.00%"
    pct = (order.settled_qty / order.quantity) * Decimal("100")
    return f"{pct.quantize(Decimal('0.01'))}%"
