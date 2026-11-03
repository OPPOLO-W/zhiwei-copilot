# 图谱构建全流程

> 读这份文档只需要记住一条主线：
> **本体（schema.yaml）定规则 → 种子 YAML 出事实 → `build_seed` 组装成 Graph → 写 jsonl → 校验 → 导入 Neo4j**。
>
> 文中的行号都是当前代码的实测位置，改动代码后请重新 grep。

---

## 0. 一句话现状

构建链路是**完整且可复现**的：`python -m backend.app.kg.build_seed --seed data/seed/t20pro`
能生成 **432 节点 / 1028 边**、证据覆盖率 100%、校验通过，重复执行输出字节级一致。

当前服务加载的就是这份图（`data/curated/`），**只含 T20 PRO 一个型号**，与语料（1 本手册 + 58 篇 FAQ）一致。

### 证据分三档，不再一律声称「改写原文」

| evidenceMode | 边数 | 含义 |
|---|---|---|
| `paraphrase` | 304 | 对语料内容的改写摘要，尚未逐字核对（`verification=pending`） |
| `assumed` | 152 | 语料中**并无对应原文**，属工程先验（`verification=unverified`） |
| `synthetic` | 498 | 模板生成的案例与工单，非真实数据 |

标 `assumed` 的主要是两层：

- **错误码层**（E11 / 10010 …）：手册故障表只有中文故障名，通篇没有这类编码。
  这一层挂在伪来源 `SYN:ASSUMED-CODES` 上，**不建 `DOCUMENTED_IN` 边**，避免虚构「记录在某文档里」。
- **根因层与方案优先级**（`causes` / `precedence`）：轴承磨损、电机故障、气阻这类机理，
  以及「先清理再更换」的顺序，都是工程判断，手册不会写。

### 与抽取层的差距

| | 节点 | 边 | 状态 |
|---|---|---|---|
| 现在图里的 | 432 | 1028 | 来自 `data/seed/t20pro/*.yaml` + `kg/synthetic.py` + `data/review/faq_cases.yaml` |
| 抽取产出的 | 585 | 888 | `data/extracted/candidates.jsonl`，尚未进图 |

`kg/` 下仍无代码引用 `data/extracted/`。**这一层没有接上不只是缺个适配器**——
抽取层的节点 key 是语料里的中文名（`驱动轮卡住`），种子用的是稳定英文 code（`LEFT-WHEEL-STUCK`），
直接合并会让图里 ID 变成中文，前端、规则、评测题全断。
两者的对应关系做过一次全量比对，结论见 5.1：部件层能对上大半，故障层几乎对不上。

---

## 1. 全链路一览

```
data/ontology/schema.yaml          23 类 / 33 关系 / 规模门槛 / 前端配色
        │  kg/ontology.py  load_ontology() 139 · get_ontology() 191
        ▼
data/seed/t20pro/*.yaml            人写的种子事实 + sources/evidence 两段声明
        │  build_seed.py  load_seed() 545   按 key 合并多个 yaml
        ▼
      seed dict
        │  build_seed.py  SeedBuilder.build() 194
        │    ├─ _declare_entities() 132   ★ 只建空壳节点（先声明）
        │    └─ 16 个 _build_*()          ★ 再填属性 + 连边
        ▼
      Graph（kg/model.py:62）
        │  kg/synthetic.py  generate_cases() 56   合成案例/客户/工单（可 --no-synthetic 跳过）
        ▼
data/curated/nodes.jsonl + edges.jsonl      model.write_jsonl() 146（按 id 排序，幂等）
        │
        ├─ kg/validate.py  validate() 38   → 9 项校验 + 控制台报告（180 行 summarize）
        │
        ├─ kg/loader.py   load() 98        → Neo4j（先跑 schema.cypher 建约束/索引）
        │
        └─ kg/memory_store.py  from_dir() 31 → 内存后端（读同一份 jsonl，不双写）
```

命令：

```bash
python -m backend.app.kg.build_seed --seed data/seed/t20pro      # 种子 + 合成案例 → data/curated/
python -m backend.app.kg.build_seed --seed data/seed/t20pro --no-synthetic   # 只要种子（245 节点 / 456 边）
python -m backend.app.kg.build_seed --seed data/seed/t20pro --out var/kg     # 输出到别处，验算用，不覆盖正式图
python -m backend.app.kg.validate --strict           # 只校验，有 ERROR 时退出码 1
python -m backend.app.kg.loader --reset              # 清空后全量导入 Neo4j Desktop
```

`--seed` 可以指向任意目录，用它就能并行维护多份种子（例如以后加第二个机型）而不互相污染。

---

## 2. 每一步具体做什么

### 2.1 本体是规则来源，不是数据

`data/ontology/schema.yaml` 定义 23 个类（每类带 `prefix` / `key` / `required` / `viz` 配色）
和 33 个关系（每类带 `head` / `tail` 域值约束）。

`kg/ontology.py` 把它读成 `Ontology` 对象，构建期用它做三件事：

| 用途 | 位置 |
|---|---|
| 校验类名合法性、关系名合法性 | `build_seed.py:55`、`build_seed.py:80` |
| 校验关系的 domain / range（含父类继承，`FAQCase` 可当 `Case` 用） | `build_seed.py:86-100`、`ontology.py:62 is_subclass()` |
| 生成节点 ID 前缀 | `build_seed.py:46 node_id()` → `ontology.py:75 prefix_of()` |

### 2.2 种子合并：多个 YAML 拼成一个 dict

`load_seed()`（`build_seed.py:512`）按扩展名扫 `data/seed/*.yaml`，然后**按 key 归并**：

- 列表型 key（`models` / `components` / `faults` / `solutions` …共 18 个，见 515-519 行）→ `extend` 追加
- 字典型 key（`productLine`）→ `update` 覆盖

当前种子规模：

| key | 条数 | key | 条数 |
|---|---|---|---|
| models | 3 | codes | 24 |
| components | 21 | symptoms | 27 |
| consumables | 7 | faults | 24 |
| spareParts | 9 | causes | 34 |
| firmware | 4 | checks | 30 |
| appVersions | 3 | solutions | 36 |
| categories | 5 | precedence | 10 |
| environments | 6 | knownIssues | 2 |
| agents | 6 | documents | 10 |

19 个 key = 291 个节点（种子部分）。

### 2.3 两阶段建图：为什么先声明再连边

`_declare_entities()`（`build_seed.py:132`）先把**所有**实体建成只有最小属性的空节点，
再由 16 个 `_build_*()` 逐个补全属性并连边。

原因是 `faults → causes → checks → faults` 本身成环（检查项 `DETECTS` 回指故障），
任何单遍顺序都会在「边的一端还没建出来」时失败。`rel()`（`build_seed.py:69`）里对
头/尾节点做了存在性断言（82-85 行），所以必须先声明齐。

### 2.4 连边的强制契约

`SeedBuilder.rel()`（`build_seed.py:69`）是**唯一**的加边入口，它强制三件事：

1. **domain / range 合法**：头尾节点的类必须在 `schema.yaml` 该关系的 `head` / `tail` 里（允许父类）
2. **证据四要素齐全**：每条边自动带上 `sourceId` / `evidence` / `evidenceMode` / `confidence`
3. **无自环**：`Graph.add_edge()`（`model.py:89`）直接抛异常

写进 jsonl 的边长这样：

```json
{"type":"OCCURS_IN","head":"FAULT:APP-BIND-FAIL","tail":"COMP:WIFI-MODULE",
 "props":{"sourceId":"DOC:T20PRO-MANUAL",
          "evidence":"设备绑定失败发生在Wi-Fi 模块：绑定失败常与 APP 版本或路由器隔离有关",
          "confidence":0.9,"evidenceMode":"paraphrase",
          "verification":"pending","curator":"manual_seed"}}
```

错误码层的边长这样，注意 `sourceId` 是伪来源、等级是 `assumed`：

```json
{"type":"HAS_ERROR_CODE","head":"MODEL:T20-PRO","tail":"ERR:T20-PRO:10010",
 "props":{"sourceId":"SYN:ASSUMED-CODES","evidence":"T20 PRO 错误码说明：10010 表示设备无法完成 Wi-Fi 配网…",
          "confidence":0.95,"evidenceMode":"assumed",
          "verification":"unverified","curator":"manual_seed"}}
```

`evidenceMode` / `verification` 的默认值来自 `CURATED_PROVENANCE`（`build_seed.py:24`），
但**每一层都可以覆盖**：种子顶部的 `sources` 与 `evidence` 两段按知识层声明，

```yaml
sources:
  codes: SYN:ASSUMED-CODES      # 错误码层没有文档可指，挂伪来源
  fault: DOC:T20PRO-MANUAL
evidence:
  codes: assumed                # 语料里确实没有 → assumed
  fault: paraphrase             # 对语料的改写 → paraphrase
```

`SeedBuilder.rel()` 收到 `evidence_mode=` 时按层盖章，并把 `assumed` 自动配成 `verification: unverified`。
这样做是为了不再「把工程假设说成原文改写」——旧图 687 条边一律 `paraphrase`，其中错误码与机理层根本没有原文。

### 2.5 合成案例：165 个节点、498 条边

`kg/synthetic.py:generate_cases()` 用固定随机种子（`RNG_SEED = 20251028`）生成
`Customer` / `Product`（用户设备）/ `Ticket` / `FAQCase` / `PublicCase`，并连出
`OWNS` / `INSTANCE_OF` / `REPORTED_BY` / `ASSIGNED_TO` / `DERIVED_FROM` / `OBSERVED` /
`ABOUT` / `RESOLVED_BY` / `SIMILAR_TO` / `DUPLICATE_OF` 等关系。

这些节点的 `dataProvenance = seed_generated`，边是 `evidenceMode = synthetic`。
`--no-synthetic` 可以完全跳过，得到纯净的 245 / 456。

### 2.6 序列化：为什么要求排序

`Graph.write_jsonl()`（`model.py:146`）按节点 id、边 `(head, type, tail)` 排序后写出，
保证同一份输入多次构建**字节级一致**——否则每次重建 git diff 都是几万行噪音，
也没法用「产物 hash 是否变化」判断构建是否真的改了东西。

`Graph.read_jsonl()`（`model.py:158`）是反向读回，`MemoryStore.from_dir()` 用的就是它。

排序还不够，**日期也必须固定**。`synthetic.py:23` 的 `BASE_DATE` 写死为 `2026-10-29`，而不是
`date.today()`——用 today 时，跨天重建会让 80 个节点的 `createdAt`、15 台设备的购买与保修日期
整体漂移，产物 hash 每天都变，"字节级一致"就不成立。固定后，同一份种子在任何一天重建都完全一致。

### 2.7 校验：9 项硬门槛

`kg/validate.py:validate()`（38 行）按顺序检查：

| # | 检查 | 行 | 严重度 |
|---|---|---|---|
| 1 | 节点/关系类型在本体白名单内 | 44 | error |
| 2 | 边的 domain / range 合法 | 55 | error |
| 3 | 无自环 | 69 | error |
| 4 | 必填属性齐全（按 schema 的 `required`） | 74 | error |
| 5 | 每条边都有 `sourceId` + `evidence`，置信度在 [0,1] | 84 | error |
| 6 | 业务键唯一（`key` 字段），`ErrorCode`/`Case` 等按 id 例外 | 96 | error |
| 7 | 无孤立节点（度数为 0） | 109 | error |
| 8 | 规模门槛：实体 ≥ `minEntities`(150)、关系 ≥ `minRelations`(300) | 118 | error |
| 9 | 每个 `required: true` 的类至少有 1 个实例 | 127 | error |

另外两类**只警告不拦**（138-144 行）：合成案例占比、`verification=pending` 的边数。

### 2.8 入 Neo4j

`kg/loader.py:load()`（98 行）：

1. `ensure_schema()`（38）逐条执行 `schema.cypher`：`Entity.id` 唯一约束、`modelCode` 唯一、
   `caseNo` 唯一、`name`/`_label`/`createdAt` 索引、全文索引（`name`/`aliasesText`/`title`/`code`）
2. 导入前再验一次白名单（102-105 行），防止手改 jsonl 混入非法类型
3. `clear_graph()`（46）按 `:Entity` 分批 `DETACH DELETE`（`--reset` 时）
4. `upsert_nodes()`（60）按「主标签 + 附加标签」分组，`MERGE (n:Entity {id})` 后 `SET n:标签`
5. `upsert_edges()`（81）按关系类型分组，`MATCH` 两端后 `MERGE`

标签和关系类型只从本体白名单取，不做任意字符串拼接。

**运行时读哪边**：`kg/__init__.py:get_store()`（21 行）优先 `Neo4jStore`，连不上且
`KG_BACKEND=memory` 时降级到 `MemoryStore`——两者读的是同一份 `data/curated`，
所以不存在双写不一致。

#### 已实际导入（2026-10-29）

DBMS 起来后 `python -m backend.app.kg.loader --reset` 的结果：

```
constraintsApplied: 7      deleted: 0
nodesUpserted: 432         edgesUpserted: 1028
dbEntities: 432            dbRelations: 1028
```

节点带双标签（`Entity` + 具体类），所以 `SHOW CONSTRAINTS` 只看到 3 条唯一约束，
另外 4 条是 `schema.cypher` 里的索引与全文索引。

⚠️ **单测覆盖不到 Neo4j**：`conftest.py:13` 强制 `KG_BACKEND=memory`，`Neo4jStore`
的 Cypher 分支在导入前从未被执行过。首次导入后跑 `diagnose` 立刻炸出
`neo4j_store.py:107` 的 `WHERE AND type(r) IN $types` 语法错误（`rel_filter`
自带前导 `AND`，与模板里的 `WHERE` 拼接冲突），已修。

因此新增等价性校验，同一批问题在两个后端跑，结果必须逐字段一致
（只排除 `trace.backend`，它是后端标识本身）：

```bash
python backend/tools/check_neo4j_parity.py            # 全部 15 题
python backend/tools/check_neo4j_parity.py --limit 5  # 冒烟
```

当前结果 **15/15 一致**。改动 `build_seed` / `neo4j_store` / 种子 YAML 后，
先重建再重跑这条命令，比直接开前端点更早发现问题。

---

## 3. 关系产出对照：哪个方法写了哪些边

种子部分（456 条 / 295 节点）：

| `_build_*()` | 写出的关系 | 条数 |
|---|---|---|
| `_build_models` | `HAS_MODEL` | 1 |
| `_build_components` | `HAS_COMPONENT` | 24 |
| `_build_consumables` | `USES_CONSUMABLE` 6 / `COMPATIBLE_WITH` 6 | 12 |
| `_build_spare_parts` | `COMPATIBLE_WITH` | 9 |
| `_build_firmware` | `HAS_FIRMWARE` 1 / `KNOWN_ISSUE` 1 | 2 |
| `_build_documents` | `HAS_MANUAL` 1 / `ABOUT` 59 | 60 |
| `_build_error_codes` | `HAS_ERROR_CODE` 23 / `HAS_CATEGORY` 23 / `HAS_CODE` 23 | 69 |
| `_build_symptoms` | `INDICATES` | 35 |
| `_build_faults` | `OCCURS_IN` 24 / `IN_CATEGORY` 24 / `CAUSED_BY` 40 | 88 |
| `_build_causes` | `CONFIRMED_BY` 33 / `RESOLVED_BY` 39 | 72 |
| `_build_checks` | `DETECTS` | 35 |
| `_build_solutions` | `REQUIRES_CONSUMABLE` 5 / `REQUIRES_PART` 5 / `DOCUMENTED_IN` 29 | 39 |
| `_build_precedence` | `PRECEDES` | 10 |
| `_build_categories` / `_build_environments` / `_build_agents` / `_build_product_line` | 只建节点，不连边 | 0 |

两点与旧版不同：

- `_build_documents` 多了 `ABOUT`（文档 → 型号）。没有它，58 篇 FAQ 里没被方案引用的 46 篇
  就是孤立节点，校验第 7 关过不去。为此 `schema.yaml` 的 `ABOUT` 头域从 `[Case]` 放宽到 `[Case, Document]`。
- `_build_error_codes` 的 `DOCUMENTED_IN` 变成了 0 条。错误码层语料无据（见第 0 节），
  干脆不建归属边；只要把种子的 `sources.codes` 改成真实文档，这些边会自动出现。

合成部分（498 条）：`ABOUT` 80、`OBSERVED` 80、`RESOLVED_BY` 80、`REPORTED_BY` 56、
`ASSIGNED_TO` 48、`DERIVED_FROM` 48、`SIMILAR_TO` 24、`RUNS_APP` 20、
`OWNS` 15、`INSTANCE_OF` 15、`RUNS_FIRMWARE` 15、`USED_IN` 15、`DUPLICATE_OF` 2。

---

## 4. 节点 ID 规则

由 `schema.yaml` 每类的 `prefix` 决定（`ontology.py:75`），实测：

| 类 | 前缀 | 样例 |
|---|---|---|
| ProductLine | `LINE:` | `LINE:DEEBOT-T` |
| ProductModel | `MODEL:` | `MODEL:T20-PRO` |
| ErrorCode | `ERR:` | `ERR:T20-PRO:10010` ← **按型号展开，同一码值在不同型号是两个点** |
| Fault | `FAULT:` | `FAULT:WATER-LEAK` ← **跨型号共享，不带型号前缀** |
| Cause / Check / Solution | `CAUSE:` / `CHK:` / `SOL:` | `SOL:ADJUST-ROUTER` |
| Symptom | `SYM:` | `SYM:BATTERY-DRAIN-FAST` |
| Component / Consumable / SparePart | `COMP:` / `CON:` / `SP:` | `COMP:BASE-STATION` |
| Document | `DOC:` | `DOC:T20-MANUAL` |
| Case 子类 | `CASE:` | `CASE:FAQ-T20-10010` |

这个设计是刻意的：错误码的含义随型号变（不同型号的 E11 不是一回事），所以按型号展开；
而「水箱漏水」这个故障在哪个型号上发生都是同一件事，所以共享。
当前语料只有一个型号，`ERR:` 前缀的优势没体现出来，但结构已经留好——
加第二个型号时只需在 `data/seed/<新目录>/` 里声明 `manualDoc` 与 `codes`，无需改代码。

---

## 5. 断层：候选格式 vs 图格式

`extract/candidates.py` 的 `CandidateNode` / `CandidateEdge`（30 / 45 行）和
`kg/model.py` 的 `Node` / `Edge`（17 / 36 行）**字段几乎一一对应**，所以补适配器不难：

| 候选字段 | 图字段 | 处理 |
|---|---|---|
| `label` | `label` + `extra_labels` | 需要补父类标签（如 `FAQCase` 也要是 `Case`），图侧 `add_node` 已经自动做 |
| `key` | `id` | 加前缀：`key="WATER-LEAK"` → `id="FAULT:WATER-LEAK"` |
| `props` | `props` | 直接搬，但 `code`/`name` 要满足 schema 的必填项 |
| `evidence` | `props.evidence` | 直接搬（抽取层是原文，可以标 `verbatim`） |
| `doc_id` | `props.sourceId` | 直接搬 |
| `locator` | `props.locator` | 直接搬（页码 / 「第 N 步」） |
| `confidence` | `props.confidence` | 直接搬 |
| `extractor` | `props.extractor` | 直接搬 |

**缺的是什么**：没有一个函数做这件事。`build_seed.py` 只认 YAML 的 key 结构
（`models` / `faults` / `solutions`…），完全不认识 `candidates.jsonl` 的扁平 `kind` 行。

两条可选路线：

- **A（推荐）**：新增 `extract/to_seed.py`，把候选按类归并成 `data/seed/t20pro/*.yaml`
  （人可审、可手改、可 diff），再走现有 `build_seed` 管道。好处是产物可读，答辩时能指着 YAML 讲。
- **B**：新增 `kg/build_from_candidates.py`，直接把 `CandidateNode` 适配成 `Node` 塞进 `Graph`，
  跳过 YAML。快，但产物不可读，出错难定位。

### 5.1 真正的拦路虎是 ID 体系，不是字段格式

字段能一一搬，**ID 不能**：抽取层的 `key` 是语料里的中文名（`驱动轮卡住`），
种子用的是稳定英文 code（`LEFT-WHEEL-STUCK`）。直接合并会让图里 ID 变成中文，
前端的证据联动、规则里的 `SYM:*` / `SOL:*` 常量、评测题全部失效。

对策是保留英文 code 为 canonical ID，把中文名挂成 `alias`。两层条目做过一次全量比对，
重合度如下：

| 层 | 种子条目 | 可合并（exact/contain） | 语料无对应 | 候选有而种子缺 |
|---|---|---|---|---|
| component | 24 | **15** | 9 | 2 |
| consumable | 6 | 2 | 4 | 0 |
| fault | 24 | **1** | 23 | 18 |
| symptom | 27 | 7 | 20 | 51 |

部件层重合度高（15/24），因为手册第 9 页有「部件功能表」、第 17 页有「保养表」，名字基本对得上。

**故障层几乎不重合（1/24），这是本次最重要的发现**。种子的 24 个故障是人工归纳的
诊断单元，手册故障表是 19 行报障描述，两者粒度不同：

- 种子把「左轮卡滞 / 右轮卡滞」拆成两个点，手册只有一行「驱动轮卡住」
- 种子的「边刷缠绕卡滞」和手册的「双边刷脱落」是**不同故障**，看着像但不能合并
- 手册的「主机运行中漏灰」「全能基站尘桶内脏污」「拖布盘无法自动升降」，
  种子完全没有对应项——这与之前双源对账查出的 3 条真实缺口一致

所以 fault 层有 18 条是「候选有而种子缺」，属于**待补进种子的候选**，
不是可以直接合并的匹配。这一步必须人工定，机器判不了。

比对过程还暴露了一个反例：机器在 4 条上出现过「共享关键词但语义不同」的错配
（把「全能基站」配到「全能基站充电极片」、把「充电极片」配到「主机充电极片」）。
这说明**纯字面匹配在这一层不可信**，只能用来生成候选、不能用来定结论。

---

## 6. 现在这张图的已知问题

### 已修复（本轮）

| 问题 | 处理 |
|---|---|
| 型号多余（T10 / T20 / T30 三个） | 收敛为单机型 T20 PRO，`data/seed/t20pro/` 独立成目录 |
| 证据来源硬编码 T30 | 11 处 `DOC:T30-*` 硬编码全部改为查种子的 `sources`；`manualDoc` 由型号显式声明，不再靠短码拼接 |
| 悬空 sourceId | 旧图 26 条边指向不存在的 `DOC:T20PRO-MANUAL` / `DOC:T30PRO-MANUAL` / `DOC:T10TURBO-MANUAL`，**现为 0** |
| 虚构文档 | 旧种子 10 个 Document 里 8 个是编的（`DOC:MAINTENANCE-GUIDE`、`DOC:SAFETY-NOTICE`…），现全部换成语料真实 doc_id（1 手册 + 58 FAQ） |
| 证据一律声称 paraphrase | 按层分级：`paraphrase` 304 / `assumed` 152 / `synthetic` 498 |

### 仍存在

| 问题 | 表现 | 影响 |
|---|---|---|
| `verbatim` 边只覆盖案例层 | 74 条（22 条审核通过的官方 FAQ），种子层仍是 `paraphrase`/`assumed` | 故障因果链的原文回溯要靠抽取层继续补，不能只靠案例 |
| 合成数据占比高 | 165 / 432 节点、498 / 1028 边是模板生成 | 答辩时必须说清，不能当真实工单 |
| 错误码层无据 | 23 个 `ERR:*` 全部标 `assumed` | 手册通篇没有 E11/10010 这类编码，只能当厂商惯例 |
| Document 只有标题 | 59 个 `Document` 节点没有正文，手册的 `url` 是本地路径 | 前端点文档看不到内容，需要接 `data/raw/chunks.jsonl` |
| 故障层与手册不同构 | 24 个种子故障只有 1 个能在故障表找到对应 | 见 5.1，需要人工决定补哪些 |

---

## 7. 建议阅读顺序

只想看懂「图怎么来的」，按这个顺序看四个文件就够：

1. `data/seed/t20pro/02_knowledge.yaml` —— 先看人写的事实长什么样（故障/根因/方案三段式），
   顶部的 `sources` / `evidence` 两段决定每一层证据怎么盖章
2. `backend/app/kg/build_seed.py:194` `build()` —— 看 16 个 `_build_*` 的调用顺序
3. `backend/app/kg/build_seed.py:69` `rel()` —— 看每一条边必须满足什么
4. `backend/app/kg/validate.py:38` `validate()` —— 看什么样的图算「合格」

想继续补「新知识进图」，再看：

5. `backend/app/extract/candidates.py:30` —— 候选的数据结构
6. `backend/app/extract/pipeline.py:93` —— 候选是怎么被生产出来的
