"""HTTP API 测试：设备同步、方案评审、公众页脱敏、历史修订还原。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from youth_art_rights.api import make_server
from youth_art_rights.models import (
    SCOPE_ONLINE_DISTRIBUTION,
    SCOPE_ON_SITE_DISPLAY,
    SCOPE_PRINT_PUBLICATION,
)
from youth_art_rights.store import Store


def _op(device: str, seq: int, op_type: str, payload: dict) -> dict:
    return {"device_id": device, "seq": seq, "type": op_type, "payload": payload}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = Store()
        cls.server = make_server("127.0.0.1", 0, cls.store)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def _request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _sync(self, ops: list[dict]) -> dict:
        status, payload = self._request("POST", "/v1/sync", {"operations": ops})
        self.assertEqual(status, 201, payload)
        return payload

    def test_01_health(self) -> None:
        status, payload = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertIn("服务已启动", payload["状态"])

    def test_02_full_flow(self) -> None:
        # 设备离线登记：两人、画布、三层贡献、首修订、素材与授权
        self._sync([
            _op("tab-1", 1, "register_participant", {
                "alias": "child-017", "guardian_verified": True,
                "guardian_contact": "家长电话-保密", "public_pen_name": "木棉",
            }),
            _op("tab-1", 2, "register_participant", {
                "alias": "child-018", "guardian_verified": True,
            }),
            _op("tab-2", 1, "create_artwork", {"artwork_id": "art-1", "title": "绿美广东二十米卷"}),
            _op("tab-2", 2, "record_contribution", {
                "contribution_id": "L1", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 0, "y": 0, "width": 100, "height": 40},
            }),
            _op("tab-2", 3, "record_contribution", {
                "contribution_id": "L2", "artwork_id": "art-1",
                "participant_alias": "child-018",
                "canvas_region": {"x": 80, "y": 0, "width": 100, "height": 40},
            }),
            _op("tab-2", 4, "revise_artwork", {
                "artwork_id": "art-1", "layer_ids": ["L1", "L2"],
            }),
        ])
        self._sync([
            _op("tab-1", 3, "register_asset", {
                "asset_id": "photo-1", "kind": "photo", "title": "共创现场合影",
                "subjects": ["child-017", "child-018"], "released": True,
            }),
            _op("tab-1", 4, "record_event", {
                "event_id": "evt-zs", "name": "绿美广东共创", "city": "广州",
                "venue_public": "中山纪念堂", "held_at": "2026-09-19T09:30:00+08:00",
                "artwork_ids": ["art-1"],
            }),
            _op("tab-1", 5, "consent_event", {
                "participant_alias": "child-017", "action": "grant",
                "scope": SCOPE_ONLINE_DISTRIBUTION,
                "effective_at": "2026-09-19T10:00:00+08:00",
            }),
            _op("tab-1", 6, "consent_event", {
                "participant_alias": "child-018", "action": "grant",
                "scope": SCOPE_ONLINE_DISTRIBUTION,
                "effective_at": "2026-09-19T10:00:00+08:00",
            }),
            _op("tab-1", 7, "consent_event", {
                "participant_alias": "child-017", "action": "grant",
                "scope": SCOPE_ON_SITE_DISPLAY,
                "effective_at": "2026-09-19T10:00:00+08:00",
            }),
        ])

        # 同序号重发：设备级幂等，整操作跳过
        again = self._sync([
            _op("tab-2", 2, "record_contribution", {
                "contribution_id": "L1", "artwork_id": "art-1",
                "participant_alias": "child-017",
                "canvas_region": {"x": 0, "y": 0, "width": 100, "height": 40},
            }),
        ])
        self.assertEqual(again["applied"], [])
        self.assertEqual(
            {(d["device_id"], d["seq"]) for d in again["duplicated"]}, {("tab-2", 2)}
        )

        # 后画覆盖前画：新修订保留全部图层，v1 仍可还原
        self._sync([
            _op("tab-2", 5, "record_contribution", {
                "contribution_id": "L3", "artwork_id": "art-1",
                "participant_alias": "child-018",
                "canvas_region": {"x": 20, "y": 0, "width": 120, "height": 40},
            }),
            _op("tab-2", 6, "revise_artwork", {
                "artwork_id": "art-1", "layer_ids": ["L1", "L2", "L3"],
            }),
        ])
        status, detail = self._request("GET", "/v1/admin/artworks/art-1?revision=1")
        self.assertEqual(status, 200)
        self.assertEqual([l["contribution_id"] for l in detail["current"]["layers"]], ["L1", "L2"])
        latest = self._request("GET", "/v1/admin/artworks/art-1")[1]
        self.assertEqual(len(latest["current"]["layers"]), 3)
        covered = {l["contribution_id"]: l["covered_by"] for l in latest["current"]["layers"]}
        self.assertIn("L3", covered["L1"])  # L1 被后画覆盖但记录仍在
        self.assertEqual(covered["L3"], [])

        # 方案评审：逐项给出可用 / 需补充同意 / 不可使用
        status, plan = self._request("POST", "/v1/plans", {
            "title": "巡展海报",
            "submitter": "publisher-1",
            "items": [
                {"asset_id": "photo-1", "scope": SCOPE_ONLINE_DISTRIBUTION},
                {"asset_id": "photo-1", "scope": SCOPE_PRINT_PUBLICATION},
                {"asset_id": "ghost", "scope": SCOPE_ON_SITE_DISPLAY},
            ],
        })
        self.assertEqual(status, 201)
        verdicts = [i["verdict"] for i in plan["review"]["items"]]
        self.assertEqual(verdicts, ["usable", "consent_needed", "blocked"])
        for item in plan["review"]["items"]:
            self.assertTrue(item["reasons"])

        # 公众页：照片可显示，且不泄露监护人联系方式
        status, page = self._request("GET", "/v1/public/assets/photo-1")
        self.assertEqual(status, 200)
        self.assertNotIn("家长电话-保密", json.dumps(page, ensure_ascii=False))

        status, art = self._request("GET", "/v1/public/artworks/art-1")
        self.assertEqual(status, 200)
        self.assertEqual(len(art["revisions"]), 2)  # 历史修订作为事实保留

        status, events = self._request("GET", "/v1/public/events")
        self.assertEqual(status, 200)
        self.assertEqual(events["events"][0]["venue"], "中山纪念堂")
        self.assertNotIn("轨迹", json.dumps(events, ensure_ascii=False))

        # child-018 撤回网络传播：照片公众页立即 404；活动历史仍在
        self._sync([_op("tab-1", 8, "consent_event", {
            "participant_alias": "child-018", "action": "revoke",
            "scope": SCOPE_ONLINE_DISTRIBUTION,
            "effective_at": "2026-09-20T12:00:00+08:00",
        })])
        status, _ = self._request("GET", "/v1/public/assets/photo-1")
        self.assertEqual(status, 404)
        status, events = self._request("GET", "/v1/public/events")
        self.assertEqual(status, 200)
        self.assertEqual(len(events["events"]), 1)

        # 撤回影响面报告
        status, impact = self._request(
            "GET", "/v1/admin/revocation-impact?alias=child-018&scope=online-distribution"
        )
        self.assertEqual(status, 200)
        self.assertIn("photo-1", [a["asset_id"] for a in impact["affected_assets"]])

    def test_03_bad_request(self) -> None:
        status, payload = self._request("POST", "/v1/sync", {"operations": [{"seq": -1}]})
        self.assertEqual(status, 400)
        self.assertIn("错误", payload)


if __name__ == "__main__":
    unittest.main()
