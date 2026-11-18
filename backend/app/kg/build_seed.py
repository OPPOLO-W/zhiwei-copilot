"""从种子 YAML 构建图谱 ABox，输出 data/curated/{nodes,edges}.jsonl。

设计要点
--------
1. 种子按「型号 × 错误码」矩阵展开，避免把同一含义的错误码写成全局节点。
2. 每条关系强制带 sourceId / evidence / evidenceMode / confidence，缺一即报错。
3. 幂等：同一份种子多次构建，输出字节级稳定（排序在 model.write_jsonl 中完成）。
4. 合成案例与工单在 synthetic.py 中生成，并显式标注 provenance。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..config import CURATED_DIR, SEED_DIR
from .model import Graph
from .ontology import Ontology, get_ontology

DEFAULT_CONFIDENCE = 0.90
CURATED_PROVENANCE = {
    "evidenceMode": "paraphrase",
    "verification": "pending",
    "curator": "manual_seed",
}


class SeedError(ValueError):
    """种子数据自检失败。"""


class SeedBuilder:
    def __init__(self, ontology: Ontology, seed: dict[str, Any]) -> None:
        self.onto = ontology
        self.seed = seed
        self.graph = Graph()
        self.models: dict[str, str] = {}       # 短码 -> 节点 ID
        self.model_codes: dict[str, str] = {}  # modelCode -> 节点 ID
        self.stats: dict[str, int] = {}
        # 型号 -> 手册 doc_id。必须由种子显式声明，不再用字符串拼接，
        # 否则会拼出 DOC:T20-MANUAL 这类语料中并不存在的 ID（真实为 DOC:T20PRO-MANUAL）。
        self.manual_by_model: dict[str, str] = {
            m["id"]: m["manualDoc"] for m in seed.get("models", []) if m.get("manualDoc")
        }
        # 各知识层的证据来源与证据等级，由种子声明，避免在代码里硬编码 DOC:T30-*
        self.sources: dict[str, str] = seed.get("sources") or {}
        self.evidence_modes: dict[str, str] = seed.get("evidence") or {}

    # ---------- 基础写入 ----------
    def node_id(self, class_name: str, key: str) -> str:
        return f"{self.onto.prefix_of(class_name)}:{key}"

    def add_node(
        self,
        class_name: str,
        node_id: str,
        props: dict[str, Any],
        extra_labels: Iterable[str] = (),
    ) -> str:
        if class_name not in self.onto.classes:
            raise SeedError(f"未定义的类: {class_name}")
        # 子类自动继承父类标签：FAQCase/Ticket 同时也是 Case，便于按 Case 统一约束与查询
        inherited: list[str] = list(extra_labels)
        parent = self.onto.classes[class_name].parent
        while parent:
            inherited.append(parent)
            parent = self.onto.classes[parent].parent
        extra_labels = tuple(dict.fromkeys(inherited))
        if not self.graph.has_node(node_id):
            self.stats[class_name] = self.stats.get(class_name, 0) + 1
        self.graph.add_node(node_id, class_name, props, extra_labels)
        return node_id

    def rel(
        self,
        rel_type: str,
        head: str,
        tail: str,
        evidence: str,
        source_id: str,
        confidence: float = DEFAULT_CONFIDENCE,
        evidence_mode: str | None = None,
        verification: str | None = None,
        **extra: Any,
    ) -> None:
        rdef = self.onto.relation(rel_type)
        if rdef is None:
            raise SeedError(f"未定义的关系类型: {rel_type}")
        if not self.graph.has_node(head):
            raise SeedError(f"关系头节点不存在: {head} ({rel_type})")
        if not self.graph.has_node(tail):
            raise SeedError(f"关系尾节点不存在: {tail} ({rel_type})")
        head_labels = set(self.graph.node(head).labels)  # type: ignore[union-attr]
        tail_labels = set(self.graph.node(tail).labels)  # type: ignore[union-attr]
        head_primary = self.graph.node(head).label  # type: ignore[union-attr]
        tail_primary = self.graph.node(tail).label  # type: ignore[union-attr]
        # 允许父类：FAQCase/Ticket 可作为 Case 的头或尾
        head_ok = head_primary in rdef.head or any(
            self.onto.is_subclass(head_primary, allowed) for allowed in rdef.head
        )
        tail_ok = tail_primary in rdef.tail or any(
            self.onto.is_subclass(tail_primary, allowed) for allowed in rdef.tail
        )
        if not head_ok:
            raise SeedError(f"{rel_type} 的 domain 不允许 {sorted(head_labels)} ({head})")
        if not tail_ok:
            raise SeedError(f"{rel_type} 的 range 不允许 {sorted(tail_labels)} ({tail})")
        props = {
            "sourceId": source_id,
            "evidence": evidence,
            "confidence": round(float(confidence), 3),
            **CURATED_PROVENANCE,
            **extra,
        }
        if evidence_mode:
            props["evidenceMode"] = evidence_mode
            # 非 paraphrase 的层默认不声称已核对：assumed 表示语料中并无对应原文
            props["verification"] = verification or (
                "unverified" if evidence_mode == "assumed" else "pending"
            )
        elif verification:
            props["verification"] = verification
        self.graph.add_edge(rel_type, head, tail, props)

    # ---------- 展开辅助 ----------
    def manual_doc(self, model_id: str) -> str:
        """型号 -> 用户手册 doc_id。种子未声明 manualDoc 即报错，不做任何猜测。"""
        doc = self.manual_by_model.get(model_id)
        if not doc:
            raise SeedError(f"型号 {model_id} 未声明 manualDoc，无法确定手册文档 ID")
        return doc

    def src(self, layer: str, default: str | None = None) -> str:
        """取某一知识层的证据来源 doc_id。"""
        doc = self.sources.get(layer) or self.sources.get("default") or default
        if not doc:
            raise SeedError(f"种子未声明 sources.{layer} 或 sources.default")
        return doc

    def mode(self, layer: str) -> str:
        """取某一知识层的证据等级，默认 paraphrase。"""
        return self.evidence_modes.get(layer, "paraphrase")

    def manual_doc_by_code(self, model_code: str) -> str:
        return self.manual_doc(self.model_codes[model_code])

    def expand_models(self, spec: Any) -> list[str]:
        if spec in ("all", None):
            return list(self.models.values())
        if isinstance(spec, str):
            spec = [spec]
        out = []
        for item in spec:
            if item not in self.models:
                raise SeedError(f"未知型号: {item}")
            out.append(self.models[item])
        return out

    # ---------- 构建各层 ----------
    def _declare_entities(self) -> None:
        """两阶段构建第一段：先声明全部实体，再连边。

        原因：faults -> causes -> checks -> faults 构成环，任何单遍顺序都会失败。
        这里只填最小属性（名称/业务键）供证据句引用，完整属性仍由各 _build_* 补齐。
        """
        line = self.seed["productLine"]
        self.line_id = line["id"]
        self.line_source = line.get("sourceId") or self.src("productLine")
        self.add_node("ProductLine", line["id"], {"name": line["name"], "code": line["code"]})

        for m in self.seed["models"]:
            self.add_node("ProductModel", m["id"], {"name": m["name"], "modelCode": m["modelCode"]})
            short = m["id"].split(":", 1)[1]
            self.models[short] = m["id"]
            self.model_codes[m["modelCode"]] = m["id"]

        for c in self.seed["components"]:
            self.add_node("Component", self.node_id("Component", c["code"]), {"name": c["name"], "code": c["code"]})
        for c in self.seed["consumables"]:
            self.add_node("Consumable", self.node_id("Consumable", c["code"]), {"name": c["name"], "sku": c["sku"]})
        for sp in self.seed["spareParts"]:
            self.add_node("SparePart", self.node_id("SparePart", sp["code"]), {"name": sp["name"], "sku": sp["sku"]})
        for cat in self.seed["categories"]:
            self.add_node("FaultCategory", self.node_id("FaultCategory", cat["code"]),
                          {"name": cat["name"], "code": cat["code"]})
        for env in self.seed["environments"]:
            self.add_node("UsageEnvironment", self.node_id("UsageEnvironment", env["code"]),
                          {"name": env["name"], "code": env["code"]})
        for a in self.seed["agents"]:
            self.add_node("ServiceAgent", self.node_id("ServiceAgent", a["code"]),
                          {"name": a["name"], "code": a["code"]})
        for d in self.seed["documents"]:
            self.add_node("Document", d["id"], {"title": d["title"], "docId": d["docId"]})
        for fw in self.seed.get("firmware", []):
            self.add_node("FirmwareVersion", fw["id"], {"version": str(fw["version"]), "name": f"固件 {fw['version']}"})
        for app in self.seed.get("appVersions", []):
            self.add_node("AppVersion", app["id"], {"version": str(app["version"]), "name": f"APP {app['version']}"})

        code_defs = {c["code"]: c for c in self.seed["codes"]}
        for m in self.seed["models"]:
            for code in m.get("codes", []):
                d = code_defs[code]
                eid = f"ERR:{m['id'].split(':', 1)[1]}:{code}"
                self.add_node("ErrorCode", eid, {
                    "name": f"{m['modelCode']} / {code}",
                    "code": code,
                    "modelCode": m["modelCode"],
                    "meaning": d["meaning"],
                })
        for sym in self.seed["symptoms"]:
            self.add_node("Symptom", self.node_id("Symptom", sym["code"]),
                          {"name": sym["name"], "code": sym["code"]})
        for f in self.seed["faults"]:
            self.add_node("Fault", self.node_id("Fault", f["code"]), {"name": f["name"], "code": f["code"]})
        for c in self.seed["causes"]:
            self.add_node("Cause", self.node_id("Cause", c["code"]), {"name": c["name"], "code": c["code"]})
        for k in self.seed["checks"]:
            self.add_node("Check", self.node_id("Check", k["code"]), {"name": k["name"], "code": k["code"]})
        for sol in self.seed["solutions"]:
            self.add_node("Solution", self.node_id("Solution", sol["code"]), {"name": sol["name"], "code": sol["code"]})

    def build(self) -> Graph:
        self._declare_entities()
        self._build_product_line()
        self._build_models()
        self._build_components()
        self._build_consumables()
        self._build_spare_parts()
        self._build_categories()
        self._build_environments()
        self._build_agents()
        self._build_documents()
        self._build_error_codes()
        self._build_symptoms()
        self._build_faults()
        self._build_firmware()   # 依赖 Fault（KNOWN_ISSUE），必须在故障之后
        self._build_causes()
        self._build_checks()
        self._build_solutions()
        self._build_precedence()
        return self.graph

    def _build_product_line(self) -> None:
        line = self.seed["productLine"]
        self.add_node("ProductLine", line["id"], {
            "name": line["name"],
            "code": line["code"],
            "description": line.get("description", ""),
        })
        self.line_id = line["id"]
        self.line_source = line.get("sourceId") or self.src("productLine")

    def _build_models(self) -> None:
        for m in self.seed["models"]:
            self.add_node("ProductModel", m["id"], {
                "name": m["name"],
                "modelCode": m["modelCode"],
                "variant": m.get("variant", ""),
                "launchYear": m.get("launchYear"),
            })
            short = m["id"].split(":", 1)[1]
            self.models[short] = m["id"]
            self.model_codes[m["modelCode"]] = m["id"]
            self.rel("HAS_MODEL", self.line_id, m["id"], m["evidence"], m.get("sourceId", self.line_source), 0.95)

    def _build_components(self) -> None:
        for c in self.seed["components"]:
            cid = self.add_node("Component", self.node_id("Component", c["code"]), {
                "name": c["name"],
                "code": c["code"],
                "partNo": c.get("partNo", ""),
                "category": c.get("category", ""),
                "maintainable": bool(c.get("maintainable", True)),
            })
            for mid in self.expand_models(c.get("models", "all")):
                model = self.graph.node(mid)
                src = self.manual_doc(mid)
                self.rel(
                    "HAS_COMPONENT", mid, cid,
                    f"{model.props['name']} 的部件说明列出{c['name']}：{c.get('note', '')}",
                    src, 0.92,
                )

    def _build_consumables(self) -> None:
        for c in self.seed["consumables"]:
            cid = self.add_node("Consumable", self.node_id("Consumable", c["code"]), {
                "name": c["name"],
                "sku": c["sku"],
                "replaceCycleDays": c.get("cycle"),
            })
            for mid in self.expand_models(c.get("compatible", "all")):
                model = self.graph.node(mid)
                src = self.manual_doc(mid)
                self.rel("USES_CONSUMABLE", mid, cid,
                         f"{model.props['name']} 使用{c['name']}，建议更换周期 {c.get('cycle')} 天：{c.get('note', '')}",
                         src, 0.92)
                self.rel("COMPATIBLE_WITH", cid, mid,
                         f"{c['name']}（{c['sku']}）与 {model.props['name']} 兼容",
                         src, 0.90)

    def _build_spare_parts(self) -> None:
        for p in self.seed["spareParts"]:
            pid = self.add_node("SparePart", self.node_id("SparePart", p["code"]), {
                "name": p["name"],
                "sku": p["sku"],
                "category": p.get("category", ""),
                "userReplaceable": bool(p.get("userReplaceable", False)),
            })
            for mid in self.expand_models(p.get("compatible", "all")):
                model = self.graph.node(mid)
                src = self.manual_doc(mid)
                self.rel("COMPATIBLE_WITH", pid, mid,
                         f"{p['name']}（{p['sku']}）可用于 {model.props['name']}：{p.get('note', '')}",
                         src, 0.90)

    def _build_firmware(self) -> None:
        for fw in self.seed.get("firmware", []):
            self.add_node("FirmwareVersion", fw["id"], {
                "version": str(fw["version"]),
                "releaseDate": fw.get("releaseDate"),
            })
            model_id = fw["model"]
            model = self.graph.node(model_id)
            src = self.manual_doc(model_id)
            self.rel("HAS_FIRMWARE", model_id, fw["id"],
                     f"{model.props['name']} 固件 {fw['version']}：{fw.get('note', '')}", src, 0.85)
        for k in self.seed.get("knownIssues", []):
            fault_id = self.node_id("Fault", k["fault"])
            if not self.graph.has_node(fault_id):
                raise SeedError(f"knownIssues 引用了未定义故障: {k['fault']}")
            self.rel("KNOWN_ISSUE", k["firmware"], fault_id, k["note"], self.src("firmware"), 0.80,
                     evidence_mode=self.mode("firmware"), severity="medium")

    def _build_categories(self) -> None:
        for c in self.seed["categories"]:
            self.add_node("FaultCategory", self.node_id("FaultCategory", c["code"]), {
                "name": c["name"],
                "code": c["code"],
                "selfServiceable": bool(c.get("selfServiceable", False)),
            })

    def _build_environments(self) -> None:
        for e in self.seed["environments"]:
            self.add_node("UsageEnvironment", self.node_id("UsageEnvironment", e["code"]), {
                "name": e["name"],
                "code": e["code"],
                "description": e.get("note", ""),
            })

    def _build_agents(self) -> None:
        for a in self.seed["agents"]:
            self.add_node("ServiceAgent", self.node_id("ServiceAgent", a["code"]), {
                "name": a["name"],
                "code": a["code"],
                "type": a.get("type", "official"),
                "city": a.get("city", ""),
                "slaHours": a.get("slaHours"),
            })

    def _build_documents(self) -> None:
        for model_id in self.models.values():
            doc = self.manual_doc(model_id)
            if not self.graph.has_node(doc):
                raise SeedError(f"型号 {model_id} 的 manualDoc={doc} 未在 documents 中声明")
            model = self.graph.node(model_id)
            self.rel("HAS_MANUAL", model_id, doc,
                     f"{model.props['name']} 的官方用户手册", doc, 0.95)
        for d in self.seed["documents"]:
            self.add_node("Document", d["id"], {
                "docId": d["docId"],
                "title": d["title"],
                "docType": d.get("docType", ""),
                "version": d.get("version", ""),
                "updatedAt": d.get("updatedAt"),
                "url": d.get("url", ""),
                "authority": d.get("authority", 3),
                "product": d.get("product", ""),
                "pageCount": d.get("pageCount"),
            })
            # 文档归属型号：不建这条边，未被方案引用的 FAQ 就会成为孤立节点
            mid = self.model_codes.get(d.get("product", ""))
            if mid:
                self.rel("ABOUT", d["id"], mid,
                         f"《{d['title']}》是 {d.get('product')} 的官方资料", d["id"], 0.95,
                         evidence_mode="paraphrase")

    def _build_error_codes(self) -> None:
        code_defs = {c["code"]: c for c in self.seed["codes"]}
        for m in self.seed["models"]:
            mid = m["id"]
            for code in m.get("codes", []):
                if code not in code_defs:
                    raise SeedError(f"型号 {m['modelCode']} 引用了未定义错误码 {code}")
                d = code_defs[code]
                allowed = self.expand_models(d.get("models", "all"))
                if mid not in allowed:
                    raise SeedError(f"错误码 {code} 声明不适用于 {m['modelCode']}")
                eid = f"ERR:{m['id'].split(':', 1)[1]}:{code}"
                self.add_node("ErrorCode", eid, {
                    "name": f"{m['modelCode']} / {code}",
                    "code": code,
                    "modelCode": m["modelCode"],
                    "meaning": d["meaning"],
                    "severity": d.get("severity", "medium"),
                    "family": d.get("family", ""),
                })
                src = self.src("codes")
                mode = self.mode("codes")
                self.rel("HAS_ERROR_CODE", mid, eid,
                         f"{m['modelCode']} 错误码说明：{d.get('note', d['meaning'])}", src, 0.95,
                         evidence_mode=mode,
                         meaning=d["meaning"], severity=d.get("severity", "medium"))
                cat_id = self.node_id("FaultCategory", d["category"])
                self.rel("HAS_CATEGORY", eid, cat_id,
                         f"{code} 属于{self.graph.node(cat_id).props['name']}", src, 0.95,
                         evidence_mode=mode)
                # 只有 sources.codes 指向真实文档时才建归属边；
                # 若错误码层本身无语料支撑（assumed），不虚构「记录在某文档里」
                if self.graph.has_node(src) and self.graph.node(src).label == "Document":
                    self.rel("DOCUMENTED_IN", eid, src,
                             f"{m['modelCode']} 的 {code} 说明见{self.graph.node(src).props.get('title', '官方文档')}",
                             src, 0.9, evidence_mode=mode)
                if d.get("symptom"):
                    sym_id = self.node_id("Symptom", d["symptom"])
                    if not self.graph.has_node(sym_id):
                        raise SeedError(f"错误码 {code} 引用了未定义症状 {d['symptom']}")
                    self.rel("HAS_CODE", sym_id, eid,
                             f"{m['modelCode']} 出现 {code} 时表现为「{d['meaning']}」", src, 0.90,
                             evidence_mode=mode)

    def _build_symptoms(self) -> None:
        for s in self.seed["symptoms"]:
            sid = self.add_node("Symptom", self.node_id("Symptom", s["code"]), {
                "name": s["name"],
                "code": s["code"],
                "severity": s.get("severity", "medium"),
                "description": s.get("note", ""),
                "aliases": s.get("aliases", []),
                "aliasesText": " ".join(s.get("aliases", [])),
            })
            for ind in s.get("indicates", []):
                fid = self.node_id("Fault", ind["fault"])
                if not self.graph.has_node(fid):
                    raise SeedError(f"症状 {s['code']} 指向未定义故障 {ind['fault']}")
                self.rel("INDICATES", sid, fid,
                         f"{s['name']}提示{self.graph.node(fid).props['name']}：{s.get('note', '')}",
                         self.src("symptom"), ind["cf"],
                         evidence_mode=self.mode("symptom"))

    def _build_faults(self) -> None:
        for f in self.seed["faults"]:
            fid = self.add_node("Fault", self.node_id("Fault", f["code"]), {
                "name": f["name"],
                "code": f["code"],
                "severity": f.get("severity", "medium"),
            })
            comp_id = self.node_id("Component", f["component"])
            if not self.graph.has_node(comp_id):
                raise SeedError(f"故障 {f['code']} 引用了未定义部件 {f['component']}")
            self.rel("OCCURS_IN", fid, comp_id,
                     f"{f['name']}发生在{self.graph.node(comp_id).props['name']}：{f.get('note', '')}",
                     self.src("fault"), 0.90, evidence_mode=self.mode("fault"))
            cat_id = self.node_id("FaultCategory", f["category"])
            self.rel("IN_CATEGORY", fid, cat_id,
                     f"{f['name']}属于{self.graph.node(cat_id).props['name']}", self.src("fault"), 0.92, evidence_mode=self.mode("fault"))
            for c in f.get("causes", []):
                cid = self.node_id("Cause", c["cause"])
                if not self.graph.has_node(cid):
                    raise SeedError(f"故障 {f['code']} 引用未定义根因 {c['cause']}")
                self.rel("CAUSED_BY", fid, cid,
                         f"{f['name']}的可能根因是{self.graph.node(cid).props['name']}",
                         self.src("fault"), c["cf"], evidence_mode=self.mode("fault"))

    def _build_causes(self) -> None:
        for c in self.seed["causes"]:
            cid = self.add_node("Cause", self.node_id("Cause", c["code"]), {
                "name": c["name"],
                "code": c["code"],
                "category": c.get("category", ""),
                "userFixable": bool(c.get("userFixable", False)),
            })
            for chk in c.get("confirm", []):
                chk_id = self.node_id("Check", chk)
                if not self.graph.has_node(chk_id):
                    raise SeedError(f"根因 {c['code']} 引用未定义检查 {chk}")
                self.rel("CONFIRMED_BY", cid, chk_id,
                         f"{c['name']}可通过「{self.graph.node(chk_id).props['name']}」确认：{c.get('note', '')}",
                         self.src("cause"), 0.85, evidence_mode=self.mode("cause"))
            for r in c.get("resolvedBy", []):
                sol_id = self.node_id("Solution", r["solution"])
                if not self.graph.has_node(sol_id):
                    raise SeedError(f"根因 {c['code']} 引用未定义方案 {r['solution']}")
                self.rel("RESOLVED_BY", cid, sol_id,
                         f"{c['name']}可通过「{self.graph.node(sol_id).props['name']}」处置：{c.get('note', '')}",
                         self.src("cause"), r["cf"], evidence_mode=self.mode("cause"))

    def _build_checks(self) -> None:
        for k in self.seed["checks"]:
            kid = self.add_node("Check", self.node_id("Check", k["code"]), {
                "name": k["name"],
                "code": k["code"],
                "method": k.get("method", ""),
                "riskLevel": k.get("riskLevel", "low"),
                "durationMin": k.get("durationMin", 5),
                "requiresTools": k.get("requiresTools", []),
            })
            for f in k.get("detects", []):
                fid = self.node_id("Fault", f)
                if not self.graph.has_node(fid):
                    raise SeedError(f"检查 {k['code']} 引用未定义故障 {f}")
                self.rel("DETECTS", kid, fid,
                         f"{k['name']}可发现{self.graph.node(fid).props['name']}：{k.get('note', '')}",
                         self.src("check"), 0.88, evidence_mode=self.mode("check"))

    def _build_solutions(self) -> None:
        cause_codes = {c["code"] for c in self.seed["causes"]}
        for s in self.seed["solutions"]:
            excludes = list(s.get("excludes", []))
            for code in excludes:
                if code not in cause_codes:
                    raise SeedError(f"方案 {s['code']} 的 excludes 引用未定义根因 {code}")
            props = {
                "name": s["name"],
                "code": s["code"],
                "type": s.get("type", "clean"),
                "riskLevel": s.get("riskLevel", "low"),
                "selfServiceable": bool(s.get("selfServiceable", True)),
                "estimatedMin": s.get("estimatedMin", 10),
            }
            # P2 领域知识：用户做完这个动作后，可以排除哪些根因；exhausts 表示「已试过但仍不成立」
            if excludes:
                props["excludesCauses"] = excludes
            if s.get("exhausts"):
                props["exhaustsSelf"] = True
            sid = self.add_node("Solution", self.node_id("Solution", s["code"]), props)
            for con in s.get("consumes", []):
                con_id = self.node_id("Consumable", con)
                if not self.graph.has_node(con_id):
                    raise SeedError(f"方案 {s['code']} 引用未定义耗材 {con}")
                self.rel("REQUIRES_CONSUMABLE", sid, con_id,
                         f"{s['name']}需要耗材{self.graph.node(con_id).props['name']}", self.src("solution"), 0.92,
                         evidence_mode=self.mode("solution"))
            for part in s.get("parts", []):
                part_id = self.node_id("SparePart", part)
                if not self.graph.has_node(part_id):
                    raise SeedError(f"方案 {s['code']} 引用未定义备件 {part}")
                self.rel("REQUIRES_PART", sid, part_id,
                         f"{s['name']}需要备件{self.graph.node(part_id).props['name']}", self.src("solution"), 0.92,
                         evidence_mode=self.mode("solution"))
            for doc in s.get("docs", []):
                if not self.graph.has_node(doc):
                    raise SeedError(f"方案 {s['code']} 引用未定义文档 {doc}")
                self.rel("DOCUMENTED_IN", sid, doc,
                         f"{s['name']}的官方依据见《{self.graph.node(doc).props['title']}》", doc, 0.90)

    def _build_precedence(self) -> None:
        for p in self.seed.get("precedence", []):
            a = self.node_id("Solution", p["before"])
            b = self.node_id("Solution", p["after"])
            for x in (a, b):
                if not self.graph.has_node(x):
                    raise SeedError(f"precedence 引用未定义方案: {x}")
            self.rel("PRECEDES", a, b, p["note"], self.src("precedence"), 0.85,
                     evidence_mode=self.mode("precedence"))


# 种子文件按「两位数字前缀」命名（01_hardware / 02_knowledge），
# 目录里其他 yaml（对账/说明类）不是种子数据，必须排除。
SEED_FILE_GLOB = "[0-9][0-9]_*.yaml"


def seed_files(seed_dir: Path) -> list[Path]:
    """找出目录下真正的种子文件；目录本身没有时下探唯一的机型子目录。"""
    files = sorted(seed_dir.glob(SEED_FILE_GLOB))
    if files:
        return files
    subs = [d for d in sorted(seed_dir.iterdir())
            if d.is_dir() and list(d.glob(SEED_FILE_GLOB))]
    if len(subs) == 1:
        return sorted(subs[0].glob(SEED_FILE_GLOB))
    if len(subs) > 1:
        raise SeedError(
            f"{seed_dir} 下有多个机型目录（{[d.name for d in subs]}），"
            f"请用 --seed 明确指定其中一个"
        )
    return []


def load_seed(seed_dir: Path | None = None) -> dict[str, Any]:
    seed_dir = seed_dir or SEED_DIR
    merged: dict[str, Any] = {}
    list_keys = {
        "models", "components", "consumables", "spareParts", "firmware", "appVersions",
        "categories", "environments", "agents", "documents", "codes", "symptoms",
        "faults", "causes", "checks", "solutions", "precedence", "knownIssues",
    }
    paths = seed_files(seed_dir)
    if not paths:
        # 静默构建空图会让所有下游看起来「没问题但什么都没有」，必须显式失败
        raise SeedError(
            f"{seed_dir} 下没有形如 01_hardware.yaml 的种子文件；"
            f"请用 --seed 指定机型目录，例如 --seed data/seed/t20pro"
        )
    for path in paths:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key, value in data.items():
            if key in list_keys:
                merged.setdefault(key, []).extend(value)
            elif key in merged and isinstance(merged[key], dict):
                merged[key].update(value)
            else:
                merged[key] = value
    return merged


def build(seed_dir: Path | None = None, out_dir: Path | None = None, synthetic: bool = True) -> Graph:
    from .reviewed_cases import apply_reviewed_cases
    from .synthetic import generate_cases

    seed = load_seed(seed_dir)
    builder = SeedBuilder(get_ontology(), seed)
    graph = builder.build()
    case_stats: dict[str, int] = {}
    if synthetic:
        case_stats = generate_cases(builder, seed)
    # 真实官方 FAQ（人工审核通过的那部分）在合成案例之后叠加：
    # 它不参与随机生成，因此重复构建结果稳定
    reviewed = apply_reviewed_cases(builder)
    if reviewed.get("FAQCase"):
        case_stats["FAQCase_official"] = reviewed["FAQCase"]

    out_dir = out_dir or CURATED_DIR
    graph.write_jsonl(out_dir)

    from .validate import summarize
    summarize(graph, builder.stats, case_stats, out_dir)
    return graph


def main() -> None:
    parser = argparse.ArgumentParser(description="构建知识图谱种子数据")
    parser.add_argument("--no-synthetic", action="store_true", help="不生成合成案例与工单")
    parser.add_argument("--seed", type=Path, default=None, help="种子目录，默认 data/seed")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    build(seed_dir=args.seed, out_dir=args.out, synthetic=not args.no_synthetic)


if __name__ == "__main__":
    main()
