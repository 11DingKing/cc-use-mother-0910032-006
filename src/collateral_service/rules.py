"""风控规则评估与担保分配规划。

评估是纯函数：读当前回放状态 + 订单申请，输出：

1. 逐条规则的通过/拒绝解释（:class:`RuleResult`）；
2. 若通过，给出冻结方案——卖方额度按额度行（FIFO + 指定行优先）、
   买方保证金按批次先入先出，每一分钱都能指到具体来源。

评估本身不落任何分录；是否受理由引擎在事务内据评估结果决定。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .models import (
    CreditAccount,
    D,
    ErrorCode,
    Order,
    RuleCode,
    RuleResult,
    ZERO,
)
from .state import LedgerState

CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Proposal:
    client_token: str
    seller_id: str
    buyer_id: str
    quantity: Decimal
    unit_price: Decimal
    margin_rate: Decimal
    order_id: str | None = None
    preferred_lines: tuple[str, ...] = ()  # 买方/运营指定优先占用的额度行


def _plan(
    required: Decimal,
    sources: list[tuple[str, Decimal]],
) -> list[tuple[str, Decimal]]:
    """按 sources 顺序贪心分配，金额保留两位，末笔吸收舍入尾差。

    sources: [(source_id, available), ...]；返回 [(source_id, amount)]。
    不足额时返回 None（调用方据此生成拒绝原因）。
    """
    total_available = sum((a for _, a in sources), ZERO)
    if total_available < required:
        return None
    plan: list[tuple[str, Decimal]] = []
    remaining = required
    for idx, (sid, available) in enumerate(sources):
        if remaining <= ZERO:
            break
        take = min(available, remaining)
        if idx == len(sources) - 1 or take == remaining:
            take = remaining  # 最后一个实际使用的来源吸收尾差
        take = D(take)
        if take > ZERO:
            plan.append((sid, take))
            remaining -= take
    return plan if remaining == ZERO else None


def _reject(code: ErrorCode, rule: RuleCode, detail: str,
            required: Decimal | None = None, available: Decimal | None = None,
            **extra: Any) -> dict[str, Any]:
    reason = {
        "code": code.value,
        "rule": rule.value,
        "detail": detail,
    }
    if required is not None:
        reason["required"] = str(required)
    if available is not None:
        reason["available"] = str(available)
    reason.update(extra)
    return reason


def evaluate(state: LedgerState, prop: Proposal, *, max_order_notional: Decimal,
             alloc_id_prefix: str) -> dict[str, Any]:
    """评估订单申请。

    返回 {"rules": [...], "reject_reasons": [...], "credit_plan", "margin_plan",
          "notional", "required_margin", "approved"}。
    """
    rules: list[RuleResult] = []
    reasons: list[dict[str, Any]] = []

    notional = D(prop.quantity * prop.unit_price)
    required_margin = D(notional * prop.margin_rate)

    seller = state.party(prop.seller_id)
    buyer = state.party(prop.buyer_id)

    # --- 规则 1：交易双方必须已登记 ---
    parties_ok = seller is not None and buyer is not None
    missing = [pid for pid, p in ((prop.seller_id, seller), (prop.buyer_id, buyer))
               if p is None]
    rules.append(RuleResult(
        RuleCode.SELLER_EXISTS, parties_ok,
        detail="交易双方已登记" if parties_ok else f"未登记主体：{','.join(missing)}"))
    if not parties_ok:
        reasons.append(_reject(ErrorCode.NOT_FOUND, RuleCode.SELLER_EXISTS,
                               f"未登记主体：{','.join(missing)}"))

    acc: CreditAccount | None = state.credit(prop.seller_id) if seller else None

    # --- 规则 2：信用账户存在且未被监管冻结 ---
    active = acc is not None and not acc.blocked
    if acc is None:
        detail = f"卖方 {prop.seller_id} 未开通信用账户"
        reasons.append(_reject(ErrorCode.RULE_CREDIT_NOT_FOUND,
                               RuleCode.CREDIT_ACTIVE, detail))
    elif acc.blocked:
        detail = f"卖方信用账户被冻结：{acc.block_reason or '监管冻结'}"
        reasons.append(_reject(ErrorCode.RULE_CREDIT_BLOCKED,
                               RuleCode.CREDIT_ACTIVE, detail))
    else:
        detail = "信用账户正常"
    rules.append(RuleResult(RuleCode.CREDIT_ACTIVE, active, detail=detail))

    # --- 规则 3：总额度可用余额覆盖订单名义金额 ---
    credit_avail = acc.available if acc else ZERO
    credit_ok = acc is not None and not acc.blocked and credit_avail >= notional
    rules.append(RuleResult(
        RuleCode.CREDIT_AVAILABLE, credit_ok,
        required=notional, available=credit_avail,
        detail=("总额度可用余额充足" if credit_ok
                else f"可用额度 {credit_avail} 不足以覆盖订单名义金额 {notional}")))
    if not credit_ok and active:
        reasons.append(_reject(ErrorCode.RULE_CREDIT_LIMIT,
                               RuleCode.CREDIT_AVAILABLE,
                               "卖方可用额度不足，同一未交割额度不得在多个订单间重复承诺",
                               required=notional, available=credit_avail,
                               seller_id=prop.seller_id))

    # --- 规则 4：单笔订单金额上限 ---
    limit_ok = notional <= max_order_notional
    limit_detail = (
        "未超过单笔上限"
        if limit_ok
        else f"订单名义金额 {notional} 超过单笔上限 {max_order_notional}"
    )
    rules.append(RuleResult(
        RuleCode.ORDER_NOTIONAL_LIMIT, limit_ok,
        required=notional, available=max_order_notional, detail=limit_detail))
    if not limit_ok:
        reasons.append(_reject(ErrorCode.RULE_ORDER_LIMIT,
                               RuleCode.ORDER_NOTIONAL_LIMIT,
                               "超过单笔订单名义金额上限",
                               required=notional, available=max_order_notional))

    # --- 规则 5：同一处理中的订单不得重复受理（防重试扩敞口的第一道闸） ---
    dup = state.open_proposal_of_token(prop.client_token)
    dup_ok = dup is None
    rules.append(RuleResult(
        RuleCode.NOT_DUPLICATE_PROPOSAL, dup_ok,
        detail=("无在途重复单" if dup_ok
                else f"client_token={prop.client_token} 已有在途订单 {dup.order_id}")))
    if not dup_ok:
        reasons.append(_reject(ErrorCode.RULE_REENTRANT,
                               RuleCode.NOT_DUPLICATE_PROPOSAL,
                               "同一 client_token 的订单仍在处理中，重复受理将扩大敞口",
                               existing_order_id=dup.order_id,
                               existing_status=dup.status.value))

    # --- 规则 6：保证金可用余额（按批次 FIFO） ---
    batches = state.batches_for(prop.buyer_id)
    open_batches = [b for b in batches if b.available > ZERO]
    margin_sources = [(b.batch_id, b.available) for b in open_batches]
    margin_total = sum((a for _, a in margin_sources), ZERO)
    margin_ok = margin_total >= required_margin
    rules.append(RuleResult(
        RuleCode.MARGIN_AVAILABLE, margin_ok,
        required=required_margin, available=margin_total,
        detail=("买方保证金批次可用余额充足" if margin_ok
                else f"可用保证金 {margin_total} 低于所需 {required_margin}，"
                     "风险不得向清算环节传递")))
    if not margin_ok:
        reasons.append(_reject(ErrorCode.RULE_MARGIN_INSUFFICIENT,
                               RuleCode.MARGIN_AVAILABLE,
                               "买方可用保证金不足，请追加担保后重试",
                               required=required_margin, available=margin_total,
                               buyer_id=prop.buyer_id))

    # --- 规则 7：额度行可构造完整冻结方案（指定行优先，其余 FIFO） ---
    credit_plan: list[tuple[str, Decimal]] = []
    lines_ok = False
    if acc is not None and not acc.blocked:
        ordered_lines = _order_lines(acc, prop.preferred_lines)
        line_sources = [(lid, line.available) for lid, line in ordered_lines
                        if line.available > ZERO]
        built = _plan(notional, line_sources)
        if built is not None:
            credit_plan = built
            lines_ok = True
    rules.append(RuleResult(
        RuleCode.CREDIT_LINES_SUFFICIENT, lines_ok,
        required=notional,
        available=sum((a for _, a in credit_plan), ZERO) if credit_plan
        else (acc.available if acc else ZERO),
        detail=("额度行冻结方案可构造" if lines_ok else "无法在额度行间构造完整冻结方案")))
    if not lines_ok and credit_ok:
        reasons.append(_reject(ErrorCode.RULE_LINE_LIMIT,
                               RuleCode.CREDIT_LINES_SUFFICIENT,
                               "总额度虽足，但各额度行可用余额无法完整覆盖本单",
                               required=notional))

    margin_plan = _plan(required_margin, margin_sources) or []

    # 带稳定 allocation_id，便于接口解释"这笔订单占用了哪些额度/批次"
    credit_plan_named = [
        (f"{alloc_id_prefix}-C{i + 1:02d}", lid, amt)
        for i, (lid, amt) in enumerate(credit_plan)
    ]
    margin_plan_named = [
        (f"{alloc_id_prefix}-M{i + 1:02d}", bid, amt)
        for i, (bid, amt) in enumerate(margin_plan)
    ]

    approved = not reasons
    return {
        "approved": approved,
        "notional": notional,
        "required_margin": required_margin,
        "rules": tuple(rules),
        "reject_reasons": tuple(reasons),
        "credit_plan": tuple(credit_plan_named),
        "margin_plan": tuple(margin_plan_named),
    }


def _order_lines(
    acc: CreditAccount, preferred: tuple[str, ...]
) -> list[tuple[str, Any]]:
    """指定额度行优先，其余按 line_id 稳定排序（等价于登记 FIFO）。"""
    lines = list(acc.lines.items())
    rank = {lid: i for i, lid in enumerate(preferred)}

    def key(item: tuple[str, Any]) -> tuple[int, str]:
        lid = item[0]
        return (rank.get(lid, len(preferred)), lid)

    lines.sort(key=key)
    return lines


# ---------------------------------------------------------------------------
# 交割释放/罚没的逐分配金额计算
# ---------------------------------------------------------------------------


def prorate_releases(
    order: Order, delivered_qty: Decimal, defaulted_qty: Decimal
) -> list[tuple[str, Decimal, Decimal]]:
    """按交割/违约数量比例，把本次释放与罚没摊到每笔占用上。

    信用池（额度行占用合计 = 订单名义金额）与保证金池（批次占用合计 =
    所需保证金）的单位担保金额不同，因此分池处理。

    关键口径：每次事件以**池内剩余未结金额**和**订单剩余数量**为基准按比例
    分摊（而不是回头用原始冻结额），因此多次部分交割的舍入误差不会累积；
    最后一个事件（数量结清）强制结清池内全部余额，违约部分先分、释放部分
    吸收尾差，保证结案时无一分钱残留。池内分配在整数"分"上用最大余数法
    完成并受每笔未结余额约束。

    返回 [(allocation_id, released, forfeited)]。
    """
    result: list[tuple[str, Decimal, Decimal]] = []
    remaining_qty = order.quantity - order.settled_qty
    event_qty = delivered_qty + defaulted_qty
    final = event_qty >= remaining_qty - Decimal("0.000001")
    for pool in ("credit_line", "margin_batch"):
        allocs = [a for a in order.allocations.values()
                  if a.source_type == pool and a.outstanding > ZERO]
        if not allocs:
            continue
        pool_outstanding = sum((a.outstanding for a in allocs), ZERO)
        # 违约罚没先按比例确定
        for_target = D(pool_outstanding * defaulted_qty / remaining_qty)
        if final:
            # 最终事件：释放额 = 剩余未结 - 罚没，尾差由释放吸收
            rel_target = pool_outstanding - for_target
        else:
            rel_target = D(pool_outstanding * delivered_qty / remaining_qty)
        result.extend(_distribute(allocs, rel_target, for_target))
    return result


def _cents(value: Decimal) -> int:
    return int(value.scaleb(2).to_integral_value())


def _apportion(weights: list[int], target: int,
               capacities: list[int]) -> list[int]:
    """把 target 分按权重最大余数法分摊，每格不超过 capacity，总和恰为 target。"""
    total_weight = sum(weights)
    result = [min(target * w // total_weight, cap)
              for w, cap in zip(weights, capacities)]
    remainder = target - sum(result)
    # 按"欠账"（理论份额 − 已分）从多到少逐分补发，只补给尚有容量的格
    while remainder > 0:
        def deficit(i: int) -> float:
            return target * weights[i] / total_weight - result[i]

        progressed = False
        for i in sorted(range(len(weights)), key=deficit, reverse=True):
            if remainder == 0:
                break
            if result[i] < capacities[i]:
                result[i] += 1
                remainder -= 1
                progressed = True
        if not progressed:  # 容量耗尽（理论上不会发生，防御性退出）
            break
    return result


def _distribute(
    allocs: list[Any], rel_target: Decimal, for_target: Decimal
) -> list[tuple[str, Decimal, Decimal]]:
    """先分摊罚没（占用赔付清算），再在剩余未结容量内分摊释放。"""
    weights = [_cents(a.amount) for a in allocs]
    outstanding = [_cents(a.outstanding) for a in allocs]
    for_cents = _apportion(weights, _cents(for_target), outstanding)
    rel_caps = [cap - f for cap, f in zip(outstanding, for_cents)]
    rel_cents = _apportion(weights, _cents(rel_target), rel_caps)
    return [
        (a.alloc_id, Decimal(rel).scaleb(-2), Decimal(forf).scaleb(-2))
        for a, rel, forf in zip(allocs, rel_cents, for_cents)
    ]
