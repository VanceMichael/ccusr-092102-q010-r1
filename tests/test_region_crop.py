"""局部画面（region-crop）只要求与裁切区域相交图层的授权。"""

import bootstrap  # noqa: F401

import unittest

from youth_art_rights.engine import ReviewEngine
from youth_art_rights.models import (
    BUCKET_CONSENT_REQUIRED,
    BUCKET_USABLE,
    SCOPE_PRINT,
)
from youth_art_rights.store import Store


class RegionCropTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.engine = ReviewEngine(self.store)
        self.store.set_guardian_verification("child-a", "verified", "书面同意书", "G-A")
        self.store.set_guardian_verification("child-b", "verified", "书面同意书", "G-B")
        self.store.add_contribution({"contribution_id": "ca", "alias": "child-a",
                                    "canvas_region": {"x": 0, "y": 0, "width": 10, "height": 10},
                                    "paint_order": 1})
        self.store.add_contribution({"contribution_id": "cb", "alias": "child-b",
                                    "canvas_region": {"x": 80, "y": 80, "width": 10, "height": 10},
                                    "paint_order": 2})

    def test_crop_only_requires_overlapping_contributor(self) -> None:
        # 裁切区域只与 child-a 的图层相交
        self.store.add_material({
            "material_id": "crop-1", "kind": "region-crop",
            "source_contribution_ids": ["ca", "cb"],
            "region": {"x": 0, "y": 0, "width": 5, "height": 5}})
        result = self.engine.review_material("crop-1", SCOPE_PRINT)
        aliases = {i.alias for i in result.items}
        self.assertIn("child-a", aliases)
        self.assertNotIn("child-b", aliases)  # b 的画不在裁切范围内
        # a 尚未授予纸质出版 → 需补充同意
        self.assertEqual(result.bucket, BUCKET_CONSENT_REQUIRED)

        self.store.record_grant("child-a", "artwork", SCOPE_PRINT, "granted",
                                effective_at="2026-09-19T12:00:00+08:00")
        result = self.engine.review_material("crop-1", SCOPE_PRINT)
        self.assertEqual(result.bucket, BUCKET_USABLE)

    def test_crop_across_two_regions_requires_both(self) -> None:
        self.store.add_material({
            "material_id": "crop-2", "kind": "region-crop",
            "source_contribution_ids": ["ca", "cb"],
            "region": {"x": 0, "y": 0, "width": 200, "height": 200}})
        result = self.engine.review_material("crop-2", SCOPE_PRINT)
        self.assertEqual({i.alias for i in result.items}, {"child-a", "child-b"})


if __name__ == "__main__":
    unittest.main()
