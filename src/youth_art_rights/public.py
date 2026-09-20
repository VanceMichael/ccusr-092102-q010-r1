"""公众页面投影。

硬规则：

* 只输出当前在「网络传播」用途上授权有效的素材；
* 个人草图未公开（``released=False``）一律不出现；
* 监护人联系方式、监护核验凭证、设备来源、精确坐标元数据全部剥离；
* 参与者对外只显示代号或笔名，不展示精确活动轨迹，只给到城市/公开场地；
* 作品历史修订作为事实可以列出，但每一修订只暴露获准图层。
"""

from __future__ import annotations

from typing import Any

from .models import SCOPE_ONLINE_DISTRIBUTION
from .review import VERDICT_USABLE, asset_people, consent_state
from .store import Store

_PUBLIC_PARTICIPANT_FIELDS = ("alias", "public_pen_name")


def _public_participant(store: Store, alias: str) -> dict[str, Any]:
    participant = store.participants[alias]
    return {
        "alias": participant.alias,
        "display_name": participant.public_pen_name or participant.alias,
    }


def _asset_is_public(store: Store, asset_id: str) -> bool:
    asset = store.assets.get(asset_id)
    if asset is None or not asset.released:
        return False
    people = asset_people(store, asset_id)
    if not people:
        return False
    return all(
        consent_state(store, alias, SCOPE_ONLINE_DISTRIBUTION, asset_id)["granted"]
        for alias in people
    )


def public_asset(store: Store, asset_id: str) -> dict[str, Any] | None:
    """单个素材的公众视图；不可见时返回 None（调用方按 404 处理，不暴露存在性）。"""
    if not _asset_is_public(store, asset_id):
        return None
    asset = store.assets[asset_id]
    return {
        "asset_id": asset.asset_id,
        "kind": asset.kind,
        "title": asset.title,
        "authors": [_public_participant(store, a) for a in asset_people(store, asset_id)],
        "uri": asset.uri,
        "captured_at": asset.captured_at,
    }


def public_artwork(store: Store, artwork_id: str, revision: int | None = None) -> dict[str, Any] | None:
    """完整作品的公众视图：逐修订列出，且每个修订只含获准图层。"""
    revisions = store.artworks.get(artwork_id)
    if not revisions:
        return None
    target = revision
    chosen = next((r for r in revisions if r.revision == target), None) if target else revisions[-1]
    if chosen is None:
        return None

    public_layers = []
    for layer_id in chosen.layer_ids:
        layer = store.contributions[layer_id]
        alias = layer.participant_alias
        state = consent_state(store, alias, SCOPE_ONLINE_DISTRIBUTION, layer.asset_id)
        if not state["granted"]:
            continue  # 未授权/已撤回的图层不展示；记录仍在内部保留
        public_layers.append(
            {
                "contribution_id": layer.contribution_id,
                "z_order": layer.z_order,
                "participant": _public_participant(store, alias),
                "region": layer.region,
                "asset": public_asset(store, layer.asset_id) if layer.asset_id else None,
            }
        )

    revision_list = [
        {
            "revision": r.revision,
            "parent_revision": r.parent_revision,
            "title": r.title,
            "created_at": r.created_at,
            "note": r.note,
            "layer_count": len(r.layer_ids),
            "visible_layer_count": sum(
                1
                for lid in r.layer_ids
                if lid in store.contributions
                and consent_state(
                    store,
                    store.contributions[lid].participant_alias,
                    SCOPE_ONLINE_DISTRIBUTION,
                    store.contributions[lid].asset_id,
                )["granted"]
            ),
        }
        for r in revisions
    ]

    composite = public_asset(store, chosen.asset_id) if chosen.asset_id else None
    return {
        "artwork_id": artwork_id,
        "title": chosen.title,
        "revision": chosen.revision,
        "parent_revision": chosen.parent_revision,
        "revisions": revision_list,  # 历史修订作为事实保留
        "layers": public_layers,
        "composite": composite,
    }


def public_events(store: Store) -> list[dict[str, Any]]:
    """活动历史：仅城市与公开场地，不含任何参与者行程信息。"""
    return [
        {
            "event_id": record.event_id,
            "name": record.name,
            "city": record.city,
            "venue": record.venue_public,
            "held_at": record.held_at,
            "artwork_ids": record.artwork_ids,
        }
        for record in store.events.values()
        if record.happened
    ]


def public_catalog(store: Store) -> dict[str, Any]:
    """公众首页：获准素材 + 作品最新修订 + 活动历史。"""
    assets = [view for asset_id in store.assets if (view := public_asset(store, asset_id))]
    artworks = [public_artwork(store, artwork_id) for artwork_id in store.artworks]
    return {
        "artworks": [a for a in artworks if a is not None],
        "standalone_assets": assets,
        "events": public_events(store),
    }
