"""启动青少年共创作品授权服务。"""

import os

from .app import build_server


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB_PATH")  # 不设置时仅内存存储，重启清空
    server, _ = build_server(host=host, port=port, db_path=db_path)
    print(f"青少年共创作品授权服务已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
