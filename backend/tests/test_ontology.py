"""本体驱动的正确性：类/关系规模、继承、领域约束都来自 schema.yaml。"""

from __future__ import annotations

from backend.app.kg.ontology import get_ontology

COURSE_MIN_ENTITIES = 60
COURSE_MIN_RELATIONS = 120


def test_ontology_scale_meets_course_gate():
    onto = get_ontology()
    assert len(onto.classes) >= 20
    assert len(onto.relations) >= 25
    gates = onto.gates
    assert gates.get("minEntitiesCourse") == COURSE_MIN_ENTITIES
    assert gates.get("minRelationsCourse") == COURSE_MIN_RELATIONS


def test_subclass_inheritance():
    onto = get_ontology()
    # Ticket / FAQCase / PublicCase 都是 Case 的子类，因此同时具备 Case 的约束
    assert onto.is_subclass("Ticket", "Case")
    assert onto.is_subclass("FAQCase", "Case")
    assert onto.is_subclass("PublicCase", "Case")
    assert not onto.is_subclass("Case", "Ticket")
    assert not onto.is_subclass("Ticket", "Ticket")


def test_every_relation_has_domain_and_range():
    onto = get_ontology()
    for name, rel in onto.relations.items():
        assert rel.head, f"{name} 缺少 head（domain）"
        assert rel.tail, f"{name} 缺少 tail（range）"
        for cls in tuple(rel.head) + tuple(rel.tail):
            assert cls in onto.classes, f"{name} 引用了未定义类 {cls}"


def test_viz_style_is_single_source():
    onto = get_ontology()
    for name, cls in onto.classes.items():
        assert cls.viz.get("color"), f"{name} 缺少可视化配色"
    # 前端直接消费这份配色，因此不允许出现非法色值
    for cls in onto.classes.values():
        assert str(cls.viz["color"]).startswith("#")


def test_prompt_context_covers_all_classes():
    onto = get_ontology()
    ctx = onto.prompt_context()
    for name in onto.classes:
        assert name in ctx
