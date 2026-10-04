"""HTTP 接口端到端测试 + SQLite 落盘重启回放测试。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collateral_service.server import serve_forever_in_thread
from collateral_service.engine import CollateralEngine, RiskConfig
from collateral_service.store import EntryStore


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd, self.base = serve_forever_in_thread(port=0)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd.store.close()

    def call(self, method: str, path: str, body=None, idem: str | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http_with_explanations(self) -> None:
        # 登记
        s, _ = self.call("POST", "/v1/parties",
                         {"party_id": "S1", "name": "卖方", "role": "seller"},
                         idem="p1")
        self.assertEqual(s, 201)
        s, _ = self.call("POST", "/v1/parties",
                         {"party_id": "B1", "name": "买方", "role": "buyer"},
                         idem="p2")
        self.assertEqual(s, 201)
        # 额度与保证金
        self.assertEqual(self.call("POST", "/v1/sellers/S1/credit",
                                   {"amount": "100000"}, idem="g1")[0], 200)
        self.assertEqual(self.call("POST", "/v1/buyers/B1/margin",
                                   {"amount": "30000", "batch_id": "MB-1"},
                                   idem="m1")[0], 201)

        # 预检：解释每条规则与拟占用
        s, preview = self.call("POST", "/v1/orders/preview", {
            "seller_id": "S1", "buyer_id": "B1", "quantity": "1000",
            "unit_price": "100", "margin_rate": "0.2"})
        self.assertEqual(s, 200)
        self.assertTrue(preview["approved"])
        self.assertEqual(preview["planned_credit"][0]["line_id"], "L-DEFAULT")
        self.assertEqual(preview["planned_credit"][0]["amount"], "100000.00")
        self.assertEqual(preview["planned_margin"][0]["amount"], "20000.00")

        # 受理
        s, order = self.call("POST", "/v1/orders", {
            "seller_id": "S1", "buyer_id": "B1", "quantity": "1000",
            "unit_price": "100", "margin_rate": "0.2",
            "client_token": "ORD-1"})
        self.assertEqual(s, 201)
        oid = order["order_id"]
        self.assertEqual(order["status"], "accepted")

        # 第二单超额：HTTP 201 + status=rejected + 明确拒绝原因
        s, bad = self.call("POST", "/v1/orders", {
            "seller_id": "S1", "buyer_id": "B1", "quantity": "1000",
            "unit_price": "100", "margin_rate": "0.2",
            "client_token": "ORD-2"})
        self.assertEqual(s, 201)
        self.assertEqual(bad["status"], "rejected")
        self.assertTrue(bad["reject_reasons"])

        # 订单视图解释占用了哪些额度/批次
        s, view = self.call("GET", f"/v1/orders/{oid}")
        self.assertEqual(s, 200)
        self.assertEqual(view["collateral"]["credit_lines"][0]["line_id"],
                         "L-DEFAULT")
        self.assertEqual(view["collateral"]["margin_batches"][0]["batch_id"],
                         "MB-1")

        # 部分违约交割
        s, settled = self.call("POST", f"/v1/orders/{oid}/settle", {
            "delivered_qty": "700", "defaulted_qty": "100"}, idem="ST-1")
        self.assertEqual(s, 200)
        self.assertEqual(settled["delivery_progress"], "80.00%")

        # 重试同一幂等键：结果一致，不新增分录
        s, retry = self.call("POST", f"/v1/orders/{oid}/settle", {
            "delivered_qty": "700", "defaulted_qty": "100"}, idem="ST-1")
        self.assertEqual(s, 200)
        self.assertEqual(retry["delivered_qty"], "700.00")

        s, ledger = self.call("GET", "/v1/ledger?order_id=" + oid)
        self.assertTrue(ledger["chain_intact"])

    def test_rejected_without_idem_key_is_400(self) -> None:
        status, body = self.call("POST", "/v1/buyers/B1/margin",
                                 {"amount": "10"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "VALIDATION")

    def test_idempotency_conflict_detected(self) -> None:
        self.call("POST", "/v1/parties",
                  {"party_id": "S", "role": "seller"}, idem="pp")
        self.call("POST", "/v1/parties",
                  {"party_id": "B", "role": "buyer"}, idem="pb")
        self.call("POST", "/v1/sellers/S/credit",
                  {"amount": "1000"}, idem="cg")
        self.call("POST", "/v1/buyers/B/margin",
                  {"amount": "1000", "batch_id": "MB"}, idem="md")
        self.call("POST", "/v1/orders", {
            "seller_id": "S", "buyer_id": "B", "quantity": "1",
            "unit_price": "100", "margin_rate": "1", "client_token": "T"})
        # 同 token 不同请求体
        status, body = self.call("POST", "/v1/orders", {
            "seller_id": "S", "buyer_id": "B", "quantity": "2",
            "unit_price": "100", "margin_rate": "1", "client_token": "T"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "IDEMPOTENCY_MISMATCH")


class PersistenceTest(unittest.TestCase):
    def test_rebuild_from_sqlite_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.db")
            store = EntryStore(path)
            eng = CollateralEngine(store, RiskConfig("10000000"))
            eng.register_party(party_id="S1", name="s", role="seller",
                               idem_key="p1")
            eng.register_party(party_id="B1", name="b", role="buyer",
                               idem_key="p2")
            eng.grant_credit(seller_id="S1", amount="100000", idem_key="g1")
            eng.deposit_margin(buyer_id="B1", amount="30000",
                               batch_id="MB1", idem_key="m1")
            oid = eng.submit_order(
                seller_id="S1", buyer_id="B1", quantity="1000",
                unit_price="100", margin_rate="0.2",
                client_token="t1")["order_id"]
            eng.settle_delivery(order_id=oid, delivered_qty="400",
                                defaulted_qty="100", idem_key="s1")
            store.close()

            # 重新打开：从只追加分录完整重建
            store2 = EntryStore(path)
            self.assertEqual(store2.verify_chain(), [])
            eng2 = CollateralEngine(store2, RiskConfig("10000000"))
            view = eng2.order_view(oid)
            self.assertEqual(view["status"], "partially_settled")
            self.assertEqual(view["collateral"]["credit_outstanding"],
                             "50000.00")
            self.assertEqual(eng2.credit_view("S1")["forfeited_cum"],
                             "10000.00")
            store2.close()


if __name__ == "__main__":
    unittest.main()
