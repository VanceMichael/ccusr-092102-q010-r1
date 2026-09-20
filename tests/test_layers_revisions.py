"""画布覆盖顺序与完整作品修订（可还原层次）测试。"""

import bootstrap  # noqa: F401  # 将 src/ 加入 sys.path

import unittest

from youth_art_rights.models import visible_area
from youth_art_rights.store import Store


class CoverageTest(unittest.TestCase):
    def test_partial_coverage_leaves_visible_area(self) -> None:
        base = {"x": 0, "y": 0, "width": 10, "height": 10}
        over = [{"region": {"x": 0, "y": 0, "width": 10, "height": 4}, "opaque": True}]
        self.assertEqual(visible_area(base, over), 60.0)

    def test_full_coverage_reports_zero_but_record_remains(self) -> None:
        store = Store()
        store.add_contribution({
            "contribution_id": "c1", "alias": "child-001",
            "canvas_region": {"x": 0, "y": 0, "width": 10, "height": 10},
            "paint_order": 1})
        store.add_contribution({
            "contribution_id": "c2", "alias": "child-002",
            "canvas_region": {"x": 0, "y": 0, "width": 10, "height": 10},
            "paint_order": 2})
        self.assertEqual(visible_area(
            {"x": 0, "y": 0, "width": 10, "height": 10},
            [{"region": {"x": 0, "y": 0, "width": 10, "height": 10}}]), 0.0)
        # 原贡献仍在：后画覆盖不等于删除
        self.assertEqual(len(store.contributions_for_alias("child-001")), 1)

    def test_transparent_layer_does_not_cover(self) -> None:
        base = {"x": 0, "y": 0, "width": 10, "height": 10}
        over = [{"region": {"x": 0, "y": 0, "width": 10, "height": 10},
                 "opaque": False}]
        self.assertEqual(visible_area(base, over), 100.0)

    def test_multiple_disjoint_blockers(self) -> None:
        base = {"x": 0, "y": 0, "width": 10, "height": 10}
        blockers = [
            {"region": {"x": 0, "y": 0, "width": 5, "height": 5}},   # 左上
            {"region": {"x": 5, "y": 5, "width": 5, "height": 5}},   # 右下
        ]
        self.assertEqual(visible_area(base, blockers), 50.0)


class RevisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        for cid, alias, order in [
            ("c1", "child-001", 1), ("c2", "child-002", 2),
            ("c3", "child-003", 3),
        ]:
            self.store.add_contribution({
                "contribution_id": cid, "alias": alias,
                "canvas_region": {"x": 0, "y": 0, "width": 5, "height": 5},
                "paint_order": order})

    def test_revisions_keep_restoreable_layers(self) -> None:
        rev1 = self.store.add_revision(["c1", "c2"], "m-flat-v1", note="首版")
        # 修订后 c3 覆盖上来：第二版完整作品包含三层，第一版仍可还原
        rev2 = self.store.add_revision(["c1", "c2", "c3"], "m-flat-v2",
                                       note="补画后", parent_revision_id=rev1["revision_id"])
        restored_v1 = self.store.ordered_layers(self.store.revision(rev1["revision_id"])["layers"])
        self.assertEqual([c["contribution_id"] for c in restored_v1], ["c1", "c2"])
        restored_v2 = self.store.ordered_layers(self.store.revision(rev2["revision_id"])["layers"])
        self.assertEqual([c["contribution_id"] for c in restored_v2], ["c1", "c2", "c3"])
        self.assertEqual(rev2["parent_revision_id"], rev1["revision_id"])

    def test_revision_rejects_unknown_layer(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_revision(["c1", "ghost"], None)

    def test_layer_ordering_is_stable_after_unordered_merge(self) -> None:
        # 模拟离线乱序并入后，展开顺序仍按落笔序
        self.store.add_revision(["c3", "c1", "c2"], None)
        rev = self.store.data["revisions"][0]
        ids = [c["contribution_id"] for c in self.store.ordered_layers(rev["layers"])]
        self.assertEqual(ids, ["c1", "c2", "c3"])


if __name__ == "__main__":
    unittest.main()
