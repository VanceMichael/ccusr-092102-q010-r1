"""HTTP API。

路由分两组：

* ``/v1/public/*`` —— 公众页面，只输出脱敏后的获准内容；
* ``/v1/admin/*``  —— 机构后台视图（含监护人联系方式等），部署时应在网关层限制访问；
* ``/v1/sync``、``/v1/plans`` —— 设备登记与发布评审。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import public as public_view
from . import review as review_mod
from .models import CONSENT_SCOPES, SCOPE_LABELS, model_to_dict
from .service import get_plan, submit_plan
from .store import Store, ValidationError


def _regions_overlap(a: dict[str, int], b: dict[str, int]) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def artwork_detail(store: Store, artwork_id: str, revision_no: int | None) -> dict[str, Any] | None:
    """作品修订的内部视图：每个修订保留完整层次，可按 z_order 还原。"""
    revisions = store.artworks.get(artwork_id)
    if not revisions:
        if artwork_id in store.artwork_meta:
            return {
                "artwork_id": artwork_id,
                "current": None,
                "revisions": [],
                "note": "画布已建档，尚无修订",
            }
        return None

    revision_views = []
    for rev in revisions:
        layers = [store.contributions[lid] for lid in rev.layer_ids if lid in store.contributions]
        layers.sort(key=lambda c: c.z_order)
        layer_views = []
        for index, layer in enumerate(layers):
            later = [
                other.contribution_id
                for other in layers[index + 1 :]
                if _regions_overlap(layer.region, other.region)
            ]
            layer_views.append(
                {
                    "contribution_id": layer.contribution_id,
                    "participant_alias": layer.participant_alias,
                    "z_order": layer.z_order,
                    "region": layer.region,
                    "asset_id": layer.asset_id,
                    "recorded_at": layer.recorded_at,
                    "covered_by": later,  # 被后画覆盖，但本层记录仍保留、可还原
                    "note": layer.note,
                }
            )
        revision_views.append(
            {
                "revision": rev.revision,
                "parent_revision": rev.parent_revision,
                "title": rev.title,
                "created_at": rev.created_at,
                "composite_asset_id": rev.asset_id,
                "layers": layer_views,
                "restorable": True,
            }
        )

    current = revision_views[-1]
    if revision_no is not None:
        match = next((r for r in revision_views if r["revision"] == revision_no), None)
        if match is None:
            return None
        current = match
    return {"artwork_id": artwork_id, "current": current, "revisions": revision_views}


class ApiHandler(BaseHTTPRequestHandler):
    store: Store  # 由 make_server 注入到类属性

    server_version = "YouthArtRights/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 简洁日志
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ------------------------------------------------------------- 基础工具

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValidationError("请求体为空")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _handle(
        self, handler: Callable[[dict[str, Any], dict[str, str]], Any]
    ) -> None:
        parts = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        try:
            body = self._read_json() if self.command == "POST" else {}
            result = handler(query, body)
        except ValidationError as exc:
            self._send_json(400, {"错误": str(exc)})
            return
        except KeyError as exc:
            self._send_json(404, {"错误": f"资源不存在: {exc.args[0]}"})
            return
        if result is None:
            self._send_json(404, {"错误": "资源不存在"})
            return
        status = 201 if self.command == "POST" else 200
        self._send_json(status, result)

    # ----------------------------------------------------------------- GET

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"

        if path == "/health":
            self._send_json(200, {"状态": "服务已启动"})
            return

        segments = [s for s in path.split("/") if s]
        if segments[:2] == ["v1", "public"]:
            self._serve_public(segments[2:])
            return
        if segments[:2] == ["v1", "admin"]:
            self._serve_admin(segments[2:])
            return
        if len(segments) == 3 and segments[:2] == ["v1", "plans"]:
            self._handle(lambda q, b, plan_id=segments[2]: self._plan_get(plan_id))
            return
        self._send_json(404, {"错误": f"未知路径: {path}"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/v1/sync":
            self._handle(lambda q, body: self.store.sync(body.get("operations", [])))
            return
        if path == "/v1/plans":
            self._handle(lambda q, body: submit_plan(self.store, body))
            return
        self._send_json(404, {"错误": f"未知路径: {path}"})

    # ------------------------------------------------------------- 公众页面

    def _serve_public(self, segments: list[str]) -> None:
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
        if not segments:
            self._send_json(404, {"错误": "未知路径"})
            return
        resource = segments[0]
        try:
            if resource == "catalog" and len(segments) == 1:
                self._send_json(200, public_view.public_catalog(self.store))
                return
            if resource == "events" and len(segments) == 1:
                self._send_json(200, {"events": public_view.public_events(self.store)})
                return
            if resource == "assets" and len(segments) == 2:
                view = public_view.public_asset(self.store, segments[1])
                self._send_public_or_404(view, "素材不存在或未获准公开展示")
                return
            if resource == "artworks" and len(segments) == 2:
                revision = int(query["revision"]) if "revision" in query else None
                view = public_view.public_artwork(self.store, segments[1], revision)
                self._send_public_or_404(view, "作品不存在或未获准公开展示")
                return
        except ValueError:
            self._send_json(400, {"错误": "revision 必须为整数"})
            return
        self._send_json(404, {"错误": "未知路径"})

    def _send_public_or_404(self, view: Any, missing_message: str) -> None:
        # 公众端对不可见资源统一 404，不区分"不存在"与"未授权"，避免暴露存在性
        if view is None:
            self._send_json(404, {"错误": missing_message})
        else:
            self._send_json(200, view)

    # ------------------------------------------------------------- 机构后台

    def _serve_admin(self, segments: list[str]) -> None:
        query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
        if segments == ["snapshot"]:
            self._send_json(200, self.store.snapshot())
            return
        if segments == ["artworks"]:
            self._send_json(
                200,
                {
                    "artworks": [
                        artwork_detail(self.store, artwork_id, None) for artwork_id in self.store.artworks
                    ]
                },
            )
            return
        if len(segments) == 2 and segments[0] == "artworks":
            revision = int(query["revision"]) if "revision" in query else None
            self._send_public_or_admin(
                artwork_detail(self.store, segments[1], revision), "作品不存在"
            )
            return
        if len(segments) == 2 and segments[0] == "participants":
            participant = self.store.participants.get(segments[1])
            self._send_public_or_admin(model_to_dict(participant) if participant else None, "参与者不存在")
            return
        if segments == ["consent"] and "alias" in query:
            alias = query["alias"]
            if alias not in self.store.participants:
                self._send_json(404, {"错误": "参与者不存在"})
                return
            scope_filter = query.get("scope")
            ledger = {
                scope: model_to_dict(
                    review_mod.consent_state(self.store, alias, scope)["events"]
                )
                for scope in CONSENT_SCOPES
                if scope_filter in (None, scope)
            }
            self._send_json(
                200,
                {
                    "alias": alias,
                    "scope_labels": SCOPE_LABELS,
                    "ledger": ledger,
                    "current": {
                        scope: review_mod.consent_state(self.store, alias, scope)["status"]
                        for scope in CONSENT_SCOPES
                        if scope_filter in (None, scope)
                    },
                },
            )
            return
        if segments == ["revocation-impact"] and "alias" in query:
            self._send_json(
                200,
                review_mod.revocation_impact(self.store, query["alias"], query.get("scope")),
            )
            return
        self._send_json(404, {"错误": "未知后台路径"})

    def _send_public_or_admin(self, view: Any, missing_message: str) -> None:
        if view is None:
            self._send_json(404, {"错误": missing_message})
        else:
            self._send_json(200, view)

    # --------------------------------------------------------------- 方案

    def _plan_get(self, plan_id: str) -> dict[str, Any] | None:
        return get_plan(self.store, plan_id)


def make_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    handler = type("BoundApiHandler", (ApiHandler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    return server
