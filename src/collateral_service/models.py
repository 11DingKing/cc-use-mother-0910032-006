"""领域模型：枚举、不可变数据类与金额（Decimal）序列化。

金额一律使用 ``decimal.Decimal``，禁止二进制浮点参与担保计算。
分录（``Entry``）是账本中唯一可持久化的数据，其余对象均由分录回放得到。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Mapping


# 金额统一保留两位小数；单笔舍入差额挂到最后一笔分配上
CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# 卖方总额度的默认额度行：grant_credit 即注入此行；
# 专项额度通过 register_credit_line 登记，各行之和恒等于总额度。
DEFAULT_LINE = "L-DEFAULT"


class DomainError(Exception):
    """业务错误：携带稳定错误码与 HTTP 状态码，接口可直接解释。"""

    def __init__(self, code: ErrorCode | str, http_status: int, message: str,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code.value if isinstance(code, ErrorCode) else str(code)
        self.http_status = http_status
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        body = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


def D(value: Any) -> Decimal:
    """把字符串/int/Decimal 安全转成两位小数 Decimal。"""
    if isinstance(value, bool):
        raise ValueError("布尔值不能作为金额")
    if isinstance(value, float):
        raise ValueError("禁止用 float 表示金额，请使用字符串")
    dec = Decimal(value)
    if dec.is_nan() or dec.is_infinite():
        raise ValueError("非法金额")
    return dec.quantize(CENT, rounding=ROUND_HALF_UP)


def money(value: Decimal | None) -> str | None:
    """Decimal -> 线性格式字符串（序列化为字符串避免浮点损失）。"""
    if value is None:
        return None
    return str(D(value))


class ErrorCode(str, Enum):
    """接口错误码，拒绝原因稳定可断言。"""

    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION = "VALIDATION"
    RULE_CREDIT_NOT_FOUND = "RULE_CREDIT_NOT_FOUND"  # 卖方没有信用账户
    RULE_CREDIT_BLOCKED = "RULE_CREDIT_BLOCKED"  # 账户被监管冻结
    RULE_CREDIT_LIMIT = "RULE_CREDIT_LIMIT"  # 可用额度不足
    RULE_MARGIN_INSUFFICIENT = "RULE_MARGIN_INSUFFICIENT"  # 可用保证金不足
    RULE_ORDER_LIMIT = "RULE_ORDER_LIMIT"  # 单笔订单超限
    RULE_LINE_LIMIT = "RULE_LINE_LIMIT"  # 指定额度行覆盖不足
    RULE_REENTRANT = "RULE_REENTRANT"  # 同一处理中重复受理
    RULE_STATE = "RULE_STATE"  # 订单状态不允许该操作
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"  # 幂等键复用但请求体不同
    ENTRY_CHAIN_BROKEN = "ENTRY_CHAIN_BROKEN"  # 哈希链校验失败


class PartyRole(str, Enum):
    SELLER = "seller"
    BUYER = "buyer"


class EntryType(str, Enum):
    """不可变分录类型。只追加，不允许 UPDATE/DELETE。"""

    PARTY_REGISTERED = "party_registered"
    CREDIT_GRANTED = "credit_granted"  # 授予/追加授信总额（正数）
    CREDIT_BLOCKED = "credit_blocked"  # 监管冻结/解冻账户
    CREDIT_LINE_GRANTED = "credit_line_granted"  # 登记一条指定额度行
    CREDIT_ADJUSTED = "credit_adjusted"  # 监管降额/恢复（amount 带符号）
    MARGIN_DEPOSITED = "margin_deposited"  # 保证金批次存入（追加担保=新批次）
    ORDER_SUBMITTED = "order_submitted"
    ORDER_REJECTED = "order_rejected"
    ORDER_ACCEPTED = "order_accepted"  # 记录担保分配（随下单在同一事务写入）
    ORDER_CANCELLED = "order_cancelled"
    DELIVERY_SETTLED = "delivery_settled"  # 成交：按交割数量释放/罚没
    ORDER_CLOSED = "order_closed"  # 全部交割完成，剩余担保结清


class OrderStatus(str, Enum):
    PROPOSED = "proposed"  # 已提交、待评估
    ACCEPTED = "accepted"  # 已受理并冻结担保
    REJECTED = "rejected"
    PARTIALLY_SETTLED = "partially_settled"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class AllocationStatus(str, Enum):
    FROZEN = "frozen"
    RELEASED = "released"  # 正常交割释放，可再次被新订单使用
    FORFEITED = "forfeited"  # 全额违约罚没
    PARTIAL_DEFAULT = "partial_default"  # 同一笔占用部分释放、部分罚没
    REFUNDED = "refunded"  # 撤单/结案全额退回


class RuleCode(str, Enum):
    """风控规则。evaluate 结果逐项解释。"""

    SELLER_EXISTS = "seller_exists"
    CREDIT_ACTIVE = "credit_active"
    CREDIT_AVAILABLE = "credit_available"
    MARGIN_AVAILABLE = "margin_available"
    ORDER_NOTIONAL_LIMIT = "order_notional_limit"
    CREDIT_LINES_SUFFICIENT = "credit_lines_sufficient"
    NOT_DUPLICATE_PROPOSAL = "not_duplicate_proposal"
    ORDER_OPEN = "order_open"


# ---------------------------------------------------------------------------
# 不可变事件（持久化单元）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """追加式账本分录。

    每条分录携带前一条分录的哈希，构成哈希链；任何插入/篡改都会让链断裂。
    """

    seq: int
    entry_id: str
    entry_type: EntryType
    timestamp: str
    actor: str
    payload: Mapping[str, Any]
    idem_key: str | None = None
    order_id: str | None = None
    prev_hash: str = ""
    entry_hash: str = ""

    def to_public(self) -> dict[str, Any]:
        """对外视图（含哈希，便于审计员验链）。"""
        return {
            "seq": self.seq,
            "entry_id": self.entry_id,
            "entry_type": self.entry_type.value,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "idem_key": self.idem_key,
            "order_id": self.order_id,
            "payload": dict(self.payload),
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


# ---------------------------------------------------------------------------
# 回放得到的领域对象
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Party:
    party_id: str
    name: str
    role: PartyRole
    created_at: str


@dataclass(slots=True)
class CreditLine:
    """卖方的一条指定额度（监管/项目专项授信）。"""

    line_id: str
    limit: Decimal
    frozen: Decimal = ZERO
    released_cum: Decimal = ZERO  # 已交割释放的累计金额（审计用）
    forfeited_cum: Decimal = ZERO
    distressed: bool = False  # 降额后限额低于已冻结占用

    @property
    def available(self) -> Decimal:
        return self.limit - self.frozen


@dataclass(slots=True)
class CreditAccount:
    """卖方信用账户：总额度 + 若干指定额度行。"""

    party_id: str
    total_limit: Decimal = ZERO
    frozen: Decimal = ZERO
    released_cum: Decimal = ZERO
    forfeited_cum: Decimal = ZERO
    blocked: bool = False
    block_reason: str = ""
    distressed: bool = False  # 监管降额后总额度低于已冻结占用
    lines: dict[str, CreditLine] = field(default_factory=dict)

    @property
    def available(self) -> Decimal:
        return self.total_limit - self.frozen


@dataclass(slots=True)
class MarginBatch:
    """买方保证金批次。追加担保即追加一批，批次独立、先入先出冻结。"""

    batch_id: str
    amount: Decimal
    frozen: Decimal = ZERO
    released_cum: Decimal = ZERO
    forfeited_cum: Decimal = ZERO
    created_entry: str = ""
    created_at: str = ""

    @property
    def available(self) -> Decimal:
        return self.amount - self.frozen


@dataclass(slots=True)
class Allocation:
    """单笔订单对某个担保来源（额度行/保证金批次）的不可变占用。"""

    alloc_id: str
    source_type: str  # "credit_line" | "margin_batch"
    source_id: str
    amount: Decimal
    released: Decimal = ZERO
    forfeited: Decimal = ZERO
    status: AllocationStatus = AllocationStatus.FROZEN

    @property
    def outstanding(self) -> Decimal:
        return self.amount - self.released - self.forfeited


@dataclass(slots=True)
class Order:
    """订单及其担保敞口。"""

    order_id: str
    client_token: str
    seller_id: str
    buyer_id: str
    quantity: Decimal
    unit_price: Decimal
    margin_rate: Decimal  # 买方保证金比例，如 0.20
    notional: Decimal
    required_margin: Decimal
    status: OrderStatus
    created_at: str
    accepted_at: str | None = None
    settled_qty: Decimal = ZERO
    delivered_qty: Decimal = ZERO
    defaulted_qty: Decimal = ZERO
    allocations: dict[str, Allocation] = field(default_factory=dict)
    reject_reasons: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    @property
    def credit_frozen(self) -> Decimal:
        return sum(
            (a.amount for a in self.allocations.values() if a.source_type == "credit_line"),
            ZERO,
        )

    @property
    def margin_frozen(self) -> Decimal:
        return sum(
            (a.amount for a in self.allocations.values() if a.source_type == "margin_batch"),
            ZERO,
        )

    @property
    def outstanding_credit(self) -> Decimal:
        return sum(
            (a.outstanding for a in self.allocations.values() if a.source_type == "credit_line"),
            ZERO,
        )

    @property
    def outstanding_margin(self) -> Decimal:
        return sum(
            (a.outstanding for a in self.allocations.values() if a.source_type == "margin_batch"),
            ZERO,
        )

    def allocation_view(self) -> list[dict[str, Any]]:
        return [
            {
                "allocation_id": a.alloc_id,
                "source_type": a.source_type,
                "source_id": a.source_id,
                "amount": money(a.amount),
                "released": money(a.released),
                "forfeited": money(a.forfeited),
                "outstanding": money(a.outstanding),
                "status": a.status.value,
            }
            for a in self.allocations.values()
        ]


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: RuleCode
    passed: bool
    required: Decimal | None = None
    available: Decimal | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.value,
            "passed": self.passed,
            "required": money(self.required),
            "available": money(self.available),
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class OrderEvaluation:
    """受理评估结果：逐规则通过情况 + 建议的担保分配方案。"""

    order_id: str
    approved: bool
    notional: Decimal
    required_margin: Decimal
    rule_results: tuple[RuleResult, ...]
    credit_plan: tuple[tuple[str, str, Decimal], ...]  # (alloc_id, line_id, amount)
    margin_plan: tuple[tuple[str, str, Decimal], ...]  # (alloc_id, batch_id, amount)
    reject_reasons: tuple[dict[str, Any], ...]

    def rejection_codes(self) -> list[str]:
        return [r["code"] for r in self.reject_reasons]

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "approved": self.approved,
            "notional": money(self.notional),
            "required_margin": money(self.required_margin),
            "rule_results": [r.to_dict() for r in self.rule_results],
            "planned_credit": [
                {"line_id": lid, "amount": money(amt)} for _, lid, amt in self.credit_plan
            ],
            "planned_margin": [
                {"batch_id": bid, "amount": money(amt)} for _, bid, amt in self.margin_plan
            ],
            "reject_reasons": list(self.reject_reasons),
        }


def clone_order(order: Order, **changes: Any) -> Order:
    """复制订单（dataclass 默认非冻结，便于状态机内部使用）。"""
    return replace(order, **changes)
