"""领域模型与常量。

设计原则：

* 参与者只以**代号**对外出现，联系方式等敏感字段仅供内部核验使用；
* 贡献是独立图层，被后来的图层覆盖不等于删除；
* 完整作品的每次修订都是不可变记录，保留可还原的层次；
* 授权以追加事件（授予/撤回）记账，按生效时间求值，撤回只影响未来使用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 四种相互独立的授权用途
SCOPE_ON_SITE_DISPLAY = "on-site-display"
SCOPE_PRINT_PUBLICATION = "print-publication"
SCOPE_ONLINE_DISTRIBUTION = "online-distribution"
SCOPE_EXTERNAL_PROVISION = "external-provision"

CONSENT_SCOPES: tuple[str, ...] = (
    SCOPE_ON_SITE_DISPLAY,
    SCOPE_PRINT_PUBLICATION,
    SCOPE_ONLINE_DISTRIBUTION,
    SCOPE_EXTERNAL_PROVISION,
)

SCOPE_LABELS: dict[str, str] = {
    SCOPE_ON_SITE_DISPLAY: "现场展示",
    SCOPE_PRINT_PUBLICATION: "纸质出版",
    SCOPE_ONLINE_DISTRIBUTION: "网络传播",
    SCOPE_EXTERNAL_PROVISION: "对外提供",
}

# 素材类型
KIND_PERSONAL_SKETCH = "personal_sketch"  # 个人草图（默认未公开）
KIND_PHOTO = "photo"  # 现场照片（可能涉及多名参与者肖像）
KIND_REGION_CROP = "region_crop"  # 局部画面
KIND_COMPLETE_ARTWORK = "complete_artwork"  # 完整作品（某一修订的合成画面）

ASSET_KINDS: tuple[str, ...] = (
    KIND_PERSONAL_SKETCH,
    KIND_PHOTO,
    KIND_REGION_CROP,
    KIND_COMPLETE_ARTWORK,
)


class ValidationError(ValueError):
    """登记数据不合法，映射为 HTTP 400。"""


@dataclass
class Participant:
    """参与共创的青少年，仅以代号作为公开标识。"""

    alias: str
    guardian_verified: bool = False
    guardian_verification: dict[str, Any] | None = None
    # 监护人联系方式仅内部保存，绝不进入公开投影
    guardian_contact: str | None = None
    public_pen_name: str | None = None
    registered_at: str = ""
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class Asset:
    """可被发布方案引用的素材：个人草图、现场照片、局部画面或完整作品。"""

    asset_id: str
    kind: str
    title: str
    author_alias: str | None = None
    subjects: list[str] = field(default_factory=list)
    uri: str = ""
    # 个人草图在被显式标记可发布前属于未公开草稿
    released: bool = False
    captured_at: str = ""
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class Contribution:
    """画布上的一层贡献。

    图层被覆盖只影响最终视觉，记录本身始终保留，撤回授权时仍可追溯到人。
    """

    contribution_id: str
    artwork_id: str
    participant_alias: str
    region: dict[str, int]  # {"x", "y", "width", "height"}
    z_order: int
    asset_id: str | None = None
    recorded_at: str = ""
    note: str = ""
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArtworkRevision:
    """完整作品的一次修订。

    修订不可变：新修订单独追加，可能基于同一父修订形成分支，
    任一历史修订都可以按其 layer_ids 还原当时的层次。
    """

    artwork_id: str
    revision: int
    parent_revision: int | None
    title: str
    layer_ids: list[str]
    asset_id: str | None = None
    created_at: str = ""
    note: str = ""
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConsentEvent:
    """授权台账事件：授予或撤回某参与者在某用途上的授权。

    asset_ids 为 None 表示覆盖该参与者名下全部素材；否则只针对指定素材。
    """

    event_id: str
    participant_alias: str
    action: str  # "grant" | "revoke"
    scope: str
    asset_ids: list[str] | None = None
    effective_at: str = ""
    recorded_at: str = ""
    reason: str = ""
    guardian_verification_ref: str = ""
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventRecord:
    """线下活动的事实记录。活动一旦举行即成为历史事实，不因授权撤回而删除。"""

    event_id: str
    name: str
    city: str
    venue_public: str  # 可公开的场地名称（不记录参与者精确轨迹）
    held_at: str
    artwork_ids: list[str] = field(default_factory=list)
    happened: bool = True
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanItem:
    """发布方案中的一项素材使用。"""

    item_id: str
    asset_id: str
    scope: str
    note: str = ""


@dataclass
class PublicationPlan:
    """海报或展览方案，含逐项可用性质证结果。"""

    plan_id: str
    title: str
    submitter: str
    submitted_at: str
    items: list[PlanItem]
    review: dict[str, Any] | None = None


@dataclass
class Operation:
    """设备离线期间产生的一条登记操作。

    同一设备上 seq 单调递增；(device_id, seq) 是幂等合并的去重键。
    """

    op_id: str
    device_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    occurred_at: str

    @property
    def key(self) -> tuple[str, int]:
        return self.device_id, self.seq


def model_to_dict(obj: Any) -> Any:
    """dataclass/列表/字典混合结构序列化为可 JSON 化的普通数据。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: model_to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [model_to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: model_to_dict(v) for k, v in obj.items()}
    return obj


def now_iso() -> str:
    """当前 UTC 时间的 ISO8601 字符串（内部时间戳统一带时区）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
