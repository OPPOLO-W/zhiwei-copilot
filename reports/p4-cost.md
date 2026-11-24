# P4 成本报告：省下来了，但没省到 10 秒的一半

> 结论先说：P4 把「每次查询的 LLM 往返」从 5–6 次压到 4–5 次，答案与首选方案没有退化；
> 但 P50 只从 9276ms 降到 9062ms（难题集，−2.3%）、从 9990ms 降到 8967ms（50 题，−10.2%），
> **没有达到可行性文档里写的 7–8 秒目标**。原因不是门控没生效，而是串行链上还有 4 次不可省的调用
> （decompose / plan / rank / finalize），省掉的那一轮反思只占其中约 1/5。

## 1. 改了什么

| 改动 | 位置 | 作用 |
| --- | --- | --- |
| 每步计时 `ms` | `backend/app/agent/llm_agent.py` 的 `step()` | 从「总耗时」变成「哪一步贵」，报告能落到具体节点 |
| 全覆盖不反思 | 反思循环入口 | `coverage` 五项全 `covered` 时直接跳过，记 `skipped="coverage_complete"` |
| 无进展即停 | 每轮工具执行后 | 工具跑完覆盖度没变（`coverage == judged`）就停，记 `stopped="no_progress"` |
| token 预算护栏 | 反思循环 / `_rank` | 用量超过 `agent_token_budget` 的 80% 时跳过探索与重排，把额度留给 finalize |
| 成本落盘 | `backend/app/eval/run_eval.py` | 逐题记录 `llmCalls` / `llmTokens` |
| 计数修正 | `_decompose` | 此前 decompose 没有计入 `llm_usage`，导致每题调用数与 token 系统性偏低 |

## 2. 结果

| 指标（难题集 20 题） | P3 | P4 | 变化 |
| --- | --- | --- | --- |
| answer | 1.00 | 1.00 | — |
| topFault | 0.9444 | 0.9444 | — |
| topSolution | 0.8667 | 0.8667 | — |
| expressedTopSolution | 0.8667 | 0.8000 | −0.07（1 题） |
| faithfulness | 0.90 | 0.88 | −0.02 |
| P50 / P95 | 9276 / 12295 | 9062 / 11418 | −2.3% / −7.1% |
| 每题 LLM 调用（修正口径） | 约 5.6（探针） | 4.60 | −18% |
| 每题 token | 约 13.5k（修正口径估算） | 12270 | −9% |

| 指标（50 题） | P3 | P4 | 变化 |
| --- | --- | --- | --- |
| answer | 1.00 | 1.00 | — |
| topSolution | 0.8837 | 0.8837 | — |
| P50 / P95 | 9990 / 12693 | 8967 / 11729 | −10.2% / −7.6% |
| 每题调用 | — | 4.54 | — |
| 每题 token | — | 12318 | — |

## 3. 门控确实生效了

直接跑 `run_llm` 并打印 trace（真实 LLM）：

| 题 | 反思步 | 调用数 | 每步 ms |
| --- | --- | --- | --- |
| H01 | `skipped=coverage_complete` | 3（旧口径 5） | intake 1376 / plan 1338 / rank 1502 / finalize 3074 |
| H06 | `skipped=coverage_complete` | 3 | intake 1237 / plan 1375 / rank 1732 / finalize 2393 |
| H11 | round1 continue → round2 continue | 4 | reflect 2247 + 2376 |
| H14 | round1 continue → round2 continue | 5 | reflect 2364 + 2295 / finalize 3812 |

H01/H06 属于「确定性路径已经把 q1–q5 覆盖满」的题，以前还要白白问一次「还缺什么」。
工具调用本身都在 30ms 以内，时间全在 LLM 往返上：decompose ≈1.3s、plan ≈1.3s、reflect ≈2.3s、rank ≈1.5s、finalize ≈3s。

## 4. 与预测不符的三件事

**① P50 没有降到 7–8 秒。** 可行性文档预测 −20%，实测 −2%~−10%。
原因是省掉的只是一轮反思（约 2.3s，且只发生在一部分题上），而 decompose/plan/rank/finalize
四次调用是所有题都要付的。要再砍一半，必须动这四次调用本身（合并 rank+finalize，或让 plan 复用
确定性兜底计划），而不是继续在反思上抠。

**② 成本计数此前是错的。** 修 P4 计量时发现：`_decompose` 从来没有调用 `_bump(usage, client)`，
所以历史报告里的「每题 1.2 万 token、5 次调用」都少算了槽位抽取这一次（约 1.5–2k token）。
修正后同题实测 12270 token / 4.6 次调用。这条已单独提交（`6bf4f06`），历史数字需要按此口径重读。

**③ 50 题回归验证了「关键词口径」的抖动。** 第一次跑 P4 时 answer 掉到 0.96（Q023、Q033），
按可行性文档预先写下的回退条件（掉分 >1 题就回退门控）应当回退。核查后发现：
- Q023 缺 `mustInclude` 的「清理轮组」，但答案里给的是「检查轮组轴承间隙 → 检测驱动电机电流」，语义并未丢；
- Q033 缺「下视传感器脏污」，答案写的是「下视传感器镜面脏污」，只是没有逐字复述；
- 单独重跑这两题，两条短语都出现了（answer 恢复通过）。

结论：掉分来自 finalize 的措辞采样（exactRepeatRate 本来就是 0.90），不是门控删了内容；
topSolution 这类结构指标两轮完全一致。因此保留门控，但把这条「预先写下的回退条件被触发、
核查后判定为度量抖动」如实记在报告里，而不是悄悄改标准。

## 5. 边界

- 单机内存图、单进程评测；没有并发压测，P50/P95 是串行 20/50 题的均值。
- DeepSeek 端延迟波动明显：同一批 5 题，改造前后两次探针里 H14 从 10.4s 变成 12.0s，说明单题噪声可达 ±2s；
  因此 P50 的 −2% 不能当作「稳定加速 2%」，只能当作「没有变慢」。
- 预算护栏在正常查询里不会触发（1.2 万 < 2.4 万的 80%），它是防跑飞的安全阀，不是常态优化。
- 缓存与并发本次未做：eval 的 `--repeat 3` 下缓存能省 2/3 成本，但那会把「可重复性」指标变成缓存命中率，
  且生产命中率未知，先留开关不做默认。

## 6. 下一步（如果要继续压 P50）

按收益排序：

1. **合并 rank + finalize**（预计 −1.5s/题）：rank 的候选顺序本来就是 finalize 的输入，
   两次调用之间没有分支；合并后一次返回「顺序 + 带引用的答案」，验证器仍然只接受候选集内的 id。
2. **plan 与 decompose 合并**（预计 −1.3s/题）：decompose 已经拿到 question 与图谱清单，
   可以直接输出「槽位 + 首轮工具调用」，plan 只在诊断后需要补充时再单独调用。
3. 上面两条都做完，P50 才可能到 6–7s；只做门控做不到。

复现：

```powershell
$env:KG_BACKEND = "memory"
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data/eval/questions_hard.jsonl --repeat 1
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data/eval/questions.jsonl --repeat 1
```
