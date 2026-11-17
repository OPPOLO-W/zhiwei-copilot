# 智维 Copilot · DEEBOT T 系列售后知识图谱与故障排查 Agent

> 人工智能原理课程作业（场景 C：企业知识管理）。以「产品—客户—工单」知识图谱为底座，
> 用**本体驱动的知识表示 + 可证明的规则推理 + RefKG-lite 式 Agent 编排**，回答真实售后问题，
> 并给出可交互的推理路径可视化。

- 设计文档：[`documents/场景C-企业知识管理KG-Agent开发文档.md`](documents/场景C-企业知识管理KG-Agent开发文档.md)
- **项目全景导读：[`docs/project-guide.md`](docs/project-guide.md)**（先读这份：数据怎么来、图怎么建、问题怎么被回答，含 7 张流程图 + 项目结构导航）
- **开发流程重演：[`docs/dev-walkthrough.md`](docs/dev-walkthrough.md)**（按开发文档 D1–D28 切 6 个阶段，配 `git tag` 可逐段回看）
- 本体说明：[`docs/ontology.md`](docs/ontology.md)（由 `schema.yaml` 生成）
- 抽取与数据账本：[`docs/extraction.md`](docs/extraction.md)
- 实验与误差分析：[`docs/evaluation.md`](docs/evaluation.md)
- 答辩演示脚本：[`docs/demo-script.md`](docs/demo-script.md)
- 图谱规模：**432 实体 / 1028 关系**（课程要求 ≥60 / ≥120）
- 推理规则：**24 条**声明式规则（课程要求 ≥20）
- 评测集：**50 题**（单跳 10 / 两跳 11 / 多跳 13 / 歧义 5 / 型号范围 4 / 安全 4 / 超范围 3）

---

## 1. 快速开始

### 1.1 依赖

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows；Linux/macOS 用 source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # 按需修改 NEO4J_PASSWORD
```

### 1.2 一键起（Docker，可选）

```bash
docker compose up -d
docker compose exec backend python -m backend.app.kg.loader --reset   # 首次导入图谱
# Neo4j Browser http://localhost:7474 · 后端 http://localhost:8000/docs · 前端 http://localhost:5173
```

代码以卷挂载，改完不用重建镜像；后端容器首次启动会 `pip install -e .`（约 1~2 分钟）。
生产部署不该这样挂源码，这里是为了评审查代码时改一行就能生效。

### 1.3 导入 Neo4j（本机 Neo4j Desktop，不用 Docker）

```bash
# 1) 在 Neo4j Desktop 里启动 DBMS（默认 bolt://localhost:7687）
# 2) 建约束与索引
#    把 backend/app/kg/schema.cypher 复制进 Neo4j Browser 执行
# 3) 全量导入（--reset 会先清空 :Entity 节点）
python -m backend.app.kg.loader --reset
```

离线/无 Neo4j 时用内存后端，数据来自同一份 `data/curated/*.jsonl`：

```bash
# .env 里设置
KG_BACKEND=memory
```

### 1.4 启动服务

```bash
# 后端
uvicorn backend.app.main:app --reload --port 8000
# 前端（另开一个终端）
cd frontend && npm install && npm run dev
# 打开 http://localhost:5173
```

前端支持深链演示，例如
`http://localhost:5173/?mode=compare&tab=agent&q=T20 PRO 报 E11，左轮不转`。

### 1.5 重建数据与跑评测

```bash
python -m backend.app.kg.build_seed          # 重建种子 + 合成数据 → data/curated
python -m backend.app.kg.validate            # 规模与质量校验（9 类检查）
python -m backend.app.eval.run_eval --all --repeat 3   # 五组系统 x 3 次（含两组消融）
python -m backend.app.eval.run_eval --system rule      # 单组：rule / agent / agent_noreflect / agent_norules / rag
```

装了 `make` 的话等价目标见 [`Makefile`](Makefile)：`make seed` / `make eval` / `make test` /
`make docs`（重新生成本体文档）/ `make check`（校验文档与本体是否同步）。

### 1.6 换一份机型语料（例如 T 系列手册）

抽取层不绑定任何具体型号，换语料只需三步。

**① 按规范放文件**——型号靠文件名识别，命名里必须出现 `DEEBOT <型号>`：

```
documents/
  User_Manual/地宝 DEEBOT T20 PRO.pdf      # 手册：文件名带型号
  Questions/地宝 DEEBOT T20 PRO/*.md       # FAQ：目录名带型号，一个型号一个目录
```

**② 先预检，再重建**（预检不覆盖任何文件）：

```bash
python -m backend.app.ingest.build --dry-run   # 只解析 documents/，打印型号识别结果
python -m backend.app.ingest.build             # PDF/FAQ → data/raw
python -m backend.app.extract.pipeline         # data/raw → data/extracted（自动识别全部型号）
```

**③ 看三个数**：`按产品` 一栏型号是否正确、`证据覆盖` 是否 100%、`带定位` 是否等于节点边数之和。
任一不对，说明文件名不规范或表格解析退化，别急着往图里灌。

> 多机型混放时，组件、耗材兼容关系按型号隔离（A 机型手册里的部件不会被判成 B 机型可用）。
> `--product` 可只抽单一型号；不传则自动抽取全部。

### 1.7 抓官网 FAQ（可选，配图全部丢弃）

官网「欢迎使用」分页里的常见问题，可用脚本批量落成 Markdown（一级标题=问题，正文=步骤）：

```bash
python -m backend.tools.fetch_faq              # 6 页全量 → documents/Questions/地宝 DEEBOT T20 PRO/
python -m backend.tools.fetch_faq --pages 1-2  # 只抓前两页
python -m backend.tools.fetch_faq --dry-run    # 只打印清单
```

落盘规则：`<img>/<video>/<iframe>` 整块删除；删图后残留的空媒体标题（`3. 视频指导`、
`**配网前准备视频指导：**`、`充电指导视频`）由 `_is_media_placeholder()` 判定后一并删掉，
含「请观看视频」这类动词的正文会保留；`<strong>` → `**`，`<br>` → 换行，列表 → `- `；
标题末尾问号去掉、`/:*"<>|` 换成 `-` 作文件名。
抓取清单（id/标题/页号/URL/字数）写到 `data/curated/faq_manifest.json`，用于核对条数；
ingest 拿这份清单把 doc_id 钉成 `DOC:FAQ-15908`，证据就能一路点回官网原页面。

抓完直接跑抽取即可，FAQ 会被拆成四类知识：

| 抽出什么 | 从哪来 | 例 |
| --- | --- | --- |
| `Symptom` 症状 | 标题 | 「机器人无法返回基站，如何处理？」→「机器人无法返回基站」 |
| `FAQCase` 案例卡 | 整篇 | 保留原文全文 + 官网链接，一条证据可回溯 |
| `Solution` 步骤 + `PRECEDES` | 编号条目 | 第 1→2→3 步，串成可执行顺序 |
| `INDICATES` 对齐 | 标题 vs 手册故障名 | 见 3.5 双源对账 |

「温馨提示 / 注」区里的条目**不当作步骤**——那是配网前提与禁令，
当成第 N 步会输出一份「照着做也配不上网」的清单。

---

## 2. 目录结构

```
backend/app/
  config.py                 # pydantic-settings，唯一配置入口
  kg/                       # 知识图谱层
    ontology.py             #   schema.yaml → 强类型本体（类/关系/可视化配色）
    model.py                #   Node/Edge/Graph + jsonl 读写
    build_seed.py           #   两阶段构建：先声明实体再连边，解决 faults↔causes↔checks 环
    synthetic.py            #   固定随机种子的合成案例/工单（带 dataProvenance）
    validate.py             #   9 类质量检查（类型、domain/range、证据、孤立点、规模…）
    store.py                #   GraphStore 抽象 + subgraph/find_paths
    neo4j_store.py          #   生产后端（只读查询）
    memory_store.py         #   离线后端（同一份 jsonl，测试/CI）
    loader.py               #   Neo4j 全量导入（--reset）
    queries.py              #   Q1~Q5 多跳查询
  rules/                    # 推理层
    engine.py               #   正向链式 + 分层求值（先正规则到不动点，再求值否定）
    facts.py                #   图 → 逻辑事实投影（含聚合事实预计算）
    loader.py               #   规则静态校验（谓词白名单、变量绑定）
    rules.yaml              #   24 条规则（诊断/排序/安全/卫生）
  reasoning/service.py      # 诊断编排：事实 → 推理 → 排序 → 证明树 + 证据
  reasoning/graph_only.py   # 消融 E 用：纯图检索链（不经规则推理）
  agent/                    # Agent 层（RefKG-lite）
    slots.py                #   Query Decoupling：槽位抽取 + 子问题
    tools.py                #   9 个窄接口工具（8 个诊断用 + 1 个消融对照；LLM 只能选工具，不能写 Cypher）
    graph.py                #   intake→decompose→plan→act→retry→verify→finalize
  api/                      # FastAPI 路由（query / graph / eval）
  ingest/                   # 摄取层：PDF/FAQ → 带页码与表格行锚点的语料块
    readers.py              #   pdfplumber 表格 + 按 x 坐标还原目录层级
    corpus.py               #   Chunk/Doc/Table；软换行与并列项判别
  extract/                  # 抽取层：语料 → 带原文证据的候选三元组
    extractors.py           #   表格适配器（故障/保养/部件/参数）+ 词表 + 型号系列推断
    faq.py                  #   FAQ 适配器（症状 / 案例卡 / 有序步骤 / 对账）
    align.py                #   双源对账报告（手册覆盖 FAQ、FAQ 覆盖手册）
    pipeline.py             #   按型号分组抽取，型号自动识别
  tools/fetch_faq.py        # 官网 FAQ 抓取（去图 → Markdown + manifest）
  tools/gen_ontology_doc.py # schema.yaml → docs/ontology.md（--check 可进 CI）
  eval/                     # 评测指标与执行器（五组系统 + 均值±标准差聚合）
  observability/trace.py    # 每次运行写 var/traces/*.jsonl
data/
  ontology/schema.yaml      # 单一事实源：23 类 + 33 关系 + 质量闸门
  seed/*.yaml               # 人工整理的产品/知识种子
  review/faq_cases.yaml     # 案例审核台账（58 条候选逐条定论，accepted 才进图）
  curated/*.jsonl           # 构建产物（Neo4j 与 MemoryStore 的共同数据源）
  raw/*.jsonl               # 摄取产物（docs/tables/chunks，带页码锚点）
  extracted/candidates.jsonl # 抽取产物（节点边混存，kind 区分）
  eval/questions.jsonl      # 50 题评测集（含 gold 标注）
frontend/                   # React + Vite + Cytoscape.js 可视化
docs/
  ontology.md               # 本体说明（生成物，含类/关系/实例数/CQ/质量闸门）
  extraction.md             # 抽取流水线与数据账本（真实/合成比例、对账结果）
  evaluation.md             # 实验设计、五组结果、消融、误差分析
  demo-script.md            # 答辩演示脚本（每幕：操作 → 画面 → 话术 → 兜底）
Makefile                    # make up/seed/eval/test/docs
docker-compose.yml          # Neo4j + 后端 + 前端一键起
```

---

## 3. 关键设计

### 3.1 本体是唯一事实源

`data/ontology/schema.yaml` 同时派生：校验器的类型/domain/range、Neo4j 标签与约束、
抽取 Prompt 的字段说明、前端节点的颜色与形状。**改本体不需要改代码**，也不会出现
「后端有 23 类、前端画 20 类」的漂移。

### 3.2 错误码按「型号 × 错误码」展开

`ERR:T20-PRO:E11` 这样的实例键避免了同一 `E11` 在不同型号间串码；当用户把某个型号
没有的错误码问出来（例如问一个语料中不存在的型号），系统返回**可解释的降级提示**而不是崩溃：

> 型号「NOT-A-MODEL」不在图谱中，当前语料覆盖：T20 PRO。

这是「知识表示粒度」直接影响「推理正确性」的直接体现。

### 3.3 可证明的规则推理

- 规则是 YAML 声明式，加载时做四类静态校验（ID 唯一、谓词白名单、变量绑定、安全标记）；
- 引擎按优先级**分层求值**：正规则跑到不动点后再求值否定条件（negation as failure），
  避免「方案还没推出来就先断定没有方案」；
- 置信度沿证明链传播（`max` / `noisy_or`），且**证明置信度与排序分数分离**
  （`confidence` 参与下游连乘，`rankScore` 只用于排序），防止自引用把分数抬到 1.0；
- 每次诊断都返回 `derivationTree`（规则 ← 前提）+ 最小证明子图 + 逐条证据（含出处文档）。

### 3.4 RefKG-lite 编排

```
intake → decompose → plan → act → retry → verify → finalize
```

- **decompose**：把问题拆成 5 个子问题，各带 `requiredEvidence` 与工具提示；
- **act**：只走窄接口工具，模型无法生成 Cypher，注入面可控；
- **retry**：按子问题覆盖度决定是否重试（放宽时间窗、扩展子图），并用
  `(tool,args)` 去重，避免重复调用。**这是查表重试，不是语义反思**——不读证据内容，
  决策空间只有 `q2 → expand_subgraph`、`q3 → search_similar_cases` 两条硬编码分支；
- **verify**：检查「网络问题却推荐换件」「安全场景建议自助拆机」「推荐不兼容备件」等矛盾。
  确定性实现，两条路径共用，LLM 不得改写。

**LLM 驱动版（`agent/llm_agent.py`）**：`intake/decompose`、`plan/act`、`reflect`、
`finalize` 四个节点由 LLM 驱动，其余（规则骨干 `run_rules`、`verify`）保持确定性。
其中 `reflect` 是真正的语义反思：输入证据摘要与覆盖度，输出自然语言 `missing`、
`decision` 与 `conflicts`，决策空间是全部白名单工具 + 任意参数。trace 上以节点名区分两件事——
确定性路径记 `retry`，LLM 路径记 `reflect`。

- 配置 `.env` 的 `LLM_API_KEY` 后，请求 `POST /api/query` 加 `"useLlm": true`
  即走 LLM 路径；不传 `useLlm` 或未配 key 时自动回退确定性实现；
- 规则骨干永远先跑：`safety_stop`、`network_first` 等安全/网络规则不被 LLM 跳过，
  LLM 生成的答复也会强制保留规则告警与免责声明；
- 每个 LLM 节点失败都降级到确定性等价实现，整条链路不会因 LLM 超时/限流而崩；
- 评测对比项：`python -m backend.app.eval.run_eval --system agent_llm`（默认 `--all`
  仍跑文档 9.3 的 A~E 五组，不改变历史口径）。

### 3.5 双源对账：手册故障表 ↔ 官网 FAQ

同一台机器有两份独立描述它的材料——手册的故障排查表是**工程视角**
（故障 → 原因 → 方法），官网 FAQ 是**用户视角**（一句话现象 → 一串步骤）。
两份各写一遍同一件事，就能互相校验：

```bash
python -m backend.app.extract.pipeline      # 顺带产出 data/extracted/faq_fault_alignment.json
```

对齐用两层判据（`extract/faq.py: _fault_similarity`），不靠逐字相似度：

| 部件 | 症状动作 | 分数 | 例 |
| --- | --- | --- | --- |
| 相同 | 同类 | 0.92 | 「拖布支架时转时不转」←→「拖布支架无法旋转」 |
| 不同 | 同类 | 0.76 | 「充不进电」←→「主机无法充电」 |
| 相同 | 不同 | 0.52 | 不采信 |
| 都不同 | | 字面分 × 0.9 | 兜底 |

只用字符相似度会漏掉「无法充电 / 充不进电」这类换个说法就几乎不共享字符的对子。
对齐边一律 `verification: pending` 并写明 `matchMethod`，**自动对齐不冒充已确认事实**。

T20 PRO 实测：手册 19 条故障（1 条注明「除 T20 机型外」不计），
FAQ 29 条故障类，对齐 21 对——手册覆盖 83%，FAQ 覆盖 52%。
两边都没覆盖上的就是真实知识缺口（如「双边刷脱落」「运行声音大」手册写了但 FAQ 没写，
「OK YIKO 无法唤醒」「清洁抹布不出热水」FAQ 写了但手册排查表没有）。

### 3.6 优雅降级

| 情况 | 行为 |
| --- | --- |
| 型号对不上错误码 | `code_not_applicable` + 逐型号含义 |
| 错误码未收录 | `unknown_code` + 核对提示 |
| 型号未收录 | `unknown_model` + 覆盖清单 |
| 完全没信号 | `unknown_intent` + 引导（Agent 返回 `out_of_scope`） |
| Neo4j 未启动 | 启动不崩，`/api/health` 报告降级，查询返回 503 与修复指引 |

---

## 4. 评测结果（50 题 × 3 次，`var/eval/aggregate_*.json`）

> 完整版含指标定义、消融解读、3 个失败案例见 [`docs/evaluation.md`](docs/evaluation.md)。
> 下表为标准差均为 0（系统确定性、不调 LLM），3 次重复是为了证明可复现。

| 系统 | answerAcc | faultF1 | solutionAcc | pathP | pathR | pathF1 | faithfulness | coverage | 工具调用 | P50/P95 (ms) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A `rule`（纯规则） | 0.980 | 0.977 | 1.000 | 0.237 | **0.942** | 0.370 | 0.804 | — | — | 327 / 383 |
| B `rag`（弱基线：单链检索） | 0.300 | 0.946 | 0.954 | **0.267** | 0.333 | 0.288 | 0.920 | — | — | **3 / 4** |
| C `agent`（RefKG-lite） | **0.980** | 0.977 | 1.000 | 0.237 | **0.942** | 0.370 | 0.804 | **0.915** | 2.06 | 719 / 820 |
| D `agent_noreflect`（消融：去覆盖度重试） | 0.980 | 0.977 | 1.000 | 0.237 | 0.942 | 0.370 | 0.804 | 0.889 | **1.94** | 734 / 902 |
| E `agent_norules`（消融：去规则） | 0.800 | 0.961 | 0.977 | 0.204 | 0.872 | 0.323 | 0.845 | 0.915 | 2.06 | 825 / 937 |

**分母说明**：50 题里有 7 题（型号未收录 4 + 超范围 3）本来就没有故障/方案/证据可答，
不参与 faultF1 / solutionAcc / path 三项均值，实际计分 43 题（结果里的 `nFaultScored`）。
把它们按 0 分计入会让整体指标被无解题系统性压低约 14%。

读法与结论：

1. **路径指标必须 P/R 一起看**。规则/Agent 输出的是完整证明链（召回 0.942），
   分母大导致 precision 被摊薄；弱基线只返回一条最短路，precision 天然更高但漏掉
   大部分必要证据（召回 0.333）。只看 F1 会得出「检索比推理更好」的错误结论。
2. **规则与 Agent 在准确率上持平**（同一条推理内核），Agent 的增益体现在
   *上下文完整度*（相似工单、耗材兼容性、覆盖度自检）与 *降级路径*，
   代价是 1~2 次额外工具调用（P50 327ms → 719ms）。
3. 弱基线的回答准确率只有 **0.300**：没有规则校验，它不会拒绝「10010 要换主板」，
   也不识别安全场景，在歧义题与安全题上直接答错；但故障名照样能猜对（0.946），
   说明**只看 faultF1 会误判系统能力**，答案级与路径级指标才是区分点。
4. **去规则（E）掉 18 个百分点**：多跳题退回「边置信度最高者」、歧义题不选主候选、
   安全题整体丢失停用告警。规则层不是解释装饰，是决定结论正确性的部件。
5. **去覆盖度重试（D）几乎没有影响**：50 题里 6 题的 `q3` 覆盖度从 1.0 掉到 0.8，
   证据链不变——因为该节点只按查表补上下文、不补证据边（确定性路径没有语义反思）。
   这是明确的改进项，不粉饰。
6. 唯一答错的计分题是 Q028（「清理轮组后还是卡，是否要换轮组」）——系统仍把
   「轮轴毛发缠绕」列为首要根因（cf 0.75 > 轴承磨损 0.45），没有利用「已清理过」这一
   前提。根因是图谱缺「已执行动作」维度，修法见 `docs/evaluation.md` §8.2。

复现：`python -m backend.app.eval.run_eval --all --repeat 3`（或 `make eval`）。

---

## 5. 可视化

前端一屏三栏 + 底部时间线：

- **左**：自然语言提问、槽位回显、结论卡（置信度、策略徽章、安全告警、可追溯结论）；
- **中**：证明子图（Cytoscape + dagre 分层，节点配色来自本体），点节点看属性，
  关系名默认隐藏、悬停/选中展开；
- **右**：证据（与图联动高亮）、证明树（规则 ← 前提，实体 id 自动换中文名）、
  规则库与触发统计、Agent 编排（子问题/覆盖度/工具调用/重试·反思）、对比模式差异；
- **底**：intake→finalize 轨迹时间线，点击查看每步载荷。

---

## 6. 测试

```bash
python -m pytest backend/tests -q      # 99 passed, 2 skipped
```

覆盖：本体规模与继承、图谱规模/证据完整性/无孤立点、规则白名单与静态校验、
四类典型场景的推理正确性、跨型号与未知码降级、Agent 槽位与去重、
**两组消融开关（去覆盖度重试 / 去规则，含安全策略消失的反例）**、
**保养类案例无症状时的详情路径**、**官方案例边的 verbatim 属性**、
API 契约与 SSE 回放、trace 落盘。

文档与本体是否同步也做成了命令，可直接进 CI：

```bash
python -m backend.tools.gen_ontology_doc --check   # docs/ontology.md 与 schema.yaml 不一致则非零退出
python -m backend.tools.gen_faq_review --check     # 案例审核台账：58 条不漏、引用 ID 合法、理由齐全
```

---

## 7. 已知限制

- **语料只覆盖单机型 T20 PRO**。跨型号比较（CQ7）目前只能返回「当前覆盖：T20 PRO」的
  降级提示；评测里的型号范围题考的就是这个降级路径。抽取层不绑定型号，补语料即可扩。
- **58 条真实官方 FAQ 里只有 22 条进图**。另外 36 条是功能教程（「如何下载 APP」这类），
  没有现象也没有处置，留在 `Document` 层；判定与理由逐条写在
  `data/review/faq_cases.yaml`。图中 `FAQCase` 共 46 条 = 22 条官方原文（`official_faq`）
  + 24 条模板代理（`faq_template_proxy`，`needsReview=true`），按 `sourceType` 可分辨。
- 未接 LLM 时 Agent 用确定性规划器（保证评测可复现）；`backend/app/llm/` 预留了
  OpenAI 兼容客户端与本体引导抽取 Prompt 的位置，接入后可通过 `useLlm` 打开。
- 合成案例/工单带 `dataProvenance=seed_generated`，全校验器单独统计合成占比，
  避免「用合成数据刷指标」。
- 相似案例检索当前是「同型号同症状 + 时间窗」的确定性匹配，未做向量召回。
- 可解释性的人工双盲评分未做（文档 9.4 的 Explainability Score 与 kappa 尚无数据）。
- 规则层事实投影每次全图扫描，Agent P50 约 0.7s；缓存投影后有望降到 0.1s 量级。
