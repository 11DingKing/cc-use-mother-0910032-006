"""线程化 HTTP JSON 接口（仅标准库）。

路由：

- POST /v1/parties                 登记主体（企业申报员）
- POST /v1/sellers/{id}/credit     授予/追加授信
- POST /v1/sellers/{id}/lines      登记专项额度行
- POST /v1/sellers/{id}/block      监管冻结/解冻
- POST /v1/sellers/{id}/reduce     监管降额
- GET  /v1/sellers/{id}/credit     信用账户视图
- POST /v1/buyers/{id}/margin      存入保证金批次（追加担保）
- GET  /v1/buyers/{id}/margin      保证金批次视图
- POST /v1/orders/preview          不落账预检（逐规则解释）
- POST /v1/orders                  受理订单（评估+冻结，原子）
- GET  /v1/orders/{id}             订单敞口与逐笔占用解释
- POST /v1/orders/{id}/settle      交割/部分违约，按进度释放
- POST /v1/orders/{id}/cancel      撤单，剩余冻结退回
- GET  /v1/ledger                  分录与哈希链审计视图
- GET  /healthz

幂等：写接口要求 ``Idempotency-Key`` 头（下单用 body.client_token）。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .engine import CollateralEngine, RiskConfig
from .models import DomainError
from .store import EntryStore


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    engine: CollateralEngine  # 由 make_server 注入到类上

    server_version = "CollateralRisk/0.2"

    # ------------------------------------------------------------------ 工具

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, default=_json_default).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DomainError("VALIDATION", 400, f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise DomainError("VALIDATION", 400, "请求体必须是 JSON 对象")
        return value

    def _idem(self, body: dict[str, Any]) -> str:
        key = self.headers.get("Idempotency-Key") or body.pop("idem_key", None)
        if not key:
            raise DomainError("VALIDATION", 400,
                              "写操作必须提供 Idempotency-Key 头或 idem_key 字段")
        return str(key)

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        if os.environ.get("COLLATERAL_HTTP_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            for pattern, verbs, fn in ROUTES:
                if method in verbs:
                    kwargs = _match(pattern, path)
                    if kwargs is not None:
                        fn(self, **kwargs)
                        return
            self._send(404, {"error": {"code": "NOT_FOUND", "message": f"无此路由：{path}"}})
        except DomainError as exc:
            self._send(exc.http_status, exc.to_dict())
        except (ValueError, TypeError, KeyError) as exc:
            self._send(400, {"error": {"code": "VALIDATION", "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": {"code": "INTERNAL", "message": repr(exc)}})


# ---------------------------------------------------------------------------
# 各端点
# ---------------------------------------------------------------------------


def h_register_party(h: Handler) -> None:
    body = h._read_body()
    key = h._idem(body)
    result = h.engine.register_party(
        party_id=body["party_id"], name=body.get("name", body["party_id"]),
        role=body["role"], idem_key=key)
    h._send(201, result)


def h_grant_credit(h: Handler, seller_id: str) -> None:
    body = h._read_body()
    h._send(200, h.engine.grant_credit(
        seller_id=seller_id, amount=str(body["amount"]), idem_key=h._idem(body)))


def h_register_line(h: Handler, seller_id: str) -> None:
    body = h._read_body()
    h._send(200, h.engine.register_credit_line(
        seller_id=seller_id, line_id=body["line_id"],
        amount=str(body["amount"]), idem_key=h._idem(body)))


def h_block_credit(h: Handler, seller_id: str) -> None:
    body = h._read_body()
    h._send(200, h.engine.set_credit_blocked(
        seller_id=seller_id, blocked=bool(body["blocked"]),
        reason=body.get("reason", ""), idem_key=h._idem(body)))


def h_reduce_credit(h: Handler, seller_id: str) -> None:
    body = h._read_body()
    h._send(200, h.engine.reduce_credit_limit(
        seller_id=seller_id, reduction=str(body["reduction"]),
        line_reductions=body.get("line_reductions"),
        reason=body.get("reason", ""), idem_key=h._idem(body)))


def h_credit_view(h: Handler, seller_id: str) -> None:
    h._send(200, h.engine.credit_view(seller_id))


def h_deposit_margin(h: Handler, buyer_id: str) -> None:
    body = h._read_body()
    h._send(201, h.engine.deposit_margin(
        buyer_id=buyer_id, amount=str(body["amount"]),
        batch_id=body.get("batch_id"), idem_key=h._idem(body)))


def h_margin_view(h: Handler, buyer_id: str) -> None:
    h._send(200, h.engine.margin_view(buyer_id))


def h_preview_order(h: Handler) -> None:
    body = h._read_body()
    h._send(200, h.engine.preview_order(
        seller_id=body["seller_id"], buyer_id=body["buyer_id"],
        quantity=str(body["quantity"]), unit_price=str(body["unit_price"]),
        margin_rate=str(body.get("margin_rate", "1")),
        preferred_lines=body.get("preferred_lines")))


def h_submit_order(h: Handler) -> None:
    body = h._read_body()
    token = body.get("client_token")
    if not token:
        raise DomainError("VALIDATION", 400,
                          "下单必须提供 client_token（同时作为幂等键）")
    result = h.engine.submit_order(
        seller_id=body["seller_id"], buyer_id=body["buyer_id"],
        quantity=str(body["quantity"]), unit_price=str(body["unit_price"]),
        margin_rate=str(body.get("margin_rate", "1")),
        client_token=str(token),
        preferred_lines=body.get("preferred_lines"),
        order_id=body.get("order_id"), actor=body.get("actor"))
    h._send(201, result)


def h_order_view(h: Handler, order_id: str) -> None:
    h._send(200, h.engine.order_view(order_id))


def h_settle(h: Handler, order_id: str) -> None:
    body = h._read_body()
    h._send(200, h.engine.settle_delivery(
        order_id=order_id, delivered_qty=str(body["delivered_qty"]),
        defaulted_qty=str(body.get("defaulted_qty", "0")),
        idem_key=h._idem(body), actor=body.get("actor")))


def h_cancel(h: Handler, order_id: str) -> None:
    body = h._read_body()
    h._send(200, h.engine.cancel_order(
        order_id=order_id, reason=body.get("reason", ""),
        idem_key=h._idem(body), actor=body.get("actor")))


def h_ledger(h: Handler) -> None:
    from urllib.parse import parse_qs

    qs = parse_qs(urlparse(h.path).query)
    h._send(200, h.engine.ledger_view(
        limit=int(qs.get("limit", ["100"])[0]),
        order_id=qs.get("order_id", [None])[0]))


def h_health(h: Handler) -> None:
    h._send(200, {"status": "ok", "entries": h.engine.store.count(),
                  "chain_intact": not h.engine.store.verify_chain()})


ROUTES: list[tuple[str, set[str], Callable[..., None]]] = [
    ("/healthz", {"GET"}, h_health),
    ("/v1/parties", {"POST"}, h_register_party),
    ("/v1/sellers/{seller_id}/credit", {"POST"}, h_grant_credit),
    ("/v1/sellers/{seller_id}/credit", {"GET"}, h_credit_view),
    ("/v1/sellers/{seller_id}/lines", {"POST"}, h_register_line),
    ("/v1/sellers/{seller_id}/block", {"POST"}, h_block_credit),
    ("/v1/sellers/{seller_id}/reduce", {"POST"}, h_reduce_credit),
    ("/v1/buyers/{buyer_id}/margin", {"POST"}, h_deposit_margin),
    ("/v1/buyers/{buyer_id}/margin", {"GET"}, h_margin_view),
    ("/v1/orders/preview", {"POST"}, h_preview_order),
    ("/v1/orders", {"POST"}, h_submit_order),
    ("/v1/orders/{order_id}", {"GET"}, h_order_view),
    ("/v1/orders/{order_id}/settle", {"POST"}, h_settle),
    ("/v1/orders/{order_id}/cancel", {"POST"}, h_cancel),
    ("/v1/ledger", {"GET"}, h_ledger),
]


def _match(pattern: str, path: str) -> dict[str, str] | None:
    p, x = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(p) != len(x):
        return None
    kwargs: dict[str, str] = {}
    for seg, val in zip(p, x):
        if seg.startswith("{") and seg.endswith("}"):
            kwargs[seg[1:-1]] = val
        elif seg != val:
            return None
    return kwargs


def make_server(host: str, port: int, db_path: str,
                max_order_notional: str = "10000000.00") -> ThreadingHTTPServer:
    store = EntryStore(db_path)
    engine = CollateralEngine(store, RiskConfig(max_order_notional))
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.engine = engine  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def serve_forever_in_thread(host: str = "127.0.0.1", port: int = 0,
                            db_path: str = ":memory:") -> tuple[ThreadingHTTPServer, str]:
    """测试辅助：后台线程启动，返回 (server, base_url)。"""
    httpd = make_server(host, port, db_path)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"
