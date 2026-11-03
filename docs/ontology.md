# 领域本体说明（TBox）

> 本文件由 `python -m backend.tools.gen_ontology_doc` 从
> [`data/ontology/schema.yaml`](../data/ontology/schema.yaml) 生成，**请勿手改**。
> 改本体 → 重跑生成命令 → 提交两份 diff，文档与实现不会漂移。

## 1. 表示方式

| 项目 | 约定 |
| --- | --- |
| 本体版本 | `0.1.0` |
| 命名空间 | `https://example.org/kg/ecovacs#` |
| 适用范围 | `productLineScope = DEEBOT_T`（导入时强制过滤，防范围蔓延） |
| TBox / ABox | 类型与关系定义在本 YAML；实例三元组存 Neo4j 属性图 |
| 类 → 标签 | 类名即节点标签，统一附加 `:Entity`；子类用多标签（`:Entity:Case:FAQCase`） |
| 对象属性 → 关系 | 关系名 UPPER_SNAKE_CASE；数据属性 → 节点属性 camelCase |
| 实体 ID | `前缀:业务键`，如 `MODEL:T20-PRO`、`ERR:T20-PRO:E11`、`FAULT:NO-SUCTION` |
| 推理语义 | RDFS/OWL-lite 子类继承 + 声明式规则 + 闭世界假设（negation as failure） |

错误码采用「型号 × 错误码」复合键（`ERR:<MODEL>:<CODE>`），而不是全局 `ERR:<CODE>`：
同一个 `E11` 在不同型号上可以是不同部件，跨型号复用是售后知识图谱最常见的语义错误。
故障（`Fault`）、根因（`Cause`）等跨型号共享的实体则使用单键（`FAULT:*` / `CAUSE:*`），
由 `OCCURS_IN` 指回型号自己的部件。

## 2. 类（TBox）

共 **23** 个类（20 个核心类 + `Case` 的 3 个子类）。
当前图谱共 **432** 个实体实例。

| 类 | 中文 | ID 前缀 | 业务唯一键 | 必填 | 实例数 | 定义 |
| --- | --- | --- | --- | --- | ---: | --- |
| `ProductLine` | 产品线 | `LINE` | `code` | 是 | 1 | 共享结构平台与售后体系的一组产品。 |
| `ProductModel` | 产品型号 | `MODEL` | `modelCode` | 是 | 1 | 对外销售的具体型号，是错误码、部件、耗材的归属维度。 |
| `Product` | 设备实例 | `PROD` | `serialNo` | 是 | 15 | 具有序列号的单台设备。 |
| `Component` | 可维修部件 | `COMP` | `code` | 是 | 24 | 设备上可检查、可清洁、可更换的功能部件（非耗材）。 |
| `Consumable` | 耗材 | `CON` | `sku` | 是 | 6 | 有更换周期的易耗件（边刷、滤网、尘袋、拖布等），区别于备件。 |
| `SparePart` | 备件 | `SP` | `sku` | 是 | 9 | 故障后更换的硬件模块（轮组、电池、水泵、主板等），通常需售后或较高动手能力。 |
| `ErrorCode` | 错误码 | `ERR` | `code` | 是 | 23 | 设备或 APP 抛出的编码化报错。**必须绑定型号**，因为同一 code 在不同型号下含义可能不同。 |
| `FaultCategory` | 故障大类 | `CAT` | `code` | 是 | 5 | 用于区分硬件、耗材、网络、用户操作等处置路径的大类。 |
| `Symptom` | 症状 / 现象 | `SYM` | `code` | 是 | 27 | 用户可观察到的现象，是自然语言到图谱的入口。 |
| `Fault` | 故障模式 | `FAULT` | `code` | 是 | 24 | 可诊断的技术故障模式，比症状更接近工程语义。 |
| `Cause` | 根因 | `CAUSE` | `code` | 是 | 34 | 导致故障的具体原因，分为硬件、耗材、环境、操作、软件五类。 |
| `Check` | 检查项 | `CHK` | `code` | 是 | 30 | 用于确认或排除根因的可执行检查，是"先清理还是先换件"的鉴别手段。 |
| `Solution` | 处置方案 | `SOL` | `code` | 是 | 36 | 针对根因或故障的处置动作，含风险等级与是否需备件。 |
| `Document` | 文档 | `DOC` | `docId` | 是 | 59 | 证据来源文档（手册、FAQ、维护指南）。 |
| `FirmwareVersion` | 固件版本 | `FW` | `version` | 是 | 1 | 设备固件版本，用于关联已知问题。 |
| `AppVersion` | APP 版本 | `APP` | `version` | 是 | 3 | 客户端版本，配网/绑定类问题常与之相关。 |
| `Customer` | 匿名客户 | `CUST` | `customerId` | 是 | 20 | 脱敏家庭档案，只保留环境特征，不含姓名/地址/电话。 |
| `UsageEnvironment` | 使用环境 | `ENV` | `code` | 是 | 6 | 影响根因分布的使用条件（宠物、长发、地毯、复式、大户型）。 |
| `ServiceAgent` | 服务人员 / 维修点 | `AGENT` | `code` | 是 | 6 | 官方售后或第三方维修资源。 |
| `Case` | 案例（超类） | `CASE` | `caseNo` | 是 | 0 | 案例超类，按来源分为 FAQCase / PublicCase / Ticket，来源不可混同。 |
| `FAQCase` | 官方 FAQ 案例 | `CASE` | `caseNo` | 是 | 46 | 以官方 FAQ 的「问题—步骤」形式组织的案例卡；是高质量种子，但不是真实工单。来源真实性看 sourceType（official_faq / faq_template_proxy）。 |
| `PublicCase` | 公开案例 | `CASE` | `caseNo` | 是 | 8 | 公开投诉/维修帖的匿名化改写；只用于语言与症状，不作为因果金标准。 |
| `Ticket` | 合成工单 | `TICKET` | `caseNo` | 是 | 48 | 基于 FAQCase/PublicCase 合成的工单，含时间、费用、结果字段；必须可追溯来源。 |

`FAQCase` / `PublicCase` / `Ticket` 是 `Case` 的子类，继承 `caseNo`、`sourceType`、
`createdAt` 等属性，并各自追加来源特有字段（如 `PublicCase.platform`、`Ticket.channel`）。
按 `Case` 统一查询能一次拿到三类案例，按子类查询可以只取官方口径或真实报修。

`Case` 是抽象超类，实例都带子类标签，因此它自己的计数为 0——不是缺数据，
而是多标签建模的必然结果。

## 3. 关系（TBox）

共 **33** 条关系。`head` / `tail` 即 domain / range，校验器逐条比对；
`minPerType` 是该关系在每种头实体类型上至少要出现的条数（防止「建了关系但图中没边」）。
当前图谱共 **1028** 条关系实例。

| 关系 | 中文 | 头实体 | 尾实体 | 基数 | 实例数 | 语义 |
| --- | --- | --- | --- | --- | ---: | --- |
| `HAS_MODEL` | 包含型号 | `ProductLine` | `ProductModel` | `1..*` | 1 | 产品线包含型号 |
| `HAS_COMPONENT` | 使用部件 | `ProductModel` | `Component` | `1..*` | 24 | 型号包含某部件 |
| `USES_CONSUMABLE` | 使用耗材 | `ProductModel` | `Consumable` | `1..*` | 6 | 型号使用某耗材 |
| `COMPATIBLE_WITH` | 兼容 | `Consumable` / `SparePart` | `ProductModel` | `*` | 15 | 耗材/备件兼容型号 |
| `HAS_ERROR_CODE` | 对应错误码 | `ProductModel` | `ErrorCode` | `1..*` | 23 | 型号对应错误码；关系上记录该型号下的含义 |
| `HAS_CODE` | 表现为错误码 | `Symptom` | `ErrorCode` | `*` | 23 | 症状对应错误码 |
| `HAS_CATEGORY` | 属于大类 | `ErrorCode` | `FaultCategory` | `1` | 23 | 错误码属于某故障大类 |
| `IN_CATEGORY` | 故障大类 | `Fault` | `FaultCategory` | `1` | 24 | 故障模式归属大类（用于 R20/R09 的大类过滤） |
| `INDICATES` | 指向故障 | `Symptom` | `Fault` | `*` | 35 | 症状提示故障；置信度写在关系上 |
| `OCCURS_IN` | 发生于部件 | `Fault` | `Component` | `1..*` | 24 | 故障发生在某部件 |
| `CAUSED_BY` | 根因为 | `Fault` | `Cause` | `*` | 40 | 故障可能根因；置信度写在关系上 |
| `CONFIRMED_BY` | 由检查确认 | `Cause` | `Check` | `*` | 33 | 根因由某检查确认 |
| `RESOLVED_BY` | 由方案解决 | `Cause` / `Fault` / `Case` | `Solution` | `*` | 141 | 根因/故障/案例对应处置方案 |
| `REQUIRES_CONSUMABLE` | 需要耗材 | `Solution` | `Consumable` | `*` | 5 | 方案需要耗材 |
| `REQUIRES_PART` | 需要备件 | `Solution` | `SparePart` | `*` | 5 | 方案需要备件 |
| `DOCUMENTED_IN` | 记载于 | `Solution` / `Fault` / `Cause` / `ErrorCode` / `Symptom` / `Case` | `Document` | `*` | 51 | 方案/故障/根因/症状/案例所依据的官方文档 |
| `OBSERVED` | 观察到 | `Case` | `Symptom` | `1..*` | 88 | 案例观察到症状 |
| `ABOUT` | 针对 | `Case` / `Document` | `Product` / `ProductModel` | `1` | 161 | 案例或文档针对设备或型号 |
| `REPORTED_BY` | 报告人 | `Case` | `Customer` | `0..1` | 56 | 案例由客户报告 |
| `OWNS` | 拥有 | `Customer` | `Product` | `*` | 15 | 客户拥有设备 |
| `USED_IN` | 使用于 | `Product` | `UsageEnvironment` | `*` | 15 | 设备使用环境 |
| `RUNS_FIRMWARE` | 运行固件 | `Product` | `FirmwareVersion` | `0..1` | 15 | 设备运行固件 |
| `RUNS_APP` | 使用 APP | `Customer` | `AppVersion` | `*` | 20 | 用户使用 APP 版本 |
| `ASSIGNED_TO` | 指派给 | `Case` | `ServiceAgent` | `0..1` | 48 | 案例指派服务人员 |
| `DERIVED_FROM` | 来源于 | `Ticket` | `FAQCase` / `PublicCase` | `1..*` | 48 | 合成工单的来源案例（可追溯性要求） |
| `SIMILAR_TO` | 相似于 | `Case` | `Case` | `*` | 24 | 相似案例；对称关系，需去重 |
| `DUPLICATE_OF` | 重复于 | `Case` | `Case` | `0..1` | 2 | 重复案例 |
| `DETECTS` | 可检测 | `Check` | `Fault` | `*` | 35 | 检查可发现某故障 |
| `PRECEDES` | 先于 | `Solution` | `Solution` | `*` | 10 | 处置步骤先后顺序 |
| `INSTANCE_OF` | 实例属于型号 | `Product` | `ProductModel` | `1` | 15 | 设备实例所属型号（文档 4.6 使用但 4.4 表遗漏，此处补全） |
| `HAS_MANUAL` | 官方手册 | `ProductModel` | `Document` | `1..*` | 1 | 型号对应的官方手册/FAQ 文档（补充：保证文档节点可达） |
| `HAS_FIRMWARE` | 提供固件 | `ProductModel` | `FirmwareVersion` | `1..*` | 1 | 型号可运行的固件版本（补充：文档缺少型号到固件的直连，R10 需要） |
| `KNOWN_ISSUE` | 已知问题 | `FirmwareVersion` | `Fault` | `*` | 1 | 固件版本的已知缺陷（R10 使用） |

### 3.1 推理主链

```text
Symptom ──INDICATES──▶ Fault ──CAUSED_BY──▶ Cause
   ▲                    │                    ├──CONFIRMED_BY──▶ Check
   │                    │                    └──RESOLVED_BY───▶ Solution
   │                    └──OCCURS_IN──▶ Component        ├──REQUIRES_CONSUMABLE──▶ Consumable
ErrorCode ──HAS_CODE(反)──┘                             ├──REQUIRES_PART────────▶ SparePart
                                                         └──DOCUMENTED_IN────────▶ Document
```

这条链就是规则层的证据链：LHS 取自链上的边，RHS 也是链上的边，
所以任一诊断结论都能反查到具体三元组，再由 `sourceId` 落到原文。

## 4. 能力问题（CQ）

CQ 是本体「够不够用」的验收口径：每个 CQ 必须有不绕路的查询能回答。

| 编号 | 能力问题 | 涉及路径 | 实现入口 |
| --- | --- | --- | --- |
| CQ1 | T20 PRO 报 E11，最可能是什么部件/根因？ | ErrorCode → Symptom → Fault → Component → Cause | POST /api/query（mode=rule） |
| CQ2 | 先清理还是直接换轮组？ | Cause → Check → Solution | 证明树中 CONFIRMED_BY / RESOLVED_BY 两条分支 |
| CQ3 | 养宠家庭反复报 E13，换边刷能解决吗？ | UsageEnvironment → Symptom → Cause → Consumable | 规则 R09（耗材优先清理）+ 环境事实 home_environment |
| CQ4 | 10010 是硬件故障还是网络问题？ | ErrorCode → FaultCategory → Cause → Solution | 规则 R20（network_first）+ Agent verify 交叉核对 |
| CQ5 | 基站不上水，查水箱、水泵还是管路？ | Component → Fault → Check | 规则 R21（water_check_order） |
| CQ6 | E41 电池异常，什么时候建议换电池？ | ErrorCode → Fault → Cause → Solution → SparePart | 规则 R14（safety_stop）+ R08（兼容性否决） |
| CQ7 | 同一错误码在不同型号上含义一样吗？ | ErrorCode → ProductModel | GET /api/graph/code/{code}；当前语料仅含单型号，返回覆盖清单 |
| CQ8 | 哪些耗材与 T20 PRO 兼容？ | Consumable → COMPATIBLE_WITH → ProductModel | Agent 工具 check_compatibility |
| CQ9 | 同一设备 30 天重复报修，应该升级还是换机？ | Ticket → Symptom → 重复计数 → Escalation | 规则 R12（chronic_issue）+ 事实层重复计数聚合 |
| CQ10 | 这个建议对应哪份官方文档/FAQ？ | Solution → Document → Evidence | 证据面板 E* → docId → 原文定位 |

## 5. 质量闸门

`backend/app/kg/validate.py` 在每次构建后跑 9 类检查，任一条不过即视为构建失败：

| 检查 | 内容 | 阈值 |
| --- | --- | --- |
| 类型合法性 | 节点标签、关系类型都在本体中声明 | 0 违规 |
| domain / range | 每条边的 head / tail 类型落在声明范围内 | 0 违规 |
| 规模 | 实体 / 关系数 | ≥ 150 / ≥ 300 |
| 课程底线 | 同一指标的课程最低要求 | ≥ 60 / ≥ 120 |
| 证据完整性 | 每条边都有 `evidence` + `sourceId` | ≥ 100% |
| 唯一键 | 同类实体的业务键不重复 | 0 重复 |
| 自环 | 头尾相同（`SIMILAR_TO` 除外） | 0 条 |
| 孤立点 | 没有任何边的节点 | 0 个 |
| 断链 | 边的端点不存在 | 0 条 |

## 6. 与 Neo4j 的对应

| 本体元素 | Neo4j 落地 |
| --- | --- |
| 类 | 节点标签 + 唯一约束（`CREATE CONSTRAINT ... REQUIRE n.<key> IS UNIQUE`） |
| 关系 | 关系类型 + 属性（`confidence` / `evidence` / `sourceId` / `evidenceMode`） |
| 型号维度 | 每型号一节点，错误码挂 `ERR:<MODEL>:<CODE>`，约束保证不跨型号复用 |
| 查询 | 只读账号；Cypher 由 `kg/queries.py` 集中维护，LLM 不生成 Cypher |

约束与索引的 DDL 见 `backend/app/kg/schema.cypher`。
