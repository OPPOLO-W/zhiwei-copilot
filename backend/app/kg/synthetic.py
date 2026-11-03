"""合成案例、客户、设备与工单。

诚实声明
--------
这些案例**不是**真实工单，也不是官方 FAQ 原文，而是基于种子知识模板化生成的代理数据：
  * FAQCase  sourceType = faq_template_proxy  （待替换为真实官方 FAQ）
  * PublicCase sourceType = public_post_rewritten（结构模拟，非真实抓取）
  * Ticket    sourceType = synthetic
所有节点带 provenance=seed_generated / needsReview=true，便于质量报告区分。

生成使用固定随机种子，选择结果可复现；时间戳基于构建日期，保证「近 90 天」类查询始终有意义。
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from typing import Any

from .build_seed import SeedBuilder

RNG_SEED = 20251028
# 固定基准日，保证同一份种子在任何一天重建都得到逐字节相同的产物。
# 曾用 date.today()：跨天重建会让 32 个案例、15 台设备、48 张工单的日期整体漂移，
# 与 build_seed.py 声称的"幂等、输出字节级稳定"相矛盾（2026-11-03 实测发现）。
BASE_DATE = date(2026, 10, 29)

CITIES = ["上海", "北京", "杭州", "成都", "深圳", "武汉", "南京", "西安"]
HOME_TYPES = ["apartment", "duplex", "house"]
CHANNELS = ["hotline", "app", "store"]
SATISFACTION = [3, 4, 4, 5, 5, 2, 4, 5]

PUBLIC_TEMPLATES = {
    "HAIR-WRAP-MAIN-BRUSH": "家里两只猫，用了三个月，主刷两端缠满了毛，机器一直报 E14，拆下来清理才恢复。",
    "HAIR-WRAP-WHEEL": "长头发掉在地上，轮子缠住之后就不动了，清理完毛发才正常。",
    "ODOR": "用了一个月，拖完地有一股臭味，后来发现是污水箱没倒干净。",
    "WATER-LEAK": "基站下面总是有一滩水，擦干第二天又出现，怀疑水箱密封圈老化。",
    "WIFI-PAIR-FAIL": "换了新路由器之后一直配网失败，APP 提示 10010，客服说只支持 2.4G。",
    "BATTERY-DRAIN-FAST": "买了一年多，现在拖一半就回充，续航明显不行了。",
    "NOISE-ABNORMAL": "最近跑起来声音特别大，像有什么东西卡住，但地面看着挺干净。",
    "FREQUENT-OFFLINE": "设备总是显示离线，重启路由就好一会，隔天又离线。",
}

FAQ_TITLE = "{model} 报 {code}（{meaning}）的处理方法"
FAQ_TEXT = "用户问：{model} 提示 {code}，{symptom}，该怎么处理？官方答复：先确认 {check}，再按「{solution}」处置。"


def _iso(d: date) -> str:
    return d.isoformat()


def _rand_dt(rng: random.Random, days_back: int = 180) -> datetime:
    offset = rng.randint(0, days_back - 1)
    return datetime.combine(BASE_DATE - timedelta(days=offset), datetime.min.time()) + timedelta(
        hours=rng.randint(8, 21), minutes=rng.randint(0, 59)
    )


def generate_cases(builder: SeedBuilder, seed: dict[str, Any]) -> dict[str, int]:
    """在已构建的产品/故障知识之上，追加客户、设备、案例与工单。"""
    rng = random.Random(RNG_SEED)
    graph = builder.graph
    onto = builder.onto
    stats = {"Customer": 0, "Product": 0, "FAQCase": 0, "PublicCase": 0, "Ticket": 0}

    syn = {"evidenceMode": "synthetic", "verification": "n/a", "curator": "synthetic_generator"}

    # 由种子驱动，不硬编码型号 / 症状 / 服务渠道（旧版曾写死 T30）
    shorts = [m["id"].split(":")[1].split("-")[0] for m in seed["models"]]
    sym_codes = [s["code"] for s in seed["symptoms"]]
    agent_codes = [a["code"] for a in seed["agents"]]
    if not shorts or not sym_codes:
        raise ValueError("种子缺少 models 或 symptoms，无法生成合成案例")

    # ---------- 1. 客户与设备 ----------
    env_codes = [e["code"] for e in seed["environments"]]
    customers: list[str] = []
    for i in range(1, 21):
        cid = f"CUST:{i:03d}"
        env = env_codes[i % len(env_codes)]
        props = {
            "customerId": f"CUST-{i:03d}",
            "homeType": HOME_TYPES[i % len(HOME_TYPES)],
            "pets": env == "PET",
            "city": CITIES[i % len(CITIES)],
            "dataProvenance": "seed_generated",
        }
        builder.add_node("Customer", cid, props)
        stats["Customer"] += 1
        customers.append(cid)
        # 环境：客户通过设备体现，这里用属性 + 设备边表达
        builder.rel("RUNS_APP", cid, f"APP:{seed['appVersions'][i % len(seed['appVersions'])]['version']}-"
                    f"{seed['appVersions'][i % len(seed['appVersions'])]['platform'].upper()}",
                    "合成客户档案：APP 版本按主流版本分布", "SYN:GENERATOR", 0.85, **syn)

    products: list[str] = []
    model_ids = [m["id"] for m in seed["models"]]
    for i in range(15):
        model_id = model_ids[i % len(model_ids)]
        model = graph.node(model_id)
        short = model_id.split(":")[1].split("-")[0]
        pid = f"PROD:{short}-2025-{i + 1:04d}"
        purchase = BASE_DATE - timedelta(days=rng.randint(60, 700))
        warranty_days = 365
        in_warranty = (BASE_DATE - purchase).days <= warranty_days
        builder.add_node("Product", pid, {
            "serialNo": f"{short}-2025-{i + 1:04d}",
            "purchaseDate": _iso(purchase),
            "warrantyStatus": "in_warranty" if in_warranty else "out_of_warranty",
            "warrantyEndDate": _iso(purchase + timedelta(days=warranty_days)),
            "dataProvenance": "seed_generated",
        })
        stats["Product"] += 1
        products.append(pid)
        builder.rel("INSTANCE_OF", pid, model_id, "合成设备档案：设备归属型号", "SYN:GENERATOR", 0.95, **syn)
        builder.rel("OWNS", customers[i % len(customers)], pid, "合成客户档案：客户拥有设备", "SYN:GENERATOR", 0.95, **syn)
        env = env_codes[i % len(env_codes)]
        builder.rel("USED_IN", pid, f"ENV:{env}", f"合成设备档案：使用环境为{env}", "SYN:GENERATOR", 0.9, **syn)
        fw = seed["firmware"][i % len(seed["firmware"])]["id"]
        builder.rel("RUNS_FIRMWARE", pid, fw, "合成设备档案：设备运行固件版本", "SYN:GENERATOR", 0.85, **syn)

    # ---------- 2. FAQCase（模板代理） ----------
    faq_cases: dict[str, list[str]] = {}
    for m in seed["models"]:
        mid = m["id"]
        short = mid.split(":")[1].split("-")[0]
        codes = [c for c in seed["codes"] if c["code"] in m.get("codes", [])]
        for c in codes[:20]:
            symptom = c.get("symptom")
            case_no = f"FAQ-{short}-{c['code']}"
            cid = f"CASE:{case_no}"
            fault = _primary_fault(graph, symptom)
            cause = _primary_cause(graph, fault)
            solution = _primary_solution(graph, cause)
            check = _primary_check(graph, cause)
            builder.add_node("FAQCase", cid, {
                "caseNo": case_no,
                "sourceType": "faq_template_proxy",
                "createdAt": _iso(BASE_DATE - timedelta(days=rng.randint(30, 400))),
                "title": FAQ_TITLE.format(model=m["modelCode"], code=c["code"], meaning=c["meaning"]),
                "rawText": FAQ_TEXT.format(
                    model=m["modelCode"], code=c["code"],
                    symptom=_sym_name(graph, symptom),
                    check=graph.node(check).props["name"] if check else "外观检查",
                    solution=graph.node(solution).props["name"] if solution else "联系售后",
                ),
                "resolution": graph.node(solution).props["name"] if solution else "转售后",
                "dataProvenance": "seed_generated",
                "needsReview": True,
            })
            stats["FAQCase"] += 1
            faq_cases.setdefault(short, []).append(cid)
            builder.rel("ABOUT", cid, mid, f"合成 FAQ 案例：针对 {m['modelCode']} 的 {c['code']}", "SYN:GENERATOR", 0.9, **syn)
            if symptom:
                builder.rel("OBSERVED", cid, f"SYM:{symptom}", f"合成 FAQ 案例：观察到{_sym_name(graph, symptom)}", "SYN:GENERATOR", 0.9, **syn)
            if solution:
                builder.rel("RESOLVED_BY", cid, solution, "合成 FAQ 案例：官方答复的处置方案", "SYN:GENERATOR", 0.85,
                            successTrue=0.8, **syn)

        # 用「无错误码」的症状补充案例，覆盖漏水/异响/异味等
        for extra in [x for x in ("WATER-LEAK", "NOISE-ABNORMAL", "ODOR", "RANDOM-WALK") if x in sym_codes]:
            case_no = f"FAQ-{short}-{extra}"
            cid = f"CASE:{case_no}"
            if graph.has_node(cid):
                continue
            fault = _primary_fault(graph, extra)
            cause = _primary_cause(graph, fault)
            solution = _primary_solution(graph, cause)
            builder.add_node("FAQCase", cid, {
                "caseNo": case_no,
                "sourceType": "faq_template_proxy",
                "createdAt": _iso(BASE_DATE - timedelta(days=rng.randint(30, 400))),
                "title": f"{m['modelCode']} 出现{_sym_name(graph, extra)}的处理方法",
                "rawText": f"用户问：{m['modelCode']} 出现{_sym_name(graph, extra)}，需要检查什么？官方答复：先{_check_name(graph, cause)}，再按方案处置。",
                "resolution": graph.node(solution).props["name"] if solution else "转售后",
                "dataProvenance": "seed_generated",
                "needsReview": True,
            })
            stats["FAQCase"] += 1
            faq_cases.setdefault(short, []).append(cid)
            builder.rel("ABOUT", cid, mid, "合成 FAQ 案例：按症状组织的官方答复", "SYN:GENERATOR", 0.85, **syn)
            builder.rel("OBSERVED", cid, f"SYM:{extra}", f"合成 FAQ 案例：观察到{_sym_name(graph, extra)}", "SYN:GENERATOR", 0.9, **syn)
            if solution:
                builder.rel("RESOLVED_BY", cid, solution, "合成 FAQ 案例：推荐处置方案", "SYN:GENERATOR", 0.8, **syn)

    # ---------- 3. PublicCase（公开叙事改写模拟） ----------
    public_cases: dict[str, list[str]] = {}
    for i, (cause_code, text) in enumerate(PUBLIC_TEMPLATES.items()):
        if cause_code == "ODOR":
            symptom, fault, cause = "ODOR", _primary_fault(graph, "ODOR"), "SEWAGE-OVERFLOW"
        elif cause_code == "WATER-LEAK":
            symptom, fault, cause = "WATER-LEAK", _primary_fault(graph, "WATER-LEAK"), "TANK-SEAL-WORN"
        elif cause_code == "FREQUENT-OFFLINE":
            symptom, fault, cause = "FREQUENT-OFFLINE", _primary_fault(graph, "FREQUENT-OFFLINE"), "ROUTER-DISTANCE"
        elif cause_code == "HAIR-WRAP-MAIN-BRUSH":
            symptom, fault, cause = "MAIN-BRUSH-STOPPED", _primary_fault(graph, "MAIN-BRUSH-STOPPED"), "HAIR-WRAP-MAIN-BRUSH"
        elif cause_code == "HAIR-WRAP-WHEEL":
            symptom, fault, cause = "LEFT-WHEEL-STUCK", _primary_fault(graph, "LEFT-WHEEL-STUCK"), "HAIR-WRAP-WHEEL"
        elif cause_code == "WIFI-PAIR-FAIL":
            symptom, fault, cause = "PAIRING-FAILED", _primary_fault(graph, "PAIRING-FAILED"), "WIFI-5G-BAND"
        elif cause_code == "BATTERY-DRAIN-FAST":
            symptom, fault, cause = "BATTERY-DRAIN-FAST", _primary_fault(graph, "BATTERY-DRAIN-FAST"), "BATTERY-AGING-CAUSE"
        else:
            symptom, fault, cause = "NOISE-ABNORMAL", _primary_fault(graph, "NOISE-ABNORMAL"), "DEBRIS-JAM"

        model = seed["models"][i % len(seed["models"])]
        short = model["id"].split(":")[1].split("-")[0]
        case_no = f"PUB-2025-{i + 1:03d}"
        cid = f"CASE:{case_no}"
        solution = _primary_solution(graph, f"CAUSE:{cause}")
        builder.add_node("PublicCase", cid, {
            "caseNo": case_no,
            "sourceType": "public_post_rewritten",
            "createdAt": _iso(BASE_DATE - timedelta(days=rng.randint(20, 300))),
            "title": f"{model['modelCode']} 用户反馈：{_sym_name(graph, symptom)}",
            "rawText": text,
            "resolution": graph.node(solution).props["name"] if solution else "未提供后续",
            "sourceUrl": "",
            "anonymized": True,
            "dataProvenance": "seed_generated",
            "needsReview": True,
        })
        stats["PublicCase"] += 1
        public_cases.setdefault(short, []).append(cid)
        builder.rel("ABOUT", cid, model["id"], "公开叙事改写：按型号归档", "SYN:GENERATOR", 0.8, **syn)
        builder.rel("OBSERVED", cid, f"SYM:{symptom}", f"公开叙事改写：用户描述{_sym_name(graph, symptom)}", "SYN:GENERATOR", 0.85, **syn)
        builder.rel("REPORTED_BY", cid, customers[i % len(customers)], "公开叙事改写：匿名化后归属客户档案", "SYN:GENERATOR", 0.7, **syn)
        if solution:
            builder.rel("RESOLVED_BY", cid, solution, "公开叙事改写：用户自述的有效处置", "SYN:GENERATOR", 0.7, **syn)

    # ---------- 4. Ticket（合成工单） ----------
    tickets: list[str] = []
    ticket_specs: list[dict[str, Any]] = []
    for i in range(45):
        model = seed["models"][i % len(seed["models"])]
        short = model["id"].split(":")[1].split("-")[0]
        pool = faq_cases.get(short) or next(iter(faq_cases.values()))
        source_case = rng.choice(pool)
        symptoms = [e.tail for e in graph.out_edges(source_case, {"OBSERVED"})]
        ticket_specs.append({
            "index": i,
            "model": model,
            "source": source_case,
            "symptom": symptoms[0] if symptoms else f"SYM:{sym_codes[0]}",
            "created_at": _rand_dt(rng),
        })

    # 构造一次重复报修：同一设备同一症状 90 天内 3 次（供 R12 触发）
    chronic_product = products[0]
    chronic_symptom = "SYM:LEFT-WHEEL-STUCK" if "LEFT-WHEEL-STUCK" in sym_codes else f"SYM:{sym_codes[0]}"
    for k in range(3):
        ticket_specs.append({
            "index": 45 + k,
            "model": seed["models"][0],
            "source": next(iter(faq_cases.values()))[0],
            "symptom": chronic_symptom,
            "created_at": datetime.combine(BASE_DATE - timedelta(days=70 - k * 20), datetime.min.time())
            + timedelta(hours=10 + k),
            "forced_product": chronic_product,
        })

    seen_symptom: dict[tuple[str, str], list[str]] = {}
    by_model_symptom: dict[tuple[str, str], list[str]] = {}
    for spec in ticket_specs:
        idx = spec["index"]
        ticket_no = f"{spec['model']['id'].split(':')[1].split('-')[0]}-2025-{idx + 1:04d}"
        tid = f"TICKET:{ticket_no}"
        product = spec.get("forced_product") or products[idx % len(products)]
        customer = _owner(graph, product) or customers[idx % len(customers)]
        resolved = rng.random() < 0.82
        solution = _primary_solution(graph, spec["source"])
        builder.add_node("Ticket", tid, {
            "caseNo": ticket_no,
            "sourceType": "synthetic",
            "createdAt": spec["created_at"].isoformat(),
            "title": f"{spec['model']['modelCode']} {_sym_name(graph, spec['symptom'].split(':')[1])}",
            "rawText": f"客户来电反馈{_sym_name(graph, spec['symptom'].split(':')[1])}，按流程排查后处置。",
            "resolution": graph.node(solution).props["name"] if solution else "转售后",
            "channel": CHANNELS[idx % len(CHANNELS)],
            "cost": 0 if resolved and idx % 3 else rng.choice([0, 39, 99, 199, 299]),
            "satisfaction": SATISFACTION[idx % len(SATISFACTION)],
            "resolved": resolved,
            "dataProvenance": "seed_generated",
        })
        stats["Ticket"] += 1
        tickets.append(tid)
        builder.rel("ABOUT", tid, product, "合成工单：关联设备", "SYN:GENERATOR", 0.95, **syn)
        builder.rel("OBSERVED", tid, spec["symptom"], "合成工单：客户描述的症状", "SYN:GENERATOR", 0.9, **syn)
        builder.rel("REPORTED_BY", tid, customer, "合成工单：报修客户", "SYN:GENERATOR", 0.95, **syn)
        builder.rel("DERIVED_FROM", tid, spec["source"], "合成工单：由案例模板派生", "SYN:GENERATOR", 0.9, **syn)
        builder.rel("ASSIGNED_TO", tid, f"AGENT:{agent_codes[idx % len(agent_codes)]}",
                    "合成工单：指派渠道", "SYN:GENERATOR", 0.8, **syn)
        if solution:
            builder.rel("RESOLVED_BY", tid, solution, "合成工单：最终处置方案", "SYN:GENERATOR",
                        0.85 if resolved else 0.4, success=resolved, **syn)
        seen_symptom.setdefault((product, spec["symptom"]), []).append(tid)
        by_model_symptom.setdefault(
            (spec["model"]["id"].split(":")[1].split("-")[0], spec["symptom"]), []
        ).append(tid)

    # 相似案例：同型号同症状的历史工单互相标记（跨设备也成立）
    similar_pairs = 0
    for (_model_short, _sym), tids in by_model_symptom.items():
        for a, b in zip(tids, tids[1:]):
            if similar_pairs >= 24:
                break
            builder.rel("SIMILAR_TO", a, b, "合成工单：同型号同症状，标记为相似", "SYN:GENERATOR", 0.75, **syn)
            similar_pairs += 1
    dup_pairs = 0
    for tids in seen_symptom.values():
        if len(tids) >= 2 and dup_pairs < 5:
            builder.rel("DUPLICATE_OF", tids[0], tids[1], "合成工单：同一问题 24 小时内重复报修",
                        "SYN:GENERATOR", 0.8, hoursApart=12, **syn)
            dup_pairs += 1

    return stats


# ---------- 图查询小工具（避免在生成器里写 Cypher）----------
def _primary_fault(graph, symptom: str | None) -> str | None:
    if not symptom:
        return None
    edges = [e for e in graph.out_edges(f"SYM:{symptom}", {"INDICATES"})]
    if not edges:
        return None
    return max(edges, key=lambda e: e.confidence).tail


def _primary_cause(graph, fault_id: str | None) -> str | None:
    if not fault_id or not graph.has_node(fault_id):
        return None
    edges = list(graph.out_edges(fault_id, {"CAUSED_BY"}))
    if not edges:
        return None
    return max(edges, key=lambda e: e.confidence).tail


def _primary_solution(graph, cause_id: str | None) -> str | None:
    if not cause_id:
        return None
    if cause_id.startswith("CASE:"):
        edges = list(graph.out_edges(cause_id, {"RESOLVED_BY"}))
        return max(edges, key=lambda e: e.confidence).tail if edges else None
    edges = list(graph.out_edges(cause_id, {"RESOLVED_BY"}))
    if not edges:
        return None
    # 优先「非高风险、可自助」的方案
    def rank(e):
        node = graph.node(e.tail)
        risk = {"low": 2, "medium": 1, "high": 0}.get(node.props.get("riskLevel", "low"), 0)
        return (e.confidence, risk)
    return max(edges, key=rank).tail


def _primary_check(graph, cause_id: str | None) -> str | None:
    if not cause_id:
        return None
    edges = list(graph.out_edges(cause_id, {"CONFIRMED_BY"}))
    return edges[0].tail if edges else None


def _owner(graph, product_id: str) -> str | None:
    for e in graph.in_edges(product_id, {"OWNS"}):
        return e.head
    return None


def _sym_name(graph, code: str) -> str:
    node = graph.node(f"SYM:{code}")
    return node.props.get("name", code) if node else code


def _check_name(graph, cause_id: str | None) -> str:
    chk = _primary_check(graph, cause_id)
    node = graph.node(chk) if chk else None
    return node.props.get("name", "外观检查") if node else "外观检查"
