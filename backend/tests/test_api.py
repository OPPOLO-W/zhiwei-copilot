"""接口契约（w2）：健康检查、图谱查询、规则诊断与错误码。

Agent / compare 模式与 SSE 回放在 w3 引入，本文件不覆盖。
"""

from __future__ import annotations


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["entities"] >= 150
    assert body["relations"] >= 300


def test_rule_query_by_code_returns_proof_and_graph(client, model):
    r = client.post("/api/query", json={"model": model, "code": "E11"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "rule"
    assert body["proof"]["mode"] == "rule"
    assert body["proof"]["derivationTree"]
    assert body["graph"]["nodes"] and body["graph"]["edges"]
    assert body["evidence"]
    assert body["detail"]["faults"]


def test_rule_query_by_case_returns_answer(client, first_case_no):
    body = client.post("/api/query", json={"caseNo": first_case_no}).json()
    assert body["answer"]
    assert body["confidence"] > 0


def test_question_only_is_422(client):
    """w2 没有槽位抽取器：只给自然语言问题时明确拒绝，而不是静默返回空结论。"""
    r = client.post("/api/query", json={"question": "T20 PRO 报 E11，左轮不转"})
    assert r.status_code == 422
    assert "结构化参数" in r.json()["detail"]


def test_unknown_case_is_404(client):
    assert client.get("/api/cases/NOT-EXIST").status_code == 404


def test_graph_stats_reports_rules_and_ontology(client):
    body = client.get("/api/graph/stats").json()
    assert body["entities"] >= 150
    assert body["ruleCount"] >= 20
    assert body["ontologyVersion"]


def test_ontology_and_rules_endpoints(client):
    onto = client.get("/api/ontology").json()
    assert len(onto["classes"]) >= 20
    rules = client.get("/api/rules").json()
    assert rules["count"] >= 20
    assert rules["items"][0]["id"]


def test_subgraph_endpoint_uses_frontend_shape(client):
    body = client.post("/api/graph/subgraph",
                       json={"nodeIds": ["SYM:LEFT-WHEEL-STUCK"], "depth": 2}).json()
    assert body["nodes"] and body["edges"]
    node = body["nodes"][0]
    for key in ("id", "label", "name", "color", "shape"):
        assert key in node, f"前端契约缺字段 {key}"
