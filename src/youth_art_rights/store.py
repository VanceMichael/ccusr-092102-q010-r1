"""JSON 仓储 + 离线设备幂等合并。

现场网络不稳时，多台设备各自产生带 (device_id, op_sequence) 的操作；
重连后整批提交。合并规则：

* 操作以 (device_id, op_sequence) 为幂等键，重复提交只返回首次结果；
* 每台设备维护已合并到的最大序号；
* 同代号重复登记且内容一致视为重复，内容冲突记录到 conflicts 且不覆盖先到数据；
* 后画覆盖前画只改变展平可见性，原贡献记录永不删除（见 models.visible_area）。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .models import ASSETS, GRANT_GRANTED, SCOPES, now_iso, parse_time


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Store:
    """内存记录 + JSON 文件落盘。所有写操作在同一把锁内串行化。"""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.RLock()
        self._data: dict = self._empty()
        if self.path and self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def _empty() -> dict:
        return {
            "participants": {},          # alias -> 参与者记录
            "contributions": [],         # 画布图层（每次落笔一条）
            "materials": {},             # material_id -> 素材记录
            "revisions": [],             # 完整作品修订（层次快照）
            "grants": [],                # 授权变更流水（只追加）
            "usages": [],                # 已排期/进行/结束的使用记录
            "reviews": [],               # 审核记录
            "applied_ops": {},           # "device/seq" -> 操作结果摘要
            "device_cursors": {},        # device_id -> 已合并最大序号
        }

    # --- 持久化 ---
    def _flush(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset(self) -> None:
        with self._lock:
            self._data = self._empty()
            self._flush()

    # --- 只读访问 ---
    @property
    def data(self) -> dict:
        return self._data

    def participant(self, alias: str) -> dict | None:
        return self._data["participants"].get(alias)

    def material(self, material_id: str) -> dict | None:
        return self._data["materials"].get(material_id)

    def contributions_for_alias(self, alias: str) -> list[dict]:
        return [c for c in self._data["contributions"] if c["alias"] == alias]

    def revision(self, revision_id: str) -> dict | None:
        return next((r for r in self._data["revisions"] if r["revision_id"] == revision_id), None)

    # --- 监护核验 ---
    def set_guardian_verification(self, alias: str, status: str, method: str,
                                  reference: str = "", verified_at: str | None = None) -> dict:
        with self._lock:
            p = self._data["participants"].setdefault(alias, {"alias": alias})
            p["guardian_verification"] = {
                "status": status,            # verified / pending / none
                "method": method,            # 如 书面同意书编号、现场视频核验
                "reference": reference,      # 凭证编号（不存联系方式本体）
                "verified_at": verified_at or now_iso(),
            }
            self._flush()
            return p["guardian_verification"]

    # --- 授权 ---
    def record_grant(self, alias: str, asset: str, scope: str, state: str,
                     effective_at: str | None = None, recorded_by: str = "staff",
                     client_op: str | None = None) -> dict:
        if asset not in ASSETS:
            raise ValueError(f"未知授权资产维度: {asset}")
        if scope not in SCOPES:
            raise ValueError(f"未知授权用途: {scope}")
        if state not in ("granted", "withdrawn"):
            raise ValueError(f"未知授权状态: {state}")
        with self._lock:
            record = {
                "grant_id": _new_id("grant"),
                "alias": alias,
                "asset": asset,
                "scope": scope,
                "state": state,
                "effective_at": effective_at or now_iso(),
                "recorded_by": recorded_by,
                "client_op": client_op,
            }
            self._data["grants"].append(record)
            self._flush()
            return record

    def grant_state(self, alias: str, asset: str, scope: str,
                    at_time: str | None = None) -> tuple[str | None, dict | None]:
        """返回 (state, record)。at_time 给定时只统计该时刻前已生效的记录。

        撤回不删除早先的同意记录：历史排期可凭快照证明当时合法。
        """
        cutoff = parse_time(at_time) if at_time else None
        latest: dict | None = None
        for g in self._data["grants"]:
            if (g["alias"], g["asset"], g["scope"]) != (alias, asset, scope):
                continue
            if cutoff and parse_time(g["effective_at"]) > cutoff:
                continue
            if latest is None or parse_time(g["effective_at"]) >= parse_time(latest["effective_at"]):
                latest = g
        return (latest["state"] if latest else None), latest

    # --- 素材 ---
    def add_material(self, material: dict) -> dict:
        with self._lock:
            mid = material["material_id"]
            if mid in self._data["materials"]:
                raise ValueError(f"素材已存在: {mid}")
            material.setdefault("created_at", now_iso())
            material.setdefault("public_released", material.get("kind") != "sketch")
            self._data["materials"][mid] = material
            self._flush()
            return material

    # --- 图层与修订 ---
    def add_contribution(self, contribution: dict) -> dict:
        with self._lock:
            cid = contribution["contribution_id"]
            if any(c["contribution_id"] == cid for c in self._data["contributions"]):
                raise ValueError(f"贡献已存在: {cid}")
            contribution.setdefault("created_at", now_iso())
            contribution.setdefault("opaque", True)
            self._data["contributions"].append(contribution)
            self._flush()
            return contribution

    def ordered_layers(self, contribution_ids: list[str]) -> list[dict]:
        """按落笔顺序返回图层；同序号以设备序号兜底，保证合并后顺序确定。"""
        layers = [c for c in self._data["contributions"] if c["contribution_id"] in contribution_ids]
        layers.sort(key=lambda c: (c.get("paint_order", 0), c.get("device_sequence", 0),
                                   c.get("device_id", ""), c["contribution_id"]))
        return layers

    def add_revision(self, layers: list[str], flattened_material_id: str | None,
                     note: str = "", parent_revision_id: str | None = None,
                     created_at: str | None = None) -> dict:
        with self._lock:
            known = {c["contribution_id"] for c in self._data["contributions"]}
            missing = [cid for cid in layers if cid not in known]
            if missing:
                raise ValueError(f"修订引用了不存在的图层: {missing}")
            revision = {
                "revision_id": _new_id("rev"),
                "parent_revision_id": parent_revision_id,
                "layers": list(layers),
                "flattened_material_id": flattened_material_id,
                "note": note,
                "created_at": created_at or now_iso(),
            }
            self._data["revisions"].append(revision)
            self._flush()
            return revision

    # --- 使用记录（历史事实） ---
    def add_usage(self, usage: dict) -> dict:
        with self._lock:
            usage.setdefault("usage_id", _new_id("use"))
            usage.setdefault("created_at", now_iso())
            self._data["usages"].append(usage)
            self._flush()
            return usage

    def add_review(self, review: dict) -> dict:
        with self._lock:
            review.setdefault("review_id", _new_id("revw"))
            review.setdefault("reviewed_at", now_iso())
            self._data["reviews"].append(review)
            self._flush()
            return review

    # ------------------------------------------------------------------
    # 离线批量合并
    # ------------------------------------------------------------------
    def sync_batch(self, device_id: str, ops: list[dict]) -> dict:
        """把一台设备积压的登记操作幂等并入。

        每条 op 必须带 op_sequence（设备内单调递增）。
        返回 applied / skipped_duplicates / conflicts 明细，便于设备端核对。
        """
        applied: list[dict] = []
        skipped: list[dict] = []
        conflicts: list[dict] = []
        with self._lock:
            for op in ops:
                seq = op.get("op_sequence")
                if not isinstance(seq, int):
                    raise ValueError("每条离线操作必须包含整数 op_sequence")
                key = f"{device_id}/{seq}"
                if key in self._data["applied_ops"]:
                    skipped.append({"op_sequence": seq,
                                    "result": self._data["applied_ops"][key]})
                    continue
                summary = self._apply_op(device_id, seq, op, conflicts)
                self._data["applied_ops"][key] = summary
                applied.append({"op_sequence": seq, "result": summary})
            if ops:
                self._data["device_cursors"][device_id] = max(
                    [seq for seq in (
                        self._data["device_cursors"].get(device_id, 0),
                    )] + [op["op_sequence"] for op in ops]
                )
            self._flush()
        return {
            "device_id": device_id,
            "applied": applied,
            "skipped_duplicates": skipped,
            "conflicts": conflicts,
            "latest_sequence": self._data["device_cursors"].get(device_id, 0),
        }

    def _apply_op(self, device_id: str, seq: int, op: dict, conflicts: list[dict]) -> str:
        kind = op.get("type")
        payload = op.get("payload", {})

        if kind == "register_participant":
            alias = payload["alias"]
            existing = self._data["participants"].get(alias)
            if existing is None:
                self._data["participants"][alias] = {
                    "alias": alias,
                    "device_id": device_id,
                    "device_sequence": seq,
                    "registered_at": payload.get("registered_at", now_iso()),
                    "guardian_verification": payload.get("guardian_verification",
                                                        {"status": "none"}),
                }
                return f"registered:{alias}"
            # 已登记：核验信息以更完整者为准，其余字段冲突不覆盖
            incoming = payload.get("guardian_verification") or {}
            current = existing.get("guardian_verification") or {}
            rank = {"verified": 2, "pending": 1, "none": 0}
            if rank.get(incoming.get("status"), 0) > rank.get(current.get("status"), 0):
                existing["guardian_verification"] = incoming
                return f"guardian-upgraded:{alias}"
            if incoming and incoming.get("status") != current.get("status"):
                conflicts.append({
                    "op_sequence": seq, "alias": alias,
                    "reason": "监护核验信息与先到设备不一致，保留先到结果",
                })
            return f"already-registered:{alias}"

        if kind == "add_contribution":
            c = dict(payload)
            c.setdefault("device_id", device_id)
            c.setdefault("device_sequence", seq)
            cid = c["contribution_id"]
            if any(x["contribution_id"] == cid for x in self._data["contributions"]):
                return f"duplicate-contribution:{cid}"
            c.setdefault("created_at", c.get("recorded_at", now_iso()))
            c.setdefault("opaque", True)
            self._data["contributions"].append(c)
            return f"contribution:{cid}"

        if kind == "add_material":
            m = dict(payload)
            mid = m["material_id"]
            if mid in self._data["materials"]:
                return f"duplicate-material:{mid}"
            m.setdefault("device_id", device_id)
            m.setdefault("device_sequence", seq)
            m.setdefault("created_at", m.get("recorded_at", now_iso()))
            m.setdefault("public_released", m.get("kind") != "sketch")
            self._data["materials"][mid] = m
            return f"material:{mid}"

        if kind == "record_grant":
            g = self.record_grant(
                alias=payload["alias"], asset=payload["asset"],
                scope=payload["scope"], state=payload.get("state", GRANT_GRANTED),
                effective_at=payload.get("effective_at"),
                recorded_by=payload.get("recorded_by", f"device:{device_id}"),
                client_op=f"{device_id}/{seq}",
            )
            return f"grant:{g['grant_id']}"

        if kind == "record_guardian_verification":
            self.set_guardian_verification(
                alias=payload["alias"], status=payload["status"],
                method=payload.get("method", ""),
                reference=payload.get("reference", ""),
                verified_at=payload.get("verified_at"),
            )
            return f"guardian:{payload['alias']}"

        raise ValueError(f"未知离线操作类型: {kind}")
