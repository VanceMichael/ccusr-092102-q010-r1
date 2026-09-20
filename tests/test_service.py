"""端到端场景测试：离线登记 → 合并 → 图层修订 → 授权评审 → 公开投影。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from youth_art_rights.models import (
    SCOPE_EXTERNAL_PROVISION,
    SCOPE_ON_SITE_DISPLAY,
    SCOPE_ONLINE_DISTRIBUTION,
    SCOPE_PRINT_PUBLICATION,
)
from youth_art_rights.public import public_artwork, public_asset, public_catalog
from youth_art_rights.review import (
    VERDICT_BLOCKED,
    VERDICT_CONSENT_NEEDED,
    VERDICT_USABLE,
    consent_state,
    review_asset_use,
    revocation_impact,
)
from youth_art_rights.service import submit_plan
from youth_art_rights.store import Store, ValidationError


def _op(device: str, seq: int, op_type: str, payload: dict, occurred_at: str | None = None) -> dict:
    op = {"device_id": device, "seq": seq, "type": op_type, "payload": payload}
    if occurred_at:
        op["occurred_at"] = occurred_at
    return op


def register_child(alias: str = "child-017", verified: bool = True) -> dict:
    return {
        "alias": alias,
        "guardian_verified": verified,
        "guardian_contact": "13800000000",
        "public_pen_name": "小绿",
    }


class OfflineSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()

    def test_idempotent_merge_by_device_sequence(self) -> None:
        batch = [
            _op("tablet-A", 1, "register_participant", register_child()),
            _op("tablet-A", 2, "register_participant", register_child("child-018")),
        ]
        first = self.store.sync(batch)
        self.assertEqual(len(first["applied"]), 2)
        self.assertEqual(first["duplicated"], [])

        # 网络恢复后同批重发：整批幂等，不产生重复记录
        second = self.store.sync(batch)
        self.assertEqual(second["applied"], [])
        self.assertEqual(
            {(d["device_id"], d["seq"]) for d in second["duplicated"]},
            {("tablet-A", 1), ("tablet-A", 2)},
        )
        self.assertEqual(len(self.store.participants), 2)

    def test_multiple_devices_merge_and_gap_report(self) -> None:
        self.store.sync([
            _op("tablet-A", 1, "register_participant", register_child()),
            _op("tablet-B", 3, "register_participant", register_child("child-018")),
        ])
        gaps = self.store.sync([])["device_gaps"]
        # tablet-B 跳过了 1、2，提示可能有离线批次尚未上传
        self.assertEqual(gaps, {"tablet-B": [1, 2]})

    def test_cross_device_forward_reference_resolves_by_replay(self) -> None:
        # 设备 B 先到（引用尚不存在的素材），设备 A 后到；多趟重放后全部生效
        result = self.store.sync([
            _op("tablet-B", 1, "create_artwork", {"artwork_id": "art-1", "title": "绿美广东"}),
            _op("tablet-A", 1, "register_participant", register_child()),
            _op("tablet-A", 2, "register_asset", {
                "asset_id": "sketch-1", "kind": "personal_sketch",
                "title": "木棉花草稿", "author_alias": "child-017",
            }),
            _op("tablet-B", 2, "record_contribution", {
                "contribution_id": "c-1", "artwork_id": "art-1",
                "participant_alias": "child-017", "asset_id": "sketch-1",
                "canvas_region": {"x": 0, "y": 0, "width": 100, "height": 100},
            }),
        ])
        self.assertEqual(len(result["rejected"]), 0)
        self.assertEqual(len(result["applied"]), 4)
        self.assertEqual(self.store.contributions["c-1"].z_order, 1)

    def test_unresolvable_operation_rejected_with_reason(self) -> None:
        result = self.store.sync([
            _op("tablet-A", 1, "record_contribution", {
                "contribution_id": "c-x", "artwork_id": "art-x",
                "participant_alias": "ghost",
                "canvas_region": {"x": 0, "y": 0, "width": 1, "height": 1},
            }),
        ])
        self.assertEqual(result["applied"], [])
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("参与者未登记", result["rejected"][0]["reason"])
        # 被拒绝的操作不占位，设备修正后可凭同序号重传
        self.assertNotIn(("tablet-A", 1), self.store.ops)

    def test_invalid_region_rejected(self) -> None:
        result = self.store.sync([
            _op("tablet-A", 1, "register_participant", register_child()),
            _op("tablet-A", 2, "create_artwork", {"artwork_id": "art-1", "title": "t"}),
            _op("tablet-A", 3, "record_contribution", {
                "contribution_id": "c-1", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 0, "y": 0, "width": -5, "height": 10},
            }),
        ])
        self.assertEqual(len(result["applied"]), 2)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("宽高", result["rejected"][0]["reason"])

    def test_malformed_batch_raises(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.sync([{"device_id": "dev", "seq": 0, "type": "x", "payload": {}}])


class LayeredArtworkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.store.sync([
            _op("dev", 1, "register_participant", register_child()),
            _op("dev", 2, "register_participant", register_child("child-018")),
            _op("dev", 3, "create_artwork", {"artwork_id": "art-1", "title": "二十米长卷"}),
            _op("dev", 4, "record_contribution", {
                "contribution_id": "layer-1", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 0, "y": 0, "width": 200, "height": 50},
            }),
            _op("dev", 5, "record_contribution", {
                "contribution_id": "layer-2", "artwork_id": "art-1",
                "participant_alias": "child-018",
                "canvas_region": {"x": 100, "y": 0, "width": 200, "height": 50},
            }),
            _op("dev", 6, "revise_artwork", {
                "artwork_id": "art-1", "layer_ids": ["layer-1", "layer-2"],
                "asset_id": "full-v1",
            }),
        ])
        self.store.sync([
            _op("dev", 7, "register_asset", {
                "asset_id": "full-v1", "kind": "complete_artwork", "title": "长卷合成 v1",
                "metadata": {"artwork_id": "art-1", "revision": 1},
            }),
        ])

    def test_overpainting_keeps_original_layer(self) -> None:
        # 第三层覆盖前两层，修订到 v2
        result = self.store.sync([
            _op("dev", 8, "record_contribution", {
                "contribution_id": "layer-3", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 50, "y": 0, "width": 300, "height": 50},
            }),
            _op("dev", 9, "revise_artwork", {
                "artwork_id": "art-1", "layer_ids": ["layer-1", "layer-2", "layer-3"],
                "asset_id": "full-v2",
            }),
        ])
        self.assertEqual(len(result["rejected"]), 0)
        revisions = self.store.artworks["art-1"]
        self.assertEqual([r.revision for r in revisions], [1, 2])
        # 原贡献仍可按 v1 还原：v1 的 layer_ids 未被修改
        self.assertEqual(revisions[0].layer_ids, ["layer-1", "layer-2"])
        self.assertEqual(revisions[1].layer_ids, ["layer-1", "layer-2", "layer-3"])
        self.assertEqual(self.store.contributions["layer-1"].z_order, 1)
        self.assertEqual(self.store.contributions["layer-3"].z_order, 3)

    def test_duplicate_contribution_id_is_idempotent(self) -> None:
        result = self.store.sync([
            _op("dev", 20, "record_contribution", {
                "contribution_id": "layer-1", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 0, "y": 0, "width": 200, "height": 50},
            }),
        ])
        self.assertEqual(result["applied"][0]["result"].get("existed"), True)
        self.assertEqual(len(self.store.contributions), 2)


class ConsentReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.store.sync([
            _op("dev", 1, "register_participant", register_child()),
            _op("dev", 2, "register_participant", register_child("child-018")),
            _op("dev", 3, "register_asset", {
                "asset_id": "sketch-1", "kind": "personal_sketch",
                "title": "个人草图", "author_alias": "child-017",
                "released": False,
            }),
            _op("dev", 4, "register_asset", {
                "asset_id": "photo-1", "kind": "photo", "title": "合影",
                "subjects": ["child-017", "child-018"], "released": True,
            }),
        ])

    def _grant(self, alias: str, scope: str, asset_ids=None, at: str = "2026-09-19T10:00:00+08:00"):
        self.store.sync([_op("dev", self._seq(), "consent_event", {
            "participant_alias": alias, "action": "grant", "scope": scope,
            "asset_ids": asset_ids, "effective_at": at,
        })])

    _counter = 100

    def _seq(self) -> int:
        self._counter += 1
        return self._counter

    def test_no_consent_means_consent_needed(self) -> None:
        outcome = review_asset_use(self.store, "sketch-1", SCOPE_ON_SITE_DISPLAY)
        self.assertEqual(outcome["verdict"], VERDICT_CONSENT_NEEDED)
        self.assertTrue(any("尚未授予" in r for r in outcome["reasons"]))

    def test_photo_requires_every_subject_consent(self) -> None:
        self._grant("child-017", SCOPE_ONLINE_DISTRIBUTION)
        outcome = review_asset_use(self.store, "photo-1", SCOPE_ONLINE_DISTRIBUTION)
        self.assertEqual(outcome["verdict"], VERDICT_CONSENT_NEEDED)
        self.assertIn("child-018", [p["alias"] for p in outcome["people"]])

        self._grant("child-018", SCOPE_ONLINE_DISTRIBUTION)
        outcome = review_asset_use(self.store, "photo-1", SCOPE_ONLINE_DISTRIBUTION)
        self.assertEqual(outcome["verdict"], VERDICT_USABLE)

    def test_scopes_are_independent(self) -> None:
        self._grant("child-017", SCOPE_ON_SITE_DISPLAY)
        self.assertTrue(consent_state(self.store, "child-017", SCOPE_ON_SITE_DISPLAY)["granted"])
        self.assertFalse(consent_state(self.store, "child-017", SCOPE_PRINT_PUBLICATION)["granted"])
        self.assertFalse(consent_state(self.store, "child-017", SCOPE_EXTERNAL_PROVISION)["granted"])

    def test_revocation_blocks_future_use_but_keeps_history(self) -> None:
        self._grant("child-017", SCOPE_ONLINE_DISTRIBUTION, at="2026-09-19T09:00:00+08:00")
        self.assertTrue(consent_state(self.store, "child-017", SCOPE_ONLINE_DISTRIBUTION)["granted"])
        self.store.sync([
            _op("dev", self._seq(), "record_event", {
                "event_id": "evt-1", "name": "中山纪念堂共创", "city": "广州",
                "venue_public": "中山纪念堂", "held_at": "2026-09-19T09:30:00+08:00",
            }),
            _op("dev", self._seq(), "consent_event", {
                "participant_alias": "child-017", "action": "revoke",
                "scope": SCOPE_ONLINE_DISTRIBUTION,
                "effective_at": "2026-09-20T08:00:00+08:00",
                "reason": "监护人调整网络传播授权",
            }),
        ])
        state = consent_state(self.store, "child-017", SCOPE_ONLINE_DISTRIBUTION)
        self.assertFalse(state["granted"])
        self.assertEqual(state["status"], "revoked")

        outcome = review_asset_use(self.store, "photo-1", SCOPE_ONLINE_DISTRIBUTION)
        self.assertEqual(outcome["verdict"], VERDICT_BLOCKED)
        self.assertTrue(any("撤回" in r for r in outcome["reasons"]))

        # 已举行的活动仍是历史事实
        self.assertEqual(self.store.events["evt-1"].happened, True)

        impact = revocation_impact(self.store, "child-017", SCOPE_ONLINE_DISTRIBUTION)
        affected = {a["asset_id"] for a in impact["affected_assets"]}
        self.assertIn("photo-1", affected)
        self.assertIn("历史事实", impact["note"])

    def test_asset_specific_consent_does_not_leak(self) -> None:
        self._grant("child-017", SCOPE_PRINT_PUBLICATION, asset_ids=["sketch-1"])
        self.assertTrue(
            consent_state(self.store, "child-017", SCOPE_PRINT_PUBLICATION, "sketch-1")["granted"]
        )
        # 仅针对草图的授权不能用于照片
        self.assertFalse(
            consent_state(self.store, "child-017", SCOPE_PRINT_PUBLICATION, "photo-1")["granted"]
        )


class PlanReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.store.sync([
            _op("dev", 1, "register_participant", register_child()),
            _op("dev", 2, "register_asset", {
                "asset_id": "sketch-1", "kind": "personal_sketch",
                "title": "草图", "author_alias": "child-017", "released": True,
            }),
            _op("dev", 3, "consent_event", {
                "participant_alias": "child-017", "action": "grant",
                "scope": SCOPE_ON_SITE_DISPLAY,
                "effective_at": "2026-09-19T10:00:00+08:00",
            }),
        ])

    def test_plan_itemized_verdicts(self) -> None:
        plan = submit_plan(self.store, {
            "title": "巡展海报 A",
            "items": [
                {"asset_id": "sketch-1", "scope": SCOPE_ON_SITE_DISPLAY},
                {"asset_id": "sketch-1", "scope": SCOPE_ONLINE_DISTRIBUTION},
                {"asset_id": "missing", "scope": SCOPE_ON_SITE_DISPLAY},
            ],
        })
        review = plan["review"]
        verdicts = [i["verdict"] for i in review["items"]]
        self.assertEqual(
            verdicts, [VERDICT_USABLE, VERDICT_CONSENT_NEEDED, VERDICT_BLOCKED]
        )
        self.assertEqual(review["counts"][VERDICT_USABLE], 1)
        self.assertEqual(review["overall"], VERDICT_BLOCKED)
        # 每项都附处理原因
        for item in review["items"]:
            self.assertTrue(item["reasons"])

    def test_plan_validation(self) -> None:
        with self.assertRaises(ValidationError):
            submit_plan(self.store, {"title": "x", "items": []})
        with self.assertRaises(ValidationError):
            submit_plan(self.store, {
                "title": "x",
                "items": [{"asset_id": "sketch-1", "scope": "bogus"}],
            })


class PublicProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        ops = [
            _op("dev", 1, "register_participant", {
                "alias": "child-017", "guardian_verified": True,
                "guardian_contact": "13800000000", "public_pen_name": "小绿",
            }),
            _op("dev", 2, "register_participant", register_child("child-018")),
            _op("dev", 3, "register_asset", {
                "asset_id": "secret-sketch", "kind": "personal_sketch",
                "title": "未公开草图", "author_alias": "child-017", "released": False,
            }),
            _op("dev", 4, "register_asset", {
                "asset_id": "photo-ok", "kind": "photo", "title": "现场照",
                "subjects": ["child-017"], "released": True,
            }),
            _op("dev", 5, "create_artwork", {"artwork_id": "art-1", "title": "长卷"}),
            _op("dev", 6, "record_contribution", {
                "contribution_id": "L1", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 0, "y": 0, "width": 10, "height": 10},
            }),
            _op("dev", 7, "record_contribution", {
                "contribution_id": "L2", "artwork_id": "art-1",
                "participant_alias": "child-018",
                "canvas_region": {"x": 5, "y": 0, "width": 10, "height": 10},
            }),
            _op("dev", 8, "revise_artwork", {
                "artwork_id": "art-1", "layer_ids": ["L1", "L2"],
            }),
            _op("dev", 9, "record_event", {
                "event_id": "evt-1", "name": "共创活动", "city": "广州",
                "venue_public": "中山纪念堂", "held_at": "2026-09-19T09:00:00+08:00",
            }),
            # child-017 授予网络传播；child-018 不授予
            _op("dev", 10, "consent_event", {
                "participant_alias": "child-017", "action": "grant",
                "scope": SCOPE_ONLINE_DISTRIBUTION,
                "effective_at": "2026-09-19T10:00:00+08:00",
            }),
        ]
        self.store.sync(ops)

    def test_unreleased_sketch_hidden_even_with_consent(self) -> None:
        self.assertIsNone(public_asset(self.store, "secret-sketch"))

    def test_public_asset_strips_sensitive_fields(self) -> None:
        view = public_asset(self.store, "photo-ok")
        self.assertIsNotNone(view)
        serialized = repr(view)
        self.assertNotIn("13800000000", serialized)
        self.assertNotIn("guardian", serialized)
        self.assertEqual(view["authors"][0]["display_name"], "小绿")

    def test_artwork_shows_only_permitted_layers_but_keeps_revision_history(self) -> None:
        view = public_artwork(self.store, "art-1")
        visible = {layer["contribution_id"] for layer in view["layers"]}
        self.assertEqual(visible, {"L1"})  # child-018 未授权网络传播
        rev = view["revisions"][0]
        self.assertEqual(rev["layer_count"], 2)
        self.assertEqual(rev["visible_layer_count"], 1)

    def test_catalog_events_have_no_precise_tracking(self) -> None:
        catalog = public_catalog(self.store)
        events = catalog["events"]
        self.assertEqual(events[0]["city"], "广州")
        self.assertNotIn("participant", repr(events))
        self.assertNotIn("guardian_contact", repr(catalog))


class PersistenceTest(unittest.TestCase):
    def test_reload_restores_state_and_plans(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "store.json")
            store = Store(path)
            store.sync([_op("dev", 1, "register_participant", register_child())])
            submit_plan(store, {
                "plan_id": "plan-1", "title": "t",
                "items": [{"asset_id": "nope", "scope": SCOPE_ON_SITE_DISPLAY}],
            })

            reloaded = Store(path)
            self.assertIn("child-017", reloaded.participants)
            self.assertIn("plan-1", reloaded.plans)
            self.assertEqual(reloaded.ops[("dev", 1)].type, "register_participant")


if __name__ == "__main__":
    unittest.main()
