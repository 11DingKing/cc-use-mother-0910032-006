"""分录回放：内存中的领域状态完全由不可变分录推导。

重启或怀疑状态漂移时，对分录顺序执行 :meth:`LedgerState.replay` 即可重建，
不存在"状态表"与"分录表"不一致的可能。

回放约定（所有金额以字符串存于 payload）：

- ``credit_granted``   amount(带符号：授予/追加为正，冲减为负)
- ``credit_blocked``   blocked(bool), reason
- ``credit_line_granted`` line_id, amount(带符号：登记为正，调减为负)
- ``credit_adjusted``  amount(带符号，监管降额为负/恢复为正), reason；
                        降额先压总额度，再按需要压各指定行；若已冻结占用
                        超过新限额，账户被标记为 ``distressed``（不回滚历史分录）
- ``margin_deposited`` batch_id, amount（追加担保=新增批次；amount 必须为正）
- ``order_submitted``  order 快照字段
- ``order_rejected``   reasons
- ``order_accepted``   allocations=[{allocation_id, source_type, source_id, amount}]
- ``order_cancelled``  —— 冻结按撤单语义释放（见 DELIVERY 默认违约）
- ``delivery_settled`` delivered_qty, defaulted_qty,
                        releases=[{allocation_id, released, forfeited}]
- ``order_closed``     releases=[...] 剩余 outstanding 全部结清
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import (
    Allocation,
    AllocationStatus,
    CreditAccount,
    CreditLine,
    D,
    DEFAULT_LINE,
    MarginBatch,
    Order,
    OrderStatus,
    Party,
    PartyRole,
    ZERO,
)
from .store import EntryStore
from .models import Entry, EntryType


class LedgerState:
    """由分录回放得到的内存状态。回放是唯一的状态变更入口。"""

    def __init__(self) -> None:
        self.parties: dict[str, Party] = {}
        self.credits: dict[str, CreditAccount] = {}
        self.margin_batches: dict[str, MarginBatch] = {}
        self.buyer_batches: dict[str, list[str]] = {}
        self.orders: dict[str, Order] = {}
        self.orders_by_token: dict[str, str] = {}
        self.last_seq: int = 0

    # ------------------------------------------------------------------ 回放

    def replay(self, store: EntryStore) -> None:
        self.__init__()  # type: ignore[misc]
        for entry in store.iter_entries():
            self.apply(entry)

    def apply(self, e: Entry) -> None:
        p = e.payload
        handler = _HANDLERS.get(e.entry_type)
        if handler is not None:
            handler(self, e, p)
        self.last_seq = e.seq

    # ------------------------------------------------------------------ 查询

    def party(self, party_id: str) -> Party | None:
        return self.parties.get(party_id)

    def credit(self, seller_id: str) -> CreditAccount | None:
        return self.credits.get(seller_id)

    def batches_for(self, buyer_id: str) -> list[MarginBatch]:
        return [self.margin_batches[b] for b in self.buyer_batches.get(buyer_id, [])]

    def order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    def open_proposal_of_token(self, client_token: str) -> Order | None:
        oid = self.orders_by_token.get(client_token)
        if oid is None:
            return None
        order = self.orders[oid]
        if order.status in (OrderStatus.PROPOSED, OrderStatus.ACCEPTED,
                            OrderStatus.PARTIALLY_SETTLED):
            return order
        return None


# ---------------------------------------------------------------------------
# 各分录类型的回放处理
# ---------------------------------------------------------------------------


def _h_party_registered(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    s.parties[p["party_id"]] = Party(
        party_id=p["party_id"],
        name=p.get("name", p["party_id"]),
        role=PartyRole(p["role"]),
        created_at=e.timestamp,
    )


def _ensure_line(acc: CreditAccount, line_id: str) -> CreditLine:
    line = acc.lines.get(line_id)
    if line is None:
        line = CreditLine(line_id=line_id, limit=ZERO)
        acc.lines[line_id] = line
    return line


def _h_credit_granted(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    """授予/追加授信：注入默认额度行 L-DEFAULT，总额度=各行之和。"""
    acc = s.credits.setdefault(p["seller_id"], CreditAccount(party_id=p["seller_id"]))
    amount = D(p["amount"])
    acc.total_limit = D(acc.total_limit) + amount
    line = _ensure_line(acc, DEFAULT_LINE)
    line.limit = D(line.limit) + amount


def _h_credit_blocked(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    acc = s.credits.setdefault(p["seller_id"], CreditAccount(party_id=p["seller_id"]))
    acc.blocked = bool(p["blocked"])
    acc.block_reason = p.get("reason", "") if acc.blocked else ""


def _h_credit_line_granted(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    """登记/追加专项额度行；总额度同步增加，保持各行之和=总额度。"""
    acc = s.credits.setdefault(p["seller_id"], CreditAccount(party_id=p["seller_id"]))
    amount = D(p["amount"])
    line = _ensure_line(acc, p["line_id"])
    line.limit = D(line.limit) + amount
    acc.total_limit = D(acc.total_limit) + amount


def _h_credit_adjusted(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    """监管降额/恢复。

    payload.amount 是总额度变动（负=降额），payload.lines 给出各专项行的
    变动明细；未被明细覆盖的部分落在默认额度行上，因此
    ``total_limit == sum(line.limit)`` 始终成立。

    历史冻结分录不可变，因此降额后若占用超过新限额，只标记 ``distressed``
    供监管视图暴露，不去回改任何旧分录；新订单一律按新限额评估。
    """
    acc = s.credits[p["seller_id"]]
    total_delta = D(p["amount"])
    acc.total_limit = D(acc.total_limit) + total_delta
    acc.distressed = acc.frozen > acc.total_limit

    specified = ZERO
    for change in p.get("lines", []):
        delta = D(change["amount"])
        line = _ensure_line(acc, change["line_id"])
        line.limit = D(line.limit) + delta
        line.distressed = line.frozen > line.limit
        specified += delta
    rest = total_delta - specified
    if rest != ZERO:
        line = _ensure_line(acc, DEFAULT_LINE)
        line.limit = D(line.limit) + rest
        line.distressed = line.frozen > line.limit


def _h_margin_deposited(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    batch = MarginBatch(
        batch_id=p["batch_id"],
        amount=D(p["amount"]),
        created_entry=e.entry_id,
        created_at=e.timestamp,
    )
    s.margin_batches[p["batch_id"]] = batch
    s.buyer_batches.setdefault(p["buyer_id"], []).append(p["batch_id"])


def _h_order_submitted(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    order = Order(
        order_id=p["order_id"],
        client_token=p["client_token"],
        seller_id=p["seller_id"],
        buyer_id=p["buyer_id"],
        quantity=D(p["quantity"]),
        unit_price=D(p["unit_price"]),
        margin_rate=D(p["margin_rate"]),
        notional=D(p["notional"]),
        required_margin=D(p["required_margin"]),
        status=OrderStatus.PROPOSED,
        created_at=e.timestamp,
    )
    s.orders[p["order_id"]] = order
    s.orders_by_token[p["client_token"]] = p["order_id"]


def _h_order_rejected(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    order = s.orders[p["order_id"]]
    order.status = OrderStatus.REJECTED
    order.reject_reasons = list(p.get("reasons", []))


def _h_order_accepted(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    order = s.orders[p["order_id"]]
    order.status = OrderStatus.ACCEPTED
    order.accepted_at = e.timestamp
    acc = s.credits[order.seller_id]
    for item in p["allocations"]:
        alloc = Allocation(
            alloc_id=item["allocation_id"],
            source_type=item["source_type"],
            source_id=item["source_id"],
            amount=D(item["amount"]),
        )
        order.allocations[alloc.alloc_id] = alloc
        if alloc.source_type == "credit_line":
            line = acc.lines[alloc.source_id]
            line.frozen = D(line.frozen) + alloc.amount
            acc.frozen = D(acc.frozen) + alloc.amount
        else:
            batch = s.margin_batches[alloc.source_id]
            batch.frozen = D(batch.frozen) + alloc.amount


def _apply_release(
    s: LedgerState, order: Order, alloc_id: str, released: Decimal, forfeited: Decimal
) -> None:
    alloc = order.allocations[alloc_id]
    alloc.released = D(alloc.released) + released
    alloc.forfeited = D(alloc.forfeited) + forfeited
    if alloc.outstanding == ZERO:
        if alloc.forfeited > ZERO and alloc.released > ZERO:
            alloc.status = AllocationStatus.PARTIAL_DEFAULT
        elif alloc.forfeited > ZERO:
            alloc.status = AllocationStatus.FORFEITED
        else:
            alloc.status = AllocationStatus.RELEASED
    if alloc.source_type == "credit_line":
        acc = s.credits[order.seller_id]
        line = acc.lines[alloc.source_id]
        # 正常交割释放：冻结回池可再用；违约罚没：永久消耗授信（限额与
        # 冻结同步核减，available 不会把罚没额重新计为可用）
        line.frozen = D(line.frozen) - released - forfeited
        line.limit = D(line.limit) - forfeited
        line.released_cum = D(line.released_cum) + released
        line.forfeited_cum = D(line.forfeited_cum) + forfeited
        acc.frozen = D(acc.frozen) - released - forfeited
        acc.total_limit = D(acc.total_limit) - forfeited
        acc.released_cum = D(acc.released_cum) + released
        acc.forfeited_cum = D(acc.forfeited_cum) + forfeited
    else:
        batch = s.margin_batches[alloc.source_id]
        # 罚没保证金已赔付清算：批次金额与冻结同步核减，不会重新可用
        batch.frozen = D(batch.frozen) - released - forfeited
        batch.amount = D(batch.amount) - forfeited
        batch.released_cum = D(batch.released_cum) + released
        batch.forfeited_cum = D(batch.forfeited_cum) + forfeited


def _h_delivery_settled(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    order = s.orders[p["order_id"]]
    order.delivered_qty = D(order.delivered_qty) + D(p["delivered_qty"])
    order.defaulted_qty = D(order.defaulted_qty) + D(p["defaulted_qty"])
    order.settled_qty = D(order.settled_qty) + D(p["delivered_qty"]) + D(
        p["defaulted_qty"]
    )
    for r in p["releases"]:
        _apply_release(s, order, r["allocation_id"], D(r["released"]), D(r["forfeited"]))
    if order.settled_qty < order.quantity:
        order.status = OrderStatus.PARTIALLY_SETTLED
    else:
        order.status = OrderStatus.SETTLED


def _h_order_cancelled(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    order = s.orders[p["order_id"]]
    # 撤单：尚未释放的冻结全部退回（信用解冻、保证金可用）
    refunded_ids = set()
    for r in p.get("releases", []):
        _apply_release(s, order, r["allocation_id"], D(r["released"]), D(r["forfeited"]))
        refunded_ids.add(r["allocation_id"])
    order.status = OrderStatus.CANCELLED
    order.closed = True
    for alloc in order.allocations.values():
        # 本次撤单退回的占用标 REFUNDED；此前已正常交割释放的仍为 RELEASED
        if alloc.alloc_id in refunded_ids:
            alloc.status = AllocationStatus.REFUNDED
        elif alloc.status == AllocationStatus.FROZEN:
            alloc.status = AllocationStatus.REFUNDED


def _h_order_closed(s: LedgerState, e: Entry, p: dict[str, Any]) -> None:
    order = s.orders[p["order_id"]]
    for r in p["releases"]:
        _apply_release(s, order, r["allocation_id"], D(r["released"]), D(r["forfeited"]))
    order.closed = True
    if order.status != OrderStatus.SETTLED:
        order.status = OrderStatus.SETTLED


_HANDLERS = {
    EntryType.PARTY_REGISTERED: _h_party_registered,
    EntryType.CREDIT_GRANTED: _h_credit_granted,
    EntryType.CREDIT_BLOCKED: _h_credit_blocked,
    EntryType.CREDIT_LINE_GRANTED: _h_credit_line_granted,
    EntryType.CREDIT_ADJUSTED: _h_credit_adjusted,
    EntryType.MARGIN_DEPOSITED: _h_margin_deposited,
    EntryType.ORDER_SUBMITTED: _h_order_submitted,
    EntryType.ORDER_REJECTED: _h_order_rejected,
    EntryType.ORDER_ACCEPTED: _h_order_accepted,
    EntryType.ORDER_CANCELLED: _h_order_cancelled,
    EntryType.DELIVERY_SETTLED: _h_delivery_settled,
    EntryType.ORDER_CLOSED: _h_order_closed,
}
