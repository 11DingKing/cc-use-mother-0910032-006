"""命令行启动：``python -m collateral_service --host 0.0.0.0 --port 8080``。"""
from __future__ import annotations

import argparse

from .server import make_server


def main() -> None:
    parser = argparse.ArgumentParser(description="交易订单担保风控服务端")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="collateral.db",
                        help="SQLite 账本路径（默认 collateral.db）")
    parser.add_argument("--max-order-notional", default="10000000.00")
    args = parser.parse_args()

    httpd = make_server(args.host, args.port, args.db, args.max_order_notional)
    print(f"担保风控服务监听 http://{args.host}:{args.port}，账本={args.db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        httpd.store.close()


if __name__ == "__main__":
    main()
