"""HTTP 接口端到端冒烟测试（随机端口 + 标准库 urllib）。"""

import bootstrap  # noqa: F401  # 将 src/ 加入 sys.path

import json
import threading
import unittest
import urllib.error
import urllib.request

from youth_art_rights.app import build_server


def request(server, method: str, path: str, payload=None):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.store = build_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_health(self) -> None:
        status, body = request(self.server, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["状态"], "服务已启动")

    def test_offline_sync_then_review_flow(self) -> None:
        # 1) 两台设备离线登记
        ops_a = [
            {"op_sequence": 1, "type": "register_participant", "payload": {
                "alias": "child-040", "guardian_verification": {
                    "status": "verified", "method": "书面同意书",
                    "reference": "G-2026-040"}}},
            {"op_sequence": 2, "type": "add_contribution", "payload": {
                "contribution_id": "c-040", "alias": "child-040",
                "canvas_region": {"x": 0, "y": 0, "width": 20, "height": 5},
                "paint_order": 1}},
            {"op_sequence": 3, "type": "record_grant", "payload": {
                "alias": "child-040", "asset": "artwork",
                "scope": "network-distribution", "state": "granted",
                "effective_at": "2026-09-19T09:00:00+08:00"}},
        ]
        status, body = request(self.server, "POST", "/sync",
                               {"device_id": "tablet-A", "ops": ops_a})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["applied"]), 3)

        # 重复提交 → 幂等
        status, body = request(self.server, "POST", "/sync",
                               {"device_id": "tablet-A", "ops": ops_a})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["skipped_duplicates"]), 3)

        # 2) 修订与素材
        status, rev = request(self.server, "POST", "/revisions",
                              {"layers": ["c-040"], "note": "首段画卷"})
        self.assertEqual(status, 201)
        status, mat = request(self.server, "POST", "/materials", {
            "material_id": "m-complete", "kind": "complete-work-layered",
            "title": "绿美广东", "revision_id": rev["revision_id"],
            "public_released": True, "capture": {"city": "广州",
                                                 "venue": "中山纪念堂",
                                                 "gps": [23.13, 113.26]}})
        self.assertEqual(status, 201)

        # 3) 审核：网络传播可用
        status, review = request(self.server, "GET",
                                 "/materials/m-complete/review?scope=network-distribution")
        self.assertEqual(status, 200)
        self.assertEqual(review["bucket"], "usable", review["reasons"])

        # 4) 撤回后再审核 → 不可用（422）
        status, _ = request(self.server, "POST", "/grants", {
            "alias": "child-040", "asset": "artwork",
            "scope": "network-distribution", "state": "withdrawn",
            "effective_at": "2026-09-20T10:00:00+08:00"})
        self.assertEqual(status, 201)
        status, review = request(self.server, "GET",
                                 "/materials/m-complete/review?scope=network-distribution")
        self.assertEqual(status, 422)
        self.assertEqual(review["bucket"], "unusable")
        self.assertTrue(any(i["status"] == "withdrawn" for i in review["items"]))

        # 5) 公开页不再展示，且不含精确位置
        status, feed = request(self.server, "GET", "/public/feed")
        self.assertEqual(status, 200)
        self.assertNotIn("m-complete", [m["material_id"] for m in feed["materials"]])
        self.assertNotIn("gps", json.dumps(feed, ensure_ascii=False))

    def test_submission_review_and_takedowns(self) -> None:
        # 未核验、未授权的参与者照片
        request(self.server, "POST", "/sync", {"device_id": "dev-1", "ops": [
            {"op_sequence": 1, "type": "register_participant",
             "payload": {"alias": "child-050",
                         "guardian_verification": {"status": "pending"}}},
            {"op_sequence": 2, "type": "add_material", "payload": {
                "material_id": "m-photo", "kind": "photo",
                "public_released": True,
                "contributors": [{"alias": "child-050", "assets": ["likeness"]}]}},
            {"op_sequence": 3, "type": "record_guardian_verification", "payload": {
                "alias": "child-050", "status": "pending",
                "method": "同意书待补签"}},
        ]})
        status, body = request(self.server, "POST", "/reviews/submission", {
            "material_ids": ["m-photo"], "scope": "print-publication"})
        self.assertEqual(status, 200)  # 需补充同意不是硬错误
        self.assertEqual(body["overall_bucket"], "consent-required")
        self.assertTrue(body["items"][0]["reasons"])

        # 排期的用途会进下线清单；已结束的不会
        request(self.server, "POST", "/usages", {
            "title": "下月画册", "scope": "print-publication",
            "status": "scheduled", "material_ids": ["m-photo"]})
        request(self.server, "POST", "/usages", {
            "title": "9月19日现场展", "scope": "on-site-display",
            "status": "concluded", "material_ids": ["m-photo"]})
        # 先补齐授权，再撤回，模拟监护人改主意
        request(self.server, "POST", "/participants/child-050/verification",
                {"status": "verified", "method": "书面同意书", "reference": "G-050"})
        request(self.server, "POST", "/grants", {
            "alias": "child-050", "asset": "likeness",
            "scope": "print-publication", "state": "granted",
            "effective_at": "2026-09-19T09:00:00+08:00"})
        request(self.server, "POST", "/grants", {
            "alias": "child-050", "asset": "likeness",
            "scope": "print-publication", "state": "withdrawn",
            "effective_at": "2026-09-20T09:00:00+08:00"})
        status, body = request(self.server, "GET", "/takedowns?alias=child-050")
        self.assertEqual(status, 200)
        titles = [t["title"] for t in body["takedowns"]]
        self.assertIn("下月画册", titles)
        self.assertNotIn("9月19日现场展", titles)

    def test_validation_errors(self) -> None:
        status, body = request(self.server, "GET",
                               "/materials/nope/review?scope=bogus")
        self.assertEqual(status, 400)
        status, body = request(self.server, "POST", "/grants", {
            "alias": "x", "asset": "artwork", "scope": "bogus"})
        self.assertEqual(status, 422)
        self.assertIn("授权用途", body["error"])
        status, body = request(self.server, "POST", "/grants", {"alias": "x"})
        self.assertEqual(status, 400)
        status, body = request(self.server, "POST", "/sync", {"device_id": "d",
                                                              "ops": [{"type": "x"}]})
        self.assertEqual(status, 422)

    def test_persistence_with_db_path(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "state.json")
            server, _ = build_server(port=0, db_path=db)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            request(server, "POST", "/sync", {"device_id": "d", "ops": [
                {"op_sequence": 1, "type": "register_participant",
                 "payload": {"alias": "child-060",
                             "guardian_verification": {"status": "none"}}}]})
            server.shutdown()
            server.server_close()
            t.join(timeout=2)

            server2, store2 = build_server(port=0, db_path=db)
            self.assertIsNotNone(store2.participant("child-060"))
            server2.server_close()


if __name__ == "__main__":
    unittest.main()
