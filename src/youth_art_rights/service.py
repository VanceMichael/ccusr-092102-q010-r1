"""发布方案的提交、评审与归档。"""

from __future__ import annotations

import uuid
from typing import Any

from .models import CONSENT_SCOPES, now_iso
from .review import review_plan
from .store import Store, ValidationError


def submit_plan(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    """接收海报/展览方案，逐项评审后归档，返回带结论的完整方案。"""
    title = body.get("title")
    if not title:
        raise ValidationError("缺少方案标题 title")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("方案至少包含一项 items")
    for index, item in enumerate(items):
        if not item.get("asset_id"):
            raise ValidationError(f"第 {index + 1} 项缺少 asset_id")
        if item.get("scope") not in CONSENT_SCOPES:
            raise ValidationError(
                f"第 {index + 1} 项 scope 必须是 {', '.join(CONSENT_SCOPES)} 之一"
            )

    review = review_plan(store, items)
    plan = {
        "plan_id": body.get("plan_id") or f"plan-{uuid.uuid4().hex[:12]}",
        "title": title,
        "submitter": body.get("submitter", "anonymous"),
        "submitted_at": now_iso(),
        "items": [
            {"item_id": item.get("item_id") or f"item-{i + 1}", **item}
            for i, item in enumerate(items)
        ],
        "review": review,
    }
    if plan["plan_id"] in store.plans:
        raise ValidationError(f"plan_id 已存在: {plan['plan_id']}")
    store.save_plan(plan)
    return plan


def get_plan(store: Store, plan_id: str) -> dict[str, Any] | None:
    return store.plans.get(plan_id)
