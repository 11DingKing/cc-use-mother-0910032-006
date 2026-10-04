"""只增不改的分录账簿：JSONL 持久化、启动重放与派生状态。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import (
    Allocation,
    CreditLineState,
    Entry,
    MarginBatchState,
    OrderState,
    RiskRule,
    to_decimal,
)


class DuplicateRequestError(Exception):
    """同一 request_id 被重复写入且未经过服务的幂等重放检查。"""


class Ledger:
    """追加式账簿。

    所有状态变更都先落成一条不可变分录，再应用到内存投影；
    重启时按顺序重放分录即可恢复完全一致的状态。
    调用方在做“检查 + 写入”的复合操作时必须持有 ``lock``，
    这样并发请求不会透支同一笔可用担保。
    """

    def __init__(self, path: str | Path | None = None):
        self.lock = threading.RLock()
        self._entries: list[Entry] = []
        self._request_index: dict[str, Entry] = {}
        self.credit_lines: dict[str, CreditLineState] = {}
        self.margin_batches: dict[str, MarginBatchState] = {}
        self.orders: dict[str, OrderState] = {}
        self.rules: dict[str, RiskRule] = {}
        self._path = Path(path) if path else None
        self._fh = None
        if self._path is not None:
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            self._apply_and_record(Entry.from_dict(json.loads(line)))
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")

    # ---------- 写入 ----------

    def append(self, kind: str, data: dict, request_id: str | None = None) -> Entry:
        """追加一条不可变分录并应用到投影。调用方须已持有 self.lock。"""
        with self.lock:
            if request_id is not None and request_id in self._request_index:
                raise DuplicateRequestError(request_id)
            seq = len(self._entries) + 1
            entry = Entry(
                seq=seq,
                entry_id=f"E{seq:06d}",
                ts=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                kind=kind,
                request_id=request_id,
                data=data,
            )
            if self._fh is not None:
                self._fh.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
                self._fh.flush()
            self._apply_and_record(entry)
            return entry

    def _apply_and_record(self, entry: Entry) -> None:
        self._apply(entry)
        self._entries.append(entry)
        if entry.request_id is not None:
            self._request_index[entry.request_id] = entry

    # ---------- 查询 ----------

    def find_request(self, request_id: str) -> Entry | None:
        return self._request_index.get(request_id)

    def entries(self) -> list[Entry]:
        return list(self._entries)

    def entries_for_order(self, order_id: str) -> list[Entry]:
        return [e for e in self._entries if e.data.get("order_id") == order_id]

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    # ---------- 投影 ----------

    def _source(self, source_kind: str, source_id: str):
        if source_kind == "credit_line":
            return self.credit_lines[source_id]
        if source_kind == "margin_batch":
            return self.margin_batches[source_id]
        raise KeyError(f"未知担保来源：{source_kind}")

    def _apply(self, entry: Entry) -> None:
        kind = entry.kind
        d = entry.data
        if kind == "credit_line_registered":
            self.credit_lines[d["line_id"]] = CreditLineState(
                line_id=d["line_id"], owner_id=d["owner_id"], limit=to_decimal(d["limit"])
            )
        elif kind == "credit_line_adjusted":
            line = self.credit_lines[d["line_id"]]
            line.limit += to_decimal(d["delta"])
        elif kind == "margin_batch_registered":
            self.margin_batches[d["batch_id"]] = MarginBatchState(
                batch_id=d["batch_id"], owner_id=d["owner_id"], amount=to_decimal(d["amount"])
            )
        elif kind == "margin_batch_adjusted":
            batch = self.margin_batches[d["batch_id"]]
            batch.amount += to_decimal(d["delta"])
        elif kind == "risk_rule_registered":
            rule = RiskRule(
                rule_id=d["rule_id"],
                version=int(d["version"]),
                margin_rate=to_decimal(d["margin_rate"]),
                exposure_rate=to_decimal(d["exposure_rate"]),
                max_order_notional=to_decimal(d["max_order_notional"]),
                max_seller_utilization=to_decimal(d["max_seller_utilization"]),
            )
            self.rules[rule.rule_id] = rule
        elif kind in ("order_accepted", "order_rejected"):
            self._apply_order_opened(entry)
        elif kind == "delivery_recorded":
            order = self.orders[d["order_id"]]
            order.delivered = int(d["delivered_total"])
            order.status = d["status_after"]
            self._apply_releases(order, d["releases"])
        elif kind == "default_recorded":
            order = self.orders[d["order_id"]]
            order.defaulted = int(d["defaulted_total"])
            order.status = d["status_after"]
            for item in d["seizures"]:
                alloc = order.find_allocation(item["source_kind"], item["source_id"])
                amount = to_decimal(item["amount"])
                alloc.seized += amount
                source = self._source(alloc.source_kind, alloc.source_id)
                source.frozen -= amount
                source.seized_total += amount
                if isinstance(source, MarginBatchState):
                    # 罚没的保证金本金划转给清算对手方，批次余额同步减少
                    source.amount -= amount
        elif kind == "order_cancelled":
            order = self.orders[d["order_id"]]
            order.status = d["status_after"]
            self._apply_releases(order, d["releases"])
        else:
            raise ValueError(f"未知分录类型：{kind}")

    def _apply_order_opened(self, entry: Entry) -> None:
        d = entry.data
        allocations = [
            Allocation(
                source_kind=item["source_kind"],
                source_id=item["source_id"],
                amount=to_decimal(item["amount"]),
            )
            for item in d.get("allocations", [])
        ]
        order = OrderState(
            order_id=d["order_id"],
            request_id=entry.request_id or "",
            seller_id=d["seller_id"],
            buyer_id=d["buyer_id"],
            quantity=int(d["quantity"]),
            price=to_decimal(d["price"]),
            notional=to_decimal(d["notional"]),
            exposure=to_decimal(d["exposure"]),
            margin_required=to_decimal(d["margin_required"]),
            rule_id=d["rule"]["rule_id"],
            rule_version=int(d["rule"]["version"]),
            status="accepted" if entry.kind == "order_accepted" else "rejected",
            allocations=allocations,
            reject_reasons=list(d.get("reasons", [])),
        )
        self.orders[order.order_id] = order
        for alloc in allocations:
            self._source(alloc.source_kind, alloc.source_id).frozen += alloc.amount

    def _apply_releases(self, order: OrderState, releases: Iterable[dict]) -> None:
        for item in releases:
            alloc = order.find_allocation(item["source_kind"], item["source_id"])
            amount = to_decimal(item["amount"])
            alloc.released += amount
            self._source(alloc.source_kind, alloc.source_id).frozen -= amount
