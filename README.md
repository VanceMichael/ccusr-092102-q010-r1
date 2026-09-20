# 青少年共创作品授权服务

保存青少年协作绘画中的参与代号、监护关系核验、画布区域、覆盖顺序、个人草图、摄影素材和
分用途授权，并支撑发布审核与公众页面。仓库中的样例均为虚构数据，不含真实未成年人身份。

## 核心规则

* **作品贡献与公开授权是两条独立记录。** 画面被后来的图层覆盖时，原贡献记录仍完整保留；
  完整作品的每次修订都保存图层快照与父修订，可还原任意历史版本。
* **授权按 资产维度 × 用途 分别控制。** 资产维度为 `name`（姓名）、`likeness`（肖像）、
  `artwork`（作品）；用途为 `on-site-display`（现场展示）、`print-publication`（纸质出版）、
  `network-distribution`（网络传播）、`external-provision`（对外提供）。
* **撤回只影响未来。** 撤回后新审核立即判为不可使用、公众页立即下线、排期/进行中的用途进入
  下线清单；状态为 `concluded` 的已举行活动作为历史事实保留。授权流水只追加、可按时点审计。
* **审核三档结论。** `usable`（可用）、`consent-required`（需补充同意或监护核验）、
  `unusable`（已撤回或素材本身不可公开），逐项给出涉及人员、维度与原因；海报/展览方案
  递归聚合所有子素材。
* **公众页只显示获准内容。** 未公开草图永不出现；无网络传播授权或已撤回的素材不出现；
  输出不含联系方式、GPS/场馆等精确轨迹，未获姓名授权时一律匿名署名。
* **离线幂等合并。** 设备以 `(device_id, op_sequence)` 为幂等键整批同步，重复提交只回放首次
  结果；设备间内容冲突记入 `conflicts` 且不覆盖先到数据。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 检查

PYTHONPATH=src PORT=8080 DB_PATH=state.json \
  python3 -m youth_art_rights                # 启动（DB_PATH 可选，默认仅内存）
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 运行状态 |
| POST | `/sync` | 离线设备批量幂等合并（登记/贡献/素材/授权/核验） |
| GET | `/participants/{alias}` | 参与者与监护核验信息 |
| POST | `/participants/{alias}/verification` | 更新监护关系核验 |
| POST | `/contributions` | 登记画布图层（区域、落笔顺序、不透明标记） |
| POST | `/materials` | 登记素材：`sketch`/`photo`/`region-crop`/`complete-work-layered`/`poster`/`exhibition-plan` |
| POST | `/revisions` | 完整作品修订（图层有序列表 + 父修订），保留可还原层次 |
| GET | `/revisions/{id}` | 展开修订，按落笔顺序返回图层 |
| POST | `/grants` | 授予或撤回某个 资产维度×用途 的授权（只追加流水） |
| POST | `/usages` | 登记发布用途（`scheduled`/`ongoing`/`concluded`） |
| POST | `/reviews/submission` | 海报/展览方案逐项审核 |
| GET | `/materials/{id}/review?scope=...` | 单素材按用途审核 |
| GET | `/takedowns?alias=&scope=` | 撤回后须下线的未来/进行中物料 |
| GET | `/public/feed` | 公众页面投影（仅获准内容，已脱敏） |
