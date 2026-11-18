# P3：让「图谱探索」真的能执行

> 结论先说：P3 之前，Agent 名义上有 6 个探索工具，实际只有 2 个能被正确调用；
> 另外 3 个（`expand_subgraph` / `find_paths` / `get_document_span`）因为模型拿不到
> 任何节点 id 和文档 id，参数无从生成。P3 把「证据摘要」变成可指认的清单、
> 让文档工具返回正文、并在运行时校验引用，数据落在下面三张表里。

## 1. 问题：探索工具为什么是摆设

改之前 `_evidence_summary()` 只输出名称：`候选故障：左驱动轮卡滞(0.82)`、`候选方案：清理轮组缠绕物`。
模型要调 `expand_subgraph(node_ids=[...])`，却没有任何 `FAULT:` / `SOL:` 形式的 id 可填；
要调 `get_document_span(doc_id)`，则连 docId 都没见过。更糟的是，即便它瞎填一个 id，
`get_document_span` 也只回 `docId/title/url/authority` 四项元数据——没有正文，
「引用手册原文」这件事在架构上就不成立。

三条硬事实（`docs/p3-feasibility.md` 里逐条列过）：

| 事实 | 后果 |
| --- | --- |
| 摘要里没有 nodeId / docId | 3 个工具的参数无法生成，工具注册表虚胖 |
| `get_document_span` 只回元数据 | 即使调对也无正文，无法支撑引用 |
| `DOCUMENTED_IN` 只覆盖 29/36 方案、22/46 案例，Fault/Cause/Check 为 0 | 只能从方案/案例出发查文档，不能「故障→手册」直连 |

## 2. 改动

**P3.0 证据可指认**（`backend/app/agent/llm_agent.py`）

- 摘要中每个候选都带 id：`左驱动轮卡滞(0.82, FAULT:LEFT-WHEEL-STUCK)`；
  方案额外带文档：`更换轮组总成 [SOL:REPLACE-WHEEL;文档 DOC:T20PRO-MANUAL]`。
- 新增两行「证据编号：E1、E2…」「证据原文：E1（DOC:T20PRO-MANUAL）左轮不转提示…」，
  模型能引用的编号与它能读到的原文同时出现，减少「编一个 E 编号」的动机。
- 工具结果压成「工具取证」块（子图邻接 / 路径 / 相似案例 / 兼容性）回灌给规划与反思，
  而不是只躺在 trace 里。

**P3.1 文档工具回正文**（`backend/app/agent/tools.py`）

- `get_document_span(doc_id, limit=3)` 返回 `passages[{chunkId,page,section,text}]`，单段截 220 字。
- 新增 `search_documents(query, k≤3)`：BM25（中文 bigram，与 RAG 基线共用 `kg/text_index.py`）
  直接检索 925 个 chunk，返回带 `chunkId` 的片段——这是「模型没有 docId 也能找到原文」的入口。
- `_summarize` 补分支，trace 里显示「《地宝 DEEBOT T20 PRO》返回 2 段正文」而不是一串字段名。

**P3.2 运行时引用校验**（`llm_agent.py::_verify_claims`）

- claim 的 `evidenceIds` 与真实证据集合求交：部分命中只保留命中的 id；
  **全部悬空则整条 claim 丢弃**并计入 `unsupportedClaims`；无引用的 claim 保留（宁缺勿造）。
- 这一步放在 finalize 内部，所以规则路径不受影响，只约束 LLM 自带的 claim。

**P3.3 度量修正**（`backend/app/eval/run_eval.py`）

- 引用白名单从「图边证据 E*」扩成「E* ∪ 工具取到的 chunkId」。
  不修的话，P3 起答案里合法的 `DOC:T20PRO-MANUAL#p28r15` 会被判成悬空引用——
  第一次跑就出现 `danglingTotal=17` 的假阳性，修完为 0。

## 3. 结果（难题集 20 题，memory 后端，单次采样）

| 指标 | P1 后（3 次均值） | P3 后 | 说明 |
| --- | --- | --- | --- |
| answerAccuracy | 0.90 | **1.00** | 20/20 关键词覆盖 |
| topFaultAccuracy | 0.9444 | 0.9444 | 不变（H17/H18 超范围不计） |
| topSolutionAccuracy | 0.5333 | **0.8667** | 13/15；H05、H12 仍错 |
| expressedTopSolution | 0.4667 | **0.8667** | 文字首选与结构首选首次完全对齐 |
| faithfulness | 0.8708 | 0.90 | |
| danglingTotal | 0 | 0 | 修白名单前曾误报 17 |
| unsupportedClaims | — | 0 | 运行时没拦下任何 claim |
| 文档工具调用 | 0 | **20 次 / 20 题** | 每题至少一次 `search_documents` 或 `get_document_span` |
| toolCalls | 3.15 | 3.45 | |
| latencyP50 | 11947ms | **9276ms** | 工具本身是本地 BM25，无额外 LLM 往返 |

需要说清的一点：0.5333 → 0.8667 不是 P3 单独贡献的。P2 的动作事实让 8 道 precondition
从 0/8 变成 7/8（J1a 已单独测过），P3 的作用是让剩下的题在**引用**和**工具取证**上站得住。
把两件事混在一起写会高估 P3，这里分开记。

**仍错的两题**

- H05（E27 雷达视窗已擦）：首选仍是「转官方售后检测」而不是「更换激光雷达总成」。
  规则层 `ESCALATE-SERVICE` cf=0.70 > `REPLACE-LDS` cf=0.60，是种子置信度的选择，不是排序问题。
- H12：方案排序错误，属候选重排未覆盖到的情形。

## 4. 50 题回归（不退化是硬约束）

| 指标 | P2 后（190116） | P3 后（192535） |
| --- | --- | --- |
| answerAccuracy | 0.98 | **1.00** |
| topFaultAccuracy | 1.00 | 1.00 |
| topSolutionAccuracy | 0.8605 | 0.8837 |
| expressedTopSolution | 0.7442 | 0.7209 |
| faithfulness | 0.8428 | 0.8348 |
| danglingTotal | 0 | 0 |
| 文档工具调用 | 0 | 49 次 / 50 题 |
| latencyP50 | 9795ms | 9990ms（+2.0%） |

50 题里唯一从错到对的是 Q023（P2 那次唯一错题）；topSolution 仍错 5 题
（Q022/Q023/Q024/Q036/Q037，与 P2 同一批），说明 P3 没有改变排序能力，只补齐了引用与取证。
P50 增加 2%，在「≤+20%」的验收线内。

## 5. 与预测不符的地方

1. **`danglingTotal` 先涨到 17**：原以为「白名单不变、只加文档引用」会很安全，
   实际是度量口径没跟上架构——合法的 chunk 引用被当成编造。教训：新增一种证据类型，
   必须同步更新「什么算合法引用」的定义，否则幻觉指标会反向噪声化。
2. **P50 反而降了 2.7s**：原预期「多调工具会变慢」。实际是 `search_documents` 把原来
   反复 `expand_subgraph` 的试错省掉了，且 BM25 在本地，不进 LLM 往返。
3. **expressed 与 decision 完全对齐**：P1 时文字首选落后结构首选 6.7 个百分点，
   原以为要改 finalize prompt 才能收敛；实际是摘要里带上 id 与原文后，
   模型写作时「看到的就是答案里的顺序」，这个偏差自己消失了。

## 6. 边界

- 文档边只覆盖 29/36 方案、22/46 案例；`Fault`/`Cause`/`Check` 没有直连文档的边，
  所以「这个故障在手册第几页」仍然答不了，只能给「相关方案的手册段落」。
- `search_documents` 是词面检索，同义说法（「吸力变小」vs「吸力下降」）召回不稳。
- 运行时校验只保证「引用的编号真实存在」，不保证「这句话确实被该证据支持」——
  语义蕴含没有做，这是 P4 之后的题目。
- 20 题单次采样，±1 题就是 6.7 个百分点；上表的差值只在 topSolution 这类 7 题级别的变化上可信。

## 7. 复现

```powershell
$env:KG_BACKEND='memory'
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval `
  --system agent_llm --questions data/eval/questions_hard.jsonl --repeat 1
```

单测：`pytest backend/tests/test_tools.py backend/tests/test_llm.py -q`
