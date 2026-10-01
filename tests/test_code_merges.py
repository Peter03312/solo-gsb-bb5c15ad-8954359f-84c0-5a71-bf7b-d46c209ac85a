"""端到端测试：过敏原代码并码（登记校验、串接解析、实时溯源合并去重、
原料声明/标签快照保留原值、下游与设备残留重算、事件流、与并发声明更正的
预演互斥、解释口径与既有接口兼容）。"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="allergen-test-merge-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402

ALL_TABLES = [
    "material_declarations", "material_batches", "processing_inputs",
    "processing_outputs", "processings", "cleanings", "labels",
    "trace_events", "investigations", "allergen_code_merges",
]


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    for t in ALL_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'trace_events'")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False,
             declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})


def merge(c, old, target, reason="并码原因"):
    return c.post("/allergen-codes/merges",
                  json={"old_code": old, "target_code": target, "reason": reason})


# ---------------------------------------------------------------- 登记校验

def test_merge_register_and_get(client):
    r = merge(client, "casein", "milk", "casein 属乳制品")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["old_code"] == "casein"
    assert body["target_code"] == "milk"
    assert body["canonical_code"] == "milk"
    assert body["is_alias"] is True
    assert body["reason"] == "casein 属乳制品"
    assert [h["old_code"] for h in body["history"]] == ["casein"]

    got = client.get("/allergen-codes/merges/casein").json()
    assert got["created_at"] == body["created_at"]


def test_merge_requires_reason(client):
    r = client.post("/allergen-codes/merges",
                    json={"old_code": "a", "target_code": "b", "reason": "   "})
    assert r.status_code == 422
    assert client.get("/allergen-codes/merges").json()["merges"] == []


def test_merge_self_mapping_rejected_without_record(client):
    r = merge(client, "milk", "milk")
    assert r.status_code == 400, r.text
    assert client.get("/allergen-codes/merges").json()["merges"] == []


def test_merge_target_must_not_be_alias(client):
    assert merge(client, "casein", "milk").status_code == 201
    r = merge(client, "old-dairy", "casein", "目标是别名")
    assert r.status_code == 409
    assert client.get("/allergen-codes/merges/old-dairy").status_code == 404


def test_repoint_same_old_code_rejected(client):
    assert merge(client, "casein", "milk").status_code == 201
    r = merge(client, "casein", "dairy", "改指")
    assert r.status_code == 409
    got = client.get("/allergen-codes/merges/casein").json()
    assert got["target_code"] == "milk"  # 原记录保持


def test_same_direction_retry_is_idempotent(client):
    first = merge(client, "casein", "milk", "首次原因")
    second = merge(client, "casein", "milk", "重试原因不覆盖")
    assert first.status_code == second.status_code == 201
    assert second.json()["created_at"] == first.json()["created_at"]
    assert second.json()["reason"] == "首次原因"
    assert len(client.get("/allergen-codes/merges").json()["merges"]) == 1
    snap = client.get("/changes/snapshot").json()
    assert snap["cursor"] == 0  # 无批次 -> 无事件；且重试不写任何记录


def test_merge_chaining_resolves_final_canonical(client):
    # casein -> milk -> dairy（目标登记时均非别名，串接合法）
    merge(client, "casein", "milk")
    merge(client, "milk", "dairy")
    for code, canon in [("casein", "dairy"), ("milk", "dairy"), ("dairy", "dairy")]:
        r = client.get(f"/allergen-codes/{code}/resolve").json()
        assert r["canonical_code"] == canon
        assert r["is_alias"] is (code != "dairy")
    hist = client.get("/allergen-codes/casein/resolve").json()["history"]
    assert [(h["old_code"], h["target_code"]) for h in hist] == [
        ("casein", "milk"), ("milk", "dairy")]


def test_resolve_unknown_code_404(client):
    material(client, "RM-1", allergens=["milk"])
    assert client.get("/allergen-codes/ghost/resolve").status_code == 404
    # 声明中出现的代码可解析（当前即规范）
    r = client.get("/allergen-codes/milk/resolve").json()
    assert r["canonical_code"] == "milk" and r["is_alias"] is False
    # 仅作为并码目标出现的代码也可解析
    merge(client, "casein", "milk")
    r = client.get("/allergen-codes/casein/resolve").json()
    assert r["canonical_code"] == "milk"


# ---------------------------------------------------------------- 实时溯源合并

def test_trace_merges_synonyms_and_dedupes_keeping_origins(client):
    # 两个原料分别声明 milk / casein，经不同路径进入 OUT-1
    material(client, "RM-MILK", allergens=["milk"])
    material(client, "RM-CASEIN", allergens=["casein"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-MILK"], ["OUT-A"])
    processing(client, "P2", "EQ-1", "2026-01-02T08:00:00Z",
               "2026-01-02T09:00:00Z", ["RM-CASEIN", "OUT-A"], ["OUT-1"])
    # 并码前：casein 与 milk 各自独立，且 milk 经投入+残留形成两条路径
    before = client.get("/batches/OUT-1/trace").json()
    assert set(before["allergens"]) == {"casein", "milk"}

    assert merge(client, "casein", "milk").status_code == 201
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk"]
    paths = t["allergen_paths"]["milk"]
    # 去重后的每条原始来源路径仍保留（casein 直接投入 + milk 经 OUT-A 投入
    # + milk 经 EQ-1 残留等），路径互不相同
    assert len(paths) >= 3
    origins = t["allergen_origins"]["milk"]
    by_decl = {o["declared_code"]: o for o in origins}
    assert set(by_decl) == {"casein", "milk"}
    assert by_decl["casein"]["batch_id"] == "RM-CASEIN"
    assert by_decl["casein"]["declaration_version"] == 1
    assert by_decl["milk"]["batch_id"] == "RM-MILK"
    # 每个原始来源保留各自路径
    assert by_decl["casein"]["paths"]
    assert by_decl["milk"]["paths"]


def test_declaration_versions_keep_raw_values(client):
    material(client, "RM-1", allergens=["casein"])
    client.post("/materials/RM-1/declarations",
                json={"allergens": ["casein", "milk"]})
    merge(client, "casein", "milk")
    m = client.get("/materials/RM-1").json()
    # 原始声明字段保持原值
    assert m["allergens"] == ["casein", "milk"]
    # 另附按当前并码解释的规范代码
    assert m["canonical_allergens"] == ["milk"]
    t = client.get("/batches/RM-1/trace").json()
    decls = {d["batch_id"]: d for d in t["basis"]["material_declarations"]}
    assert decls["RM-1"]["allergens"] == ["casein", "milk"]  # 依据保留原值


def test_pending_status_unaffected_by_merge(client):
    material(client, "RM-P", pending=True)
    merge(client, "casein", "milk")
    t = client.get("/batches/RM-P/trace").json()
    assert t["pending"] is True
    assert t["allergens"] == []


# ---------------------------------------------------------------- 标签快照不变

def test_issued_label_snapshot_keeps_premerge_codes(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-1"], ["OUT-1"])
    lab = client.post("/batches/OUT-1/labels", json={}).json()
    merge(client, "casein", "milk")
    frozen = client.get(f"/labels/{lab['label_id']}").json()
    # 快照保留签发时刻的旧代码结论，不随并码改写
    assert frozen["conclusion"]["allergens"] == ["casein"]
    assert "allergen_origins" not in frozen["conclusion"]
    # 实时结论按并码合并
    assert client.get("/batches/OUT-1/trace").json()["allergens"] == ["milk"]


# ---------------------------------------------------------------- 下游重算与事件

def test_merge_recomputes_equipment_residue_downstream(client):
    material(client, "RM-OLD", allergens=["wheat"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-OLD"], ["OUT-1"])
    material(client, "RM-NEW", allergens=["gluten"],
             declared_at="2026-01-02T06:00:00Z")
    processing(client, "P2", "EQ-1", "2026-01-02T08:00:00Z",
               "2026-01-02T09:00:00Z", ["RM-NEW"], ["OUT-2"])
    # 无有效清洗：OUT-2 继承 wheat 残留
    assert set(client.get("/batches/OUT-2/trace").json()["allergens"]) == {
        "gluten", "wheat"}
    merge(client, "wheat", "gluten")
    t = client.get("/batches/OUT-2/trace").json()
    assert t["allergens"] == ["gluten"]  # wheat 残留并入 gluten
    origins = {o["declared_code"] for o in t["allergen_origins"]["gluten"]}
    assert origins == {"wheat", "gluten"}  # 含设备残留来源


def test_merge_records_events_only_for_actual_changes(client):
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-B", allergens=["peanut"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-A"], ["OUT-A"])
    cursor = client.get("/changes/snapshot").json()["cursor"]
    merge(client, "casein", "milk")
    events = client.get(f"/changes/events?cursor={cursor}&limit=100").json()["events"]
    batches = {e["batch_id"] for e in events}
    # 仅引用 casein 的批次变化；RM-B(peanut) 无事件
    assert batches == {"RM-A", "OUT-A"}
    assert all(e["op"] == "allergen_code_merge" for e in events)
    assert events[0]["op_ref"] == {
        "old_code": "casein", "target_code": "milk", "reason": "并码原因"}
    # 事件按批次号排序，seq 全局递增
    assert [e["batch_id"] for e in events] == sorted(batches)
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    ev = events[0]
    assert ev["before"]["conclusion"]["allergens"] == ["casein"]
    assert ev["after"]["conclusion"]["allergens"] == ["milk"]


def test_idempotent_retry_produces_no_events(client):
    material(client, "RM-1", allergens=["casein"])
    merge(client, "casein", "milk")
    cursor = client.get("/changes/snapshot").json()["cursor"]
    merge(client, "casein", "milk", "重复")
    page = client.get(f"/changes/events?cursor={cursor}&limit=100").json()
    assert page["events"] == []


def test_rejected_merge_is_atomic_no_record_no_event(client):
    material(client, "RM-1", allergens=["casein"])
    merge(client, "casein", "milk")
    cursor = client.get("/changes/snapshot").json()["cursor"]
    r = merge(client, "casein", "other")  # 改指 -> 409
    assert r.status_code == 409
    page = client.get(f"/changes/events?cursor={cursor}&limit=100").json()
    assert page["events"] == []
    assert client.get("/allergen-codes/merges/casein").json()[
        "target_code"] == "milk"


def test_chained_merge_events_and_resolve(client):
    material(client, "RM-1", allergens=["a"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-1"], ["OUT-1"])
    merge(client, "a", "b")
    merge(client, "b", "c")
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["c"]
    origins = t["allergen_origins"]["c"]
    assert origins[0]["declared_code"] == "a"
    assert client.get("/allergen-codes/a/resolve").json()[
        "canonical_code"] == "c"


# ---------------------------------------------------------------- 登记/更正/预演按当前并码解释

def test_new_registration_interprets_alias_by_current_merges(client):
    merge(client, "casein", "milk")
    material(client, "RM-1", allergens=["casein", "milk"])
    t = client.get("/batches/RM-1/trace").json()
    assert t["allergens"] == ["milk"]
    paths = t["allergen_paths"]["milk"]
    # 同一批次声明的两个同义代码产生同一路径 -> 去重为一条
    assert len(paths) == 1
    origins = {(o["declared_code"], o["batch_id"])
               for o in t["allergen_origins"]["milk"]}
    assert origins == {("casein", "RM-1"), ("milk", "RM-1")}


def test_correction_preview_interprets_current_merges(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-1"], ["OUT-1"])
    merge(client, "casein", "milk")
    # 声明更正预演：更正后的声明仍含别名，按当前并码解释为规范代码
    pv = client.post("/materials/RM-1/declarations/preview",
                     json={"allergens": ["casein", "peanut"]}).json()
    out = next(b for b in pv["affected_batches"] if b["batch_id"] == "OUT-1")
    assert "milk" in out["after"]["conclusion"]["allergens"]
    assert "peanut" in out["after"]["conclusion"]["allergens"]
    assert "casein" not in out["after"]["conclusion"]["allergens"]
    # 预演不落库
    assert client.get("/materials/RM-1").json()["version"] == 1


def test_merge_invalidates_concurrent_declaration_preview(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1", "2026-01-01T08:00:00Z",
               "2026-01-01T09:00:00Z", ["RM-1"], ["OUT-1"])
    pv = client.post("/materials/RM-1/declarations/preview",
                     json={"allergens": ["peanut"]}).json()
    token = pv["preview_id"]
    # 并发：预演后发生并码提交
    assert merge(client, "casein", "milk").status_code == 201
    # 旧凭据提交必须拒绝（不得出现“按旧代码更正 + 新并码”的混合状态）
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409
    # 声明版本未被写入
    assert client.get("/materials/RM-1").json()["version"] == 1
    # 重新预演后可提交，且结论按当前并码解释
    pv2 = client.post("/materials/RM-1/declarations/preview",
                      json={"allergens": ["milk", "peanut"]}).json()
    ok = client.post(f"/previews/{pv2['preview_id']}/commit")
    assert ok.status_code == 200, ok.text
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk", "peanut"]


# ---------------------------------------------------------------- 快照与既有字段兼容

def test_snapshot_uses_canonical_codes_with_origins(client):
    material(client, "RM-1", allergens=["casein"])
    merge(client, "casein", "milk")
    snap = client.get("/changes/snapshot").json()
    row = next(b for b in snap["batches"] if b["batch_id"] == "RM-1")
    assert row["conclusion"]["allergens"] == ["milk"]
    assert row["conclusion"]["allergen_origins"]["milk"][0][
        "declared_code"] == "casein"
    # 依据保留原值
    assert row["basis"]["material_declarations"][0]["allergens"] == ["casein"]


def test_merge_does_not_affect_unrelated_batches_release(client):
    material(client, "RM-1", allergens=["casein"])
    merge(client, "casein", "milk")
    t = client.get("/batches/RM-1/trace").json()
    assert t["release"]["held"] is False
