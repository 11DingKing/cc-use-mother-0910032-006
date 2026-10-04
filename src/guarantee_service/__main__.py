"""python -m guarantee_service 启动入口。"""
from __future__ import annotations

import argparse

from .api import create_server
from .ledger import Ledger
from .service import GuaranteeService


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="guarantee_service", description="积分交易担保风控服务端")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument(
        "--ledger",
        default="data/ledger.jsonl",
        help="分录持久化文件路径；传 :memory: 表示仅内存运行",
    )
    args = parser.parse_args(argv)

    path = None if args.ledger.strip().lower() in (":memory:", "none", "") else args.ledger
    ledger = Ledger(path)
    service = GuaranteeService(ledger)
    httpd = create_server(service, args.host, args.port)
    print(f"担保风控服务端已启动：http://{args.host}:{args.port}（分录存储：{path or '仅内存'}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭……")
    finally:
        httpd.server_close()
        ledger.close()


if __name__ == "__main__":
    main()
