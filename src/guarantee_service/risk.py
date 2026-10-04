"""风控规则与订单敞口计算。"""
from __future__ import annotations

from decimal import Decimal

from .models import RiskRule, q2, q2_up

DEFAULT_RULE_ID = "standard"


def default_rule() -> RiskRule:
    """系统内置的兜底风控规则（首次启动时自动登记为第 1 版）。"""
    return RiskRule(
        rule_id=DEFAULT_RULE_ID,
        version=1,
        margin_rate=Decimal("0.2"),
        exposure_rate=Decimal("1"),
        max_order_notional=Decimal("100000000"),
        max_seller_utilization=Decimal("1"),
    )


def compute_requirements(rule: RiskRule, quantity: int, price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """返回（名义金额，卖方敞口，买方保证金）。冻结要求一律向上取整。"""
    notional = q2(price * quantity)
    exposure = q2_up(notional * rule.exposure_rate)
    margin = q2_up(notional * rule.margin_rate)
    return notional, exposure, margin
