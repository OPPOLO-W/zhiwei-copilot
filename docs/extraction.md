# 知识抽取与图谱构建

> 本文说明「语料怎么变成图谱」的每一步，以及**哪些是原文、哪些是推断、哪些是手写**。
> 这份文档存在的意义是：让每个三元组都能回答「你凭什么这么说」。
> 相关代码：`backend/app/ingest/`、`backend/app/extract/`、`backend/app/kg/`。

---

## 1. 三条铁律

| 铁律 | 含义 | 落地方式 |
| --- | --- | --- |
| 候选不等于事实 | 抽取器只产「候选」，进图要过审核 | 候选落 `data/extracted/candidates.jsonl`，进图数据落 `data/seed/*.yaml` / `data/curated/*.jsonl` |
| 每条边都要能追溯 | 无 `evidence` + `sourceId` 的边不允许存在 | `SeedBuilder.rel()` 强制三参数；校验器把证据覆盖率低于 100% 判为构建失败 |
| 来源等级必须标明 | 「原文」「改写」「工程先验」「合成」不可混同 | 每条边带 `evidenceMode`，每层知识单独声明，不搞一刀切 |

第三条最容易被忽略。一个常见做法是：种子数据全部标 `paraphrase`，看起来整洁，
但错误码的「含义」、根因的「可能性」这类知识在语料里根本没有原文可改写——
标成 paraphrase 是撒谎。本项目按知识层分级（见 §5）。

---

## 2. 语料

| 类型 | 数量 | 来源 | 说明 |
| --- | ---: | --- | --- |
| 用户手册 | 1 份 / 32 页 | 《地宝 DEEBOT T20 PRO 使用说明书》 | 故障排查表、保养表、部件功能表、技术参数表 |
| 官方 FAQ | 58 篇 | 科沃斯官网「欢迎使用」分页，`backend/tools/fetch_faq.py` 抓取 | 一级标题=问题，正文=编号步骤；图片/视频块整块丢弃 |

抓取落盘规则（`fetch_faq.py`）：

- `<img>/<video>/<iframe>` 整块删除，删图后残留的空媒体标题（`3. 视频指导` 等）一并删掉，
  但含「请观看视频」这类动词的正文保留；
- `<strong>` → `**`，`<br>` → 换行，列表 → `- `；
- 标题末尾问号去掉、非法字符换成 `-` 作为文件名；
- 抓取清单（id / 标题 / 页号 / URL / 字数）写 `data/curated/faq_manifest.json`，
  用于核对条数，也用于把 `doc_id` 钉成 `DOC:FAQ-15908`——**证据能一路点回官网原页面**。

「温馨提示 / 注」区里的条目**不当作步骤**：那是配网前提与禁令，当成第 N 步会输出一份
「照着做也配不上网」的清单。

---

## 3. 流水线

```text
documents/            ① 摄取 ingest          ② 抽取 extract         ③ 对账 align
  User_Manual/*.pdf ─▶ data/raw/          ─▶ data/extracted/      ─▶ faq_fault_alignment.json
  Questions/<型号>/*.md   docs.jsonl          candidates.jsonl
                          tables.jsonl        （585 节点 + 888 边）
                          chunks.jsonl
                                                    │
                                                    │ ④ 审核 review（人工 + 确定性校验）
                                                    ▼
                                          data/curated/*.jsonl ─▶ Neo4j / MemoryStore
```

```bash
python -m backend.app.ingest.build --dry-run      # 只预检型号识别，不写文件
python -m backend.app.ingest.build                # ① PDF/FAQ → data/raw
python -m backend.app.extract.pipeline            # ② 抽取 + ③ 对账
python -m backend.app.kg.build_seed               # ④ 种子 + 合成 → data/curated
python -m backend.app.kg.validate                 # 9 类质量校验
```

**① 摄取**（`ingest/`）：PDF 用 pdfplumber 抽表，按 x 坐标还原目录层级；FAQ 按标题切块。
输出带页码/表格行锚点的语料块，锚点决定了证据能不能定位回原文。

**② 抽取**（`extract/`）：不调 LLM，全部是结构适配器 + 规则。这是刻意的取舍——
本项目的评测要可复现，抽取环节引入 LLM 会让「同一份语料两次抽出不同图」，
而这恰恰是课程要求里「工程可复现」最怕的事。

| 抽取器 | 输入 | 产出 | 证据方式 | 条数 |
| --- | --- | --- | --- | ---: |
| `table/fault` | 手册故障排查表 | Fault / Cause / Check / Solution + 5 类边 | `verbatim`（表格行） | 304 |
| `table/maintenance` | 保养表 | Consumable + 更换周期 | `verbatim` | 37 |
| `table/component` | 部件功能表 | Component + 功能描述 | `verbatim` | 8 |
| `table/params` | 技术参数表 | 型号属性 | `verbatim` | 1 |
| `manual/struct` | 手册文档级 | Document / ProductLine / ProductModel | `verbatim` | 62 |
| `faq/case` | 每篇 FAQ | FAQCase 案例卡 + OBSERVED + ABOUT | `verbatim`（标题/全文） | 232 |
| `faq/symptom` | FAQ 标题 | Symptom | `verbatim` | 132 |
| `faq/step` | FAQ 编号步骤 | Solution + 有序 PRECEDES | `verbatim` | 472 |
| `faq/check` | 步骤里的检查动作 | Check + DETECTS | `verbatim` | 29 |
| `faq/order` | 步骤序号 | PRECEDES 链 | `verbatim` | 169 |
| `faq/align` | 标题 × 手册故障名 | INDICATES（待确认） | `inferred` | 21 |

合计 **585 个候选节点 + 888 条候选边**，其中 `evidenceMode=verbatim` 的有 520 节点 + 722 边。

**③ 对账**（`extract/align.py`）：同一台机器有两份独立描述它的材料——手册是工程视角
（故障→原因→方法），FAQ 是用户视角（一句话现象→一串步骤）。两份各写一遍同一件事就能互相校验。

对齐判据是两层，不靠逐字相似度（`extract/faq.py: _fault_similarity`）：

| 部件 | 症状动作 | 分数 | 例 |
| --- | --- | ---: | --- |
| 相同 | 同类 | 0.92 | 「拖布支架时转时不转」←→「拖布支架无法旋转」 |
| 不同 | 同类 | 0.76 | 「充不进电」←→「主机无法充电」 |
| 相同 | 不同 | 0.52（不采信） | — |
| 都不同 | — | 字面分 × 0.9（兜底） | — |

只用字符相似度会漏掉「无法充电 / 充不进电」这种换个说法几乎不共享字符的对子。

T20 PRO 实测（`data/extracted/faq_fault_alignment.json`）：

| 指标 | 数值 |
| --- | --- |
| 手册故障条目 | 19（其中 1 条注明「除 T20 机型外」，不计） |
| FAQ 故障类篇目 | 29 |
| 对齐成功 | 21 对 |
| 手册覆盖 FAQ | 83.3%（15/18） |
| FAQ 覆盖手册 | 51.7%（15/29） |

两边都没覆盖上的是真实知识缺口：手册写了「双边刷脱落」「运行声音大」但 FAQ 没写；
FAQ 写了「OK YIKO 无法唤醒」「清洁抹布不出热水」但手册排查表没有。
这类缺口不做「既然没人写就编一条」的补全，而是留在图外，在报告里列为已知盲区。

对齐边一律 `verification: pending` 并写明 `matchMethod` / `matchScore`：
**自动对齐不冒充已确认事实**。

---

## 4. 审核与进图

| 环节 | 谁做 | 检查什么 |
| --- | --- | --- |
| 候选生成 | 抽取器 | 只产候选，`verification` 默认 `pending` |
| 结构校验 | `extract/candidates.py` | 标签/关系必须在本体；端点必须存在；自环禁止 |
| 人工审核 | 本项目作者 | 错误码含义、根因可能性、方案风险等级逐条核对 |
| 进图 | `kg/build_seed.py` | 只接受通过审核的种子（`data/seed/<型号>/NN_*.yaml`） |
| 构建后校验 | `kg/validate.py` | 9 类检查，任一不过即构建失败 |

当前进图数据来自两条线：人工种子（`data/seed/t20pro/01_hardware.yaml` + `02_knowledge.yaml`）
与**审核通过的真实官方 FAQ 案例**（`data/review/faq_cases.yaml` → `kg/reviewed_cases.py`）。
抽取候选用于对账与校对（例如：种子里的 `DUST-BAG` 部件名与语料不符，改成了 `DUST-COLLECT`；
种子里的 `DOC:T20-MANUAL` 与真实 `DOC:T20PRO-MANUAL` 不符，导致 687 条边断链，靠对账发现）。

### 4.1 58 条官方 FAQ 的审核结论

58 条抽出来的 FAQ 并不都是「案例」：官方 FAQ 页里混着大量功能教程。
按「有没有现象、有没有可对齐的处置」逐条定论，结果写进 `data/review/faq_cases.yaml`：

| 结论 | 条数 | 判定口径 | 去向 |
| --- | ---: | --- | --- |
| `troubleshoot` → accepted | 13 | 有现象 → 处置，能对齐本体症状或方案 | 进图，建 OBSERVED / RESOLVED_BY |
| `maintenance` → accepted | 9 | 无现象，但有可对齐的保养/更换动作 | 进图，只建 RESOLVED_BY |
| `usage` → rejected | 36 | 纯功能设置/使用教程，既无现象也无处置 | 不进 Case 层，原文由 Document 节点承载 |

台账里每条都留了 `note`（为什么收/为什么拒）和 `autoHint`（机器当时建议的对齐），
两者不一致的条目最能说明人工审核在干什么——例如 `FAQ-15910`「路线乱」被机器建议到
`RESEAT-WATER-TANK`（因为正文里出现了「水箱」二字），人工改判为 `CLEAN-WHEEL`，
原文处置确实是「擦拭驱动轮脏污」。

**拒掉 36 条不是保守，是避免概念失效**：把「如何下载 APP」也标成案例卡，
相似案例检索会把教程当成故障历史，比少 36 条更糟。
`data/review/faq_cases.yaml` 的 `--check` 会校验：引用的症状/方案必须在本体里存在、
accepted 条目必须填 kind、usage 类不得挂症状或方案、58 条一条不漏。

进图后 `verbatim` 边从 **0 → 74 条**（22 条案例 × ABOUT/DOCUMENTED_IN + 8 条 OBSERVED + 22 条 RESOLVED_BY）。

---

## 5. 证据分级

种子顶部按知识层声明来源与证据等级，`SeedBuilder.rel(evidence_mode=...)` 按层盖章；
标 `assumed` 的自动配 `verification: unverified`。

| 等级 | 定义 | 本项目用在哪 | 边数 |
| --- | --- | --- | ---: |
| `verbatim` | 原文逐字，可回查页码/段落 | 22 条审核通过的官方 FAQ 案例分析卡及其处置/出处边 | 74 |
| `paraphrase` | 对原文的改写摘要 | 症状、故障名、方案步骤、故障归类 | 304 |
| `assumed` | 语料无原文的工程先验 | 错误码含义、根因可能性、检查耗时/风险 | 152 |
| `synthetic` | 固定随机种子的模板生成 | 客户、设备、工单、模板 FAQ 案例 | 498 |

为什么允许 `assumed` 存在：错误码「E11 = 左驱动轮异常」这句话在手册里没有逐字段落，
它是从「E11 故障排查表」的上文与部件表推出来的工程判断。硬要标 `paraphrase`
就是把推断伪装成原文。标 `assumed` 的代价是报告里必须承认这部分没原文支撑，
好处是审阅者能一眼看出哪些结论需要人工兜底。

错误码这一层还挂了伪来源 `SYN:ASSUMED-CODES`，并且**不建 `DOCUMENTED_IN` 边**——
手册里没有「错误码 → 文档段落」这层结构，硬连一条边只会让证据覆盖率看起来漂亮而实际为空。

---

## 6. 当前图谱账本

| 项目 | 数值 |
| --- | ---: |
| 实体 | 432 |
| 关系 | 1028 |
| 种子（人工）节点 | 295 |
| 真实案例节点 | 22（审核通过的官方 FAQ，`sourceType=official_faq`） |
| 合成节点 | 115（Customer 20 / Product 15 / Ticket 48 / FAQCase 24 / PublicCase 8） |
| 边：`verbatim` | 74 |
| 边：种子 `paraphrase` | 304 |
| 边：种子 `assumed` | 152 |
| 边：合成 `synthetic` | 498 |
| 悬空边 | 0 |
| 孤立节点 | 0 |

`FAQCase` 合计 46 条（22 真实 + 24 模板代理），落在开发文档要求的 45~60 区间内；
两种来源用 `sourceType` 区分（`official_faq` / `faq_template_proxy`），
前者 `needsReview=false`，后者 `needsReview=true`，不会被混成同一质量档。

合成数据全部带 `dataProvenance = seed_generated` + `needsReview = true`，
`kg/validate.py` 单独统计合成占比并在构建日志里报出来——
「用合成数据刷指标」这件事，至少要让人看得见。

生成合成数据用固定随机种子（`RNG_SEED = 20251028`），时间戳基于构建日期，
保证「近 90 天」这类查询在任何时候跑都有意义。

---

## 7. 复现

```bash
python -m backend.app.ingest.build --dry-run     # 预检：型号识别、证据覆盖、锚点数
python -m backend.app.ingest.build               # documents/ → data/raw
python -m backend.app.extract.pipeline           # data/raw → data/extracted
python -m backend.app.kg.build_seed              # 种子 + 合成 → data/curated
python -m backend.app.kg.validate                # 9 类校验
python -m backend.app.kg.loader --reset          # data/curated → Neo4j
```

换机型只需按命名规范放文件（手册名带 `DEEBOT <型号>`，FAQ 放 `Questions/地宝 DEEBOT <型号>/`），
抽取层不绑定任何具体型号；`--product` 可只抽单一型号，不传则自动抽取全部。

---

## 8. 下一步

1. 手册故障表与 FAQ 的 6 条「两边都没覆盖」缺口，补人工核对后单列为 `coverage_gaps.md`；
2. 24 条模板代理 `FAQCase` 仍在支撑工单生成（Ticket 需要按症状分组的案例池），
   若后续再抓到同类机型的真实 FAQ，可逐步替换并按同一台账扩充；
3. 抽取器目前是规则式，接 LLM 时只需替换 `extract/extractors.py` 的实现，
   候选结构与审核流程不变——审核台账不关心候选是谁抽的。
