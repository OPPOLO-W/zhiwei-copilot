# P0：让「LLM 到底贡献了什么」可测量

> 本文记录 P0 三件事的动机、实现与结果。目的不是提高分数，而是把此前被架构
> 抹掉的贡献识别出来，并给后续 P1（规则定候选 + LLM 重排）一个可对照的基线。

## 1. 问题：原来的评测看不见 LLM

| 现象 | 根因 |
| --- | --- |
| 50 题上 `agent` 与 `agent_llm` 六项指标逐位相同 | `run_eval._run_agent` 的 fault/solution/triple/claim 全部取自 `run.diagnosis`（规则产物），LLM 只改写 `answer` 文本 |
| 四项硬幻觉里 fabricated / dangling 恒为 0 | 只吃结构化诊断字段；规则产物天然合法，检测器没有输入 |
| 「RAG 比 KG 弱」无法成立 | 旧 `rag` 基线不调用 LLM、只取一条图链，还把引用写死成 `E1`——是稻草人 |

## 2. 改动

### 2.1 claims / warnings 回填（39abbe1）

- `AgentRun` 增加 `claims`、`warnings`；`_finalize` 返回 `_Final(text, claims, warnings)`，
  LLM 自己写的 claim 标记 `source="llm"` 并保留 `evidenceIds`。
- 失败回退时 claims 退回规则模板，`extra.claimsSource` 标记 `rule`。
- API `/api/query` 的 agent / compare 分支改用 `_final_claims` / `_merge_warnings`，
  `detail.claimsSource` 可查来源；前端答案卡对 LLM claim 显示 `LLM` 徽标，悬停高亮对应证据。

### 2.2 双评分（1b10cd0）

同一份输出上并列两组指标：

- `topSolutionAccuracy`（decision）：诊断结构里排第一的方案是否等于金标准；
- `expressedTopSolutionAccuracy`（expressed）：**答案文字里最先提到的方案**是否等于金标准
  （`grounding.first_mention` 按字符位置取最早出现的候选）。

另加 `expressedGoldSolutionRate`：文字里出现过正确方案的比例——它与 `answer_correct`
的差，就是「提到即算对」的水分。

### 2.3 真 Vanilla RAG 基线（55da8a0）

`backend/app/eval/rag.py`：BM25（中文按字符 bigram，零依赖）+ 一次 LLM 调用，`k=6`。

- 语料 = `data/raw/chunks.jsonl`（手册/FAQ 原始分块）+ 图谱三元组拍平成「头 关系 尾：证据」文本。
  两边知识相同，只差结构——这样比较的是检索与推理形态，不是「有没有 LLM」。
- 输出契约：`answer / faults / solutions / claims[{text, passage}] / warnings`；
  claim 的 `passage` 换算成真实片段 id，越界编号记空引用，交给悬空指标判罚。
- 刻意不给多轮工具调用、规则、反思、槽位约束。

## 3. 结果（难题集 20 题，内存后端）

| 系统 | answer | topFault | topSolution | expressed topSol | faith | fab | dang | overclaim | safety | P50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| rule | 0.65 | 0.6111 | 0.0 | 0.0 | 0.5125 | 0 | 0 | 0 | 0 | 6 ms |
| agent | 0.65 | 0.6111 | 0.0 | 0.0 | 0.5125 | 0 | 0 | 0 | 0 | 14 ms |
| agent_llm | 0.90 | 0.9444 | 0.40 | 0.5333 | 0.89 | 0 | 0 | 0 | 0 | 11021 ms |
| rag（旧弱基线） | 0.25 | 0.6111 | 0.1333 | — | 0.55 | 0 | 11 | 0 | 0 | 0 ms |
| rag_llm（真 RAG） | 0.30 | 0.4444 | 0.0 | 0.0667 | 1.00 | 39 | 0 | 3 | 1 | 2352 ms |

## 4. 解读

1. **LLM 的贡献是真实的，但仍是「翻译 + 表达」，不是「推理」**：topFault 从
   0.6111 升到 0.9444，来源是把口语描述归一化成图谱里规范的症状名（槽位补召回），
   规则拿到更干净的输入后自然排得更准；首方案 0.0→0.40 也经由这条路径。
   在此之上，答案文字的首选命中率（0.5333）又高于结构里的首选（0.40），说明
   `_finalize` 重新组织语言时还做了最后一次取舍。两个增量都不是 LLM 在候选之间
   做重排或验证得到的——那正是 P1 要补的能力。
2. **同为「有 LLM」，RAG 明显更弱**：0.30 vs 0.90 的答案准确率、0.0 vs 0.40 的首选方案，
   且出现 39 次编造实体、3 次越界断言、1 次安全违规——单次检索生成拿不到
   「症状→故障→根因→方案」的多跳结构，也守不住安全边界。
3. **旧指标不是坏了，是没被喂数据**：fabricated / dangling 在 RAG 上立刻出现非零值，
   说明检测器有效；此前 agent_llm 恒为 0，是因为送检字段来自规则。

## 5. 已知偏差（必须写进论文，否则就是新的稻草人）

- **fabricated 口径不对称**：`agent_llm` 送检的 fault/solution 来自规则结构，
  `rag_llm` 送检的是 LLM 自己抽的字段。因此「RAG 编造更多」不能直接当作模型能力结论，
  只能说明「让 LLM 自由产出结构会引入编造」。P1 应改为：所有系统都用同一套文本解析
  口径（`expressed_entities` + 名称白名单）统一比对。
- **RAG 未调参**：`k=6`、单次调用、无查询改写；提高 k 或加 query expansion 会改善其分数。
  本表只主张「在同等知识、朴素配置下，KG 方案更强」，不主张 RAG 上限如此。
- **`answer_correct` 仍是关键词包含**：rule 的 0.65 里，`expressedGoldSolutionRate`
  0.5333 而首选方案 0，说明「提到过」不等于「推荐了」。论文应以 topSolution 为主指标。

## 6. 复现

```powershell
$env:KG_BACKEND='memory'
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system rule       --questions data\eval\questions_hard.jsonl --limit 20
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent      --questions data\eval\questions_hard.jsonl --limit 20
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm  --questions data\eval\questions_hard.jsonl --limit 20
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system rag_llm    --questions data\eval\questions_hard.jsonl --limit 20
```

结果落在 `var/eval/{system}_{stamp}.json`，逐题行含 `claimsSource`、`expressed*`、
`goldFaultName` 等字段，可复核每条判定的依据。
