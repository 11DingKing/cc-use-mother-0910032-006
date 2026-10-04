"""基于标准库的 HTTP JSON 接口层。"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import GuaranteeService, ServiceError

SERVICE_NAME = "guarantee-service"
SERVICE_VERSION = "1.0.0"


def _first(query: dict, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _index(service: GuaranteeService, match, body, query):
    return 200, {
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "description": "积分交易担保风控：信用额度、保证金批次、订单敞口与风控规则的不可变分录服务",
        "routes": [f"{method} {template}" for method, template, _pattern, _func in ROUTES],
    }


def _health(service: GuaranteeService, match, body, query):
    return 200, {"status": "ok"}


def _post_credit_lines(service, match, body, query):
    return 200, service.register_credit_line(body)


def _get_credit_line(service, match, body, query):
    return 200, service.credit_line_view(match.group(1))


def _post_credit_adjustment(service, match, body, query):
    return 200, service.adjust_credit_line(match.group(1), body)


def _post_margin_batches(service, match, body, query):
    return 200, service.register_margin_batch(body)


def _get_margin_batch(service, match, body, query):
    return 200, service.margin_batch_view(match.group(1))


def _post_margin_adjustment(service, match, body, query):
    return 200, service.adjust_margin_batch(match.group(1), body)


def _post_rules(service, match, body, query):
    return 200, service.register_rule(body)


def _get_rules(service, match, body, query):
    return 200, {"rules": service.list_rules()}


def _post_orders(service, match, body, query):
    return 200, service.accept_order(body)


def _get_orders(service, match, body, query):
    return 200, {"orders": service.list_orders(status=_first(query, "status"), owner_id=_first(query, "owner_id"))}


def _get_order(service, match, body, query):
    return 200, service.explain_order(match.group(1))


def _post_delivery(service, match, body, query):
    return 200, service.record_delivery(match.group(1), body)


def _post_default(service, match, body, query):
    return 200, service.record_default(match.group(1), body)


def _post_cancellation(service, match, body, query):
    return 200, service.cancel_order(match.group(1), body)


def _get_account(service, match, body, query):
    return 200, service.account_summary(match.group(1))


def _get_ledger(service, match, body, query):
    after_seq = int(_first(query, "after_seq") or 0)
    limit = int(_first(query, "limit") or 100)
    return 200, {"entries": service.list_entries(after_seq=after_seq, limit=limit)}


# （方法，路径模板，处理函数）；路径模板中的 {name} 捕获一段非斜杠文本
_ROUTE_TABLE = [
    ("GET", "/", _index),
    ("GET", "/health", _health),
    ("POST", "/credit-lines", _post_credit_lines),
    ("GET", "/credit-lines/{id}", _get_credit_line),
    ("POST", "/credit-lines/{id}/adjustments", _post_credit_adjustment),
    ("POST", "/margin-batches", _post_margin_batches),
    ("GET", "/margin-batches/{id}", _get_margin_batch),
    ("POST", "/margin-batches/{id}/adjustments", _post_margin_adjustment),
    ("POST", "/risk-rules", _post_rules),
    ("GET", "/risk-rules", _get_rules),
    ("POST", "/orders", _post_orders),
    ("GET", "/orders", _get_orders),
    ("GET", "/orders/{id}", _get_order),
    ("POST", "/orders/{id}/deliveries", _post_delivery),
    ("POST", "/orders/{id}/defaults", _post_default),
    ("POST", "/orders/{id}/cancellation", _post_cancellation),
    ("GET", "/accounts/{id}/summary", _get_account),
    ("GET", "/ledger", _get_ledger),
]


def _compile(template: str) -> re.Pattern:
    pattern = re.sub(r"\{[a-z_]+\}", r"([^/]+)", template)
    return re.compile(pattern)


ROUTES = [(method, template, _compile(template), func) for method, template, func in _ROUTE_TABLE]


class _Handler(BaseHTTPRequestHandler):
    server_version = "GuaranteeService/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def log_message(self, format: str, *args) -> None:  # 静默访问日志
        return

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ServiceError(400, "INVALID_JSON", "请求体不是合法 JSON") from None
        if not isinstance(body, dict):
            raise ServiceError(400, "INVALID_PARAMS", "请求体必须是 JSON 对象")
        return body

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path if parsed.path == "/" else parsed.path.rstrip("/")
        query = parse_qs(parsed.query)
        try:
            body = self._read_body() if method == "POST" else {}
            for route_method, _template, pattern, func in ROUTES:
                if route_method != method:
                    continue
                match = pattern.fullmatch(path)
                if match:
                    status, payload = func(self.server.service, match, body, query)
                    self._send_json(status, payload)
                    return
            self._send_json(404, {"error": {"code": "NOT_FOUND", "message": f"路由不存在：{method} {path}"}})
        except ServiceError as exc:
            self._send_json(
                exc.status,
                {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            )
        except ValueError as exc:
            self._send_json(400, {"error": {"code": "INVALID_PARAMS", "message": str(exc)}})
        except Exception:  # pragma: no cover - 兜底
            traceback.print_exc()
            self._send_json(500, {"error": {"code": "INTERNAL", "message": "服务内部错误"}})


def create_server(service: GuaranteeService, host: str, port: int) -> ThreadingHTTPServer:
    """创建多线程 HTTP 服务；共享的账簿锁保证并发下单不会透支担保。"""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.service = service
    return httpd
