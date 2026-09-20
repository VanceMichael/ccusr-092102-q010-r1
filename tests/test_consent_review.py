"""分用途授权、撤回时效、发布审核与公开页脱敏测试。"""

import bootstrap  # noqa: F401  # 将 src/ 加入 sys.path

import unittest

from youth_art_rights.engine import ReviewEngine
from youth_art_rights.models import (
    BUCKET_CONSENT_REQUIRED,
    BUCKET_UNUSABLE,
    BUCKET_USABLE,
    SCOPE_EXTERNAL,
    SCOPE_NETWORK,
    SCOPE_ON_SITE,
    SCOPE_PRINT,
)
from youth_art_rights.store import Store


class ConsentScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.engine = ReviewEngine(self.store)
        self._build_world()

    def _build_world(self) -> None:
        s = self.store
        # child-017：监护已核验；作品授权现场+网络，肖像仅现场，姓名未授权
        s.set_guardian_verification("child-017", "verified", "书面同意书", "G-2026-017")
        for scope in (SCOPE_ON_SITE, SCOPE_NETWORK):
            s.record_grant("child-017", "artwork", scope, "granted",
                           effective_at="2026-09-19T09:00:00+08:00")
        s.record_grant("child-017", "likeness", SCOPE_ON_SITE, "granted",
                       effective_at="2026-09-19T09:00:00+08:00")
        # child-021：监护尚未核验
        s.set_guardian_verification("child-021", "pending", "待补交同意书")

        # 三层贡献：017 底层，021 中层完全覆盖，017 顶层一小块
        s.add_contribution({"contribution_id": "c-017-base", "alias": "child-017",
                            "canvas_region": {"x": 0, "y": 0, "width": 100, "height": 20},
                            "paint_order": 1})
        s.add_contribution({"contribution_id": "c-021-mid", "alias": "child-021",
                            "canvas_region": {"x": 0, "y": 0, "width": 100, "height": 20},
                            "paint_order": 2})
        s.add_contribution({"contribution_id": "c-017-top", "alias": "child-017",
                            "canvas_region": {"x": 80, "y": 0, "width": 20, "height": 20},
                            "paint_order": 3})

        # 个人草图（未公开）
        s.add_material({"material_id": "m-sketch-017", "kind": "sketch",
                        "title": "017 个人草图", "public_released": False,
                        "contributors": [{"alias": "child-017", "assets": ["artwork"]}],
                        "capture": {"city": "广州", "venue": "中山纪念堂",
                                    "gps": [23.133, 113.265],
                                    "occurred_at": "2026-09-19T09:20:00+08:00"},
                        "guardian_phone": "13800000000"})

        # 现场照片：两人肖像
        s.add_material({"material_id": "m-photo-group", "kind": "photo",
                        "title": "共同创作现场", "public_released": True,
                        "contributors": [
                            {"alias": "child-017", "assets": ["likeness"],
                             "detail": "画面左一"},
                            {"alias": "child-021", "assets": ["likeness"],
                             "detail": "画面右一"}],
                        "capture": {"city": "广州", "venue": "中山纪念堂",
                                    "gps": [23.133, 113.265],
                                    "occurred_at": "2026-09-19T10:00:00+08:00"}})

        # 完整作品（层次版）
        rev = s.add_revision(["c-017-base", "c-021-mid", "c-017-top"],
                             "m-flat-v1", note="二十米画卷合影段")
        s.add_material({"material_id": "m-complete", "kind": "complete-work-layered",
                        "title": "绿美广东画卷（合影段）", "revision_id": rev["revision_id"],
                        "public_released": True,
                        "location": {"city": "广州", "venue": "中山纪念堂"}})

        # 海报：引用照片 + 完整作品
        s.add_material({"material_id": "m-poster", "kind": "poster",
                        "title": "巡展海报", "material_ids": ["m-photo-group", "m-complete"]})

    # ------------------------------------------------------------------
    def test_complete_work_includes_covered_contributor(self) -> None:
        contributors = {c["alias"] for c in self.engine.material_contributors(
            self.store.material("m-complete"))}
        # child-021 的画覆盖了底层，child-017 的底层也被 c-021 大面积覆盖，
        # 但两层都是贡献，授权检查一个都不能少
        self.assertEqual(contributors, {"child-017", "child-021"})

    def test_on_site_photo_usable_only_after_both_verified_and_granted(self) -> None:
        # child-021 监护核验中 → 需补充
        result = self.engine.review_material("m-photo-group", SCOPE_ON_SITE)
        self.assertEqual(result.bucket, BUCKET_CONSENT_REQUIRED)
        statuses = {(i.alias, i.status) for i in result.items}
        self.assertIn(("child-017", "ok"), statuses)
        self.assertIn(("child-021", "unverified"), statuses)

        # 补齐核验并授权现场肖像后 → 可用
        self.store.set_guardian_verification("child-021", "verified", "现场视频核验", "V-021")
        self.store.record_grant("child-021", "likeness", SCOPE_ON_SITE, "granted",
                                effective_at="2026-09-19T11:00:00+08:00")
        result = self.engine.review_material("m-photo-group", SCOPE_ON_SITE)
        self.assertEqual(result.bucket, BUCKET_USABLE)

    def test_network_photo_blocked_without_likeness_grant(self) -> None:
        # 017 只有现场肖像授权，没有网络肖像授权
        result = self.engine.review_material("m-photo-group", SCOPE_NETWORK)
        self.assertNotEqual(result.bucket, BUCKET_USABLE)
        missing = [i for i in result.items if i.alias == "child-017"
                   and i.asset == "likeness" and i.status == "missing"]
        self.assertTrue(missing, "应指出 017 缺少肖像-网络传播授权")

    def test_private_sketch_is_always_unusable(self) -> None:
        result = self.engine.review_material("m-sketch-017", SCOPE_PRINT)
        self.assertEqual(result.bucket, BUCKET_UNUSABLE)
        self.assertTrue(any("个人草图未公开" in r for r in result.reasons))

    def test_poster_review_aggregates_components_and_reasons(self) -> None:
        submission = self.engine.review_submission(
            ["m-poster", "m-sketch-017"], SCOPE_NETWORK)
        by_id = {item["material_id"]: item for item in submission["items"]}
        # 海报含未授权肖像的照片 → 不可直接用；草图 → 不可用
        self.assertNotEqual(by_id["m-poster"]["bucket"], BUCKET_USABLE)
        self.assertEqual(by_id["m-sketch-017"]["bucket"], BUCKET_UNUSABLE)
        self.assertEqual(submission["overall_bucket"], BUCKET_UNUSABLE)
        # 每项都给出原因
        for item in submission["items"]:
            self.assertTrue(item["reasons"])

    def test_withdrawal_blocks_future_but_keeps_history(self) -> None:
        # 017 补授网络肖像，海报先排期网络传播，同时当天现场展示已结束
        self.store.record_grant("child-017", "likeness", SCOPE_NETWORK, "granted",
                                effective_at="2026-09-19T11:00:00+08:00")
        self.store.set_guardian_verification("child-021", "verified", "现场视频核验", "V-021")
        self.store.record_grant("child-021", "likeness", SCOPE_NETWORK, "granted",
                                effective_at="2026-09-19T11:00:00+08:00")
        self.store.add_usage({
            "title": "公众号巡展预告", "scope": SCOPE_NETWORK,
            "status": "scheduled", "material_ids": ["m-photo-group"]})
        self.store.add_usage({
            "title": "中山纪念堂现场展出", "scope": SCOPE_ON_SITE,
            "status": "concluded", "material_ids": ["m-photo-group"]})

        # 监护人次日撤回肖像的网络传播授权
        self.store.record_grant("child-017", "likeness", SCOPE_NETWORK, "withdrawn",
                                effective_at="2026-09-20T08:00:00+08:00")

        takedowns = self.engine.takedown_list(alias="child-017")
        titles = {t["title"] for t in takedowns}
        self.assertIn("公众号巡展预告", titles)       # 未来用途必须下线
        self.assertNotIn("中山纪念堂现场展出", titles)  # 已举行活动作为历史事实保留

        # 新审核立即变为不可用
        result = self.engine.review_material("m-photo-group", SCOPE_NETWORK)
        self.assertEqual(result.bucket, BUCKET_UNUSABLE)
        withdrawn = [i for i in result.items if i.status == "withdrawn"]
        self.assertTrue(withdrawn)

        # 撤回当时点之前，授权仍然有效（审计可还原）
        state, record = self.store.grant_state(
            "child-017", "likeness", SCOPE_NETWORK,
            at_time="2026-09-19T20:00:00+08:00")
        self.assertEqual(state, "granted")
        state, _ = self.store.grant_state(
            "child-017", "likeness", SCOPE_NETWORK,
            at_time="2026-09-21T00:00:00+08:00")
        self.assertEqual(state, "withdrawn")

    def test_regranting_restores_usability(self) -> None:
        self.store.record_grant("child-017", "likeness", SCOPE_EXTERNAL, "granted",
                                effective_at="2026-09-19T11:00:00+08:00")
        self.store.record_grant("child-017", "likeness", SCOPE_EXTERNAL, "withdrawn",
                                effective_at="2026-09-20T08:00:00+08:00")
        self.assertEqual(
            self.engine.review_material("m-photo-group", SCOPE_EXTERNAL).bucket,
            BUCKET_UNUSABLE)
        self.store.record_grant("child-017", "likeness", SCOPE_EXTERNAL, "granted",
                                effective_at="2026-09-21T08:00:00+08:00")
        # 021 仍缺核验/授权，故只能到 consent-required，证明是 017 一侧恢复
        result = self.engine.review_material("m-photo-group", SCOPE_EXTERNAL)
        self.assertEqual(result.bucket, BUCKET_CONSENT_REQUIRED)
        self.assertFalse(any(i.alias == "child-017" and i.blocked for i in result.items))

    def test_public_feed_is_masked_and_consent_filtered(self) -> None:
        # 初始：草图不公开；照片肖像授权不齐；完整作品中 021 缺作品网络授权；
        # 海报含不可用子素材——全部不得出现在公开页
        feed = {m["material_id"]: m for m in self.engine.public_feed()}
        self.assertNotIn("m-sketch-017", feed)
        self.assertNotIn("m-photo-group", feed)
        self.assertNotIn("m-poster", feed)
        self.assertNotIn("m-complete", feed)

        # 021 补齐监护核验与网络作品授权后，完整作品方可上线
        self.store.set_guardian_verification("child-021", "verified", "书面同意书", "G-021")
        self.store.record_grant("child-021", "artwork", SCOPE_NETWORK, "granted",
                                effective_at="2026-09-19T11:00:00+08:00")
        feed = {m["material_id"]: m for m in self.engine.public_feed()}
        self.assertIn("m-complete", feed)
        complete = feed["m-complete"]
        self.assertNotIn("gps", str(complete))         # 精确轨迹不出现
        self.assertNotIn("guardian_phone", str(complete))  # 联系方式不出现
        self.assertNotIn("venue", str(complete))       # 不到场馆粒度
        self.assertEqual(complete["location"], "广州")  # 仅城市级
        # 两人均未授予网络姓名授权 → 匿名署名
        self.assertEqual(complete["credits"], ["匿名参与者"])

        # 017 单独授予姓名授权并登记公开署名后，显示公开名；021 仍匿名
        self.store.record_grant("child-017", "name", SCOPE_NETWORK, "granted",
                                effective_at="2026-09-19T11:05:00+08:00")
        self.store.participant("child-017")["public_name"] = "小绿芽"
        feed = {m["material_id"]: m for m in self.engine.public_feed()}
        self.assertEqual(set(feed["m-complete"]["credits"]), {"小绿芽", "匿名参与者"})

        # 撤授权后立即从公开页消失
        self.store.record_grant("child-021", "artwork", SCOPE_NETWORK, "withdrawn",
                                effective_at="2026-09-20T09:00:00+08:00")
        self.assertNotIn("m-complete",
                         {m["material_id"] for m in self.engine.public_feed()})

    def test_unregistered_alias_is_flagged(self) -> None:
        self.store.add_material({"material_id": "m-ghost", "kind": "photo",
                                 "contributors": [{"alias": "child-999",
                                                   "assets": ["likeness"]}]})
        result = self.engine.review_material("m-ghost", SCOPE_NETWORK)
        self.assertEqual(result.bucket, BUCKET_CONSENT_REQUIRED)
        self.assertTrue(any(i.status == "unregistered" for i in result.items))


if __name__ == "__main__":
    unittest.main()
