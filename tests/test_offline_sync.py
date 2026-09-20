"""离线登记与幂等合并测试。"""

import bootstrap  # noqa: F401  # 将 src/ 加入 sys.path

import unittest

from youth_art_rights.store import Store


def make_ops():
    return [
        {"op_sequence": 1, "type": "register_participant",
         "payload": {"alias": "child-017", "guardian_verification": {"status": "pending"}}},
        {"op_sequence": 2, "type": "add_contribution", "payload": {
            "contribution_id": "c-017-a", "alias": "child-017",
            "canvas_region": {"x": 0, "y": 0, "width": 10, "height": 10},
            "paint_order": 1, "recorded_at": "2026-09-19T09:20:00+08:00"}},
        {"op_sequence": 3, "type": "record_grant", "payload": {
            "alias": "child-017", "asset": "artwork",
            "scope": "on-site-display", "state": "granted",
            "effective_at": "2026-09-19T09:21:00+08:00"}},
    ]


class OfflineSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()

    def test_first_sync_applies_all_ops(self) -> None:
        result = self.store.sync_batch("tablet-A", make_ops())
        self.assertEqual(len(result["applied"]), 3)
        self.assertEqual(result["latest_sequence"], 3)
        self.assertIsNotNone(self.store.participant("child-017"))
        self.assertEqual(len(self.store.contributions_for_alias("child-017")), 1)

    def test_resubmit_same_batch_is_idempotent(self) -> None:
        first = self.store.sync_batch("tablet-A", make_ops())
        second = self.store.sync_batch("tablet-A", make_ops())  # 设备重发
        self.assertEqual(len(first["applied"]), 3)
        self.assertEqual(len(second["applied"]), 0)
        self.assertEqual(len(second["skipped_duplicates"]), 3)
        # 没有重复落库
        self.assertEqual(len(self.store.contributions_for_alias("child-017")), 1)
        self.assertEqual(len(self.store.data["grants"]), 1)

    def test_multiple_offline_devices_merge(self) -> None:
        self.store.sync_batch("tablet-A", make_ops())
        ops_b = [
            {"op_sequence": 1, "type": "register_participant",
             "payload": {"alias": "child-021",
                         "guardian_verification": {"status": "verified",
                                                   "method": "书面同意书",
                                                   "reference": "G-2026-021"}}},
            {"op_sequence": 2, "type": "add_contribution", "payload": {
                "contribution_id": "c-021-a", "alias": "child-021",
                "canvas_region": {"x": 5, "y": 5, "width": 10, "height": 10},
                "paint_order": 2}},
        ]
        self.store.sync_batch("tablet-B", ops_b)
        # 两台设备各自游标独立
        self.assertEqual(self.store.data["device_cursors"]["tablet-A"], 3)
        self.assertEqual(self.store.data["device_cursors"]["tablet-B"], 2)
        self.assertEqual(len(self.store.data["contributions"]), 2)

        # A 设备恢复后续传 seq 4，游标前进，seq 1-3 再混进来仍被跳过
        again = make_ops() + [
            {"op_sequence": 4, "type": "add_material", "payload": {
                "material_id": "m-photo-1", "kind": "photo", "title": "现场照片",
                "contributors": [{"alias": "child-017", "assets": ["likeness"]}]}}]
        result = self.store.sync_batch("tablet-A", again)
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(len(result["skipped_duplicates"]), 3)
        self.assertEqual(result["latest_sequence"], 4)

    def test_conflicting_verification_keeps_first(self) -> None:
        self.store.sync_batch("tablet-A", [
            {"op_sequence": 1, "type": "register_participant", "payload": {
                "alias": "child-030",
                "guardian_verification": {"status": "verified",
                                          "method": "书面同意书",
                                          "reference": "G-2026-030"}}},
        ])
        result = self.store.sync_batch("tablet-B", [
            {"op_sequence": 7, "type": "register_participant", "payload": {
                "alias": "child-030",
                "guardian_verification": {"status": "none"}}},
        ])
        self.assertTrue(result["conflicts"])
        self.assertEqual(
            self.store.participant("child-030")["guardian_verification"]["status"],
            "verified")

    def test_persistence_across_reopen(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.json"
            Store(db).sync_batch("tablet-A", make_ops())
            reopened = Store(db)
            self.assertIsNotNone(reopened.participant("child-017"))
            # 重放同一批不产生重复
            result = reopened.sync_batch("tablet-A", make_ops())
            self.assertEqual(len(result["skipped_duplicates"]), 3)

    def test_missing_sequence_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.sync_batch("tablet-A", [{"type": "ping", "payload": {}}])


if __name__ == "__main__":
    unittest.main()
