# P1：候选重排 —— 规则定候选，LLM 只排序

> 承接 `reports/p0-attribution.md`。P0 的结论是：LLM 的增益来自「把口语归一化成图谱槽位」，
> 而不是「在候选之间做选择」。P1 就是把「选择」这件事真正交给 LLM，并且让它的收益可以被单独测量。
>
> 复现口径：`KG_BACKEND=memory`，题集 `data/eval/questions_hard.jsonl`（20 题，其中 15 题有方案金标准），
> 每配置重复 3 次，取均值±总体标准差。

## 1. 为什么是「重排」而不是「让 LLM 重新推理」

难题集上规则系统的 `topSolution = 0/20`：方案列表里通常**有**正确方案，但排第一的总是错的
（`P0` 报告已记录 `expressedGoldSolutionRate = 0.5333`、`topSolution = 0.0`）。
这正好是两类系统分歧最大的地方，也是唯一不该由规则独占的判断：

| 判断 | 谁来做 | 理由 |
| --- | --- | --- |
| 有哪些候选故障/方案 | 规则 + 图谱 | 可审计、可复现，安全红线不交给模型 |
| 候选里谁排第一 | LLM | 需要读懂题面里「已经试过什么」，规则没有这个输入 |
| 要不要停用/转售后 | 规则（锁死） | 安全边界不参与「更懂用户」的权衡 |

如果让 LLM 从零推理，等于放弃规则的证据链和安全边界；如果只让 LLM 写文案（P0 之前），
它的判断永远进不了 `diagnosis`，也就永远量不出来。重排是这两者之间唯一可验证的中间态。

## 2. 机制

新增一个 `rank` 节点，位置在 **reflect 之后、verify 之前**：反思阶段收集的相似案例正是
「用户已经做过什么」的证据，重排必须看到它；放在 verify 之前，则重排后的顺序会进入
`diagnosis` 与最终答复，而不是只改一句措辞。

- `backend/app/llm/prompts.py`：`RankPick{nodeId, evidenceIds, reason}`、`RankOutput{faults, solutions, note}`、
  `rank_prompt(...)`。提示词里写死四条约束：只能重排、不得增删改写、`evidenceIds` 只能引用候选自带的编号、
  告警含「停用/转官方售后」时安全方案必须第一、没把握就保持原序。
- `backend/app/agent/llm_agent.py`：
  - `_candidates(d)` 只把前 4 个故障/方案送进 prompt（控制 token，也避免让模型在长尾里挑）；
  - `_rank(...)` 在两类候选都 < 2 时直接返回 `None`，不浪费一次调用；
  - `_apply_ranking(d, ranking)` 是**验证器**，不是解析器：候选集外的 `nodeId` 记 `rejected` 并丢弃，
    重复项跳过，`evidenceIds` 不属于该节点的记 `evidenceMiss`，模型漏掉的候选按规则原序补回末尾，
    `safety_stop` 生效时把规则首选锁回第一位（`locked=true`）；
  - trace 记录 `rank` 步的 `driver / locked / rejected / evidenceMiss / note / llmDriven`，逐题落盘为
    `rankDriver / rankLocked / rankRejected / rankEvidenceMiss`（`run_eval.py`）；`note` 是模型给的一句话
    排序理由，只供人工复核，不参与任何判定。
- 消融开关：`KG_NO_RANK=1`（CLI `--no-rank`）只关这一个节点，其余链路不变，
  因此「重排贡献多少」可以用同一份代码、同一批题做 A/B，而不是拿两个版本对比。

## 3. 单测（`backend/tests/test_llm.py`，6 条）

| 用例 | 断言 |
| --- | --- |
| `test_apply_ranking_reorders_only_within_candidates` | 只改顺序，集合不变 |
| `test_apply_ranking_rejects_unknown_ids_and_keeps_all_candidates` | 候选外 id 进 `rejected`，未被提到的候选仍在末尾 |
| `test_apply_ranking_locks_safety_solution` | `safety_stop` 时首选被锁回，`locked=true` |
| `test_apply_ranking_no_ranking_keeps_rule_order` | LLM 失败 → 完全保留规则顺序 |
| `test_run_llm_applies_llm_ranking` | 反转 prompt 候选后，`diagnosis.faults/solutions` 真的被改 |
| `test_run_llm_skips_ranking_when_ablation_flag_set` | `KG_NO_RANK=1` 时 driver=rule 且顺序与规则一致 |

全量测试 163 项全绿（2 skip），前端 `npm run build` 通过。

## 4. 结果（20 题难题集 × 3 次重复）

| 指标 | 关重排（A/B） | 开重排 | Δ |
| --- | --- | --- | --- |
| 答案准确率 | 0.900 ± 0.041 | 0.900 ± 0.041 | 0 |
| 首选故障 | 0.9444 ± 0 | 0.9444 ± 0 | 0 |
| **首选方案** | **0.4000 ± 0** | **0.5333 ± 0** | **+0.1333** |
| 文字首选方案 | 0.4222 | 0.4667 | +0.0445 |
| faultF1 | 0.9074 ± 0 | 0.9074 ± 0 | 0 |
| pathF1 | 0.2894 ± 0 | 0.2894 ± 0 | 0 |
| 忠实度 | 0.8717 | 0.8708 | −0.001 |
| 上下文覆盖 | 0.7133 | 0.7000 | −0.013 |
| P50 延迟 | 11391 ms | 11948 ms | +5% |

逐题配对（同一题号比较三组重复的稳定结果）：**H07、H08 由错转对，无任何回退**。

| 题 | 规则顺序（首位错） | 重排后首位 | 金标准 |
| --- | --- | --- | --- |
| H07 | 清理轮组缠绕物 | 转官方售后检测 | 转官方售后检测 |
| H08 | 更换滤网 | 清理风机与风道 | 清理风机与风道 |

两题的共同点：题面里都写了「已经做过什么」。H07 说已清理毛发、已换轮组仍卡滞，H08 说已换过滤网，
规则顺序仍在推荐已经试过的动作；LLM 读到这些证据后把「没试过的」提到前面。

重排确实发生了 13/20 题（`rankDriver=llm`），5 题模型返回原序（`driver=rule`），2 题（H17/H18）
是超范围题，链路在 rank 之前就已降级。`rejected` 全程为 0：提示词的枚举约束没有被违反，
验证器没有触发过丢弃。

## 5. 必须承认的两个教训

1. **单次运行的 ±1 题不是效应。** P1 第一次运行的首选方案是 0.60（9/15），看起来比 P0 的 0.40 涨了 0.2；
   但重复三次都是 0.5333（8/15），H06 时而翻转、时而不翻。n=15 时一题就是 0.067，
   任何 |Δ| ≤ 1 题的结论都不能写进报告。上表的 0.5333 是三组重复的稳定值，0.1333 恰好是 2 题。
2. **文字没有跟上结构。** `expressedTopSolution`（0.4667）仍低于 `topSolution`（0.5333），
   说明 `finalize` 重新组织语言时还在按旧顺序讲——重排的结果还没有被答复层消费完。
   这是 P2 的第一件事：finalize 的输入要显式给出排序后的候选，或让 claims 直接由结构生成。

## 6. 已知边界

- **安全锁在真实数据上没被触发过**：`locked` 两次评测全程为 false，因为安全题（如 Q044 电池鼓包）
  的候选方案只有「停用并送修」一条，`<2` 时根本不会调用重排。锁的正确性目前只有单测保证。
- **重排不能救「缺事实」的题**：`precondition` 组（用户已做过检查）仍 0/8，
  因为候选生成本身就没用上「已排除」这条信息。这正是 P2 的 `check_results` 回传要做的事。
- **成本**：每题多 1 次 LLM 调用（P50 +5%，调用次数 +25%）。相比它换来的 2/15，
  这个价在离线评测里可接受；在线场景要按 P4 的分层路由来决定是否值得调。

## 7. 复现

```powershell
$env:KG_BACKEND='memory'
# 开重排
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data\eval\questions_hard.jsonl --repeat 3
# 关重排（A/B）
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data\eval\questions_hard.jsonl --repeat 3 --no-rank
```

结果分别落在 `var/eval/agent_llm_*.json` 与 `var/eval/agent_llm_norank_*.json`，
多样本汇总在 `var/eval/aggregate_*.json`（含 `noRank` 标记）；逐题行带 `rankDriver/rankLocked/rankRejected`。
