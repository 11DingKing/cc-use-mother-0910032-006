"""担保风控引擎的领域回归测试。

覆盖：额度登记、保证金批次、受理冻结、逐笔占用解释、拒绝原因、
追加担保、部分违约罚没、交割进度释放、撤单退回、监管降额/冻结、
幂等重试不扩敞口、并发下单串行化、只追加约束与哈希链。
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collateral_service.engine import CollateralEngine, RiskConfig
from collateral_service.models import (
    DEFAULT_LINE,
    DomainError,
    ErrorCode,
)
from collateral_service.state import LedgerState
from collateral_service.store import EntryStore


def make_engine(max_notional: str = "10000000.00") -> CollateralEngine:
    return CollateralEngine(EntryStore(":memory:"), RiskConfig(max_notional))


def bootstrap(eng: CollateralEngine, *, credit="100000", extra_lines=(),
              margin=("30000",)):
    eng.register_party(party_id="S1", name="卖方", role="seller", idem_key="p-s")
    eng.register_party(party_id="B1", name="买方", role="buyer", idem_key="p-b")
    eng.register_party(party_id="B2", name="买方2", role="buyer", idem_key="p-b2")
    eng.grant_credit(seller_id="S1", amount=credit, idem_key="c-base")
    for i, amt in enumerate(extra_lines):
        eng.register_credit_line(seller_id="S1", line_id=f"L-E{i}",
                                 amount=amt, idem_key=f"c-line-{i}")
    for i, amt in enumerate(margin):
        eng.deposit_margin(buyer_id="B1", amount=amt,
                           batch_id=f"MB-{i}", idem_key=f"m-{i}")


class RegistrationTest(unittest.TestCase):
    def test_role_validation_and_duplicate(self) -> None:
        eng = make_engine()
        with self.assertRaises(DomainError) as cm:
            eng.register_party(party_id="X", name="x", role="regulator",
                               idem_key="k1")
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION.value)
        eng.register_party(party_id="X", name="x", role="seller", idem_key="k1")
        with self.assertRaises(DomainError):
            eng.register_party(party_id="X", name="x", role="seller",
                               idem_key="k2")

    def test_float_money_rejected(self) -> None:
        eng = make_engine()
        eng.register_party(party_id="S", name="s", role="seller", idem_key="k")
        with self.assertRaises(DomainError):
            eng.grant_credit(seller_id="S", amount=100.0, idem_key="g")  # type: ignore[arg-type]

    def test_credit_requires_seller_role(self) -> None:
        eng = make_engine()
        eng.register_party(party_id="B", name="b", role="buyer", idem_key="k")
        with self.assertRaises(DomainError) as cm:
            eng.grant_credit(seller_id="B", amount="10", idem_key="g")
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION.value)


class AcceptanceTest(unittest.TestCase):
    def test_order_freezes_exact_credit_and_margin_with_explanation(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="100000", margin=("12000", "20000"))
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2", client_token="t1")
        self.assertEqual(order["status"], "accepted")
        self.assertEqual(order["notional"], "100000.00")
        self.assertEqual(order["required_margin"], "20000.00")
        col = order["collateral"]
        # 信用占用 100000，落在默认额度行
        self.assertEqual(col["credit_outstanding"], "100000.00")
        self.assertEqual(len(col["credit_lines"]), 1)
        self.assertEqual(col["credit_lines"][0]["line_id"], DEFAULT_LINE)
        # 保证金按批次 FIFO：先吃 MB-0(12000)，再吃 MB-1(8000)
        batches = col["margin_batches"]
        self.assertEqual([b["batch_id"] for b in batches], ["MB-0", "MB-1"])
        self.assertEqual([b["frozen_amount"] for b in batches],
                         ["12000.00", "8000.00"])
        # 卖方可用额度下降；买方批次视图同步
        cv = eng.credit_view("S1")
        self.assertEqual(cv["available"], "0.00")
        self.assertEqual(cv["frozen"], "100000.00")
        mv = eng.margin_view("B1")
        self.assertEqual(mv["available"], "12000.00")
        self.assertEqual(mv["total_frozen"], "20000.00")

    def test_rejected_order_explains_every_failing_rule(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="50000", margin=("5000",))
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2", client_token="t-bad")
        self.assertEqual(order["status"], "rejected")
        codes = {r["code"] for r in order["reject_reasons"]}
        self.assertIn(ErrorCode.RULE_CREDIT_LIMIT.value, codes)
        self.assertIn(ErrorCode.RULE_MARGIN_INSUFFICIENT.value, codes)
        # 拒绝原因带 required/available，可直接向申报方解释
        credit_reason = next(r for r in order["reject_reasons"]
                             if r["code"] == ErrorCode.RULE_CREDIT_LIMIT.value)
        self.assertEqual(credit_reason["required"], "100000.00")
        self.assertEqual(credit_reason["available"], "50000.00")
        # 拒绝不产生任何冻结
        self.assertEqual(eng.credit_view("S1")["frozen"], "0.00")
        self.assertEqual(eng.margin_view("B1")["total_frozen"], "0.00")

    def test_preview_does_not_write_or_freeze(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        before = eng.store.count()
        preview = eng.preview_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2")
        self.assertTrue(preview["approved"])
        rules = {r["rule"]: r for r in preview["rule_results"]}
        self.assertTrue(rules["credit_available"]["passed"])
        self.assertEqual(eng.store.count(), before)
        self.assertEqual(eng.credit_view("S1")["frozen"], "0.00")

    def test_preferred_credit_line_is_used_first(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="100000", extra_lines=("60000",))
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="500",
            unit_price="100", margin_rate="0.2", client_token="t-pref",
            preferred_lines=["L-E0"])
        self.assertEqual(order["status"], "accepted")
        lines = order["collateral"]["credit_lines"]
        self.assertEqual(lines[0]["line_id"], "L-E0")
        self.assertEqual(lines[0]["frozen_amount"], "50000.00")

    def test_single_order_notional_limit(self) -> None:
        eng = make_engine(max_notional="90000")
        bootstrap(eng)
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2", client_token="t-limit")
        codes = {r["code"] for r in order["reject_reasons"]}
        self.assertIn(ErrorCode.RULE_ORDER_LIMIT.value, codes)
        self.assertEqual(order["status"], "rejected")

    def test_unknown_seller_gives_clear_rejection(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        order = eng.submit_order(
            seller_id="GHOST", buyer_id="B1", quantity="1",
            unit_price="1", margin_rate="1", client_token="t-ghost")
        self.assertEqual(order["status"], "rejected")
        codes = {r["code"] for r in order["reject_reasons"]}
        self.assertIn(ErrorCode.NOT_FOUND.value, codes)

    def test_margin_topup_allows_new_order(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="1000000", margin=("10000",))
        bad = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2", client_token="t-old")
        self.assertEqual(bad["status"], "rejected")
        # 追加担保 = 新增独立批次
        eng.deposit_margin(buyer_id="B1", amount="20000",
                           batch_id="MB-TOP", idem_key="m-top")
        preview = eng.preview_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2")
        self.assertTrue(preview["approved"])
        # 旧 token 严格幂等：仍返回旧的拒绝单；新 token 正常受理
        retry = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2", client_token="t-old")
        self.assertTrue(retry.get("idempotent_replay"))
        self.assertEqual(retry["status"], "rejected")
        ok = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2", client_token="t-new")
        self.assertEqual(ok["status"], "accepted")

    def test_credit_cannot_be_double_promised_across_orders(self) -> None:
        """同一未交割额度不得在多订单重复承诺：第二单只能用剩余额度。"""
        eng = make_engine()
        bootstrap(eng, credit="100000", margin=("100000",))
        first = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="800",
            unit_price="100", margin_rate="1", client_token="t1")
        self.assertEqual(first["status"], "accepted")
        second = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="500",
            unit_price="100", margin_rate="1", client_token="t2")
        self.assertEqual(second["status"], "rejected")
        reason = next(r for r in second["reject_reasons"]
                      if r["code"] == ErrorCode.RULE_CREDIT_LIMIT.value)
        self.assertEqual(reason["available"], "20000.00")
        self.assertEqual(reason["required"], "50000.00")
        # 20000 的小单仍然可以用剩余额度
        third = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="200",
            unit_price="100", margin_rate="1", client_token="t3")
        self.assertEqual(third["status"], "accepted")
        self.assertEqual(eng.credit_view("S1")["frozen"], "100000.00")


class IdempotencyTest(unittest.TestCase):
    def test_submit_retry_returns_same_order_and_does_not_grow_exposure(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        kwargs = dict(seller_id="S1", buyer_id="B1", quantity="1000",
                      unit_price="100", margin_rate="0.2")
        first = eng.submit_order(client_token="dup-1", **kwargs)
        entries_after_first = eng.store.count()
        for _ in range(5):
            again = eng.submit_order(client_token="dup-1", **kwargs)
            self.assertEqual(again["order_id"], first["order_id"])
            self.assertTrue(again["idempotent_replay"])
        self.assertEqual(eng.store.count(), entries_after_first)
        self.assertEqual(eng.credit_view("S1")["frozen"], "100000.00")
        self.assertEqual(eng.margin_view("B1")["total_frozen"], "20000.00")

    def test_same_idem_key_different_body_is_conflict(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        eng.submit_order(seller_id="S1", buyer_id="B1", quantity="100",
                         unit_price="10", margin_rate="0.2",
                         client_token="tok")
        with self.assertRaises(DomainError) as cm:
            eng.submit_order(seller_id="S1", buyer_id="B1", quantity="200",
                             unit_price="10", margin_rate="0.2",
                             client_token="tok")
        self.assertEqual(cm.exception.code,
                         ErrorCode.IDEMPOTENCY_MISMATCH.value)

    def test_settle_retry_is_safe_after_completion(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="100",
            unit_price="100", margin_rate="1", client_token="t1")
        oid = order["order_id"]
        eng.settle_delivery(order_id=oid, delivered_qty="40", idem_key="s1")
        n = eng.store.count()
        # 用同一个交割幂等键反复重试，不新增分录、不报错
        for _ in range(3):
            view = eng.settle_delivery(order_id=oid, delivered_qty="40",
                                       idem_key="s1")
            self.assertEqual(view["delivered_qty"], "40.00")
        self.assertEqual(eng.store.count(), n)
        self.assertEqual(eng.credit_view("S1")["frozen"], "6000.00")

    def test_deposit_and_credit_grant_retry(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        n1 = eng.store.count()
        for _ in range(3):
            eng.deposit_margin(buyer_id="B1", amount="999",
                               batch_id="MB-X", idem_key="dep-1")
        self.assertEqual(eng.store.count(), n1 + 1)
        for _ in range(3):
            eng.grant_credit(seller_id="S1", amount="500", idem_key="gr-1")
        cv = eng.credit_view("S1")
        self.assertEqual(cv["lines"][DEFAULT_LINE]["limit"], "100500.00")


class DeliveryTest(unittest.TestCase):
    def _accepted(self, eng: CollateralEngine, token="t1", qty="1000",
                  rate="0.2"):
        return eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity=qty,
            unit_price="100", margin_rate=rate, client_token=token)

    def test_partial_delivery_releases_proportional_collateral(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = self._accepted(eng)["order_id"]
        view = eng.settle_delivery(order_id=oid, delivered_qty="600",
                                  idem_key="s1")
        self.assertEqual(view["status"], "partially_settled")
        self.assertEqual(view["delivery_progress"], "60.00%")
        # 信用释放 60%：100000 -> 未结 40000；保证金 20000 -> 未结 8000
        self.assertEqual(view["collateral"]["credit_outstanding"], "40000.00")
        self.assertEqual(view["collateral"]["margin_outstanding"], "8000.00")
        cv = eng.credit_view("S1")
        self.assertEqual(cv["available"], "60000.00")
        self.assertEqual(cv["released_cum"], "60000.00")
        mv = eng.margin_view("B1")
        # 释放的保证金重新可用（30000 总存入 - 8000 未结冻结）
        self.assertEqual(mv["available"], "22000.00")

    def test_partial_default_forfeits_and_rest_closes(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = self._accepted(eng)["order_id"]
        part = eng.settle_delivery(order_id=oid, delivered_qty="600",
                                   defaulted_qty="100", idem_key="s1")
        self.assertEqual(part["defaulted_qty"], "100.00")
        # 违约 10%：信用罚没 10000，保证金罚没 2000，且不可恢复
        line = part["collateral"]["credit_lines"][0]
        self.assertEqual(line["forfeited"], "10000.00")
        self.assertEqual(line["released"], "60000.00")
        batch = part["collateral"]["margin_batches"][0]
        self.assertEqual(batch["forfeited"], "2000.00")
        cv = eng.credit_view("S1")
        self.assertEqual(cv["forfeited_cum"], "10000.00")
        self.assertEqual(cv["frozen"], "30000.00")
        # 剩余 300 正常交割后全部结清：罚没额不回吐
        done = eng.settle_delivery(order_id=oid, delivered_qty="300",
                                   idem_key="s2")
        self.assertEqual(done["status"], "settled")
        self.assertTrue(done["closed"])
        self.assertEqual(done["collateral"]["credit_outstanding"], "0.00")
        self.assertEqual(done["collateral"]["margin_outstanding"], "0.00")
        self.assertEqual(eng.credit_view("S1")["forfeited_cum"], "10000.00")
        self.assertEqual(eng.margin_view("B1")["forfeited_cum"], "2000.00")

    def test_full_default_forfeits_everything(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = self._accepted(eng)["order_id"]
        done = eng.settle_delivery(order_id=oid, delivered_qty="0",
                                   defaulted_qty="1000", idem_key="s1")
        self.assertEqual(done["status"], "settled")
        self.assertEqual(done["collateral"]["credit_outstanding"], "0.00")
        self.assertEqual(eng.credit_view("S1")["forfeited_cum"], "100000.00")
        self.assertEqual(eng.margin_view("B1")["forfeited_cum"], "20000.00")
        self.assertEqual(eng.credit_view("S1")["available"], "0.00")
        self.assertEqual(eng.margin_view("B1")["available"], "10000.00")

    def test_overdelivery_rejected(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = self._accepted(eng)["order_id"]
        eng.settle_delivery(order_id=oid, delivered_qty="900", idem_key="s1")
        with self.assertRaises(DomainError) as cm:
            eng.settle_delivery(order_id=oid, delivered_qty="200",
                                idem_key="s2")
        self.assertEqual(cm.exception.code, ErrorCode.VALIDATION.value)

    def test_settle_after_settled_rejected_but_retry_safe(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = self._accepted(eng)["order_id"]
        eng.settle_delivery(order_id=oid, delivered_qty="1000", idem_key="s1")
        with self.assertRaises(DomainError):
            eng.settle_delivery(order_id=oid, delivered_qty="1",
                                idem_key="s-new")

    def test_cancel_refunds_remaining_freezes(self) -> None:
        eng = make_engine()
        bootstrap(eng, margin=("12000", "20000"))
        oid = self._accepted(eng)["order_id"]
        eng.settle_delivery(order_id=oid, delivered_qty="400", idem_key="s1")
        cancelled = eng.cancel_order(order_id=oid, reason="协商终止",
                                     idem_key="x1")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["closed"])
        # 剩余冻结全部退回，无罚没
        self.assertEqual(cancelled["collateral"]["credit_outstanding"], "0.00")
        self.assertEqual(cancelled["collateral"]["margin_outstanding"], "0.00")
        self.assertEqual(eng.credit_view("S1")["available"], "100000.00")
        self.assertEqual(eng.credit_view("S1")["forfeited_cum"], "0.00")
        self.assertEqual(eng.margin_view("B1")["total_frozen"], "0.00")
        for a in cancelled["collateral"]["credit_lines"] + \
                cancelled["collateral"]["margin_batches"]:
            self.assertEqual(a["status"], "refunded")

    def test_rounding_residue_is_absorbed_without_overdraft(self) -> None:
        """三个保证金批次按比例释放，舍入尾差由末笔吸收，合计精确。"""
        eng = make_engine()
        eng.register_party(party_id="S1", name="s", role="seller", idem_key="ps")
        eng.register_party(party_id="B1", name="b", role="buyer", idem_key="pb")
        eng.grant_credit(seller_id="S1", amount="100.00", idem_key="g")
        for i, amt in enumerate(("3.34", "3.33", "3.33")):
            eng.deposit_margin(buyer_id="B1", amount=amt,
                               batch_id=f"MB-{i}", idem_key=f"m{i}")
        # 1 件 @100，保证金率 10% = 10.00，冻结三批
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1",
            unit_price="100", margin_rate="0.1", client_token="t1")
        self.assertEqual(order["status"], "accepted")
        done = eng.settle_delivery(order_id=order["order_id"],
                                   delivered_qty="1", idem_key="s1")
        total_rel = sum(
            Decimal(a["released"]) for a in done["collateral"]["margin_batches"])
        self.assertEqual(total_rel, Decimal("10.00"))
        self.assertEqual(
            sum(Decimal(a["outstanding"])
                for a in done["collateral"]["margin_batches"]),
            Decimal("0.00"))


class RegulationTest(unittest.TestCase):
    def test_blocked_credit_rejects_new_orders(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        eng.set_credit_blocked(seller_id="S1", blocked=True,
                               reason="监管检查", idem_key="blk1")
        order = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1",
            unit_price="100", margin_rate="1", client_token="t1")
        self.assertEqual(order["status"], "rejected")
        codes = {r["code"] for r in order["reject_reasons"]}
        self.assertIn(ErrorCode.RULE_CREDIT_BLOCKED.value, codes)
        eng.set_credit_blocked(seller_id="S1", blocked=False,
                               reason="检查通过", idem_key="blk2")
        ok = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1",
            unit_price="100", margin_rate="1", client_token="t2")
        self.assertEqual(ok["status"], "accepted")

    def test_regulator_reduction_flags_distressed_but_keeps_history(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="100000", margin=("100000",))
        oid = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="1", client_token="t1")["order_id"]
        # 监管降额 40000：限额 60000 < 冻结 100000 -> distressed
        view = eng.reduce_credit_limit(
            seller_id="S1", reduction="40000", reason="监管降额",
            idem_key="r1")
        self.assertEqual(view["total_limit"], "60000.00")
        self.assertTrue(view["distressed"])
        # 历史冻结分录原样保留，订单仍可继续交割释放
        part = eng.settle_delivery(order_id=oid, delivered_qty="500",
                                   idem_key="s1")
        self.assertEqual(part["collateral"]["credit_outstanding"], "50000.00")
        # 降额后新订单：可用额度 = 60000 - 50000 = 10000
        preview = eng.preview_order(
            seller_id="S1", buyer_id="B1", quantity="200",
            unit_price="100", margin_rate="1")
        self.assertFalse(preview["approved"])
        small = eng.preview_order(
            seller_id="S1", buyer_id="B1", quantity="100",
            unit_price="100", margin_rate="1")
        self.assertTrue(small["approved"])

    def test_reduction_cannot_make_limit_negative(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        with self.assertRaises(DomainError):
            eng.reduce_credit_limit(seller_id="S1", reduction="999999",
                                    reason="x", idem_key="r1")

    def test_line_specific_reduction(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="100000", extra_lines=("50000",),
                  margin=("1000000",))
        # 默认行降 30000，专项行 L-E0 降 20000，合计 50000
        view = eng.reduce_credit_limit(
            seller_id="S1", reduction="50000",
            line_reductions={"L-E0": "20000"},
            reason="专项压降", idem_key="r1")
        self.assertEqual(view["total_limit"], "100000.00")
        self.assertEqual(view["lines"]["L-E0"]["limit"], "30000.00")
        self.assertEqual(view["lines"][DEFAULT_LINE]["limit"], "70000.00")


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_orders_never_oversell_credit(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="100000", margin=("1000000",))

        def submit(i: int) -> str:
            # 每单名义 30000，100 个线程抢 100000 额度 -> 恰好 3 单成交，
            # 第 4 单起都只能看到 <= 10000 可用额度而被拒
            order = eng.submit_order(
                seller_id="S1", buyer_id="B1", quantity="300",
                unit_price="100", margin_rate="1", client_token=f"c-{i}")
            return order["status"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            statuses = list(pool.map(submit, range(100)))
        accepted = [s for s in statuses if s == "accepted"]
        rejected = [s for s in statuses if s == "rejected"]
        self.assertEqual(len(accepted), 3)
        self.assertEqual(len(rejected), 97)
        self.assertEqual(eng.credit_view("S1")["frozen"], "90000.00")
        self.assertLessEqual(
            Decimal(eng.credit_view("S1")["frozen"]),
            Decimal(eng.credit_view("S1")["total_limit"]))

    def test_concurrent_same_token_single_acceptance(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        results: list[str] = []
        lock = threading.Lock()

        def submit() -> None:
            order = eng.submit_order(
                seller_id="S1", buyer_id="B1", quantity="100",
                unit_price="100", margin_rate="1", client_token="same-token")
            with lock:
                results.append(order["order_id"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(lambda _: submit(), range(20)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(eng.credit_view("S1")["frozen"], "10000.00")

    def test_concurrent_deposits_all_persist(self) -> None:
        eng = make_engine()
        bootstrap(eng)

        def deposit(i: int) -> None:
            eng.deposit_margin(buyer_id="B1", amount="100",
                               batch_id=f"MB-C{i}", idem_key=f"d-{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(deposit, range(50)))
        mv = eng.margin_view("B1")
        self.assertEqual(len(mv["batches"]), 51)  # 启动 1 批 + 50 批


class LedgerIntegrityTest(unittest.TestCase):
    def test_append_only_triggers_block_update_and_delete(self) -> None:
        store = EntryStore(":memory:")
        store.append(entry_id="e1", entry_type=__import__(
            "collateral_service.models", fromlist=["EntryType"]).EntryType
            .PARTY_REGISTERED, actor="a", payload={"x": 1})
        with self.assertRaises(sqlite3.DatabaseError):
            store._conn.execute("UPDATE entries SET actor='hacker' WHERE seq=1")
        with self.assertRaises(sqlite3.DatabaseError):
            store._conn.execute("DELETE FROM entries WHERE seq=1")

    def test_hash_chain_rebuilds_identical_state(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="1000",
            unit_price="100", margin_rate="0.2",
            client_token="t1")["order_id"]
        eng.settle_delivery(order_id=oid, delivered_qty="300",
                            defaulted_qty="50", idem_key="s1")
        self.assertEqual(eng.store.verify_chain(), [])
        rebuilt = LedgerState()
        rebuilt.replay(eng.store)
        self.assertEqual(rebuilt.credit("S1").frozen,
                         eng.state.credit("S1").frozen)
        self.assertEqual(
            rebuilt.order(oid).delivered_qty,
            eng.state.order(oid).delivered_qty)
        self.assertEqual(
            {a.alloc_id: (a.released, a.forfeited)
             for a in rebuilt.order(oid).allocations.values()},
            {a.alloc_id: (a.released, a.forfeited)
             for a in eng.state.order(oid).allocations.values()})

    def test_line_limits_always_sum_to_total(self) -> None:
        eng = make_engine()
        bootstrap(eng, credit="100000", extra_lines=("40000", "10000"),
                  margin=("1000000",))
        eng.submit_order(seller_id="S1", buyer_id="B1", quantity="100",
                         unit_price="100", margin_rate="1",
                         client_token="t1")
        eng.reduce_credit_limit(seller_id="S1", reduction="30000",
                                line_reductions={"L-E0": "10000"},
                                reason="r", idem_key="r1")
        cv = eng.credit_view("S1")
        total_lines = sum(
            (Decimal(v["limit"]) for v in cv["lines"].values()), Decimal("0"))
        self.assertEqual(total_lines, Decimal(cv["total_limit"]))

    def test_ledger_view_per_order_explains_all_entries(self) -> None:
        eng = make_engine()
        bootstrap(eng)
        oid = eng.submit_order(
            seller_id="S1", buyer_id="B1", quantity="100",
            unit_price="100", margin_rate="1", client_token="t1")["order_id"]
        eng.settle_delivery(order_id=oid, delivered_qty="100", idem_key="s1")
        view = eng.ledger_view(order_id=oid)
        self.assertTrue(view["chain_intact"])
        types = [e["entry_type"] for e in view["entries"]]
        self.assertEqual(types,
                         ["order_submitted", "order_accepted",
                          "delivery_settled", "order_closed"])
        # 每条 accepted 分录都能指回具体来源
        accepted = next(e for e in view["entries"]
                        if e["entry_type"] == "order_accepted")
        sources = {(a["source_type"], a["source_id"])
                   for a in accepted["payload"]["allocations"]}
        self.assertIn(("credit_line", DEFAULT_LINE), sources)


class RandomizedSettlementPropertyTest(unittest.TestCase):
    """固定种子的随机化属性测试：任意额度行/批次结构与任意交割-违约序列下，
    摊分都必须满足不超冻结、无残留、合计精确三个不变量。"""

    def test_random_partial_settlement_sequences(self) -> None:
        import random

        rng = random.Random(20261004)
        for trial in range(80):
            eng = make_engine("1000000000")
            eng.register_party(party_id="S", name="s", role="seller",
                               idem_key=f"ps-{trial}")
            eng.register_party(party_id="B", name="b", role="buyer",
                               idem_key=f"pb-{trial}")
            eng.grant_credit(seller_id="S",
                             amount=rng.choice(["100000", "1000000", "500000"]),
                             idem_key=f"g-{trial}")
            for i in range(rng.randint(0, 4)):
                eng.register_credit_line(
                    seller_id="S",
                    line_id=f"L{trial}-{i}",
                    amount=str(rng.randint(1000, 200000)),
                    idem_key=f"gl-{trial}-{i}")
            for i in range(rng.randint(1, 5)):
                eng.deposit_margin(
                    buyer_id="B",
                    amount=f"{rng.randint(100, 90000)}.{rng.randint(0, 99):02d}",
                    batch_id=f"MB{trial}-{i}", idem_key=f"m-{trial}-{i}")
            qty = Decimal(rng.randint(1, 300))
            price = Decimal(f"{rng.randint(1, 500)}.{rng.randint(0, 99):02d}")
            rate = Decimal(rng.choice(["0.1", "0.2", "0.5", "1"]))
            order = eng.submit_order(
                seller_id="S", buyer_id="B", quantity=str(qty),
                unit_price=str(price), margin_rate=str(rate),
                client_token=f"t-{trial}")
            if order["status"] != "accepted":
                continue
            oid = order["order_id"]
            n = int(qty)
            seg = rng.randint(1, 5)
            bounds = sorted(rng.sample(range(1, n), min(seg - 1, n - 1))) if n > 1 else []
            pts = [0] + bounds + [n]
            for k in range(len(pts) - 1):
                size = pts[k + 1] - pts[k]
                defaulted = rng.randint(0, size)
                view = eng.settle_delivery(
                    order_id=oid, delivered_qty=str(size - defaulted),
                    defaulted_qty=str(defaulted),
                    idem_key=f"s-{trial}-{k}")
                for pool_key in ("credit_lines", "margin_batches"):
                    for a in view["collateral"][pool_key]:
                        released = Decimal(a["released"])
                        forfeited = Decimal(a["forfeited"])
                        frozen = Decimal(a["frozen_amount"])
                        self.assertGreaterEqual(released, 0)
                        self.assertGreaterEqual(forfeited, 0)
                        self.assertLessEqual(
                            released + forfeited, frozen + Decimal("0.001"))
            final = eng.order_view(oid)
            col = final["collateral"]
            self.assertEqual(col["credit_outstanding"], "0.00")
            self.assertEqual(col["margin_outstanding"], "0.00")
            self.assertTrue(final["closed"])
            for pool_key, total_key in (
                ("credit_lines", "credit_occupied_total"),
                ("margin_batches", "margin_occupied_total"),
            ):
                total = sum(
                    (Decimal(a["released"]) + Decimal(a["forfeited"])
                     for a in col[pool_key]), Decimal("0"))
                self.assertEqual(total, Decimal(col[total_key]))


if __name__ == "__main__":
    unittest.main()
