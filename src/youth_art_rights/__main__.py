"""启动授权服务。

用法::

    python3 -m youth_art_rights [--host 0.0.0.0] [--port 8080] [--store data/store.json]

``--store`` 指定后，设备登记与方案以 JSON 原子落盘，重启不丢失；
不指定则仅使用内存台账，适合演示与测试。
"""

from __future__ import annotations

import argparse

from . import SERVICE_NAME
from .api import make_server
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--store", default=None, help="JSON 台账文件路径")
    args = parser.parse_args()

    store = Store(args.store)
    server = make_server(args.host, args.port, store)
    print(f"{SERVICE_NAME} 已启动：http://{args.host}:{args.port}")
    print("公众目录: GET /v1/public/catalog    设备合并: POST /v1/sync")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
