"""授权判定、发布审核与公开页投影（纯业务逻辑，不接触 HTTP）。

判定原则：

* 授权按 资产维度（姓名/肖像/作品）× 用途（现场/纸质/网络/对外）分别判断；
* 未取得授权且监护关系已核验 → “需补充同意”；已撤回或素材本身不可公开 → “不可使用”；
* 撤回只影响状态为 scheduled/ongoing 的未来与进行中用途；concluded 的使用是历史事实，不下线；
* 公开页只投影网络传播授权齐备、且已标记公开的素材，并去除精确位置、时间与未公开草图。
"""

from __future__ import annotations

from .models import (
    ASSET_LABELS,
    BUCKET_CONSENT_REQUIRED,
    BUCKET_UNUSABLE,
    BUCKET_USABLE,
    KIND_COMPLETE_LAYERED,
    KIND_EXHIBITION_PLAN,
    KIND_POSTER,
    KIND_SKETCH,
    SCOPE_LABELS,
    SCOPE_NETWORK,
    USAGE_CONCLUDED,
    ReviewItem,
    ReviewResult,
    visible_area,
)

COMPOSITE_KINDS = (KIND_POSTER, KIND_EXHIBITION_PLAN)


class ReviewEngine:
    def __init__(self, store) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 素材涉及的人与资产维度
    # ------------------------------------------------------------------
    def material_contributors(self, material: dict) -> list[dict]:
        """返回 [{alias, assets, detail}]，detail 说明为何涉及该人。

        完整作品按修订中的全部图层归集——被后画覆盖的图层仍是该孩子的贡献，
        授权与署名要求不因其不可见而消失。
        """
        kind = material.get("kind")

        if kind == KIND_COMPLETE_LAYERED and material.get("revision_id"):
            revision = self.store.revision(material["revision_id"])
            out: dict[str, dict] = {}
            if revision:
                order = {cid: i for i, cid in enumerate(revision["layers"])}
                layers = self.store.ordered_layers(revision["layers"])
                for layer in layers:
                    entry = out.setdefault(layer["alias"], {
                        "alias": layer["alias"],
                        "assets": ["artwork"],
                        "details": [],
                    })
                    entry["details"].append(
                        f"完整作品图层（落笔序 {layer.get('paint_order', order[layer['contribution_id']])}）"
                    )
            return list(out.values())

        if kind == "region-crop" and material.get("source_contribution_ids"):
            crop = material["region"]
            out = {}
            for cid in material["source_contribution_ids"]:
                layer = next((c for c in self.store.data["contributions"]
                              if c["contribution_id"] == cid), None)
                if not layer:
                    continue
                area = visible_area(layer["canvas_region"], [])
                overlap = area - visible_area(
                    layer["canvas_region"], [{"region": crop, "opaque": True}])
                # 该图层与裁切区域有交集才需要其授权
                if overlap <= 0:
                    continue
                entry = out.setdefault(layer["alias"], {
                    "alias": layer["alias"], "assets": ["artwork"], "details": []})
                entry["details"].append(f"局部画面裁切（相交面积 {overlap:.0f}）")
            return list(out.values())

        # 照片 / 草图 / 海报等：素材自带 contributors 声明
        return [
            {"alias": c["alias"], "assets": list(c.get("assets", ["artwork"])),
             "details": [c.get("detail", kind or "material")]}
            for c in material.get("contributors", [])
        ]

    def component_materials(self, material: dict) -> list[dict]:
        """海报/展览方案引用的子素材。"""
        if material.get("kind") not in COMPOSITE_KINDS:
            return []
        out = []
        for mid in material.get("material_ids", []):
            child = self.store.material(mid)
            if child:
                out.append(child)
        return out

    # ------------------------------------------------------------------
    # 单项授权检查
    # ------------------------------------------------------------------
    def _check_asset(self, alias: str, asset: str, scope: str, detail: str) -> ReviewItem:
        participant = self.store.participant(alias)
        label = f"{ASSET_LABELS[asset]}（{SCOPE_LABELS[scope]}）"
        if participant is None:
            return ReviewItem(alias, asset, scope, "unregistered",
                              f"{detail}：代号 {alias} 未登记，无法核验监护关系")
        verification = participant.get("guardian_verification") or {}
        if verification.get("status") != "verified":
            status_text = {"pending": "核验中", "none": "未核验"}.get(
                verification.get("status"), verification.get("status", "未知"))
            return ReviewItem(alias, asset, scope, "unverified",
                              f"{detail}：{label}——{alias} 监护关系{status_text}，须先核验")
        state, record = self.store.grant_state(alias, asset, scope)
        if state is None:
            return ReviewItem(alias, asset, scope, "missing",
                              f"{detail}：{alias} 未授予{label}")
        if state == "withdrawn":
            return ReviewItem(alias, asset, scope, "withdrawn",
                              f"{detail}：{alias} 的{label}授权已于 "
                              f"{record['effective_at']} 撤回，禁止用于新发布")
        return ReviewItem(alias, asset, scope, "ok",
                          f"{detail}：{alias} 的{label}授权有效")

    # ------------------------------------------------------------------
    # 审核整个素材
    # ------------------------------------------------------------------
    def review_material(self, material_id: str, scope: str) -> ReviewResult:
        material = self.store.material(material_id)
        if material is None:
            return ReviewResult(material_id, scope, BUCKET_UNUSABLE,
                                reasons=[f"素材 {material_id} 不存在"])

        result = ReviewResult(material_id, scope, BUCKET_USABLE)

        # 未公开的个人草图一律不可进入对外物料
        if material.get("kind") == KIND_SKETCH and not material.get("public_released"):
            result.bucket = BUCKET_UNUSABLE
            result.reasons.append("个人草图未公开，不得用于任何对外物料")

        # 海报/展览方案：递归审核每个子素材，任一不可用则整体不可用
        for child in self.component_materials(material):
            child_result = self.review_material(child["material_id"], scope)
            result.items.extend(child_result.items)
            result.reasons.extend(
                f"[子素材 {child['material_id']}] {r}" for r in child_result.reasons)
            if child_result.bucket == BUCKET_UNUSABLE:
                result.bucket = BUCKET_UNUSABLE
            elif child_result.bucket == BUCKET_CONSENT_REQUIRED and \
                    result.bucket != BUCKET_UNUSABLE:
                result.bucket = BUCKET_CONSENT_REQUIRED

        for contributor in self.material_contributors(material):
            alias = contributor["alias"]
            for asset in contributor["assets"]:
                detail = "；".join(contributor["details"])
                item = self._check_asset(alias, asset, scope, detail)
                result.items.append(item)
                if item.status == "withdrawn":
                    result.bucket = BUCKET_UNUSABLE
                elif item.blocked and result.bucket != BUCKET_UNUSABLE:
                    result.bucket = BUCKET_CONSENT_REQUIRED

        blocked = [i for i in result.items if i.blocked]
        if blocked and not result.reasons:
            withdrawn = [i for i in blocked if i.status == "withdrawn"]
            result.reasons = [i.reason for i in (withdrawn or blocked)]
        elif result.bucket == BUCKET_USABLE:
            result.reasons.append("全部涉及人员的对应授权齐备，可用于"
                                  f"{SCOPE_LABELS[scope]}")

        saved = self.store.add_review(result.to_dict())
        result.review_id = saved["review_id"]
        return result

    def review_submission(self, material_ids: list[str], scope: str) -> dict:
        """发布人员提交海报或展览方案，逐项给出三档结论。"""
        items = []
        overall = BUCKET_USABLE
        for mid in material_ids:
            r = self.review_material(mid, scope).to_dict()
            items.append(r)
            if r["bucket"] == BUCKET_UNUSABLE:
                overall = BUCKET_UNUSABLE
            elif r["bucket"] == BUCKET_CONSENT_REQUIRED and overall != BUCKET_UNUSABLE:
                overall = BUCKET_CONSENT_REQUIRED
        return {
            "scope": scope,
            "scope_label": SCOPE_LABELS[scope],
            "overall_bucket": overall,
            "items": items,
        }

    # ------------------------------------------------------------------
    # 撤回后的下线清单（只影响未来与进行中用途）
    # ------------------------------------------------------------------
    def takedown_list(self, alias: str | None = None, scope: str | None = None) -> list[dict]:
        affected = []
        for usage in self.store.data["usages"]:
            if usage.get("status") == USAGE_CONCLUDED:
                continue  # 已举行的活动是历史事实，保留
            if scope and usage.get("scope") != scope:
                continue
            hit_items = []
            for mid in usage.get("material_ids", []):
                material = self.store.material(mid)
                if not material:
                    continue
                for contributor in self.material_contributors(material):
                    if alias and contributor["alias"] != alias:
                        continue
                    for asset in contributor["assets"]:
                        state, record = self.store.grant_state(
                            contributor["alias"], asset, usage.get("scope", scope or ""))
                        if state == "withdrawn":
                            hit_items.append({
                                "material_id": mid,
                                "alias": contributor["alias"],
                                "asset": asset,
                                "withdrawn_at": record["effective_at"],
                            })
            if hit_items:
                affected.append({
                    "usage_id": usage["usage_id"],
                    "title": usage.get("title", ""),
                    "status": usage.get("status"),
                    "scope": usage.get("scope"),
                    "action": "须在用途生效前下线或替换相关素材",
                    "items": hit_items,
                })
        return affected

    def _all_contributors(self, material: dict) -> list[dict]:
        """素材自身 + 递归子素材涉及的全部人员（用于网络授权判定）。"""
        merged: dict[str, set[str]] = {}
        details: dict[str, list[str]] = {}

        def absorb(mat: dict, prefix: str = "") -> None:
            for child in self.component_materials(mat):
                absorb(child, f"{prefix}{mat['material_id']}→")
            for contributor in self.material_contributors(mat):
                assets = merged.setdefault(contributor["alias"], set())
                assets.update(contributor["assets"])
                details.setdefault(contributor["alias"], []).extend(
                    f"{prefix}{d}" for d in contributor["details"])

        absorb(material)
        return [{"alias": alias, "assets": sorted(assets),
                 "details": details[alias]} for alias, assets in merged.items()]

    # ------------------------------------------------------------------
    # 公开页投影：只显示获准内容，并做脱敏
    # ------------------------------------------------------------------
    def public_feed(self) -> list[dict]:
        feed = []
        for material in self.store.data["materials"].values():
            if material.get("kind") == KIND_SKETCH:
                continue  # 未公开草图永不进公开页
            if not material.get("public_released", True):
                continue
            # 复合素材（海报/展览方案）的任一子素材是未公开草图，直接排除
            if any(c.get("kind") == KIND_SKETCH and not c.get("public_released")
                   for c in self.component_materials(material)):
                continue
            denied = False
            credits = []
            for contributor in self._all_contributors(material):
                p_alias = contributor["alias"]
                for asset in contributor["assets"]:
                    state, _ = self.store.grant_state(p_alias, asset, SCOPE_NETWORK)
                    if state != "granted":
                        denied = True  # 未授权或已撤回网络传播
                # 署名需单独的姓名授权；否则一律匿名
                name_state, _ = self.store.grant_state(p_alias, "name", SCOPE_NETWORK)
                if name_state == "granted":
                    participant = self.store.participant(p_alias) or {}
                    credits.append(participant.get("public_name") or p_alias)
                else:
                    credits.append("匿名参与者")
            if denied:
                continue
            feed.append(self._project(material, credits))
        return feed

    @staticmethod
    def _project(material: dict, credits: list[str]) -> dict:
        """公开视图：剥离联系方式、精确坐标/轨迹与内部元数据。"""
        capture = material.get("capture") or {}
        location_info = material.get("location") or {}
        # 只保留城市级：素材自带 location 或拍摄元数据均可
        coarse_location = location_info.get("city") or capture.get("city")
        projected = {
            "material_id": material["material_id"],
            "kind": material.get("kind"),
            "title": material.get("title", ""),
            "description": material.get("description", ""),
            "credits": sorted(set(credits)),
            "location": coarse_location,
            "reference": material.get("reference", ""),
        }
        return projected
