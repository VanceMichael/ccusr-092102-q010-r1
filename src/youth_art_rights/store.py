"""存储与离线合并。

所有登记都以"设备操作"（Operation）的形式追加写入：

* 同一台设备用单调递增的 ``seq`` 编号；
* 合并键为 ``(device_id, seq)``，重复提交整批幂等；
* 设备间乱序到达时做多趟重放，先到的跨设备引用会在后续趟次补齐；
* 持久化为单个 JSON 文件，写入采用临时文件 + 原子替换。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from .models import (
    CONSENT_SCOPES,
    ArtworkRevision,
    Asset,
    ConsentEvent,
    Contribution,
    EventRecord,
    Operation,
    Participant,
    ValidationError,
    model_to_dict,
    now_iso,
)


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise ValidationError(f"缺少必填字段: {key}")
    return payload[key]


def _validate_region(region: Any) -> dict[str, int]:
    if not isinstance(region, dict):
        raise ValidationError("canvas_region 必须是对象")
    coords: dict[str, int] = {}
    for key in ("x", "y", "width", "height"):
        value = region.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"canvas_region.{key} 必须为整数")
        coords[key] = value
    if coords["x"] < 0 or coords["y"] < 0:
        raise ValidationError("画布坐标不能为负")
    if coords["width"] <= 0 or coords["height"] <= 0:
        raise ValidationError("画布区域宽高必须为正数")
    return coords


class Store:
    """内存台账 + JSON 原子持久化。"""

    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.ops: dict[tuple[str, int], Operation] = {}
        self.participants: dict[str, Participant] = {}
        self.assets: dict[str, Asset] = {}
        self.contributions: dict[str, Contribution] = {}
        self.artworks: dict[str, list[ArtworkRevision]] = {}
        self.artwork_meta: dict[str, dict[str, Any]] = {}
        self.consent_events: list[ConsentEvent] = []
        self.events: dict[str, EventRecord] = {}
        self.plans: dict[str, Any] = {}
        if path and os.path.exists(path):
            self._load()

    # ------------------------------------------------------------------ 持久化

    def _load(self) -> None:
        assert self._path
        with open(self._path, encoding="utf-8") as fh:
            data = json.load(fh)
        for raw in data.get("ops", []):
            op = Operation(**raw)
            self.ops[op.key] = op
        ordered = sorted(self.ops.values(), key=lambda o: (o.device_id, o.seq))
        self._replay(ordered)
        for plan in data.get("plans", []):
            self.plans[plan["plan_id"]] = plan

    def _persist_locked(self) -> None:
        if not self._path:
            return
        directory = os.path.dirname(os.path.abspath(self._path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self._path}.tmp.{os.getpid()}.{id(self)}"
        snapshot = {
            "ops": [model_to_dict(op) for op in self.ops.values()],
            "plans": list(self.plans.values()),
        }
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "participants": {k: model_to_dict(v) for k, v in self.participants.items()},
                "assets": {k: model_to_dict(v) for k, v in self.assets.items()},
                "contributions": {k: model_to_dict(v) for k, v in self.contributions.items()},
                "artworks": {k: model_to_dict(v) for k, v in self.artworks.items()},
                "artwork_meta": self.artwork_meta,
                "consent_events": [model_to_dict(e) for e in self.consent_events],
                "events": {k: model_to_dict(v) for k, v in self.events.items()},
            }

    # --------------------------------------------------------------- 离线合并

    def sync(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        """合并一批设备操作，返回幂等结果报告。

        报告区分 applied（本次新生效）、duplicated（设备序号重复，原样跳过）
        和 rejected（校验失败，附原因）。
        """
        with self._lock:
            incoming: list[Operation] = []
            for index, raw in enumerate(operations):
                try:
                    incoming.append(self._build_op(raw, index))
                except ValidationError as exc:
                    raise ValidationError(f"第 {index + 1} 条操作: {exc}") from exc

            fresh: list[Operation] = []
            duplicated: list[dict[str, Any]] = []
            seen_inside_batch: set[tuple[str, int]] = set()
            for op in incoming:
                if op.key in self.ops or op.key in seen_inside_batch:
                    duplicated.append({"device_id": op.device_id, "seq": op.seq})
                    continue
                seen_inside_batch.add(op.key)
                fresh.append(op)

            # 按设备序号确定全局顺序，保证自动层级等结果可复现
            fresh.sort(key=lambda o: (o.device_id, o.seq))
            applied, rejected = self._replay(fresh)
            if applied:
                applied_keys = {(a["device_id"], a["seq"]) for a in applied}
                for op in fresh:
                    if op.key in applied_keys:
                        self.ops[op.key] = op
                self._persist_locked()

            gaps = self._device_gaps()
            return {
                "applied": applied,
                "duplicated": duplicated,
                "rejected": rejected,
                "device_gaps": gaps,
            }

    def _build_op(self, raw: dict[str, Any], index: int) -> Operation:
        if not isinstance(raw, dict):
            raise ValidationError("操作必须是对象")
        device_id = _require(raw, "device_id")
        seq = raw.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
            raise ValidationError("seq 必须为正整数")
        op_type = _require(raw, "type")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        return Operation(
            op_id=f"{device_id}:{seq}",
            device_id=device_id,
            seq=seq,
            type=op_type,
            payload=payload,
            occurred_at=raw.get("occurred_at") or now_iso(),
        )

    def _device_gaps(self) -> dict[str, list[int]]:
        highs: dict[str, int] = {}
        present: dict[str, set[int]] = {}
        for device_id, seq in self.ops:
            highs[device_id] = max(highs.get(device_id, 0), seq)
            present.setdefault(device_id, set()).add(seq)
        return {
            device_id: [s for s in range(1, high + 1) if s not in present[device_id]]
            for device_id, high in highs.items()
            if len(present[device_id]) != high
        }

    def _replay(self, ops: list[Operation]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """多趟重放：跨设备前向引用在后续趟次自动满足。"""
        pending = list(ops)
        applied: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        while pending:
            progressed = False
            still: list[Operation] = []
            for op in pending:
                try:
                    result = self._apply(op)
                except ValidationError as exc:
                    still.append((op, str(exc)))  # type: ignore[arg-type]
                    continue
                applied.append(
                    {"device_id": op.device_id, "seq": op.seq, "type": op.type, "result": result}
                )
                progressed = True
            if not progressed:
                for item in still:
                    op, reason = item  # type: ignore[misc]
                    rejected.append(
                        {"device_id": op.device_id, "seq": op.seq, "type": op.type, "reason": reason}
                    )
                break
            pending = [item[0] for item in still]  # type: ignore[misc]
        return applied, rejected

    # ------------------------------------------------------------------ 操作应用

    def _apply(self, op: Operation) -> dict[str, Any]:
        source = {"device_id": op.device_id, "seq": op.seq, "occurred_at": op.occurred_at}
        handlers = {
            "register_participant": self._apply_participant,
            "register_asset": self._apply_asset,
            "record_contribution": self._apply_contribution,
            "create_artwork": self._apply_create_artwork,
            "revise_artwork": self._apply_revise_artwork,
            "consent_event": self._apply_consent_event,
            "record_event": self._apply_record_event,
        }
        handler = handlers.get(op.type)
        if handler is None:
            raise ValidationError(f"未知操作类型: {op.type}")
        return handler(op.payload, source)

    def _apply_participant(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        alias = _require(p, "alias")
        if not alias.startswith("child-"):
            raise ValidationError("参与者代号必须以 child- 开头")
        if alias in self.participants:
            # 重复登记同一代号视为同一人的补充信息，幂等更新非空字段
            existing = self.participants[alias]
            if p.get("guardian_contact"):
                existing.guardian_contact = p["guardian_contact"]
            if "guardian_verified" in p:
                existing.guardian_verified = bool(p["guardian_verified"])
            if p.get("guardian_verification"):
                existing.guardian_verification = p["guardian_verification"]
            if p.get("public_pen_name"):
                existing.public_pen_name = p["public_pen_name"]
            return {"alias": alias, "updated": True}
        participant = Participant(
            alias=alias,
            guardian_verified=bool(p.get("guardian_verified", False)),
            guardian_verification=p.get("guardian_verification"),
            guardian_contact=p.get("guardian_contact"),
            public_pen_name=p.get("public_pen_name"),
            registered_at=p.get("registered_at") or source["occurred_at"],
            source=source,
        )
        self.participants[alias] = participant
        return {"alias": alias, "created": True}

    def _apply_asset(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        asset_id = _require(p, "asset_id")
        if asset_id in self.assets:
            return {"asset_id": asset_id, "existed": True}
        kind = _require(p, "kind")
        if kind not in {
            "personal_sketch",
            "photo",
            "region_crop",
            "complete_artwork",
        }:
            raise ValidationError(f"未知素材类型: {kind}")
        author = p.get("author_alias")
        if author and author not in self.participants:
            raise ValidationError(f"素材作者未登记: {author}")
        subjects = p.get("subjects", [])
        if not isinstance(subjects, list) or any(s not in self.participants for s in subjects):
            raise ValidationError("肖像主体必须是已登记的参与者代号")
        asset = Asset(
            asset_id=asset_id,
            kind=kind,
            title=_require(p, "title"),
            author_alias=author,
            subjects=subjects,
            uri=p.get("uri", ""),
            released=bool(p.get("released", kind != "personal_sketch")),
            captured_at=p.get("captured_at", ""),
            created_at=p.get("created_at") or source["occurred_at"],
            metadata=p.get("metadata", {}),
            source=source,
        )
        self.assets[asset_id] = asset
        return {"asset_id": asset_id, "created": True}

    def _apply_contribution(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        contribution_id = _require(p, "contribution_id")
        if contribution_id in self.contributions:
            return {"contribution_id": contribution_id, "existed": True}
        alias = _require(p, "participant_alias")
        if alias not in self.participants:
            raise ValidationError(f"参与者未登记: {alias}")
        artwork_id = _require(p, "artwork_id")
        if artwork_id not in self.artworks:
            raise ValidationError(f"作品尚未创建: {artwork_id}（贡献只能挂到已有作品上）")
        region = _validate_region(p.get("canvas_region"))
        asset_id = p.get("asset_id")
        if asset_id and asset_id not in self.assets:
            raise ValidationError(f"素材不存在: {asset_id}")
        z_order = p.get("z_order")
        if z_order is None:
            used = [c.z_order for c in self.contributions.values() if c.artwork_id == artwork_id]
            z_order = (max(used) + 1) if used else 1
        elif not isinstance(z_order, int) or z_order <= 0:
            raise ValidationError("z_order 必须为正整数")
        contribution = Contribution(
            contribution_id=contribution_id,
            artwork_id=artwork_id,
            participant_alias=alias,
            region=region,
            z_order=z_order,
            asset_id=asset_id,
            recorded_at=p.get("recorded_at") or source["occurred_at"],
            note=p.get("note", ""),
            source=source,
        )
        self.contributions[contribution_id] = contribution
        return {"contribution_id": contribution_id, "created": True, "z_order": z_order}

    def _apply_create_artwork(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        artwork_id = _require(p, "artwork_id")
        if artwork_id in self.artworks:
            raise ValidationError(f"作品已存在，请使用 revise_artwork 追加修订: {artwork_id}")
        # 先建空画布；图层随后逐层登记，首次 revise_artwork 产出修订 1
        self.artworks[artwork_id] = []
        self.artwork_meta[artwork_id] = {
            "title": _require(p, "title"),
            "created_at": p.get("created_at") or source["occurred_at"],
            "note": p.get("note", ""),
            "source": source,
        }
        return {"artwork_id": artwork_id, "revision": 0, "layers": 0}

    def _apply_revise_artwork(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        artwork_id = _require(p, "artwork_id")
        if artwork_id not in self.artworks:
            raise ValidationError(f"作品不存在: {artwork_id}（应先 create_artwork 建画布）")
        revisions = self.artworks[artwork_id]
        layer_ids = _require(p, "layer_ids")
        self._validate_layers(artwork_id, layer_ids)
        if p.get("parent_revision") is not None:
            parent = p["parent_revision"]
            if parent not in {r.revision for r in revisions}:
                raise ValidationError(f"父修订不存在: {parent}")
        else:
            parent = revisions[-1].revision if revisions else None
        revision = ArtworkRevision(
            artwork_id=artwork_id,
            revision=len(revisions) + 1,
            parent_revision=parent,
            title=p.get("title") or self.artwork_meta[artwork_id]["title"],
            layer_ids=list(layer_ids),
            asset_id=p.get("asset_id"),
            created_at=p.get("created_at") or source["occurred_at"],
            note=p.get("note", ""),
            source=source,
        )
        revisions.append(revision)
        return {"artwork_id": artwork_id, "revision": revision.revision, "layers": len(layer_ids)}

    def _validate_layers(
        self, artwork_id: str, layer_ids: Any, allow_new: bool = False
    ) -> list[str]:
        if not isinstance(layer_ids, list) or not layer_ids:
            raise ValidationError("layer_ids 必须为非空数组（修订至少包含一层贡献）")
        if len(set(layer_ids)) != len(layer_ids):
            raise ValidationError("同一修订内图层不能重复")
        for layer_id in layer_ids:
            contribution = self.contributions.get(layer_id)
            if contribution is None and allow_new:
                raise ValidationError(f"图层贡献不存在: {layer_id}")
            if contribution is None:
                raise ValidationError(f"图层贡献不存在: {layer_id}")
            if contribution.artwork_id != artwork_id:
                raise ValidationError(f"图层不属于该作品: {layer_id}")
        return layer_ids

    def _apply_consent_event(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        alias = _require(p, "participant_alias")
        if alias not in self.participants:
            raise ValidationError(f"参与者未登记: {alias}")
        action = _require(p, "action")
        if action not in {"grant", "revoke"}:
            raise ValidationError("action 必须为 grant 或 revoke")
        scope = _require(p, "scope")
        if scope not in CONSENT_SCOPES:
            raise ValidationError(f"未知授权用途: {scope}")
        asset_ids = p.get("asset_ids")
        if asset_ids is not None:
            if not isinstance(asset_ids, list) or not asset_ids:
                raise ValidationError("asset_ids 为空时应传 null 表示全部素材")
            for asset_id in asset_ids:
                asset = self.assets.get(asset_id)
                if asset is None:
                    raise ValidationError(f"素材不存在: {asset_id}")
                if alias != asset.author_alias and alias not in asset.subjects:
                    raise ValidationError(f"素材与参与者无关，不能就此授权: {asset_id}")
        effective_at = p.get("effective_at") or source["occurred_at"]
        event = ConsentEvent(
            event_id=p.get("event_id") or f"{alias}:{scope}:{action}:{effective_at}",
            participant_alias=alias,
            action=action,
            scope=scope,
            asset_ids=list(asset_ids) if asset_ids is not None else None,
            effective_at=effective_at,
            recorded_at=source["occurred_at"],
            reason=p.get("reason", ""),
            guardian_verification_ref=p.get("guardian_verification_ref", ""),
            source=source,
        )
        self.consent_events.append(event)
        return {"event_id": event.event_id, "action": action, "scope": scope}

    def _apply_record_event(self, p: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        event_id = _require(p, "event_id")
        if event_id in self.events:
            return {"event_id": event_id, "existed": True}
        artwork_ids = p.get("artwork_ids", [])
        for artwork_id in artwork_ids:
            if artwork_id not in self.artworks:
                raise ValidationError(f"作品不存在: {artwork_id}")
        record = EventRecord(
            event_id=event_id,
            name=_require(p, "name"),
            city=_require(p, "city"),
            venue_public=_require(p, "venue_public"),
            held_at=_require(p, "held_at"),
            artwork_ids=list(artwork_ids),
            happened=bool(p.get("happened", True)),
            source=source,
        )
        self.events[event_id] = record
        return {"event_id": event_id, "created": True}

    # ------------------------------------------------------------- 服务端写入

    def save_plan(self, plan: dict[str, Any]) -> None:
        with self._lock:
            self.plans[plan["plan_id"]] = plan
            self._persist_locked()
