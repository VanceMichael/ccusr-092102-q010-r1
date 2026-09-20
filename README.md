# 青少年共创作品授权服务

中山纪念堂「绿美广东」二十米共创画卷的后端台账：保存参与者代号、监护关系核验、
画布区域、覆盖顺序、个人草图、摄影素材与分用途授权；支持多台设备在弱网环境下
离线登记、恢复连接后按设备序号幂等合并。

仓库中的样例与测试数据均为虚构，不含真实未成年人身份。

## 核心规则

* **代号对外**：参与者只以 `child-xxx` 代号或笔名公开，监护人联系方式、核验凭证
  仅供内部后台，绝不进入公众投影。
* **覆盖不是删除**：每层贡献独立保存并带 `z_order`；后画覆盖前画时，只是合成了
  新的作品修订，历史修订按其图层清单随时可还原。
* **授权按用途分别控制**：现场展示（`on-site-display`）、纸质出版
  （`print-publication`）、网络传播（`online-distribution`）、对外提供
  （`external-provision`）彼此独立；授权以「授予 / 撤回」事件记账，按生效时间
  求值，可精确到单个素材。
* **撤回只影响未来**：撤回后该素材在新使用场景中判定为不可用，并给出需要下线的
  物料清单；已经举行的活动作为历史事实保留，贡献图层不删除。
* **发布评审逐项给结论**：海报/展览方案提交后，每项素材标注「可用 / 需补充同意 /
  不可使用」及处理原因；照片涉及多人时任一人未授权即不可用。
* **公众页最小可见**：只显示获准网络传播的素材；未公开个人草图一律隐藏；
  对不可见资源统一返回 404，不暴露资源存在性。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 24 个测试

PYTHONPATH=src python3 -m youth_art_rights --port 8080 --store data/store.json
```

不指定 `--store` 时使用纯内存台账。指定文件后，所有设备操作与发布方案以 JSON
原子落盘（临时文件 + rename），重启后重放操作日志恢复状态。

## 离线登记与合并

每台设备对自己产生的操作从 1 开始递增编号，`POST /v1/sync` 提交一批：

```json
{
  "operations": [
    {"device_id": "tablet-A", "seq": 1, "type": "register_participant",
     "payload": {"alias": "child-017", "guardian_verified": true,
                 "guardian_contact": "仅内部可见", "public_pen_name": "木棉"}},
    {"device_id": "tablet-A", "seq": 2, "type": "create_artwork",
     "payload": {"artwork_id": "art-1", "title": "绿美广东二十米卷"}},
    {"device_id": "tablet-A", "seq": 3, "type": "record_contribution",
     "payload": {"contribution_id": "L1", "artwork_id": "art-1",
                 "participant_alias": "child-017",
                 "canvas_region": {"x": 0, "y": 0, "width": 100, "height": 40}}},
    {"device_id": "tablet-A", "seq": 4, "type": "revise_artwork",
     "payload": {"artwork_id": "art-1", "layer_ids": ["L1"]}}
  ]
}
```

响应区分 `applied` / `duplicated`（设备序号重复，原样跳过）/ `rejected`
（引用无法满足等，附原因，不占序号可修正重传），并在 `device_gaps` 中提示某台
设备是否有缺号批次尚未上传。设备间乱序到达时做多趟重放，跨设备前向引用会在
后续趟次自动满足。

操作类型：`register_participant`、`register_asset`（personal_sketch / photo /
region_crop / complete_artwork）、`create_artwork`（建空画布）、
`record_contribution`（逐层登记，可自动分配 `z_order`）、`revise_artwork`
（不可变修订，可带 `parent_revision` 形成分支）、`consent_event`（grant/revoke，
`asset_ids` 为 null 表示覆盖该参与者全部素材）、`record_event`（活动事实）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/sync` | 设备离线批次合并，幂等 |
| POST | `/v1/plans` | 提交海报/展览方案，返回逐项评审 |
| GET | `/v1/plans/{id}` | 查询方案与评审结论 |
| GET | `/v1/public/catalog` | 公众目录（获准素材 + 作品 + 活动历史） |
| GET | `/v1/public/artworks/{id}?revision=n` | 作品公众视图，只含获准图层，历史修订可见 |
| GET | `/v1/public/assets/{id}` | 素材公众视图；不可见统一 404 |
| GET | `/v1/public/events` | 活动历史，仅城市/公开场地 |
| GET | `/v1/admin/artworks/{id}` | 内部视图：全部层次、覆盖关系、可还原标记 |
| GET | `/v1/admin/consent?alias=&scope=` | 授权台账与当前状态 |
| GET | `/v1/admin/revocation-impact?alias=&scope=` | 撤回后未来需下线的物料与方案条目 |
| GET | `/v1/admin/snapshot` | 全量台账（部署时应在网关层限制访问） |

## 代码结构

```
src/youth_art_rights/
  models.py   # 领域模型、四种授权用途与素材类型
  store.py    # 操作日志、设备序号幂等合并、多趟重放、JSON 原子持久化
  review.py   # 授权求值、逐项/整方案评审、撤回影响面
  public.py   # 公众页脱敏投影
  service.py  # 发布方案提交与归档
  api.py      # HTTP 路由（stdlib ThreadingHTTPServer，无第三方依赖）
```
