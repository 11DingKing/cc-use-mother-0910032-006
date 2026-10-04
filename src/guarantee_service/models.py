"""领域模型：不可变分录与由分录派生的内存状态。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0")


def to_decimal(value: Any) -> Decimal:
    """把 JSON 输入安全转换为 Decimal，拒绝布尔与空串。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("金额字段不能是布尔值")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("金额字段不能为空")
        return Decimal(text)
    raise ValueError(f"无法解析金额：{value!r}")


def q2(value: Decimal) -> Decimal:
    """按分位四舍五入。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def q2_up(value: Decimal) -> Decimal:
    """按分位向上取整：风控冻结宁可多不可少。"""
    return value.quantize(CENT, rounding=ROUND_UP)


def mstr(value: Decimal) -> str:
    """金额的稳定字符串表示。"""
    return str(q2(value))


@dataclass(frozen=True)
class Entry:
    """一条不可变分录。"""

    seq: int
    entry_id: str
    ts: str
    kind: str
    request_id: str | None
    data: dict

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "entry_id": self.entry_id,
            "ts": self.ts,
            "kind": self.kind,
            "request_id": self.request_id,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Entry":
        return cls(
            seq=int(raw["seq"]),
            entry_id=str(raw["entry_id"]),
            ts=str(raw["ts"]),
            kind=str(raw["kind"]),
            request_id=raw.get("request_id"),
            data=dict(raw["data"]),
        )


@dataclass
class Allocation:
    """订单对某一额度来源的一笔冻结及其后续释放/罚没。"""

    source_kind: str  # credit_line | margin_batch
    source_id: str
    amount: Decimal
    released: Decimal = ZERO
    seized: Decimal = ZERO

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.released - self.seized

    def to_dict(self) -> dict:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "amount": mstr(self.amount),
            "released": mstr(self.released),
            "seized": mstr(self.seized),
            "outstanding": mstr(self.outstanding),
        }


ORDER_OPEN_STATUSES = ("accepted", "delivering")


@dataclass
class OrderState:
    """订单投影：由 order_accepted / order_rejected 及后续分录派生。"""

    order_id: str
    request_id: str
    seller_id: str
    buyer_id: str
    quantity: int
    price: Decimal
    notional: Decimal
    exposure: Decimal
    margin_required: Decimal
    rule_id: str
    rule_version: int
    status: str
    allocations: list[Allocation] = field(default_factory=list)
    delivered: int = 0
    defaulted: int = 0
    reject_reasons: list[dict] = field(default_factory=list)

    @property
    def outstanding_qty(self) -> int:
        return self.quantity - self.delivered - self.defaulted

    @property
    def frozen_outstanding(self) -> Decimal:
        return sum((a.outstanding for a in self.allocations), ZERO)

    def find_allocation(self, source_kind: str, source_id: str) -> Allocation:
        for alloc in self.allocations:
            if alloc.source_kind == source_kind and alloc.source_id == source_id:
                return alloc
        raise KeyError(f"订单 {self.order_id} 没有来源 {source_kind}/{source_id} 的冻结")

    def to_dict(self) -> dict:
        result = {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "seller_id": self.seller_id,
            "buyer_id": self.buyer_id,
            "status": self.status,
            "quantity": self.quantity,
            "price": mstr(self.price),
            "notional": mstr(self.notional),
            "exposure": mstr(self.exposure),
            "margin_required": mstr(self.margin_required),
            "rule": {"rule_id": self.rule_id, "version": self.rule_version},
            "delivery": {
                "total": self.quantity,
                "delivered": self.delivered,
                "defaulted": self.defaulted,
                "outstanding": self.outstanding_qty,
            },
            "allocations": [a.to_dict() for a in self.allocations],
            "frozen_outstanding": mstr(self.frozen_outstanding),
        }
        if self.reject_reasons:
            result["reject_reasons"] = self.reject_reasons
        return result


@dataclass
class CreditLineState:
    """信用额度投影。"""

    line_id: str
    owner_id: str
    limit: Decimal
    frozen: Decimal = ZERO
    seized_total: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        return self.limit - self.frozen

    @property
    def breached(self) -> bool:
        """监管降额后冻结超过新限额即为击穿。"""
        return self.frozen > self.limit


@dataclass
class MarginBatchState:
    """保证金批次投影。"""

    batch_id: str
    owner_id: str
    amount: Decimal
    frozen: Decimal = ZERO
    seized_total: Decimal = ZERO

    @property
    def available(self) -> Decimal:
        return self.amount - self.frozen


@dataclass
class RiskRule:
    """风控规则（按 rule_id 版本化，订单记录当时使用的快照）。"""

    rule_id: str
    version: int
    margin_rate: Decimal
    exposure_rate: Decimal
    max_order_notional: Decimal
    max_seller_utilization: Decimal

    def snapshot(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "margin_rate": str(self.margin_rate),
            "exposure_rate": str(self.exposure_rate),
            "max_order_notional": str(self.max_order_notional),
            "max_seller_utilization": str(self.max_seller_utilization),
        }

    def to_entry_data(self) -> dict:
        return self.snapshot()
