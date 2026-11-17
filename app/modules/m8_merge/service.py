"""合并编排：创建合并会话、逐项裁定、生成合并批次、回读结果。

三条硬约束在这里落地：
1. 采信走只读预览，分支文件不被改写，也不落快照；
2. 主线和输入在会话创建时钉住，确认期间主线再变即拒绝旧会话；
3. 合并批次键由「输入摘要 + 裁定 + 规则集」确定，重试只复用不重复。
"""

from __future__ import annotations

import json
import sqlite3

from app import config, errors, manifest as manifest_mod, store
from app.modules.m1_intake import service as intake
from app.modules.m2_types import service as types
from app.modules.m4_mapping import service as mapping
from app.modules.m6_batches import guard, service as batch_service, window as window_mod
from app.modules.m8_merge import merge

FILE_ROLES = ("ancestor", "ours", "theirs", "refs")

DEFAULT_FILES = {
    "ancestor": config.BASE_OLD,
    "ours": config.DEFAULT_OLD,
    "theirs": config.DEFAULT_NEW,
    "refs": config.DEFAULT_REFS,
}

MAX_REASON = 500
VALID_CHOICES = ("ours", "theirs")


def _now() -> str:
    return config.now_utc().isoformat()


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(name: str) -> str:
    return manifest_mod.sha256_file(config.resolve_sample(name))


def _files_of(session: dict) -> dict:
    return {role: session[f"{role}_file"] for role in FILE_ROLES}


def _prepared(name: str) -> dict:
    return types.prepare(intake.preview(name))


def _version_label_of(snapshot: dict) -> str:
    return snapshot.get("version_label") or snapshot.get("source_path") or ""


# ---------- 会话键与批次键 ----------

def _session_key(files: dict, digests: dict, head_version_seq: int) -> str:
    payload = {
        "files": files,
        "digests": digests,
        "head_version_seq": head_version_seq,
        "ruleset": config.RULESET_VERSION,
    }
    return manifest_mod.sha256_bytes(_canonical(payload).encode("utf-8"))


def rulings_digest(rulings: dict[str, dict]) -> str:
    payload = {
        key: {"choice": value["choice"], "reason": value["reason"]}
        for key, value in sorted(rulings.items())
    }
    return manifest_mod.sha256_bytes(_canonical(payload).encode("utf-8"))


def batch_key_for(session: dict, rulings: dict[str, dict]) -> str:
    window = {
        "from": session.get("effective_from"),
        "to": session.get("effective_to"),
    }
    params = {
        "kind": "merge",
        "merge_id": session["merge_id"],
        "rulings": rulings_digest(rulings),
        "window": f"{window['from']}..{window['to'] or ''}",
        "version_label": session.get("version_label"),
    }
    return manifest_mod.batch_key(session["digests"], params, config.RULESET_VERSION)


# ---------- 只读分析 ----------

def _analyze(session: dict, rulings: dict[str, dict] | None = None) -> dict:
    ancestor = _prepared(session["ancestor_file"])
    ours = _prepared(session["ours_file"])
    theirs = _prepared(session["theirs_file"])
    rules = dict(rulings if rulings is not None else {})
    analysis = merge.analyze(
        ancestor["fields"], ours["fields"], theirs["fields"],
        rules, version_label=session["version_label"],
    )
    analysis["snapshots"] = {"ancestor": ancestor, "ours": ours, "theirs": theirs}
    return analysis


def _side_diff(ancestor_fields: list[dict], side_fields: list[dict]) -> dict:
    base = {field["field_id"]: field for field in ancestor_fields}
    side = {field["field_id"]: field for field in side_fields}
    rows: list[dict] = []
    counts = {"unchanged": 0, "added": 0, "removed": 0, "changed": 0}
    for fid in sorted(set(base) | set(side)):
        before = base.get(fid)
        after = side.get(fid)
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        elif merge.field_signature(before) == merge.field_signature(after):
            status = "unchanged"
        else:
            status = "changed"
        counts[status] += 1
        rows.append({
            "field_id": fid,
            "table": (after or before or {}).get("table"),
            "name": (after or before or {}).get("name"),
            "status": status,
            "before": merge.summarize(before),
            "after": merge.summarize(after),
        })
    rows.sort(key=lambda row: ((row["table"] or "").lower(),
                               (row["name"] or "").lower(), row["field_id"]))
    return {"counts": counts, "rows": rows}


def _merged_snapshot(session: dict, analysis: dict) -> dict:
    fields = analysis["merged_fields"]
    version_label = session["version_label"]
    tables = sorted({field["table"] for field in fields})
    snapshot = {
        "schema_id": intake.schema_id_for(version_label, fields),
        "version_label": version_label,
        "source_path": f"merge:{session['merge_id']}",
        "source_sha256": rulings_digest(store.load_merge_rulings(session["merge_id"])),
        "encoding": "utf-8",
        "bom": False,
        "fallback": False,
        "bytes": 0,
        "line_count": len(fields),
        "created_at": _now(),
        "fields": fields,
        "diagnostics": [],
        "tables": tables,
        "stats": {"column_lines": len(fields), "parsed": len(fields),
                  "parse_failures": 0, "accepted": len(fields), "blocked": 0,
                  "warnings": 0, "tables": len(tables)},
        "field_count": len(fields),
        "blocked_count": 0,
    }
    return snapshot


# ---------- 会话读取与守卫 ----------

def _load(merge_id: str) -> dict:
    session = store.load_merge_session(merge_id)
    if session is None:
        raise errors.NotFound("没有这个合并会话", detail={"merge_id": merge_id})
    return session


def _assert_current(session: dict) -> None:
    """主线和输入都钉在会话创建那一刻，任何一处变了都拒绝旧会话。"""
    current = store.current_version_seq()
    if current != int(session["head_version_seq"]):
        raise errors.StaleVersion(
            f"确认期间主线已从序号 {session['head_version_seq']} 变为 {current}，请重建合并会话",
            detail={"reason": "MERGE_STALE", "scope": "mainline",
                    "head_version_seq": int(session["head_version_seq"]),
                    "current_version_seq": current},
        )
    for role, name in _files_of(session).items():
        try:
            actual = _digest(name)
        except (errors.NotFound, errors.BadInput):
            raise errors.StaleVersion(
                f"合并输入 {role} 已不可用，请重建合并会话",
                detail={"reason": "MERGE_STALE", "scope": role, "file": name},
            )
        if actual != session["digests"].get(role):
            raise errors.StaleVersion(
                f"合并输入 {role}（{name}）在确认期间被改动，请重建合并会话",
                detail={"reason": "MERGE_STALE", "scope": role, "file": name},
            )


def _status_of(session: dict, analysis: dict) -> str:
    if session.get("run_id") and store.run_by_id(session["run_id"]):
        return "merged"
    if not analysis["items"]:
        return "ready"
    return "open"


def _touch(session: dict, analysis: dict, status: str | None = None) -> dict:
    session["status"] = status or _status_of(session, analysis)
    session["updated_at"] = _now()
    store.save_merge_session(session)
    return session


# ---------- 对外动作 ----------

def _resolve_files(body: dict) -> dict:
    files = {}
    for role in FILE_ROLES:
        key = f"{role}_file"
        value = body.get(key)
        files[role] = str(value).strip() if value else DEFAULT_FILES[role]
    return files


def _default_version_label(ours: dict, theirs: dict) -> str:
    label = theirs.get("version_label") or ours.get("version_label") or "merge"
    return f"{label}+merge"


def create(body: dict | None = None) -> dict:
    body = body or {}
    files = _resolve_files(body)
    guard.check_stale(body.get("base_version_seq"), store.current_version_seq())

    ours = _prepared(files["ours"])
    ancestor = _prepared(files["ancestor"])
    theirs = _prepared(files["theirs"])
    mapping.load_refs(files["refs"])  # 先失败先报，别拖到出批

    digests = {role: _digest(name) for role, name in files.items()}
    head_version_seq = store.current_version_seq()
    key = _session_key(files, digests, head_version_seq)
    merge_id = "mg-" + key[:12]

    existing = store.load_merge_session(merge_id)
    if existing is not None:
        view = present(existing)
        view["reused"] = True
        return view

    version_label = str(body.get("version_label") or "").strip() or _default_version_label(
        ours, theirs)
    # 先按版本标签归一窗口，非法输入在创建时就挡住
    window_mod.normalize(body.get("effective_from"), body.get("effective_to"), version_label)

    created_at = _now()
    session = {
        "merge_id": merge_id,
        "session_key": key,
        "status": "open",
        "created_at": created_at,
        "updated_at": created_at,
        "head_version_seq": head_version_seq,
        "head_run_id": (store.latest_run() or {}).get("run_id"),
        "version_label": version_label,
        "effective_from": body.get("effective_from"),
        "effective_to": body.get("effective_to"),
        "ancestor_file": files["ancestor"],
        "ours_file": files["ours"],
        "theirs_file": files["theirs"],
        "refs_file": files["refs"],
        "digests": digests,
        "ancestor_schema_id": ancestor["schema_id"],
        "ours_schema_id": ours["schema_id"],
        "theirs_schema_id": theirs["schema_id"],
        "ruleset": config.RULESET_VERSION,
        "run_id": None,
        "batch_key": None,
    }
    analysis = _analyze(session, {})
    _touch(session, analysis)
    view = present(session)
    view["reused"] = False
    return view


def submit_rulings(merge_id: str, body: dict | None = None) -> dict:
    body = body or {}
    session = _load(merge_id)
    if session.get("run_id") and store.run_by_id(session["run_id"]):
        raise errors.Conflict(
            "合并批次已生成，不能再改裁定；需要新结果请新建会话",
            detail={"reason": "MERGE_BATCH_DONE", "merge_id": merge_id,
                    "run_id": session["run_id"]},
        )
    _assert_current(session)

    raw = body.get("rulings")
    if not isinstance(raw, list) or not raw:
        raise errors.BadInput("需要给出 rulings 列表", detail={"field": "rulings"})
    if len(raw) > 200:
        raise errors.BadInput("一次最多提交 200 条裁定", detail={"count": len(raw)})

    submitted: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise errors.BadInput("裁定项必须是对象", detail={"entry": repr(entry)})
        item_key = str(entry.get("item_key") or "").strip()
        choice = str(entry.get("choice") or "").strip().lower()
        reason = str(entry.get("reason") or "").strip()
        if not item_key:
            raise errors.BadInput("裁定缺少 item_key", detail={"entry": entry})
        if item_key in submitted:
            raise errors.BadInput("同一裁定了出现两次", detail={"item_key": item_key})
        if choice not in VALID_CHOICES:
            raise errors.BadInput("裁定只能选 ours 或 theirs",
                                  detail={"item_key": item_key, "choice": choice})
        if not reason:
            raise errors.BadInput("裁定必须写明理由", detail={"item_key": item_key})
        if len(reason) > MAX_REASON:
            raise errors.BadInput("理由过长", detail={"item_key": item_key,
                                                     "limit": MAX_REASON})
        submitted[item_key] = {"choice": choice, "reason": reason}

    existing = store.load_merge_rulings(merge_id)
    prospective = dict(existing)
    for item_key, value in submitted.items():
        prospective[item_key] = dict(value, decided_at=_now())

    analysis = _analyze(session, prospective)
    known = {item["item_key"]: item for item in analysis["all_items"]}
    unknown = sorted(set(submitted) - set(known))
    if unknown:
        raise errors.BadInput("裁定指向不存在的冲突项", detail={"item_keys": unknown})
    for item_key, value in submitted.items():
        choices = known[item_key].get("choices") or []
        if value["choice"] not in choices:
            raise errors.BadInput("该侧没有可选候选", detail={
                "item_key": item_key, "choice": value["choice"], "choices": choices})

    for item_key, value in submitted.items():
        store.save_merge_ruling(merge_id, item_key, value["choice"], value["reason"], _now())

    rulings = store.load_merge_rulings(merge_id)
    analysis = _analyze(session, rulings)
    _touch(session, analysis)
    return present(session, analysis=analysis)


def generate_batch(merge_id: str, body: dict | None = None) -> dict:
    body = body or {}
    session = _load(merge_id)
    rulings = store.load_merge_rulings(merge_id)

    # 1) 已经出过批：直接复用，不因版本号前进而误判陈旧
    if session.get("run_id"):
        run = store.run_by_id(session["run_id"])
        if run is not None:
            view = present(session)
            view["reused"] = True
            return view

    key = batch_key_for(session, rulings)

    # 2) 出批中断：批次已落库但会话没来得及收尾，按批次键恢复
    orphan = store.find_run_by_key(key)
    if orphan is not None:
        _finish(session, orphan, key, rulings,
                analysis=_analyze(session, rulings))
        view = present(session)
        view["reused"] = True
        view["recovered"] = True
        return view

    # 3) 确认期间主线变了就拒绝，别拿旧会话覆盖新主线
    _assert_current(session)
    guard.check_stale(body.get("base_version_seq"), store.current_version_seq())

    analysis = _analyze(session, rulings)
    if analysis["items"]:
        raise errors.Conflict(
            "还有冲突项未裁定，不能生成合并批次",
            detail={"reason": "MERGE_RULING_MISSING",
                    "pending": [item["item_key"] for item in analysis["items"]]},
        )
    # 失效裁定只提示不阻断：它不改变合并结果，也没有删除接口能清掉

    snapshots = analysis["snapshots"]
    merged = _merged_snapshot(session, analysis)
    merged_prepared = types.prepare(merged)
    window = window_mod.normalize(session.get("effective_from"), session.get("effective_to"),
                                  session["version_label"])
    refs = mapping.load_refs(session["refs_file"])

    params = {
        "kind": "merge",
        "merge_id": merge_id,
        "rulings": rulings_digest(rulings),
        "decisions": {key_: value["choice"] for key_, value in sorted(rulings.items())},
    }
    names = {
        "ancestor": session["ancestor_file"],
        "ours": session["ours_file"],
        "theirs": session["theirs_file"],
        "refs": session["refs_file"],
        "merged": merged["schema_id"],
    }
    extra = {
        "kind": "merge",
        "merge_id": merge_id,
        "merge_files": _files_of(session),
        "merge_digests": session["digests"],
        "merge_rulings": [
            {"item_key": key_, "choice": value["choice"], "reason": value["reason"],
             "decided_at": value["decided_at"]}
            for key_, value in sorted(rulings.items())
        ],
        "merge_applied": analysis["applied"],
        "merge_auto": analysis["auto"],
        "merge_dropped": analysis["dropped"],
        "merge_deleted": analysis["deleted"],
        "merge_counts": analysis["counts"],
        "merge_ancestor_schema_id": snapshots["ancestor"]["schema_id"],
        "merge_theirs_schema_id": snapshots["theirs"]["schema_id"],
    }

    run = batch_service.build_run(
        old_snapshot=snapshots["ours"],
        new_snapshot=merged_prepared,
        base_snapshot=snapshots["ancestor"],
        refs=refs,
        window=window,
        digests=session["digests"],
        names=names,
        params=params,
        key=key,
        extra=extra,
    )

    store.save_snapshot(merged)
    try:
        store.save_run(run)
    except sqlite3.IntegrityError:
        # 并发重复提交：同一批次键只留一份
        run = store.find_run_by_key(key)
        if run is None:
            raise

    _finish(session, run, key, rulings, analysis=analysis)
    view = present(session)
    view["reused"] = False
    return view


def _finish(session: dict, run: dict, key: str, rulings: dict[str, dict],
            analysis: dict | None = None) -> None:
    session["run_id"] = run["run_id"]
    session["batch_key"] = key
    session["status"] = "merged"
    session["updated_at"] = _now()
    if analysis is not None:
        # 出批时的合并字段钉进会话，事后文件再动也回读到当时的结果
        session["merged_fields"] = analysis["merged_fields"]
        session["merged_counts"] = analysis["counts"]
        session["merged_schema_id"] = intake.schema_id_for(session["version_label"],
                                                           analysis["merged_fields"])
    store.save_merge_session(session)


def _baseline_seq(session: dict) -> int:
    """会话认定的主线基准：未出批时是创建序号，已出批时是它自己的批次序号。"""
    run = store.run_by_id(session["run_id"]) if session.get("run_id") else None
    if run is not None:
        return int(run["version_seq"])
    return int(session["head_version_seq"])


def _staleness(session: dict) -> list[dict]:
    """回读用：列出会话之后变过的主线与输入，不抛异常。"""
    notes: list[dict] = []
    baseline = _baseline_seq(session)
    current = store.current_version_seq()
    if current != baseline:
        notes.append({"scope": "mainline", "reason": "MERGE_STALE",
                      "baseline_version_seq": baseline,
                      "current_version_seq": current})
    for role, name in _files_of(session).items():
        try:
            actual = _digest(name)
        except (errors.NotFound, errors.BadInput):
            notes.append({"scope": role, "reason": "MERGE_STALE", "file": name,
                          "detail": "输入已不可用"})
            continue
        if actual != session["digests"].get(role):
            notes.append({"scope": role, "reason": "MERGE_STALE", "file": name,
                          "detail": "输入摘要已变"})
    return notes


# ---------- 回读 ----------

def _run_result(run: dict | None) -> dict | None:
    if not run:
        return None
    impact = run.get("impact") or {}
    return {
        "run_id": run["run_id"],
        "version_seq": run["version_seq"],
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
        "old_schema_id": run.get("old_schema_id"),
        "new_schema_id": run.get("new_schema_id"),
        "effective_from": run.get("effective_from"),
        "effective_to": run.get("effective_to"),
        "buckets": run.get("buckets") or {},
        "verdicts": run.get("verdicts") or {},
        "counts": run.get("counts") or {},
        "breaking_fields": (run.get("counts") or {}).get("breaking", 0),
        "ledger": run.get("ledger") or {},
        "impact": impact.get("summary") or {},
        "impact_rows": (impact.get("impact") or {}).get("rows") or [],
        "warnings": run.get("warnings") or [],
    }


def present(session: dict, analysis: dict | None = None) -> dict:
    """回读视图：祖先、双方差异、冲突、裁定与合并结果都在这里。"""
    rulings = store.load_merge_rulings(session["merge_id"])
    analysis = analysis or _analyze(session, rulings)
    snapshots = analysis["snapshots"]
    run = store.run_by_id(session["run_id"]) if session.get("run_id") else None

    # 已出批的会话以当时钉下的合并字段为准，事后改动文件不影响回读
    pinned = session.get("merged_fields")
    merged_fields = pinned if pinned is not None else analysis["merged_fields"]
    merged_counts = session.get("merged_counts") or analysis["counts"]
    merged_schema = session.get("merged_schema_id") or intake.schema_id_for(
        session["version_label"], merged_fields)

    return {
        "merge_id": session["merge_id"],
        "status": _status_of(session, analysis),
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "head_version_seq": int(session["head_version_seq"]),
        "head_run_id": session.get("head_run_id"),
        "ruleset": session.get("ruleset"),
        "version_label": session["version_label"],
        "effective_from": session.get("effective_from"),
        "effective_to": session.get("effective_to"),
        "files": _files_of(session),
        "digests": session["digests"],
        "staleness": _staleness(session),
        "ancestor": _side_summary(snapshots["ancestor"]),
        "ours": _side_summary(snapshots["ours"]),
        "theirs": _side_summary(snapshots["theirs"]),
        "side_diffs": {
            "ours": _side_diff(snapshots["ancestor"]["fields"], snapshots["ours"]["fields"]),
            "theirs": _side_diff(snapshots["ancestor"]["fields"],
                                 snapshots["theirs"]["fields"]),
        },
        "classification": {
            "auto": analysis["auto"],
            "applied": analysis["applied"],
            "dropped": analysis["dropped"],
            "deleted": analysis["deleted"],
        },
        "conflicts": {
            "fields": analysis["field_conflicts"],
            "names": analysis["name_conflicts"],
        },
        "items": analysis["items"],
        "pending": analysis["pending"],
        "rulings": rulings,
        "orphan_rulings": analysis["orphan_rulings"],
        "counts": merged_counts,
        "merged": {
            "version_label": session["version_label"],
            "schema_id": merged_schema,
            "field_count": len(merged_fields),
            "fields": merged_fields,
        },
        "batch_key": session.get("batch_key"),
        "result": _run_result(run),
    }


def _side_summary(snapshot: dict) -> dict:
    return {
        "file": snapshot.get("source_path"),
        "version_label": snapshot.get("version_label"),
        "schema_id": snapshot.get("schema_id"),
        "encoding": snapshot.get("encoding"),
        "field_count": snapshot.get("field_count"),
        "blocked_count": snapshot.get("blocked_count"),
        "tables": snapshot.get("tables") or [],
        "stats": snapshot.get("stats") or {},
        "fields": snapshot["fields"],
    }


def get(merge_id: str) -> dict:
    return present(_load(merge_id))


def listing(limit: int = 30) -> list[dict]:
    rows = []
    for session in store.list_merge_sessions(limit):
        rulings = store.load_merge_rulings(session["merge_id"])
        run = store.run_by_id(session["run_id"]) if session.get("run_id") else None
        rows.append({
            "merge_id": session["merge_id"],
            "status": session["status"],
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "head_version_seq": session["head_version_seq"],
            "version_label": session["version_label"],
            "files": _files_of(session),
            "rulings": len(rulings),
            "run_id": session.get("run_id"),
            "verdicts": (run or {}).get("verdicts") or {},
        })
    return rows
