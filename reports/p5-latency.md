# P5：把「每题 9 秒」压下来，同时不拿质量换

> 结论先行：**P5.1（把候选重排并进 finalize）经实测真的会掉 topSolution，已回退；P5.2（探索计划改由规则生成）留下了**。
> 最终：难题集 P50 9062 → **6384 ms**（−29.6%），50 题回归 8967 → **5102 ms**（−43%），topSolution 两套题集一位不差，答案与四项硬幻觉不变。
> 每步一个提交，回退点 `pre-p0`；被否掉的方案留在 git 历史里（`a814da3`）而不是被悄悄抹掉。

## 1. 动机（P4 结束时的账）

| 口径 | calls/题 | tokens/题 | P50 |
| --- | --- | --- | --- |
| 难题集 20 | 4.60 | 12270 | 9062 ms |
| 50 题 | 4.54 | 12318 | 8967 ms |

四次串行调用（decompose → plan → reflect → rank → finalize 中的四次），每次 1.5–3.8 s。
`step.ms` 实测（P4 加的分步计时）：intake/plan 各约 1.3 s，reflect 每轮 2.2–2.4 s，rank 1.5 s，finalize 2.4–3.8 s。
**延迟主项是输出 token**，不是网络——这决定了「省一次调用」不一定省时间。

## 2. P5.1 合并排序：省了往返，赔了质量（已回退）

做法：把「候选重排」并进 finalize，一次调用里先给 `faultOrder/solutionOrder` 再写答案，删掉独立的 `rank` 节点。

- 首版让模型回 `[{nodeId, evidenceIds, reason}]`：20 题 P50 **9234 ms**，比 P4 的 9062 还慢——多出的 8 个对象的输出 token 正好抵消省下的往返。
- 改瘦成只回 nodeId 字符串数组：P50 **8551 ms**（−5.6%），20 题指标不变。
- 但 50 题回归：**topSolution 0.8837 → 0.7907**，Q014/Q019/Q026/Q038 四题把首选方案从「清洗滤网 / 清理风机与风道」换成「更换滤网」。

归因实验（先做隔离，再改提示词）：

1. `KG_LLM_PLAN=1`（只留 P5.1、计划仍走 LLM）重跑 50 题 → 同样 0.7907、同样四题翻转 ⇒ **与 P5.2 无关，是合并后的提示词**。
2. 原因：独立 rank 的提示词写的是「solutions 按**最先该执行的**方案排」；合并后写的是「solutionOrder 第一位必须就是 answer 里**最先推荐的**方案」，模型于是按「最彻底/最有效」排，清洗被更换挤掉。
3. 把排序依据补回去（先低风险低成本自查、先清洗再更换、不要因更换更彻底就前置）→ 50 题 **0.814**，两次独立运行完全一致（0.814 / 0.814）。
4. 判定：**真实退化 3 题（7 个百分点），不是采样抖动**（同配置两跑同值）。按 P5 的验收口径「topSolution 不得退化」，回退 P5.1，保留 P5.2。

`837d806` 同时做了回退与保留：`prompts.rank_prompt`、`_rank()`、独立 `rank` 节点、`RankOutput` 全部恢复；`llm_agent.run_llm` 的节点顺序回到 reflect → rank → ask_user → verify → finalize。

## 3. P5.2 探索计划改由规则生成（保留）

原来这一轮由 LLM 选工具，50 题里它的输出几乎固定是「查相似案例 + 查文档」，与规则能算出来的完全重合，却多付一次往返（约 1.3 s、1.5k token）。改为 `_auto_plan()`：

1. `search_similar_cases(model, symptom, days=365)`——与原首轮一致；
2. 取 `diagnosis.solutions[:2]` 中第一个有 `DOCUMENTED_IN` 的方案，`get_document_span(doc_id)`；否则若有症状，`search_documents(symptom, k=3)`；
3. 若候选根因 ≥2 且已有故障，`expand_subgraph(faults[0], depth=2)`。

`KG_LLM_PLAN=1` 可切回 LLM 计划，用于 A/B 与答辩演示「为什么不让模型选工具」。

## 4. 最终结果

| 指标 | 难题集 20（P4） | 难题集 20（P5） | 50 题（P4） | 50 题（P5） |
| --- | --- | --- | --- | --- |
| answerCorrect | 1.000 | 1.000 | 1.000 | 0.960 ※ |
| topFault | 0.944 | 0.944 | 1.000 | 1.000 |
| **topSolution** | **0.867** | **0.867** | **0.8837** | **0.8837** |
| expressedTopSolution | 0.800 | 0.800 | 0.7209 | 0.6744 ※ |
| faithfulness | 0.88 | 0.89 | 0.848 | 0.848 |
| fabricated / dangling / overclaim / safety | 0/0/0/0 | 0/0/0/0 | 0/0/0/0 | 0/0/0/0 |
| calls/题 | 4.60 | **3.05** | 4.54 | **2.70** |
| tokens/题 | 12270 | **9690** | 12318 | **9253** |
| **P50** | 9062 ms | **6384 ms** | 8967 ms | **5102 ms** |
| P95 | 11418 ms | 8352 ms | — | — |

※ 两处是「关键词/首次出现位置」口径的措辞抖动，不是内容丢失：Q023 答案里写的是「检查轮组轴承间隙」而非金标准短语「清理轮组」，Q034 缺「升级过程网络中断」这七个字；`expressed` 掉的三题（Q015/Q028/Q046）同样是句子里先提到了别的方案名。硬指标 topSolution 两套题集一位不差。这条已经第三次出现，报告里把它当口径局限写，而不是当成模型退化。

## 5. 顺带修掉的两个 bug

- **安全检测假阳性**（`f1ca565`）：H19 答案里写「是否可继续使用须以售后检测为准」，`safety_violation` 把这句转述判成违规（0 → 1）。加 `_hedged()`：短语前 6 字含「是否/能否/要不要/视/待」或后 12 字含「为准/由售后/待确认/检测」时不算违规；仍然对「鼓包了还能用」这种直接建议报违规。
- **评测静默空跑**（`cdf3986`）：`--questions` 路径不存在时 `rows: []`、指标全零还「成功」。改成 `SystemExit` 并打印当前目录。这一条是 P5 第一次跑难题集时踩到的，差点把 n=0 当成灾难性退化。

## 6. 边界与没做的事

- 单机、串行调用，P50 受 DeepSeek 侧抖动 ±2 s 影响；报告里的 P50 都是单次或两次一致值的记录，不是多次均值。
- 排序仍单独占一次调用（P5.1 证明合并会掉点），所以「4 次调用」只压到 3 次，没有压到 2 次。
- `answer` 与 `expressed` 是关键词口径，会随措辞抖动；主判据始终是 topSolution。
- 缓存、并发、流式都未做：本项目是单用户演示，做这些对答辩没有解释价值。

## 7. 复现

```powershell
$env:KG_BACKEND='memory'
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data/eval/questions_hard.jsonl --repeat 1
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data/eval/questions.jsonl --repeat 1
# 对照：不让模型选工具（P5.1 归因实验）
$env:KG_LLM_PLAN='1'
```

对应结果文件：`var/eval/agent_llm_20261118_214744.json`（难题集）、`var/eval/agent_llm_20261118_214528.json`（50 题）、`..._213210.json`（P5.1 归因）。
