"""担保风控服务的领域行为回归测试。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guarantee_service.ledger import Ledger
from guarantee_service.service import GuaranteeService, ServiceError


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger()
        self.svc = GuaranteeService(self.ledger)
        self.svc.register_credit_line({"line_id": "CL-S1", "owner_id": "seller-1", "limit": "1000.00"})
        self.svc.register_margin_batch({"batch_id": "MB-B1", "owner_id": "buyer-1", "amount": "500.00"})

    def tearDown(self) -> None:
        self.ledger.close()

    def place_order(self, **override) -> dict:
        payload = {
            "request_id": "req-1",
            "order_id": "O-1",
            "seller_id": "seller-1",
            "buyer_id": "buyer-1",
            "quantity": 10,
            "price": "10.00",
        }
        payload.update(override)
        return self.svc.accept_order(payload)


class AcceptOrderTest(ServiceTestBase):
    def test_accept_freezes_and_explains(self) -> None:
        outcome = self.place_order()
        self.assertEqual(outcome["status"], "accepted")
        self.assertEqual(outcome["notional"], "100.00")
        self.assertEqual(outcome["exposure"], "100.00")
        self.assertEqual(outcome["margin_required"], "20.00")
        self.assertEqual(
            outcome["allocations"],
            [
                {"source_kind": "credit_line", "source_id": "CL-S1", "amount": "100.00"},
                {"source_kind": "margin_batch", "source_id": "MB-B1", "amount": "20.00"},
            ],
        )
        self.assertIn("CL-S1", outcome["explanation"])
        self.assertIn("MB-B1", outcome["explanation"])

        line = self.svc.credit_line_view("CL-S1")
        self.assertEqual((line["frozen"], line["available"]), ("100.00", "900.00"))
        self.assertEqual(line["frozen_by"], [{"order_id": "O-1", "outstanding": "100.00"}])
        batch = self.svc.margin_batch_view("MB-B1")
        self.assertEqual((batch["frozen"], batch["available"]), ("20.00", "480.00"))

        explained = self.svc.explain_order("O-1")
        self.assertEqual(explained["frozen_outstanding"], "120.00")
        self.assertEqual(explained["delivery"]["outstanding"], 10)
        self.assertEqual([e["kind"] for e in explained["events"]], ["order_accepted"])

    def test_rejection_explains_reason_and_freezes_nothing(self) -> None:
        outcome = self.place_order(request_id="req-big", order_id="O-BIG", quantity=2000)
        self.assertEqual(outcome["status"], "rejected")
        codes = [r["code"] for r in outcome["reasons"]]
        self.assertIn("INSUFFICIENT_CREDIT", codes)
        self.assertIn("INSUFFICIENT_MARGIN", codes)
        credit_reason = next(r for r in outcome["reasons"] if r["code"] == "INSUFFICIENT_CREDIT")
        self.assertEqual(credit_reason["details"]["shortfall"], "19000.00")
        self.assertIn("信用额度不足", outcome["explanation"])

        # 拒绝不落任何冻结，但留下不可变的拒绝分录供审计与重放
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "0.00")
        self.assertEqual(self.svc.margin_batch_view("MB-B1")["frozen"], "0.00")
        explained = self.svc.explain_order("O-BIG")
        self.assertEqual(explained["status"], "rejected")
        self.assertEqual([e["kind"] for e in explained["events"]], ["order_rejected"])

    def test_unknown_accounts_rejected(self) -> None:
        outcome = self.place_order(request_id="req-x", order_id="O-X", seller_id="ghost")
        self.assertEqual(outcome["status"], "rejected")
        self.assertIn("NO_CREDIT_LINE", [r["code"] for r in outcome["reasons"]])

    def test_retry_replays_outcome_without_expanding_exposure(self) -> None:
        first = self.place_order()
        entries_before = len(self.ledger.entries())
        for _ in range(3):
            again = self.place_order()
            self.assertEqual(again, first)
        self.assertEqual(len(self.ledger.entries()), entries_before)
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "100.00")

        # 被拒绝的订单重试同样重放，不产生新分录
        rejected = self.place_order(request_id="req-big", order_id="O-BIG", quantity=2000)
        entries_before = len(self.ledger.entries())
        self.assertEqual(self.place_order(request_id="req-big", order_id="O-BIG", quantity=2000), rejected)
        self.assertEqual(len(self.ledger.entries()), entries_before)

    def test_same_request_id_with_different_payload_conflicts(self) -> None:
        self.place_order()
        with self.assertRaises(ServiceError) as ctx:
            self.place_order(quantity=11)
        self.assertEqual(ctx.exception.code, "REQUEST_ID_CONFLICT")

    def test_duplicate_order_id_conflicts(self) -> None:
        self.place_order()
        with self.assertRaises(ServiceError) as ctx:
            self.place_order(request_id="req-2")
        self.assertEqual(ctx.exception.code, "DUPLICATE_ORDER_ID")

    def test_concurrent_orders_never_overcommit(self) -> None:
        # 额度 1000，单笔敞口 100：恰好只能成交 10 笔
        accepted: list[dict] = []
        rejected: list[dict] = []
        barrier = threading.Barrier(16)

        def worker(i: int) -> None:
            barrier.wait()
            outcome = self.place_order(request_id=f"req-c{i}", order_id=f"O-C{i}")
            (accepted if outcome["status"] == "accepted" else rejected).append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(accepted), 10)
        self.assertEqual(len(rejected), 6)
        self.assertTrue(all(any(r["code"] == "INSUFFICIENT_CREDIT" for r in o["reasons"]) for o in rejected))
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "1000.00")
        # 敞口总额绝不超过额度上限
        self.assertLessEqual(
            sum(float(o["exposure"]) for o in accepted),
            float(self.svc.credit_line_view("CL-S1")["limit"]),
        )


class DeliveryAndDefaultTest(ServiceTestBase):
    def test_delivery_releases_by_progress_and_settles_exactly(self) -> None:
        self.place_order()
        first = self.svc.record_delivery("O-1", {"quantity": 4})
        self.assertEqual(first["status"], "delivering")
        self.assertEqual(first["released_total"], "48.00")  # 40% 的 100 + 20
        line = self.svc.credit_line_view("CL-S1")
        self.assertEqual((line["frozen"], line["available"]), ("60.00", "940.00"))

        final = self.svc.record_delivery("O-1", {"quantity": 6})
        self.assertEqual(final["status"], "settled")
        self.assertEqual(final["released_total"], "72.00")
        # 全部释放，无尾差
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "0.00")
        self.assertEqual(self.svc.margin_batch_view("MB-B1")["frozen"], "0.00")
        explained = self.svc.explain_order("O-1")
        for alloc in explained["allocations"]:
            self.assertEqual(alloc["released"], alloc["amount"])
            self.assertEqual(alloc["outstanding"], "0.00")

    def test_delivery_retry_does_not_release_twice(self) -> None:
        self.place_order()
        first = self.svc.record_delivery("O-1", {"quantity": 4, "request_id": "d-1"})
        again = self.svc.record_delivery("O-1", {"quantity": 4, "request_id": "d-1"})
        self.assertEqual(again, first)
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "60.00")

    def test_partial_default_seizes_proportionally_and_conserves(self) -> None:
        self.place_order()
        self.svc.record_delivery("O-1", {"quantity": 2})  # 释放 20%
        outcome = self.svc.record_default("O-1", {"quantity": 3, "reason": "买方逾期未补保证金"})
        self.assertEqual(outcome["status"], "delivering")
        self.assertEqual(outcome["seized_total"], "36.00")  # 30% 的 100 + 20
        batch = self.svc.margin_batch_view("MB-B1")
        self.assertEqual(batch["amount"], "494.00")  # 罚没 6.00 划转清算
        self.assertEqual(batch["seized_total"], "6.00")
        self.assertEqual(self.svc.credit_line_view("CL-S1")["seized_total"], "30.00")

        final = self.svc.record_delivery("O-1", {"quantity": 5})
        self.assertEqual(final["status"], "settled")
        # 每笔冻结 = 已释放 + 已罚没，守恒
        for alloc in self.svc.explain_order("O-1")["allocations"]:
            released = float(alloc["released"])
            seized = float(alloc["seized"])
            self.assertAlmostEqual(released + seized, float(alloc["amount"]), places=2)

    def test_default_closing_order_marks_defaulted(self) -> None:
        self.place_order()
        outcome = self.svc.record_default("O-1", {"quantity": 10})
        self.assertEqual(outcome["status"], "defaulted")
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "0.00")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.record_delivery("O-1", {"quantity": 1})
        self.assertEqual(ctx.exception.code, "ORDER_CLOSED")

    def test_over_delivery_rejected(self) -> None:
        self.place_order()
        with self.assertRaises(ServiceError):
            self.svc.record_delivery("O-1", {"quantity": 11})

    def test_cancel_releases_remaining(self) -> None:
        self.place_order()
        self.svc.record_delivery("O-1", {"quantity": 4})
        outcome = self.svc.cancel_order("O-1", {})
        self.assertEqual(outcome["status"], "cancelled")
        self.assertEqual(outcome["released_total"], "72.00")
        self.assertEqual(self.svc.credit_line_view("CL-S1")["frozen"], "0.00")
        self.assertEqual(self.svc.margin_batch_view("MB-B1")["frozen"], "0.00")


class AdjustmentTest(ServiceTestBase):
    def test_regulatory_cut_flags_breach_and_blocks_new_orders(self) -> None:
        self.place_order()  # 冻结 100
        outcome = self.svc.adjust_credit_line("CL-S1", {"delta": "-950.00", "reason": "regulatory_cut"})
        self.assertEqual(outcome["limit"], "50.00")
        self.assertTrue(outcome["breached"])
        self.assertEqual(outcome["breach_amount"], "50.00")

        rejected = self.place_order(request_id="req-2", order_id="O-2", quantity=1)
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("CREDIT_LINE_BREACHED", [r["code"] for r in rejected["reasons"]])

        # 追加担保后解除击穿，可重新下单
        topup = self.svc.adjust_credit_line("CL-S1", {"delta": "100.00", "reason": "topup"})
        self.assertFalse(topup["breached"])
        accepted = self.place_order(request_id="req-3", order_id="O-3", quantity=1)
        self.assertEqual(accepted["status"], "accepted")

    def test_adjustment_reason_sign_enforced(self) -> None:
        with self.assertRaises(ServiceError):
            self.svc.adjust_credit_line("CL-S1", {"delta": "10.00", "reason": "regulatory_cut"})
        with self.assertRaises(ServiceError):
            self.svc.adjust_credit_line("CL-S1", {"delta": "-10.00", "reason": "topup"})
        with self.assertRaises(ServiceError):
            self.svc.adjust_credit_line("CL-S1", {"delta": "10.00", "reason": "whatever"})

    def test_margin_withdrawal_only_from_available(self) -> None:
        self.place_order()  # 冻结 20，可用 480
        with self.assertRaises(ServiceError) as ctx:
            self.svc.adjust_margin_batch("MB-B1", {"delta": "-490.00", "reason": "withdrawal"})
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_AVAILABLE")
        outcome = self.svc.adjust_margin_batch("MB-B1", {"delta": "-480.00", "reason": "withdrawal"})
        self.assertEqual(outcome["amount"], "20.00")

    def test_margin_topup(self) -> None:
        outcome = self.svc.adjust_margin_batch("MB-B1", {"delta": "300.00", "reason": "topup"})
        self.assertEqual(outcome["amount"], "800.00")
        self.assertEqual(outcome["available"], "800.00")


class RuleAndPersistenceTest(ServiceTestBase):
    def test_rule_version_snapshot_recorded_on_order(self) -> None:
        self.svc.register_rule(
            {
                "rule_id": "standard",
                "margin_rate": "0.5",
                "exposure_rate": "1",
                "max_order_notional": "100000000",
                "max_seller_utilization": "1",
            }
        )
        outcome = self.place_order()
        self.assertEqual(outcome["rule"]["version"], 2)
        self.assertEqual(outcome["margin_required"], "50.00")

    def test_persistence_replay_restores_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"
            ledger = Ledger(path)
            svc = GuaranteeService(ledger)
            svc.register_credit_line({"line_id": "CL-S1", "owner_id": "seller-1", "limit": "1000.00"})
            svc.register_margin_batch({"batch_id": "MB-B1", "owner_id": "buyer-1", "amount": "500.00"})
            svc.accept_order(
                {
                    "request_id": "req-1",
                    "order_id": "O-1",
                    "seller_id": "seller-1",
                    "buyer_id": "buyer-1",
                    "quantity": 10,
                    "price": "10.00",
                }
            )
            svc.record_delivery("O-1", {"quantity": 4})
            before_line = svc.credit_line_view("CL-S1")
            before_order = svc.explain_order("O-1")
            ledger.close()

            # 重启：重放分录后状态完全一致，且幂等索引恢复（重试仍不扩大敞口）
            ledger2 = Ledger(path)
            svc2 = GuaranteeService(ledger2)
            self.assertEqual(svc2.credit_line_view("CL-S1"), before_line)
            self.assertEqual(svc2.explain_order("O-1"), before_order)
            entries_before = len(ledger2.entries())
            replayed = svc2.accept_order(
                {
                    "request_id": "req-1",
                    "order_id": "O-1",
                    "seller_id": "seller-1",
                    "buyer_id": "buyer-1",
                    "quantity": 10,
                    "price": "10.00",
                }
            )
            self.assertEqual(replayed["status"], "accepted")
            self.assertEqual(len(ledger2.entries()), entries_before)
            ledger2.close()


if __name__ == "__main__":
    unittest.main()
