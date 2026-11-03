// =============================================================================
// Neo4j Schema：约束、索引、全文索引
// 对应开发文档 6.1。写入前由 loader 逐条执行（IF NOT EXISTS，可重复执行）。
// =============================================================================

// 所有节点统一带 :Entity 标签，实体 ID 全局唯一
CREATE CONSTRAINT entity_id IF NOT EXISTS
FOR (n:Entity) REQUIRE n.id IS UNIQUE;

// 型号代码唯一
CREATE CONSTRAINT model_code IF NOT EXISTS
FOR (n:ProductModel) REQUIRE n.modelCode IS UNIQUE;

// 案例编号唯一
CREATE CONSTRAINT case_no IF NOT EXISTS
FOR (n:Case) REQUIRE n.caseNo IS UNIQUE;

// ErrorCode 唯一性由 Entity.id 保证：id = "ERR:<modelCode>:<code>"
// 因此不建 code 全局唯一约束，避免同一 E11 在不同型号下互相覆盖

CREATE INDEX entity_name IF NOT EXISTS
FOR (n:Entity) ON (n.name);

CREATE INDEX entity_label IF NOT EXISTS
FOR (n:Entity) ON (n._label);

CREATE INDEX case_created IF NOT EXISTS
FOR (n:Case) ON (n.createdAt);

CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS
FOR (n:Entity) ON EACH [n.name, n.aliasesText, n.title, n.code];
