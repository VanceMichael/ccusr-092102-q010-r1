"""领域常量与纯函数：授权维度、时间、画布覆盖计算、审核结果。

本模块不含存储与网络逻辑，便于离线登记与服务端共用同一套判定规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --- 授权资产维度：监护人可以分别调整姓名、肖像、作品 ---
ASSET_NAME = "name"
ASSET_LIKENESS = "likeness"
ASSET_ARTWORK = "artwork"
ASSETS = (ASSET_NAME, ASSET_LIKENESS, ASSET_ARTWORK)

ASSET_LABELS = {
    ASSET_NAME: "姓名",
    ASSET_LIKENESS: "肖像",
    ASSET_ARTWORK: "作品",
}

# --- 授权用途：现场展示、纸质出版、网络传播、对外提供，各自独立 ---
SCOPE_ON_SITE = "on-site-display"
SCOPE_PRINT = "print-publication"
SCOPE_NETWORK = "network-distribution"
SCOPE_EXTERNAL = "external-provision"
SCOPES = (SCOPE_ON_SITE, SCOPE_PRINT, SCOPE_NETWORK, SCOPE_EXTERNAL)

SCOPE_LABELS = {
    SCOPE_ON_SITE: "现场展示",
    SCOPE_PRINT: "纸质出版",
    SCOPE_NETWORK: "网络传播",
    SCOPE_EXTERNAL: "对外提供",
}

GRANT_GRANTED = "granted"
GRANT_WITHDRAWN = "withdrawn"

# 审核三档结论
BUCKET_USABLE = "usable"                # 可用
BUCKET_CONSENT_REQUIRED = "consent-required"  # 需补充同意/核验
BUCKET_UNUSABLE = "unusable"            # 不可使用

# 物料类型
KIND_PHOTO = "photo"
KIND_SKETCH = "sketch"
KIND_REGION_CROP = "region-crop"
KIND_COMPLETE_FLATTENED = "complete-work-flattened"
KIND_COMPLETE_LAYERED = "complete-work-layered"
KIND_POSTER = "poster"
KIND_EXHIBITION_PLAN = "exhibition-plan"

USAGE_SCHEDULED = "scheduled"
USAGE_ONGOING = "ongoing"
USAGE_CONCLUDED = "concluded"


def now_iso() -> str:
    """带时区的当前时间，ISO 8601。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    """解析 ISO 8601；朴素时间视为东八区（活动现场所在地）。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt


# --- 画布区域覆盖 ---
# 区域统一用闭开区间 (x1, y1, x2, y2) 表示。

def to_rect(region: dict) -> tuple[float, float, float, float]:
    return (
        float(region["x"]),
        float(region["y"]),
        float(region["x"]) + float(region["width"]),
        float(region["y"]) + float(region["height"]),
    )


def _subtract(a: tuple, b: tuple) -> list[tuple]:
    """返回 a 未被 b 覆盖的部分（至多 4 个矩形）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix1 >= ix2 or iy1 >= iy2:
        return [a]  # 无交集
    parts: list[tuple] = []
    if iy1 > ay1:
        parts.append((ax1, ay1, ax2, iy1))       # 上方
    if iy2 < ay2:
        parts.append((ax1, iy2, ax2, ay2))       # 下方
    if ix1 > ax1:
        parts.append((ax1, iy1, ix1, iy2))       # 左侧
    if ix2 < ax2:
        parts.append((ix2, iy1, ax2, iy2))      # 右侧
    return parts


def visible_area(region: dict, blockers: list[dict]) -> float:
    """区域在扣除后续不透明覆盖物后剩余的面积；为 0 表示被完全覆盖。

    后画覆盖前画只影响展平画面中的可见性，不删除原贡献记录。
    """
    remaining = [to_rect(region)]
    for blocker in blockers:
        if not blocker.get("opaque", True):
            continue
        b = to_rect(blocker["region"])
        remaining = [piece for cur in remaining for piece in _subtract(cur, b)]
        if not remaining:
            return 0.0
    return sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in remaining)


# --- 审核结果 ---

@dataclass
class ReviewItem:
    alias: str
    asset: str
    scope: str
    status: str  # ok / missing / withdrawn / unverified / unregistered
    reason: str

    @property
    def blocked(self) -> bool:
        return self.status != "ok"


@dataclass
class ReviewResult:
    material_id: str
    scope: str
    bucket: str
    items: list[ReviewItem] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    review_id: str | None = None

    def to_dict(self) -> dict:
        data = {
            "material_id": self.material_id,
            "scope": self.scope,
            "scope_label": SCOPE_LABELS.get(self.scope, self.scope),
            "bucket": self.bucket,
            "items": [vars(item) for item in self.items],
            "reasons": self.reasons,
        }
        if self.review_id:
            data["review_id"] = self.review_id
        return data
