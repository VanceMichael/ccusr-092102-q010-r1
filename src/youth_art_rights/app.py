"""JSON HTTP 接口（仅依赖标准库）。

路由总览：
  GET  /health
  POST /sync                                  离线设备批量幂等合并
  GET  /participants/{alias}
  POST /participants/{alias}/verification     监护关系核验
  POST /contributions                         登记画布图层
  POST /materials                             登记素材（草图/照片/局部/完整作品）
  POST /revisions                             完整作品修订（保留可还原层次）
  GET  /revisions/{id}                        按落笔顺序展开图层（用于还原）
  POST /grants                                授权/撤回（按资产维度×用途）
  POST /usages                                登记发布用途（scheduled/ongoing/concluded）
  POST /reviews/submission                    海报/展览方案逐项审核
  GET  /materials/{id}/review?scope=...       单素材审核
  GET /takedowns?alias=&scope=                撤回后须下线的未来/进行中物料
  GET /public/feed                            公众页面（仅获准内容，已脱敏）
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .engine import ReviewEngine
from .models import SCOPES
from .store import Store


class AppHandler(BaseHTTPRequestHandler):
    store: Store = None  # 由 build_server 注入到子类

    def log_message(self, fmt, *args) -> None:  # 安静：测试输出不被访问日志污染
        return

    # --- 基础收发 ---
    def _send(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=list).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _error(self, status: int, message: str) -> None:
        self._send({"error": message}, status)

    # --- 路由 ---
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        try:
            if path == "/health":
                self._send({"状态": "服务已启动"})
            elif path == "/public/feed":
                self._send({"materials": ReviewEngine(self.store).public_feed()})
            elif path.startswith("/participants/"):
                alias = path.split("/")[2]
                participant = self.store.participant(alias)
                self._send(participant or {}, 200 if participant else 404)
            elif path.startswith("/revisions/"):
                revision = self.store.revision(path.split("/")[2])
                if not revision:
                    self._error(404, "修订不存在")
                    return
                revision = dict(revision)
                revision["ordered_layers"] = self.store.ordered_layers(revision["layers"])
                self._send(revision)
            elif path.startswith("/materials/") and path.endswith("/review"):
                segments = path.split("/")
                material_id = segments[2]
                scope = query.get("scope", [""])[0]
                if scope not in SCOPES:
                    self._error(400, "scope 必须是 on-site-display/print-publication/"
                                    "network-distribution/external-provision 之一")
                    return
                result = ReviewEngine(self.store).review_material(material_id, scope)
                self._send(result.to_dict(),
                           200 if result.bucket != "unusable" else 422)
            elif path == "/takedowns":
                alias = query.get("alias", [None])[0]
                scope = query.get("scope", [None])[0]
                self._send({"takedowns": ReviewEngine(self.store)
                            .takedown_list(alias=alias, scope=scope)})
            else:
                self._error(404, "未找到该路径")
        except ValueError as exc:
            self._error(400, str(exc))

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            body = self._read_json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._error(400, f"请求体不是合法 JSON: {exc}")
            return
        try:
            if path == "/sync":
                result = self.store.sync_batch(body["device_id"], body.get("ops", []))
                self._send(result)
            elif path == "/contributions":
                self._send(self.store.add_contribution(body), 201)
            elif path == "/materials":
                self._send(self.store.add_material(body), 201)
            elif path == "/revisions":
                revision = self.store.add_revision(
                    layers=body["layers"],
                    flattened_material_id=body.get("flattened_material_id"),
                    note=body.get("note", ""),
                    parent_revision_id=body.get("parent_revision_id"),
                    created_at=body.get("created_at"),
                )
                self._send(revision, 201)
            elif path == "/grants":
                self._send(self.store.record_grant(
                    alias=body["alias"], asset=body["asset"], scope=body["scope"],
                    state=body.get("state", "granted"),
                    effective_at=body.get("effective_at"),
                    recorded_by=body.get("recorded_by", "staff"),
                ), 201)
            elif path == "/usages":
                self._send(self.store.add_usage(body), 201)
            elif path == "/reviews/submission":
                scope = body.get("scope", "")
                if scope not in SCOPES:
                    self._error(400, "scope 必须是四个授权用途之一")
                    return
                result = ReviewEngine(self.store).review_submission(
                    body.get("material_ids", []), scope)
                self._send(result,
                           200 if result["overall_bucket"] != "unusable" else 422)
            elif path.endswith("/verification") and path.startswith("/participants/"):
                alias = path.split("/")[2]
                self._send(self.store.set_guardian_verification(
                    alias=alias, status=body["status"], method=body.get("method", ""),
                    reference=body.get("reference", ""),
                    verified_at=body.get("verified_at")), 201)
            else:
                self._error(404, "未找到该路径")
        except KeyError as exc:
            self._error(400, f"缺少必填字段: {exc.args[0]}")
        except ValueError as exc:
            self._error(422, str(exc))


def build_server(host: str = "127.0.0.1", port: int = 0,
                 db_path: str | None = None) -> tuple[ThreadingHTTPServer, Store]:
    """构造可测试的服务器实例（端口 0 时由系统分配端口）。"""
    store = Store(db_path)

    class _BoundHandler(AppHandler):
        pass

    _BoundHandler.store = store
    server = ThreadingHTTPServer((host, port), _BoundHandler)
    return server, store
