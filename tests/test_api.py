"""担保风控 HTTP 接口的端到端测试。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guarantee_service.api import create_server
from guarantee_service.ledger import Ledger
from guarantee_service.service import GuaranteeService


class ApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ledger = Ledger()
        cls.service = GuaranteeService(cls.ledger)
        cls.httpd = create_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.ledger.close()

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiFlowTest(ApiTestBase):
    def test_full_trading_flow_over_http(self) -> None:
        status, line = self.call("POST", "/credit-lines", {"line_id": "CL-A", "owner_id": "seller-a", "limit": "2000.00"})
        self.assertEqual(status, 200)
        self.assertEqual(line["available"], "2000.00")
        status, batch = self.call("POST", "/margin-batches", {"batch_id": "MB-A", "owner_id": "buyer-a", "amount": "800.00"})
        self.assertEqual(status, 200)

        order_payload = {
            "request_id": "http-req-1",
            "order_id": "O-HTTP-1",
            "seller_id": "seller-a",
            "buyer_id": "buyer-a",
            "quantity": 20,
            "price": "25.00",
        }
        status, accepted = self.call("POST", "/orders", order_payload)
        self.assertEqual(status, 200)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["exposure"], "500.00")
        self.assertEqual(accepted["margin_required"], "100.00")
        self.assertEqual(len(accepted["allocations"]), 2)

        # 重试同一 request_id：响应一致，敞口不变
        status, replayed = self.call("POST", "/orders", order_payload)
        self.assertEqual(status, 200)
        self.assertEqual(replayed, accepted)
        status, line = self.call("GET", "/credit-lines/CL-A")
        self.assertEqual(line["frozen"], "500.00")

        # 订单解释：占用哪些额度一目了然
        status, explained = self.call("GET", "/orders/O-HTTP-1")
        self.assertEqual(status, 200)
        self.assertEqual(explained["allocations"][0]["source_id"], "CL-A")
        self.assertIn("CL-A", explained["explanation"])

        # 交割一半 -> 释放一半
        status, delivery = self.call("POST", "/orders/O-HTTP-1/deliveries", {"quantity": 10})
        self.assertEqual(delivery["status"], "delivering")
        self.assertEqual(delivery["released_total"], "300.00")
        status, line = self.call("GET", "/credit-lines/CL-A")
        self.assertEqual(line["frozen"], "250.00")

        # 部分违约 -> 罚没对应比例
        status, default = self.call("POST", "/orders/O-HTTP-1/defaults", {"quantity": 5, "reason": "交割逾期"})
        self.assertEqual(default["seized_total"], "150.00")
        status, batch = self.call("GET", "/margin-batches/MB-A")
        self.assertEqual(batch["amount"], "775.00")  # 罚没 25.00（保证金 100 的 5/20）划转清算

        # 监管降额 -> 冻结 125 超过新限额 100，击穿标记
        status, cut = self.call("POST", "/credit-lines/CL-A/adjustments", {"delta": "-1900.00", "reason": "regulatory_cut"})
        self.assertTrue(cut["breached"])
        self.assertEqual(cut["breach_amount"], "25.00")

        # 主体汇总与审计分录
        status, summary = self.call("GET", "/accounts/seller-a/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["totals"]["credit_frozen"], "125.00")
        status, ledger_view = self.call("GET", "/ledger")
        kinds = [e["kind"] for e in ledger_view["entries"]]
        for expected in ("credit_line_registered", "order_accepted", "delivery_recorded", "default_recorded", "credit_line_adjusted"):
            self.assertIn(expected, kinds)

    def test_rejection_explains_reasons_over_http(self) -> None:
        self.call("POST", "/credit-lines", {"line_id": "CL-R", "owner_id": "seller-r", "limit": "100.00"})
        self.call("POST", "/margin-batches", {"batch_id": "MB-R", "owner_id": "buyer-r", "amount": "10.00"})
        status, outcome = self.call(
            "POST",
            "/orders",
            {
                "request_id": "http-req-r",
                "order_id": "O-HTTP-R",
                "seller_id": "seller-r",
                "buyer_id": "buyer-r",
                "quantity": 50,
                "price": "10.00",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(outcome["status"], "rejected")
        codes = [r["code"] for r in outcome["reasons"]]
        self.assertIn("INSUFFICIENT_CREDIT", codes)
        self.assertIn("INSUFFICIENT_MARGIN", codes)
        status, line = self.call("GET", "/credit-lines/CL-R")
        self.assertEqual(line["frozen"], "0.00")

    def test_error_responses(self) -> None:
        status, body = self.call("GET", "/orders/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")
        status, body = self.call("POST", "/orders", {"request_id": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_PARAMS")
        status, body = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.call("POST", "/credit-lines", {"line_id": "CL-ERR", "owner_id": "seller-err", "limit": "1.00"})
        status, body = self.call("POST", "/credit-lines", {"line_id": "CL-ERR", "owner_id": "seller-err", "limit": "1.00"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "DUPLICATE_ID")

    def test_concurrent_orders_over_http_never_overcommit(self) -> None:
        self.call("POST", "/credit-lines", {"line_id": "CL-C", "owner_id": "seller-c", "limit": "500.00"})
        self.call("POST", "/margin-batches", {"batch_id": "MB-C", "owner_id": "buyer-c", "amount": "100000.00"})
        results: list[dict] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            _status, outcome = self.call(
                "POST",
                "/orders",
                {
                    "request_id": f"http-c{i}",
                    "order_id": f"O-HTTP-C{i}",
                    "seller_id": "seller-c",
                    "buyer_id": "buyer-c",
                    "quantity": 10,
                    "price": "10.00",
                },
            )
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        accepted = [o for o in results if o["status"] == "accepted"]
        self.assertEqual(len(accepted), 5)
        status, line = self.call("GET", "/credit-lines/CL-C")
        self.assertEqual(line["frozen"], "500.00")
        self.assertEqual(line["available"], "0.00")


if __name__ == "__main__":
    unittest.main()
