"""授权求值与发布方案评审。

授权模型：

* 授权按事件追加，同一（参与者、用途、素材集合）以 ``effective_at`` 最晚的事件为准；
* 授予/撤回只影响求值时刻之后的使用判断；
* 素材若涉及多名参与者（照片），任一相关人在该用途上未授权即不可用；
* 完整作品与局部画面以其上所有贡献图层的参与者授权为准。
"""

from __future__ import annotations

from typing import Any

from .models import (
    CONSENT_SCOPES,
    KIND_COMPLETE_ARTWORK,
    KIND_PERSONAL_SKETCH,
    KIND_PHOTO,
    KIND_REGION_CROP,
    SCOPE_LABELS,
    ArtworkRevision,
)
from .store import Store

# 评审结论
VERDICT_USABLE = "usable"                # 可用
VERDICT_CONSENT_NEEDED = "consent_needed"  # 需补充同意
VERDICT_BLOCKED = "blocked"              # 不可使用


def _events_for(store: Store, alias: str, scope: str, asset_id: str | None):
    """与某参与者/用途/素材相关、在 at 之前已生效的事件，按生效时间排序。"""
    related = []
    for event in store.consent_events:
        if event.participant_alias != alias or event.scope != scope:
            continue
        if event.asset_ids is not None and asset_id not in event.asset_ids:
            continue
        related.append(event)
    related.sort(key=lambda e: (e.effective_at, e.recorded_at))
    return related


def consent_state(
    store: Store, alias: str, scope: str, asset_id: str | None = None
) -> dict[str, Any]:
    """单个参与者在某用途（可限定素材）上的当前授权状态。"""
    events = _events_for(store, alias, scope, asset_id)
    if not events:
        return {"granted": False, "status": "never_granted", "basis": None, "events": []}
    latest = events[-1]
    return {
        "granted": latest.action == "grant",
        "status": "granted" if latest.action == "grant" else "revoked",
        "basis": latest.event_id,
        "latest_effective_at": latest.effective_at,
        "events": [
            {
                "event_id": e.event_id,
                "action": e.action,
                "effective_at": e.effective_at,
                "asset_ids": e.asset_ids,
            }
            for e in events
        ],
    }


def asset_people(store: Store, asset_id: str) -> list[str]:
    """素材使用时需要获得授权的全部参与者代号。"""
    asset = store.assets.get(asset_id)
    if asset is None:
        return []
    people: list[str] = []
    if asset.author_alias:
        people.append(asset.author_alias)
    for alias in asset.subjects:
        if alias not in people:
            people.append(alias)
    if asset.kind in (KIND_REGION_CROP, KIND_COMPLETE_ARTWORK):
        # 局部/完整画面：叠加图层贡献者的授权（区域裁剪按区域内图层筛选）
        region = asset.metadata.get("region")
        artwork_id = asset.metadata.get("artwork_id")
        revision_no = asset.metadata.get("revision")
        layers = _revision_layers(store, artwork_id, revision_no)
        for layer in layers:
            if region is not None and not _regions_overlap(layer.region, region):
                continue
            if layer.participant_alias not in people:
                people.append(layer.participant_alias)
    return people


def _revision_layers(
    store: Store, artwork_id: str | None, revision_no: int | None
) -> list[Any]:
    if not artwork_id or artwork_id not in store.artworks:
        return []
    revisions = store.artworks[artwork_id]
    if not revisions:
        return []
    revision: ArtworkRevision | None = None
    if revision_no is not None:
        revision = next((r for r in revisions if r.revision == revision_no), None)
    revision = revision or revisions[-1]
    return [store.contributions[lid] for lid in revision.layer_ids if lid in store.contributions]


def _regions_overlap(a: dict[str, int], b: dict[str, int]) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def review_asset_use(store: Store, asset_id: str, scope: str) -> dict[str, Any]:
    """逐项评审一条素材使用，给出结论与可操作原因。"""
    if scope not in CONSENT_SCOPES:
        return {
            "verdict": VERDICT_BLOCKED,
            "reasons": [f"未知授权用途: {scope}"],
            "people": [],
        }
    asset = store.assets.get(asset_id)
    if asset is None:
        return {
            "verdict": VERDICT_BLOCKED,
            "reasons": ["素材不存在，无法使用"],
            "people": [],
        }

    reasons: list[str] = []
    people = asset_people(store, asset_id)
    if not people:
        reasons.append("素材没有可追溯的参与者，无法证明授权来源")

    person_states: list[dict[str, Any]] = []
    anyone_revoked = False
    for alias in people:
        state = consent_state(store, alias, scope, asset_id)
        person_states.append({"alias": alias, **{k: v for k, v in state.items() if k != "events"}})
        if state["status"] == "revoked":
            anyone_revoked = True
            reasons.append(
                f"{alias} 已撤回「{SCOPE_LABELS[scope]}」授权"
                f"（依据 {state['basis']}，撤回仅影响此后的新使用）"
            )
        elif state["status"] == "never_granted":
            reasons.append(f"{alias} 尚未授予「{SCOPE_LABELS[scope]}」授权，需补充监护人同意")

    if asset.kind == KIND_PERSONAL_SKETCH and not asset.released:
        reasons.append("该素材为未公开个人草图，除需授权外不得出现在公众页面")

    if asset.kind == KIND_PHOTO and not people:
        reasons.append("现场照片缺少肖像主体登记")

    if people and all(s["status"] == "granted" for s in person_states):
        verdict = VERDICT_USABLE
        reasons = [f"全部 {len(people)} 名相关参与者在「{SCOPE_LABELS[scope]}」上授权有效"]
    elif anyone_revoked:
        verdict = VERDICT_BLOCKED
    else:
        verdict = VERDICT_CONSENT_NEEDED

    # 非素材作者/肖像主体的硬伤同样阻断
    if asset.kind == KIND_PERSONAL_SKETCH and not asset.released and verdict == VERDICT_USABLE:
        verdict = VERDICT_CONSENT_NEEDED

    return {
        "verdict": verdict,
        "reasons": reasons,
        "people": person_states,
        "asset_kind": asset.kind,
    }


def review_plan(store: Store, items: list[dict[str, Any]]) -> dict[str, Any]:
    """评审整张方案，逐项给出可用 / 需补充同意 / 不可使用。"""
    results = []
    counts = {VERDICT_USABLE: 0, VERDICT_CONSENT_NEEDED: 0, VERDICT_BLOCKED: 0}
    for index, item in enumerate(items):
        asset_id = item.get("asset_id", "")
        scope = item.get("scope", "")
        outcome = review_asset_use(store, asset_id, scope)
        row = {
            "item_id": item.get("item_id") or f"item-{index + 1}",
            "asset_id": asset_id,
            "scope": scope,
            "scope_label": SCOPE_LABELS.get(scope, scope),
            "note": item.get("note", ""),
            **outcome,
        }
        counts[row["verdict"]] += 1
        results.append(row)
    overall = (
        VERDICT_USABLE
        if counts[VERDICT_BLOCKED] == 0 and counts[VERDICT_CONSENT_NEEDED] == 0
        else VERDICT_BLOCKED
        if counts[VERDICT_BLOCKED] > 0
        else VERDICT_CONSENT_NEEDED
    )
    return {"overall": overall, "counts": counts, "items": results}


def revocation_impact(store: Store, alias: str, scope: str | None = None) -> dict[str, Any]:
    """撤回某授权后，未来需要下线/停用的素材与方案条目（历史活动不受影响）。"""
    scopes = (scope,) if scope else CONSENT_SCOPES
    affected_assets: list[dict[str, Any]] = []
    for asset_id in store.assets:
        if alias not in asset_people(store, asset_id):
            continue
        per_scope = {s: review_asset_use(store, asset_id, s)["verdict"] for s in scopes}
        if any(v != VERDICT_USABLE for v in per_scope.values()):
            affected_assets.append({"asset_id": asset_id, "verdicts": per_scope})
    affected_plans = []
    for plan in store.plans.values():
        review = plan.get("review") or {}
        for row in review.get("items", []):
            if alias in [p["alias"] for p in row.get("people", [])] and row["scope"] in scopes:
                if row["verdict"] != VERDICT_USABLE:
                    affected_plans.append(
                        {"plan_id": plan["plan_id"], "item_id": row["item_id"], "verdict": row["verdict"]}
                    )
    return {
        "alias": alias,
        "note": "撤回仅影响未来使用；已举行的活动作为历史事实保留，不删除贡献图层",
        "affected_assets": affected_assets,
        "affected_plan_items": affected_plans,
    }
