# 处理链路与文件索引

这份文档回答一个问题：**一份材料从进项目到变成图谱里的节点，中间经过了哪些文件。**

每个环节都给出 `文件:行号`，可以直接跳过去看。

---

## 0. 命令序列

按顺序执行，上一步的产物是下一步的输入：

```bash
python -m backend.tools.fetch_faq          # 官网 FAQ → documents/Questions/*.md
python -m backend.app.ingest.build         # documents/ → data/raw/
python -m backend.app.extract.pipeline     # data/raw/ → data/extracted/
python -m backend.tools.gen_faq_review --init   # 抽取候选 → 人工审核台账（首次）
python -m backend.app.kg.build_seed        # 种子 + 合成 + 审核案例 → data/curated/
python -m backend.app.kg.loader            # data/curated/ → Neo4j
uvicorn backend.app.main:app --reload      # 起服务
```

前五步都已跑通，产物是真实语料 + 人工定论；最后两步产物与 `data/curated` 逐条一致。

---

## 1. 手册通道：PDF → 语料块

这份手册共 **32 页**，目录 5 个一级章节，页码区间以实际摄取结果为准（不是手册印刷页码）：

| 章节 | 页码 | 主要内容 |
|---|---|---|
| 前言 | 1-3 | 安全规范、使用限制 |
| 1. 产品组成 | 5-9 | 包装内容物、部件名称、传感器 |
| 2. 产品使用 | 10-16 | 准备工作、App 下载、充电、清扫、拖地 |
| 3. 部件保养 | 17-25 | 保养频率表、尘盒、滚刷、基站、尘袋、过滤件、关机存放 |
| 4. 常见问题排查 | 26-29 | 故障排查表（4 页，51 行） |
| 5. 产品参数 | 30-31 | 参数表、有害物质表 |

> 这些数字请以 `data/raw/chunks.jsonl` 里 `section` + `page` 的实际分布为准，
> 换机型后必须重新统计，不要沿用本文档的数值。

入口 `backend/app/ingest/build.py:155`（`main`），先由 `collect()`（`build.py:104`）扫描
`documents/` 目录并给每份文档定身份。

| 步骤 | 文件 | 函数 | 行 | 做什么 |
|---|---|---|---|---|
| 1.1 型号识别 | `ingest/build.py` | `_infer_product()` | 57 | 从文件名用 `_MODEL_RE`（36 行）提型号，不用白名单 |
| 1.2 文档编号 | `ingest/build.py` | `_manual_id()` | 66 | `DOC:T20PRO-MANUAL` |
| 1.3 版面解析 | `ingest/readers.py` | `read_pdf()` | 167 | 逐页：聚行 → 切栏 → 还原折行 → 抽表 → 切块 |
| 1.4 按 y 聚行 | `ingest/readers.py` | `_page_lines()` | 27 | 取**首个非空白字符**的 x0，缩进信息才不会丢 |
| 1.5 目录层级 | `ingest/readers.py` | `_parse_toc()` | 61 | 按编号形态判层级（`3.` 一级 / `3.3` 二级） |
| 1.6 页 → 章节 | `ingest/readers.py` | `_section_of()` | 84 | 由目录区间反推当前页所属章节 |
| 1.7 双栏排序 | `ingest/readers.py` | `_order_by_column()` | 149 | 按 `_COLUMN_GAP=15.0`（24 行）切段，先左栏后右栏 |
| 1.8 折行还原 | `ingest/readers.py` | `_merge_lines()` | 120 | 上一行无句末标点且本行不是新条目 → 合并 |
| 1.9 表头恢复 | `ingest/readers.py` | `_split_merged_header()` | 94 | PDF 把表头和首行并成一格时拆回 |
| 1.10 单元格扁平化 | `ingest/corpus.py` | `cell_flat()` | 185 | 用行长比值区分「软换行」与「并列项」 |
| 1.11 写盘 | `ingest/build.py` | `main()` | 155 | 写 `data/raw/{docs,chunks,tables}.jsonl` |

### 为什么这几步值得看

- **1.4 的坑**：PDF 的缩进常用空格字形实现，若取 `chars[0].x0` 会一律拿到同一个值，目录层级全丢。
- **1.5 的坑**：早先是按 x0 缩进聚类判层级。这份手册是双栏，右栏整列右移（一级章节在 x≈312，
  左栏一级在 x≈28），右栏的一级章节全被当成小节，「3.8 关机存放」被读成第 8 章，
  第 25 页之后的内容全挂错章节。改成按编号形态判，与版式解耦。
- **1.7 的坑**：`page.extract_text()` 按 y 拼接，双栏会把左右栏交错成一句
  （「清理地面上的电源1. 仅适用制造商专配的充电座」）。必须先切栏再排序。
- **1.10 的坑**：一格里换行可能是排版折断，也可能是并列的多个部件。用「末行长度 ≥ 2.5 × 中位行长度」
  判别，前者直拼、后者用顿号，否则会造出「无法返、回全能基站」这种断词。

### 产物

```
data/raw/docs.jsonl     59 条    doc_id / title / doc_type / product / page_count / sections / url
data/raw/chunks.jsonl   925 条   chunk_id / kind / page / section / text / headers / anchor
data/raw/tables.jsonl   9 条     table_id / page / headers / rows / note / prose_cols
```

`chunks.jsonl` 的 `kind` 有四类：`paragraph` 456、`list_item` 245、`heading` 139、`table_row` 85。

---

## 2. FAQ 通道：网页 → Markdown

入口 `backend/tools/fetch_faq.py:180`（`main`）。这是唯一一个**不在 `backend/app` 下**的模块，
因为它是采集工具，不参与运行时。

| 步骤 | 文件 | 函数 | 行 | 做什么 |
|---|---|---|---|---|
| 2.1 列表页 | `tools/fetch_faq.py` | `collect()` | 53 | `welcome-p1..p6` → 58 个 `question-<id>` |
| 2.2 删图 | `tools/fetch_faq.py` | `DROP_TAGS` | 35 | `img/picture/figure/script/iframe/video` 整块 decompose |
| 2.3 文本化 | `tools/fetch_faq.py` | `_render()` | 71 | 只在块级元素（`BLOCK_TAGS`，36 行）与 `<br>` 换行 |
| 2.4 清洗 | `tools/fetch_faq.py` | `_tidy()` | 111 | 去重复空行、孤立加粗符、纯媒体占位行 |
| 2.5 落盘 | `tools/fetch_faq.py` | `main()` | 180 | `documents/Questions/地宝 DEEBOT T20 PRO/<标题>.md` + manifest |
| 2.6 分块 | `ingest/readers.py` | `read_markdown()` | 305 | 切成 heading / list_item / paragraph，保留列表边界 |
| 2.7 加粗判定 | `ingest/readers.py` | `_is_bold_heading()` | 284 | 整行加粗是小标题，还是被加粗的正文 |

### 为什么这几步值得看

- **2.3 的坑**：一开始用 `node.get_text("\n")`，每个标签边界都断行，把
  「1. 扫描…（安装时请授权…）」拆成两行。改成自写递归，只认块级元素。
- **2.6 的坑**：`**温馨提示：**` 的首字符 `*` 会被判成列表符号，正文变成 `*注：…` 的假步骤。
  现在的规则是「凡以 `**` 开头先剥星号，再判断形态」；`温馨提示/注/注意/说明/备注` 这一行会被
  升格成 section 标记（`readers.py:305` 内的处理），下游靠 section 就能把这一整区与真正的步骤分开。
- **2.7 的坑**：「注：……」这种只有开头加粗的行，若按「整行加粗即标题」处理，会把正文吞成标题。
- **2.4 的坑**：删图后原文会留下一行空标题。实际遇到过三种写法：`3. 视频指导`（还占了步号）、
  `**配网前准备视频指导：**`、`充电指导视频`。判定函数是 `_is_media_placeholder()`（`fetch_faq.py:119`）：
  去掉编号与加粗后，整行只剩「修饰词 + 媒体词」才算占位；含「请看观播放点」等动词的一律保留，
  避免把「请观看视频」这类正文误删。

### 产物

```
documents/Questions/地宝 DEEBOT T20 PRO/*.md   58 篇，21630 字，平均 372 字
data/curated/faq_manifest.json                  id / 标题 / 页号 / URL / 字数
```

---

## 3. 抽取层：语料 → 候选三元组

入口 `backend/app/extract/pipeline.py:52`（`build`），先按型号分组（`_group_by_product`，42 行），
再交给 `_build_product()`（93 行）分派。

### 3.1 手册：按表头分派到表格适配器

`extract/pipeline.py:93` 里按 `headers` 匹配，四个适配器都在 `extract/extractors.py`：

| 表头特征 | 函数 | 行 | 抽成 |
|---|---|---|---|
| `故障情况` + `可能原因` + `解决方法` | `extract_fault_table()` | 195 | Fault / Cause / Solution / Check + `CAUSED_BY` / `RESOLVED_BY` / `DETECTS` |
| `维护频率` + `更换频率` | `extract_maintenance_table()` | 379 | Consumable + `REQUIRES_CONSUMABLE` |
| `功能描述` | `extract_component_function_table()` | 437 | Component + `HAS_COMPONENT` |
| `产品型号` + `设备型号` | `extract_param_table()` | 487 | ProductModel.deviceCodes 等参数 |

配套的词表与归一化也在同一个文件：

| 函数 | 行 | 用途 |
|---|---|---|
| `series_code()` | 36 | 型号首字母 → 系列代号（T20 PRO → T） |
| `canonical_component()` | 109 | 部件别名归一（下视传感器 / 下视感应器） |
| `classify_category()` | 134 | 故障 → 6 个 FaultCategory |
| `verified_components()` | 141 | 从全语料筛出「确实被提到过」的部件，作为实体链接词表 |
| `components_in()` | 155 | 一段文本里命中了哪些部件 |
| `_forward_fill()` | 178 | 故障表的合并单元格向下回填 |

### 3.2 FAQ：一篇拆成四类知识

`backend/app/extract/faq.py:127`（`extract_faq_doc`）。

| 抽成 | 来源 | 边的去向 |
|---|---|---|
| `Symptom` | 标题 | `DOCUMENTED_IN` → Document、`OCCURS_IN` → Component |
| `FAQCase` | 整篇（保留原文与官网链接） | `OBSERVED` → Symptom、`ABOUT` → ProductModel |
| `Solution` | 编号条目，按篇内独立编号 | `PRECEDES` 串步序、`DOCUMENTED_IN` → Document |
| `INDICATES` | 标题与手册故障名对齐 | Symptom → Fault |

判定辅助：

| 函数 | 行 | 用途 |
|---|---|---|
| `faq_type()` | 66 | 「如何/怎么」开头 → `operation`，其余 → `troubleshoot` |
| `_is_note()` | 108 | 单条是否为注释 |
| `_is_note_section()` | 112 | 整段是否处于「温馨提示」区 |
| `_looks_like_action()` | 57 | 排除参数说明、续航时间这类非动作条目 |
| `_step_text()` | 122 | 去掉序号与排版残留，取纯动作文本 |
| `slug()` | 61 | 中文标题 → 稳定 key |
| `_solution_type()` | 90 | 方案类型：clean / replace / reset / reconfigure / escalate |

### 3.3 候选落盘

`backend/app/extract/candidates.py` 定义 `CandidateNode`（30 行）/ `CandidateEdge`（45 行）/
`Extraction`（59 行），`Extraction.write()`（102 行）写出 jsonl，`summarize()`（128 行）出统计。

```
data/extracted/candidates.jsonl   585 节点 / 888 边
```

每行都带 `evidence`（原文片段）、`locator`（页码或「标题 · 第 N 步」）、`doc_id`、`confidence`、
`extractor`（哪个抽取器产出的）。**证据覆盖率 100%，1473 条带定位。**

---

## 4. 双源对账

`backend/app/extract/align.py:32`（`alignment_report`），报告写到
`data/extracted/faq_fault_alignment.json`。

判据不看逐字相似度（中文换个说法就几乎不共享字符），而是两层：

```
部件相同 + 症状同类  → 0.92
仅症状同类           → 0.76
仅部件相同           → 0.52（不采信）
都没有               → 字面相似度 × 0.9（兜底）
```

- `_symptom_tags()`（404 行）：12 类等价说法（不转 / 无法充电 / 无法返回 / 水路 / 集尘 / 漏扫 /
  地图 / 连接 / 噪音 / 被困 / 无响应 / 未完成）
- `_fault_similarity()`（408 行）：给出 `(分数, 依据)`，依据会写进边的 `matchMethod` 供复核
- `_applies_to()`（24 行）：把「除 T20 机型外」这类注记的故障判为不适用本机型

对账结果：手册 18 条适用，覆盖 15（83.3%）；FAQ 29 条故障类，覆盖 15（51.7%）。

所有对齐边标 `verification: pending`，自动对齐不冒充已确认事实。

---

## 5. 下游：图 → 规则 → 推理 → API

这一段目前跑的还是**旧图**，等新候选落地成 seed 后才切换。

| 环节 | 文件 | 函数 | 行 | 说明 |
|---|---|---|---|---|
| 本体 | `kg/ontology.py` | `load_ontology()` | 139 | 读 `data/ontology/schema.yaml`（23 类 / 33 关系） |
| 种子构建 | `kg/build_seed.py` | `build()` | 565 | `data/seed/t20pro/*.yaml` → `data/curated/*.jsonl` |
| 合成数据 | `kg/synthetic.py` | `generate_cases()` | — | 生成 165 个虚构案例/工单，`synthetic=False` 可关 |
| 本体校验 | `kg/validate.py` | `validate()` | 38 | 类/关系是否合规、必填属性是否齐 |
| 入库 | `kg/loader.py` | `load()` | 98 | 写 Neo4j；`MemoryStore` 走 `memory_store.py:114` |
| 规则加载 | `rules/loader.py` | `load_rules()` | 116 | 24 条 Datalog 规则 |
| 事实投影 | `rules/facts.py` | `build_facts()` | 65 | 图 → 原子（含 `?CF*` 置信度绑定） |
| 推理引擎 | `rules/engine.py` | `Engine` | 111 | 正向链式到不动点，分层求值 |
| 诊断编排 | `reasoning/service.py` | `diagnose()` | 89 | 规则结果 → 可读答案 + 证明树 + 证据 |
| 子图证明 | `reasoning/service.py` | `_proof_graph()` | 446 | 前端高亮用的子图 |
| 槽位抽取 | `agent/slots.py` | `extract_slots()` | 60 | 从问句提型号 / 错误码 / 现象 |
| Agent 主循环 | `agent/graph.py` | `run()` | 41 | RefKG-lite 六阶段 |
| 工具集 | `agent/tools.py` | `Tools` | 38 | check_compatibility / lookup_error_code 等 |
| 统一入口 | `api/query.py` | `query()` | 71 | rule / agent / compare 三模式 |
| 评测 | `eval/run_eval.py` | `run_eval()` | 109 | 三系统对比 |

---

## 6. 当前状态

| 层 | 数据 | 来源 | 状态 |
|---|---|---|---|
| 语料 `data/raw` | 59 文档 / 925 块 / 9 表 | T20 PRO 真实手册 + 官网 FAQ | 已切换到真实语料 |
| 候选 `data/extracted` | 585 节点 / 888 边 | 上述语料 | 候选池，供对账与审核取材 |
| 审核 `data/review` | 58 条 → 受理 22 / 拒绝 36 | 人工逐条定论 | 已定稿，`--check` 可校验 |
| 图 `data/curated` | 432 节点 / 1028 边 | `data/seed/t20pro` + 合成案例 + 审核通过的官方 FAQ | 种子 295/456，真实案例 22/74，合成 115/498 |

**当前链路已闭环**：抽取候选经人工审核后，受理的 22 条以 `verbatim` 证据进图
（`kg/reviewed_cases.py`），拒绝的 36 条留在 `Document` 层；规则、评测题、前端预填问句
都已按这版图重写。

图的事实底座仍然是人工种子（295/456）——抽取层的作用是**对账与纠错**，不是直接生成图。
这一点在 `docs/extraction.md` §4 有说明，也是「为什么种子不直接用抽取结果」的答案。

---

## 7. 建议的阅读顺序

如果只想快速理解，按这个顺序看四个文件就够：

1. `backend/app/ingest/readers.py` — PDF 怎么变成结构化语料（167 行 `read_pdf`）
2. `backend/app/extract/pipeline.py` — 抽取怎么分派（52 行 `build` / 93 行 `_build_product`）
3. `backend/app/extract/faq.py` — 一篇 FAQ 怎么变成症状 + 案例卡 + 步骤链（127 行）
4. `backend/app/extract/align.py` — 两个来源怎么对账（32 行）

---

## 8. 已知缺口

- 手册 p11 的 Wi-Fi 指示灯表、p31 的有害物质表没有适配器，按普通表格行进了语料，未结构化。
- FAQ 的 58 篇里有 29 篇是操作类（配网、清洁模式切换等），只入知识库，不参与故障推理。
- 手册故障表里 `双边刷脱落`、`运行声音大`、`尘桶内脏污` 三条在 FAQ 中无对应，属真实知识缺口。
- 候选里的 `Component` 只有 17 个，来自手册部件表与故障表文本命中，尚未与 seed 里原有的
  21 个部件做实体对齐。
