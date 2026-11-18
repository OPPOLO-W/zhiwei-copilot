"""接口契约：三种模式、错误码、可视化数据形状与 SSE 回放。"""

from __future__ import annotations

import json

import pytest

from backend.app.api.query import RUNS


def test_health(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["entities"] >= 60
    assert body["relations"] >= 120


def test_rule_mode_returns_proof_and_graph(client, model):
    r = client.post("/api/query", json={"question": f"{model} 报 E11，左轮不转", "mode": "rule"})
    assert r.status_code == 200
    body = r.json()
    assert body["proof"]["mode"] == "rule"
    assert body["proof"]["derivationTree"]
    assert body["graph"]["nodes"] and body["graph"]["edges"]
    assert body["evidence"]
    assert body["detail"]["faults"]


def test_agent_mode_returns_coverage_and_tools(client, model):
    r = client.post("/api/query", json={"question": f"{model} 报 E11，左轮不转", "mode": "agent"})
    body = r.json()
    assert body["status"] == "ok"
    assert set(body["detail"]["coverage"]) == {"q1", "q2", "q3", "q4", "q5"}
    assert body["detail"]["toolResults"]
    assert body["slots"]["code"] == "E11"
    assert body["slots"]["intent"]


def test_compare_mode_exposes_divergence(client):
    r = client.post("/api/query", json={"question": "T20 PRO 报 10010 配网失败", "mode": "compare"})
    body = r.json()
    assert body["mode"] == "compare"
    assert body["ruleResult"] and body["agentResult"]
    div = body["detail"]["divergence"]
    assert "topFaultSame" in div and "onlyInAgent" in div


def test_rule_mode_without_slots_is_422(client):
    r = client.post("/api/query", json={"question": "帮我推荐一台吸尘器", "mode": "rule"})
    assert r.status_code == 422


def test_agent_mode_without_slots_gives_guidance(client):
    r = client.post("/api/query", json={"question": "帮我推荐一台吸尘器", "mode": "agent"})
    assert r.status_code == 200
    assert r.json()["status"] == "out_of_scope"


def test_session_is_created_and_returned(client, model):
    body = client.post("/api/query",
                       json={"question": f"{model} 报 E11，左轮不转", "mode": "agent"}).json()
    assert body["sessionId"].startswith("s_")
    assert body["detail"]["session"]["turn"] >= 1


def test_follow_up_inherits_slots_from_session(client, model):
    """第二轮只说「还是不行」：型号/错误码必须从会话继承，否则整段降级成补充型号。"""
    first = client.post("/api/query",
                        json={"question": f"{model} 报 E11，左轮不转", "mode": "agent"}).json()
    sid = first["sessionId"]
    second = client.post("/api/query", json={"question": "还是不行，怎么办", "mode": "agent",
                                             "sessionId": sid}).json()
    assert second["sessionId"] == sid
    assert second["slots"]["model"] == model
    assert second["slots"]["code"] == "E11"
    assert second["status"] == "ok"


def test_unknown_case_is_404(client):
    assert client.get("/api/cases/NOT-EXIST").status_code == 404


def test_ontology_and_rules_endpoints(client):
    onto = client.get("/api/ontology").json()
    assert len(onto["classes"]) >= 20
    rules = client.get("/api/rules").json()
    assert rules["count"] >= 20
    assert rules["items"][0]["id"]


def test_subgraph_endpoint_uses_same_shape(client):
    body = client.post("/api/graph/subgraph",
                       json={"nodeIds": ["SYM:LEFT-WHEEL-STUCK"], "depth": 2}).json()
    assert body["nodes"] and body["edges"]
    node = body["nodes"][0]
    for key in ("id", "label", "name", "color", "shape"):
        assert key in node, f"前端契约缺字段 {key}"


def test_stream_replays_events(client, model):
    r = client.post("/api/query", json={"question": f"{model} 报 E11", "mode": "agent"})
    run_id = r.json()["runId"]
    assert run_id in RUNS

    with client.stream("GET", f"/api/query/{run_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        payload = "".join(chunk for chunk in resp.iter_text())
    assert "event:" in payload
    assert "done" in payload


def test_trace_is_written_to_disk(client, model):
    from pathlib import Path

    from backend.app.config import get_settings

    directory = Path(get_settings().trace_dir)
    before = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    client.post("/api/query", json={"question": f"{model} 报 E11", "mode": "rule"})
    after = sorted(directory.glob("*.jsonl"))
    assert len(after) >= len(before)
    assert after, "trace 文件应被创建"
    line = after[-1].read_text(encoding="utf-8").strip().splitlines()[-1]
    record = json.loads(line)
    assert record["mode"] == "rule"
    assert record["runId"]
