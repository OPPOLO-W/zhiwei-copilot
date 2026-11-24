# P2：让用户说过的排查事实进入推理（多轮会话 + 追问）

> 分支 `p0/llm-attribution`，提交 `5f1f19d` → `ae1933f`/`3a9658f` → `da34979` → `c6ee5e0` → `f6d6bc7`。
> 回退点 `pre-p0`（P0 之前）、`v7-llm-symptom`（阶段 B 结束）。

## 1. 为什么 P2 才是分水岭

P0/P1 之后 LLM 已经能做三件事：把口语归一化成槽位、在候选内重排、写带引用的答案。
但难题集里 8 道 `precondition` 题（H01–H08）的首选方案一直是 **0/8**，而且原因不是"理解不了"：

| 题面 | 用户已经说过的事实 | 系统当时怎么处理 |
| --- | --- | --- |
| H01「轮组的毛发我已经清理干净了，还是卡」 | 清理过轮组 → 毛发缠绕可排除 | 照旧把"轮轴毛发缠绕"排第一，推荐"清理轮组缠绕物" |
| H06「清水箱我重新装过了」 | 重装过水箱 → 未卡到位可排除 | 仍推荐"重新安装清水箱" |
| H07「毛发清理过、轮组也换新了」 | 两种干预都无效 → 只剩售后 | 仍推荐"清理轮组缠绕物" |

这些不是说给规则引擎的"检查结果"，而是**已执行过的动作**。旧链路里没有任何入口能表达它：
`diagnose(...)` 早就收 `check_results`，但 API 只在 rule/compare 分支传、agent 分支不传，前端从不发，LLM 也不抽。

## 2. 四处改动

### P2.0 图谱动作效应表 + R25/R26/R27（`5f1f19d`）

- 领域知识进图谱而不是进 prompt：`data/seed/t20pro/02_knowledge.yaml` 给 7 个方案加
  `excludes: [被否定的根因]` / `exhausts: true`，`build_seed.py` 落到节点属性
  `excludesCauses` / `exhaustsSelf`（不改 schema、不动关系，`validate` 不受影响）。
- 新谓词 `action_attempted(S)`、`solution_effect(S, C, "excludes"|"exhausts")`：
  - **R25** 动作排除根因：`action_attempted(S) + solution_effect(S, C, "excludes")` → `excluded_cause`
  - **R26** 阴性检查排除根因：`check_result(K, F, "negative") + cause_confirmed_by(C, K)` → `excluded_cause`
  - **R27** 已试过的方案不再推荐：`action_attempted(S) + solution_effect(S, S, "exhausts")` → `reject_solution`
- 排除要**三处传导**，只过滤 `causes` 不够：
  1. 根因级：`causes` 跳过 `excluded_causes`；
  2. 故障级：某故障的全部根因都被排除 → 整条故障退出（H06 的"清水箱缺水"随之消失）；
  3. 方案级：方案的全部 support 根因都被排除才剔除（多根因共享的方案不误删）。

### P2.1 用户自述事实并进 decompose（`ae1933f` / `3a9658f`）

`SlotExtraction` 增加 `facts: [FactDraft{kind, ref, result, quote}]`，与槽位同一次 LLM 调用返回，省一次往返。
七道门（`_gate_facts`），任何一道不过就丢弃并记原因：

| 门 | 拒绝码 | 挡住的典型错误 |
| --- | --- | --- |
| 引用必须逐字出现在用户话里 | `quote_not_verbatim` | 模型把"清理干净了"改写成"已清理" |
| 实体必须存在于图谱 | `unknown_entity` | 编一个不存在的方案 code |
| 否定动作不能当事实 | `negated_action` | "我**没**清理过"被当成"清理过" |
| 结果枚举 + 极性 | `polarity_mismatch` / `result_unknown` | "还是卡"被标成 negative |
| 检查项必须唯一指向一个故障 | `check_covers_multiple_faults` | 轴承检查被用来否定"右轮"故障 |
| 数量上限 | `over_limit` | 一句话抽出 12 条事实 |

另外两条硬约束：**只有 `result == "positive"` 的检查结论才喂给规则引擎**——自由文本里推阴性，
会把 H04 的"还是充不上"读成"检查结果是坏的"，反而排除整条故障；而追问按钮点出来的答案可信，
单独走 `check_results` 通道。

### P2.2 会话（`da34979`）

`SessionStore`（TTL 30 min、容量 500、带锁）保存 `slots / facts / asked_checks / check_results / turn`。
`QueryRequest.sessionId` 首次留空、响应回填；后续轮次继承槽位（只补空位、不覆盖），
`prior_facts` 回灌给 LLM。**不缓存推理结论**——结论必须每轮按当前事实重推。

### P2.3 追问决策（`c6ee5e0`）

- 算的是**根因**的信息增益，不是故障的：`CAUSE -CONFIRMED_BY-> CHECK` 的入边反查检查项。
  用 `DETECTS` 算会得到 0（H01 三个检查都指向同一个故障），问了等于没问。
- `IG = H(prior) - [p_pos·H(after_pos) + (1-p_pos)·H(after_neg)]`，`score = IG / cost`，
  `cost = riskLevel × durationMin`；先验取 `diagnosis.causes` 的置信度（不是均匀分布）。
- 问不问：`top1 < 0.70` 或 `top1 - top2 < 0.25` 才问；安全拦截 / 查询落空 / 超范围 / 非自助项一律不问；
  本轮已抽到 action/check 事实也不问（用户刚说完做过什么，别再问同一件事）。
- 一个会话只问一次：答完就该给结论，否则会变成二十问。

### P2.4 前端与 API 贯穿（`f6d6bc7`）

`AskCard` 三个按钮（有异常 / 正常 / 不确定）→ `ask({sessionId, checkResults:[{check, fault, result}]})`；
后端把按钮答案并入 `session.check_results`，下一轮继续生效。同时修了两处接线：
`QueryRequest` 的校验允许"只有 sessionId + checkResults"的请求；确定性 `graph.run` 也收 `check_results`
（此前只有 LLM 路径认按钮答案）。

## 3. 结果

### 3.1 J1a：8 道 precondition，0 提问（`scripts/p2_j1a.py`）

| 系统 | 首选方案命中 |
| --- | --- |
| rule | **0/8** |
| agent_llm（P2.1 后） | **7/8** |

7/8 的 7 道全部靠"用户自述事实"翻盘：H01/H02→更换轮组总成、H03→更换边刷、H04→更换充电触点组件、
H06→水泵排气、H07→转官方售后检测、H08→清理风机与风道。

### 3.2 J1b：问一句是否真的收窄（`scripts/p2_j1b.py`，6 题真实 LLM）

| 题 | 首轮首选检查 | 是否追问 | 答"正常"后的首选检查 | 命中 goldCheck |
| --- | --- | --- | --- | --- |
| H14 | 手动转动左轮 | 是（IG 0.531） | **检查轮组轴承间隙** | ✅ |
| H15 | 检查滤网 | 是 | **检查风道与吸力** | ✅ |
| H16 | 检查充电触点 | 否 | — | 首轮即 gold |
| H01/H06/H08 | 已随事实收窄 | 否（`facts_already_known`） | — | — |

汇总 `{"n":6,"asked":2,"goldAfterTurn2":2,"maxRounds":2}`。
即：**被问到的两道都收窄到了 gold，没被问的题也没有退步**。

### 3.3 50 题回归（`agent_llm_20261118_190116.json`，n=50）

| 指标 | P1 时 | P2.4 后 |
| --- | --- | --- |
| answerAccuracy | 0.98 | 0.98 |
| topFaultAccuracy | 1.0 | 1.0 |
| topSolutionAccuracy | 0.8837 | 0.8605（43 题，±1 题） |
| 首选方案（口头）| 0.7442 | 0.7442 |
| 四项硬幻觉 | 0/0/0/0 | 0/0/0/0 |
| latency P50 / P95 | 11633 / — | 9795 / 11719 |
| rank 审计 | — | llm 29 / rule 15 / 无候选 6，锁 2 次，rejected 0 |

`topSolution` 的差是 1 题（37 vs 38），在 n=43 上等于噪声；结论按"不退化"记。

## 4. 与预测不符的地方（必须写进论文）

1. **H05（E27 雷达）仍是"转官方售后"**，不是 gold"更换激光雷达总成"。原因在种子置信度：
   `ESCALATE-SERVICE` cf 0.70 > `REPLACE-LDS` 0.60。这是规则偏好，不是 LLM 能力问题；P2 没有改。
2. **H16 问不出来**：E23 在图里只有"触点氧化"一个根因，"到底是触点还是电池"缺第二个根因/检查项。
   追问的覆盖面受图谱覆盖度限制，不是算法限制。
3. **首次 J1a 只有 4/8**，两类失败：LLM 重排把"转官方售后"提到首位（加 escalation 锁解决）、
   检查结论被标成阴性（只喂阳性解决）。第二类失败说明"让模型判断自己说的话是不是否定句"并不可靠，
   所以极性门控保留，但按钮通道绕开它。

## 5. 边界

- 自述事实按"硬排除"处理，演示够用但生产应给折扣（用户可能记错、可能没真做）。
- 一个会话只问一次；真正的多轮收敛（问 2–3 轮）没有做，因为图谱里能问的检查项本身不够多。
- 会话是进程内存储，重启即丢；与 `RUNS` 同级别，课程演示不引入 Redis。
- 多轮会放大 LLM 调用量（每轮 4–5 次），成本按轮次线性增长。

## 6. 复现

```powershell
$env:KG_BACKEND='memory'
.\.venv\Scripts\python.exe scripts\p2_j1a.py      # 8 道 precondition，0 提问
.\.venv\Scripts\python.exe scripts\p2_j1b.py      # 6 题两轮对话
.\.venv\Scripts\python.exe -m backend.app.eval.run_eval --system agent_llm --questions data/eval/questions.jsonl --repeat 1
```
