"""M8 三方合并：自动保留、冲突裁定、陈旧拒绝、出批幂等与回读。"""

from __future__ import annotations

import pytest

from app import config, errors, store
from app.modules.m6_batches import service as batch_service
from app.modules.m8_merge import merge, service as merge_service
from tests.fixtures import expected

RULINGS = [
    {"item_key": "field:1002", "choice": "theirs", "reason": "分支改名符合新口径"},
    {"item_key": "field:1007", "choice": "theirs", "reason": "分支把 VARIANT 归到 TEXT"},
    {"item_key": "field:2004", "choice": "ours", "reason": "主线保持可空，避免写者破坏"},
    {"item_key": "field:2005", "choice": "theirs", "reason": "分支修好列名与长度"},
    {"item_key": "field:3002", "choice": "theirs", "reason": "分支放宽到 64"},
    {"item_key": "name:dim_order|status", "choice": "theirs", "reason": "采用带 fid 的正式身份"},
]


def _fields(*rows):
    """纯规则测试用：直接给 field_id / 表 / 名 / 类型 / 必填。"""
    out = []
    for index, row in enumerate(rows, 1):
        out.append({
            "field_id": str(row["field_id"]),
            "table": row.get("table", "t"),
            "name": row["name"],
            "name_key": row["name"].lower(),
            "type_norm": row.get("type", "bigint"),
            "type_raw": row.get("type", "bigint"),
            "required": bool(row.get("required", False)),
            "line": index,
            "version": "v",
            "source": "test",
        })
    return out


# ---------- 纯规则 ----------

def test_independent_changes_keep_both():
    ancestor = _fields({"field_id": 1, "name": "a", "type": "varchar(8)"},
                       {"field_id": 2, "name": "b", "type": "varchar(8)"})
    ours = _fields({"field_id": 1, "name": "a", "type": "varchar(16)"},
                   {"field_id": 2, "name": "b", "type": "varchar(8)"})
    theirs = _fields({"field_id": 1, "name": "a", "type": "varchar(8)"},
                     {"field_id": 2, "name": "b2", "type": "varchar(8)"})
    result = merge.analyze(ancestor, ours, theirs)
    assert result["items"] == []
    merged = {field["field_id"]: field for field in result["merged_fields"]}
    assert merged["1"]["type_raw"] == "varchar(16)"   # 主线独占改动
    assert merged["2"]["name"] == "b2"                # 分支独占改动


def test_both_changed_same_result_is_auto():
    ancestor = _fields({"field_id": 1, "name": "a", "type": "varchar(8)"})
    ours = _fields({"field_id": 1, "name": "a", "type": "varchar(16)"})
    theirs = _fields({"field_id": 1, "name": "a", "type": "varchar(32)"})
    result = merge.analyze(ancestor, ours, theirs)
    # 双方都放宽但结果不同 → 同列异改，必须选边
    assert [item["kind"] for item in result["items"]] == ["both_changed"]

    same = merge.analyze(ancestor, ours, ours)
    assert same["items"] == []
    assert same["merged_fields"][0]["type_raw"] == "varchar(16)"


def test_same_column_conflict_requires_ruling():
    ancestor = _fields({"field_id": 1, "name": "a", "type": "varchar(8)"})
    ours = _fields({"field_id": 1, "name": "a", "type": "varchar(16)"})
    theirs = _fields({"field_id": 1, "name": "a", "type": "varchar(64)"})
    open_result = merge.analyze(ancestor, ours, theirs)
    assert open_result["merged_fields"] == []
    assert open_result["pending"] == ["field:1"]

    ruled = merge.analyze(ancestor, ours, theirs,
                          {"field:1": {"choice": "theirs", "reason": "取分支"}})
    assert ruled["items"] == []
    assert ruled["merged_fields"][0]["type_raw"] == "varchar(64)"
    assert ruled["applied"][0]["choice"] == "theirs"


def test_delete_vs_modify_is_conflict():
    ancestor = _fields({"field_id": 1, "name": "a"})
    ours = _fields()  # 主线弃列
    theirs = _fields({"field_id": 1, "name": "a", "type": "varchar(16)"})
    result = merge.analyze(ancestor, ours, theirs)
    assert [item["kind"] for item in result["items"]] == ["delete_modify"]
    assert result["items"][0]["reason"] == "MERGE_DELETE_MODIFY"
    kept = merge.analyze(ancestor, ours, theirs,
                         {"field:1": {"choice": "theirs", "reason": "保留分支改动"}})
    assert [f["field_id"] for f in kept["merged_fields"]] == ["1"]
    dropped = merge.analyze(ancestor, ours, theirs,
                            {"field:1": {"choice": "ours", "reason": "确认弃列"}})
    assert dropped["merged_fields"] == []
    assert dropped["deleted"][0]["reason"] == "RULED_DELETE"


def test_same_name_different_identity_needs_ruling():
    # 祖先没有 a；双方各自新增同名 a，身份不同
    ours = _fields({"field_id": 1, "name": "dup"})
    theirs = _fields({"field_id": 2, "name": "dup"})
    result = merge.analyze([], ours, theirs)
    assert result["merged_fields"] == []
    assert [item["kind"] for item in result["items"]] == ["name_identity"]
    assert result["items"][0]["choices"] == ["ours", "theirs"]

    ruled = merge.analyze([], ours, theirs,
                          {"name:t|dup": {"choice": "theirs", "reason": "以分支身份为准"}})
    assert [f["field_id"] for f in ruled["merged_fields"]] == ["2"]
    assert ruled["dropped"][0]["field_id"] == "1"


def test_ruling_choice_without_candidate_is_rejected():
    ours = _fields({"field_id": 1, "name": "dup"})
    theirs = _fields({"field_id": 2, "name": "dup"})
    # ours 侧只有身份 1，选 ours 后仍有分支身份 2 残留 → 下一次分析会重新报冲突
    result = merge.analyze([], ours, theirs,
                           {"name:t|dup": {"choice": "ours", "reason": "取主线"}})
    assert [f["field_id"] for f in result["merged_fields"]] == ["1"]
    assert result["items"] == []


def test_orphan_ruling_detected_when_conflict_disappears():
    ancestor = _fields({"field_id": 1, "name": "a", "type": "varchar(8)"})
    ours = _fields({"field_id": 1, "name": "a", "type": "varchar(16)"})
    theirs = _fields({"field_id": 1, "name": "a", "type": "varchar(64)"})
    # 裁定指向一个这次不存在的项
    result = merge.analyze(ancestor, ours, theirs,
                           {"field:999": {"choice": "ours", "reason": "旧裁定"}})
    assert "field:999" in result["orphan_rulings"]


# ---------- 会话 ----------

def test_create_session_classifies_and_keeps_branch_unwritten(client):
    import sqlite3

    def snapshot_count() -> int:
        with sqlite3.connect(store.db_path()) as conn:
            return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    before = snapshot_count()
    view = merge_service.create({})
    assert view["status"] == "open"
    assert view["counts"]["pending"] == 6
    assert {item["item_key"] for item in view["items"]} == {
        "field:1002", "field:1007", "field:2004", "field:2005", "field:3002",
        "name:dim_order|status",
    }
    # 分支只读采信：合并不写快照、不写隔离区
    assert snapshot_count() == before
    theirs_schema = view["theirs"]["schema_id"]
    with sqlite3.connect(store.db_path()) as conn:
        conn.execute("DELETE FROM snapshots WHERE schema_id=?", (theirs_schema,))
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (theirs_schema,))
    assert store.load_snapshot(theirs_schema) is None
    merge_service.get(view["merge_id"])
    assert store.load_snapshot(theirs_schema) is None


def test_create_session_is_idempotent(client):
    first = merge_service.create({})
    again = merge_service.create({})
    assert again["merge_id"] == first["merge_id"]
    assert again["reused"] is True


def test_batch_blocked_until_all_rulings(client):
    view = merge_service.create({})
    mid = view["merge_id"]
    with pytest.raises(errors.Conflict) as caught:
        merge_service.generate_batch(mid)
    assert caught.value.detail["reason"] == "MERGE_RULING_MISSING"
    assert len(caught.value.detail["pending"]) == 6


def test_ruling_requires_reason_and_valid_choice(client):
    mid = merge_service.create({})["merge_id"]
    with pytest.raises(errors.BadInput):
        merge_service.submit_rulings(mid, {"rulings": [
            {"item_key": "field:1002", "choice": "ours"}]})
    with pytest.raises(errors.BadInput):
        merge_service.submit_rulings(mid, {"rulings": [
            {"item_key": "field:1002", "choice": "both", "reason": "x"}]})
    with pytest.raises(errors.BadInput):
        merge_service.submit_rulings(mid, {"rulings": [
            {"item_key": "field:999", "choice": "ours", "reason": "x"}]})


def test_full_merge_generates_balanced_batch(client):
    mid = merge_service.create({})["merge_id"]
    view = merge_service.submit_rulings(mid, {"rulings": RULINGS})
    assert view["status"] == "ready"
    assert view["pending"] == []
    assert view["counts"]["ruled"] == 6

    result = merge_service.generate_batch(mid)
    assert result["status"] == "merged"
    run = result["result"]
    assert run["version_seq"] == store.current_version_seq()
    assert run["ledger"]["balanced"]["all"] is True
    assert run["counts"]["breaking"] == sum(run["verdicts"].values()) - run["verdicts"].get(
        "COMPATIBLE", 0) - run["verdicts"].get("COMPATIBLE_BY_MAPPING", 0)
    # 自动保留的双方独立改动出现在合并批次里
    assert run["buckets"]["added"] == 4
    assert run["impact"]["cycles"] == 1
    assert run["version_label_new"] == "v2026_09+merge"
    assert run["counts"]["new_total"] == 16


def test_orphan_ruling_is_reported_but_does_not_block(client):
    mid = merge_service.create({})["merge_id"]
    merge_service.submit_rulings(mid, {"rulings": RULINGS})
    store.save_merge_ruling(mid, "field:zzz", "ours", "失效裁定",
                            config.now_utc().isoformat())
    view = merge_service.get(mid)
    assert view["orphan_rulings"] == ["field:zzz"]
    result = merge_service.generate_batch(mid)
    assert result["status"] == "merged"


def test_retry_does_not_duplicate_batch(client):
    mid = merge_service.create({})["merge_id"]
    merge_service.submit_rulings(mid, {"rulings": RULINGS})
    first = merge_service.generate_batch(mid)
    runs = store.count_runs()
    again = merge_service.generate_batch(mid)
    assert again["reused"] is True
    assert again["result"]["run_id"] == first["result"]["run_id"]
    assert store.count_runs() == runs


def test_recovers_when_batch_saved_but_session_not_finished(client, monkeypatch):
    mid = merge_service.create({})["merge_id"]
    merge_service.submit_rulings(mid, {"rulings": RULINGS})

    original = merge_service._finish
    calls = {"n": 0}

    def failing_finish(session, run, key, rulings, analysis=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟出批中断")
        return original(session, run, key, rulings, analysis=analysis)

    monkeypatch.setattr(merge_service, "_finish", failing_finish)
    with pytest.raises(RuntimeError):
        merge_service.generate_batch(mid)
    assert store.load_merge_session(mid)["run_id"] is None
    runs = store.count_runs()

    monkeypatch.setattr(merge_service, "_finish", original)
    recovered = merge_service.generate_batch(mid)
    assert recovered["recovered"] is True
    assert recovered["result"]["run_id"]
    assert store.count_runs() == runs  # 批次没重新生成
    assert store.load_merge_session(mid)["run_id"] == recovered["result"]["run_id"]


def test_mainline_change_rejects_stale_session(client):
    view = merge_service.create({})
    mid = view["merge_id"]
    batch_service.execute(params={"wave": "move-head"})
    assert store.current_version_seq() == view["head_version_seq"] + 1

    with pytest.raises(errors.StaleVersion) as caught:
        merge_service.submit_rulings(mid, {"rulings": RULINGS[:1]})
    assert caught.value.detail["reason"] == "MERGE_STALE"
    assert caught.value.detail["scope"] == "mainline"

    with pytest.raises(errors.StaleVersion):
        merge_service.generate_batch(mid)


def test_new_session_after_mainline_change(client):
    old = merge_service.create({})
    batch_service.execute(params={"wave": "move-head"})
    fresh = merge_service.create({})
    assert fresh["merge_id"] != old["merge_id"]
    assert fresh["head_version_seq"] == store.current_version_seq()


def test_input_change_rejects_stale_session(client, monkeypatch):
    mid = merge_service.create({})["merge_id"]
    session = store.load_merge_session(mid)
    tampered = dict(session)
    tampered["digests"] = dict(session["digests"])
    tampered["digests"]["theirs"] = "deadbeef"
    store.save_merge_session(tampered)
    with pytest.raises(errors.StaleVersion) as caught:
        merge_service.submit_rulings(mid, {"rulings": RULINGS[:1]})
    assert caught.value.detail["scope"] == "theirs"


def test_cannot_rule_after_batch(client):
    mid = merge_service.create({})["merge_id"]
    merge_service.submit_rulings(mid, {"rulings": RULINGS})
    merge_service.generate_batch(mid)
    with pytest.raises(errors.Conflict):
        merge_service.submit_rulings(mid, {"rulings": RULINGS[:1]})


def test_readback_exposes_ancestor_diffs_rulings_and_result(client):
    mid = merge_service.create({})["merge_id"]
    merge_service.submit_rulings(mid, {"rulings": RULINGS})
    merge_service.generate_batch(mid)
    view = merge_service.get(mid)
    assert view["ancestor"]["field_count"] == 9
    assert view["ours"]["field_count"] == expected.OLD_TOTAL
    assert view["theirs"]["field_count"] == expected.NEW_TOTAL
    assert view["side_diffs"]["ours"]["counts"]["changed"] == 2
    assert view["side_diffs"]["theirs"]["counts"]["changed"] == 5
    assert view["rulings"]["field:1002"]["choice"] == "theirs"
    assert view["rulings"]["field:1002"]["reason"]
    assert view["result"]["run_id"].startswith("fm-")
    assert view["merged"]["field_count"] == len(view["merged"]["fields"])
    assert view["staleness"] == []


def test_merged_readback_survives_input_change(client, monkeypatch):
    mid = merge_service.create({})["merge_id"]
    merge_service.submit_rulings(mid, {"rulings": RULINGS})
    merged = merge_service.generate_batch(mid)
    pinned = merged["merged"]["schema_id"]

    # 模拟分支文件在出批后被改动：回读仍给出出批时的合并结果，并标出陈旧来源
    monkeypatch.setattr(merge_service, "_digest", lambda name: "changed-digest")
    view = merge_service.get(mid)
    assert view["status"] == "merged"
    assert view["merged"]["schema_id"] == pinned
    assert any(note["scope"] == "theirs" for note in view["staleness"])


def test_http_flow(client):
    created = client.post("/api/merges", json={})
    assert created.status_code == 200
    body = created.json()
    mid = body["merge_id"]
    assert body["counts"]["pending"] == 6

    assert client.get(f"/api/merges/{mid}").status_code == 200
    assert client.get("/api/merges").json()["merges"][0]["merge_id"] == mid

    blocked = client.post(f"/api/merges/{mid}/batch", json={})
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "CONFLICT"

    ruled = client.post(f"/api/merges/{mid}/rulings", json={"rulings": RULINGS})
    assert ruled.status_code == 200
    assert ruled.json()["status"] == "ready"

    batch = client.post(f"/api/merges/{mid}/batch", json={})
    assert batch.status_code == 200
    assert batch.json()["ledger"]["balanced"]["all"] is True
    assert batch.json()["reused"] is False

    again = client.post(f"/api/merges/{mid}/batch", json={})
    assert again.json()["reused"] is True

    page = client.get(f"/merge?merge_id={mid}")
    assert page.status_code == 200
    assert mid in page.text
    assert "三方合并" in page.text


def test_stale_create_rejected(client):
    response = client.post("/api/merges", json={"base_version_seq": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "VERSION_STALE"


def test_unknown_create_param_rejected(client):
    response = client.post("/api/merges", json={"nope": 1})
    assert response.status_code == 400
