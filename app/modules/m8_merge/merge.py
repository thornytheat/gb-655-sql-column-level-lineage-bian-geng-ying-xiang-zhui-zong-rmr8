"""三方合并：以共同祖先识别双方独立改动，冲突留待逐项裁定。

身份只看 field_id，与兼容判定同一口径：
- 一方改了、另一方没动 → 自动保留改动；
- 双方改成一样 → 自动保留；
- 双方改成不一样 → 同列异改，逐项选边；
- 一方弃列、另一方改列 → 弃列与改列相撞，逐项选边；
- 合并后同表同列名落到不同身份 → 同名异身份，逐项选边。

这里只做纯计算：不碰存储、不写文件、不改原分支。裁定由调用方传入。
"""

from __future__ import annotations

FIELD_PREFIX = "field:"
NAME_PREFIX = "name:"

UNRESOLVED = object()

KEPT_ACTIONS = ("kept_ours", "kept_theirs", "kept_both")


def field_signature(field: dict | None) -> tuple | None:
    """参与比较的四元组；类型用归一写法，避免同义写法被当成改动。"""
    if field is None:
        return None
    return (
        field.get("table"),
        field.get("name"),
        field.get("type_norm") or field.get("type_raw"),
        bool(field.get("required")),
    )


def _same(left: dict | None, right: dict | None) -> bool:
    return field_signature(left) == field_signature(right)


def _index(fields: list[dict] | None) -> dict[str, dict]:
    return {field["field_id"]: field for field in (fields or [])}


def summarize(field: dict | None) -> dict | None:
    if field is None:
        return None
    return {
        "field_id": field.get("field_id"),
        "table": field.get("table"),
        "name": field.get("name"),
        "type": field.get("type_norm") or field.get("type_raw"),
        "required": bool(field.get("required")),
    }


def _conflict_kind(base: dict | None, ours: dict | None, theirs: dict | None) -> str:
    if ours is None or theirs is None:
        return "delete_modify"
    return "both_changed"


def _field_item(field_id: str, kind: str, base: dict | None, ours: dict | None,
                theirs: dict | None, ruling: dict | None) -> dict:
    table = (ours or theirs or base or {}).get("table") or ""
    item = {
        "item_key": FIELD_PREFIX + field_id,
        "kind": kind,
        "reason": "MERGE_DELETE_MODIFY" if kind == "delete_modify" else "MERGE_BOTH_CHANGED",
        "table": table,
        "field_id": field_id,
        "choices": ["ours", "theirs"],
        "base": summarize(base),
        "ours": summarize(ours),
        "theirs": summarize(theirs),
        "ruled": None,
    }
    if ruling and ruling.get("choice") in item["choices"]:
        item["ruled"] = {"choice": ruling["choice"], "reason": ruling.get("reason"),
                         "decided_at": ruling.get("decided_at")}
    return item


def _candidate(field: dict | None, fid: str, ours_idx: dict, theirs_idx: dict,
               origin: str) -> dict:
    return {
        "field": field,
        "field_id": fid,
        "origin": origin,
        "ours_present": fid in ours_idx,
        "theirs_present": fid in theirs_idx,
    }


def _name_groups(candidates: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for cand in candidates:
        field = cand["field"]
        if field is None:
            continue
        key = ((field.get("table") or "").lower(), (field.get("name") or "").lower())
        groups.setdefault(key, []).append(cand)
    return groups


def _name_item(key: tuple[str, str], group: list[dict], ruling: dict | None) -> dict:
    table, name = key
    group = sorted(group, key=lambda cand: cand["field_id"])
    choices = []
    if any(cand["ours_present"] for cand in group):
        choices.append("ours")
    if any(cand["theirs_present"] for cand in group):
        choices.append("theirs")
    item = {
        "item_key": f"{NAME_PREFIX}{table}|{name}",
        "kind": "name_identity",
        "reason": "MERGE_NAME_IDENTITY",
        "table": group[0]["field"].get("table") or table,
        "name": group[0]["field"].get("name") or name,
        "choices": choices,
        "candidates": [
            {
                "field": summarize(cand["field"]),
                "ours_present": cand["ours_present"],
                "theirs_present": cand["theirs_present"],
                "origin": cand["origin"],
            }
            for cand in group
        ],
        "ruled": None,
    }
    if ruling and ruling.get("choice") in choices:
        item["ruled"] = {"choice": ruling["choice"], "reason": ruling.get("reason"),
                         "decided_at": ruling.get("decided_at")}
    return item


def _pick_name_winner(group: list[dict], choice: str) -> dict | None:
    if choice == "ours":
        pool = [cand for cand in group if cand["ours_present"]]
    else:
        pool = [cand for cand in group if cand["theirs_present"]]
    if not pool:
        return None
    return sorted(pool, key=lambda cand: cand["field_id"])[0]


def _clean_fields(candidates: list[dict], version_label: str) -> list[dict]:
    ordered = sorted(
        (cand["field"] for cand in candidates if cand["field"] is not None),
        key=lambda field: ((field.get("table") or "").lower(),
                           (field.get("name") or "").lower(),
                           field.get("field_id") or ""),
    )
    per_table: dict[str, int] = {}
    out: list[dict] = []
    for field in ordered:
        table = field.get("table") or ""
        per_table[table] = per_table.get(table, 0) + 1
        out.append({
            "field_id": field["field_id"],
            "table": table,
            "name": field.get("name") or "",
            "name_key": (field.get("name") or "").lower(),
            "type_raw": field.get("type_raw") or "unknown",
            "required": bool(field.get("required")),
            "line": per_table[table],
            "version": version_label,
            "source": "merge",
        })
    return out


def analyze(ancestor: list[dict], ours: list[dict], theirs: list[dict],
            rulings: dict[str, dict] | None = None,
            version_label: str = "") -> dict:
    """按共同祖先对齐三方，返回自动结论、全部冲突、未决项与合并字段。"""
    rulings = rulings or {}
    base_idx = _index(ancestor)
    ours_idx = _index(ours)
    theirs_idx = _index(theirs)
    field_ids = sorted(set(base_idx) | set(ours_idx) | set(theirs_idx))

    auto: list[dict] = []
    field_items: list[dict] = []
    candidates: list[dict] = []
    applied: list[dict] = []
    deleted_by_rule: list[dict] = []
    used_keys: set[str] = set()

    for fid in field_ids:
        base = base_idx.get(fid)
        mine = ours_idx.get(fid)
        other = theirs_idx.get(fid)
        entry = {"field_id": fid,
                 "table": (mine or other or base or {}).get("table"),
                 "name": (mine or other or base or {}).get("name")}

        if _same(mine, other):
            if mine is None:
                auto.append(dict(entry, action="deleted_by_both", side="both"))
                deleted_by_rule.append(dict(entry, side="both", reason="DELETE_BOTH"))
                continue
            if _same(mine, base):
                auto.append(dict(entry, action="unchanged", side="base"))
            else:
                auto.append(dict(entry, action="kept_both", side="both"))
            candidates.append(_candidate(mine, fid, ours_idx, theirs_idx, "both"))
            continue

        if _same(mine, base):
            # 只有分支改了
            if other is None:
                auto.append(dict(entry, action="deleted_by_theirs", side="theirs"))
                deleted_by_rule.append(dict(entry, side="theirs", reason="DELETE_THEIRS"))
            else:
                auto.append(dict(entry, action="kept_theirs", side="theirs"))
                candidates.append(_candidate(other, fid, ours_idx, theirs_idx, "theirs"))
            continue

        if _same(other, base):
            # 只有主线改了
            if mine is None:
                auto.append(dict(entry, action="deleted_by_ours", side="ours"))
                deleted_by_rule.append(dict(entry, side="ours", reason="DELETE_OURS"))
            else:
                auto.append(dict(entry, action="kept_ours", side="ours"))
                candidates.append(_candidate(mine, fid, ours_idx, theirs_idx, "ours"))
            continue

        # 双方都动了，且结果不同
        key = FIELD_PREFIX + fid
        used_keys.add(key)
        kind = _conflict_kind(base, mine, other)
        item = _field_item(fid, kind, base, mine, other, rulings.get(key))
        field_items.append(item)
        if item["ruled"] is None:
            continue

        choice = item["ruled"]["choice"]
        chosen = mine if choice == "ours" else other
        applied.append({
            "item_key": key,
            "kind": kind,
            "choice": choice,
            "reason": item["ruled"]["reason"],
            "field_id": fid,
        })
        if chosen is None:
            deleted_by_rule.append(dict(entry, side=choice, reason="RULED_DELETE"))
            continue
        candidates.append(_candidate(chosen, fid, ours_idx, theirs_idx, choice))

    # 合并后同表同列名落到不同身份：仍需逐项选边
    groups = _name_groups(candidates)
    name_items: list[dict] = []
    dropped: list[dict] = []
    drop_ids: set[str] = set()

    for key, group in sorted(groups.items()):
        if len({cand["field_id"] for cand in group}) < 2:
            continue
        item_key = f"{NAME_PREFIX}{key[0]}|{key[1]}"
        used_keys.add(item_key)
        item = _name_item(key, group, rulings.get(item_key))
        name_items.append(item)
        if item["ruled"] is None:
            # 未决的同名异身份不能出现在合并结果里，两侧候选都先扣下
            drop_ids.update(cand["field_id"] for cand in group)
            continue
        choice = item["ruled"]["choice"]
        winner = _pick_name_winner(group, choice)
        if winner is None:
            item["ruled"] = None
            item["invalid_reason"] = "选边在该侧没有候选"
            continue
        applied.append({
            "item_key": item_key,
            "kind": "name_identity",
            "choice": choice,
            "reason": item["ruled"]["reason"],
            "field_id": winner["field_id"],
        })
        for cand in group:
            if cand["field_id"] == winner["field_id"]:
                continue
            drop_ids.add(cand["field_id"])
            dropped.append({
                "item_key": item_key,
                "field_id": cand["field_id"],
                "table": cand["field"].get("table"),
                "name": cand["field"].get("name"),
                "identity": "ours" if cand["ours_present"] else "theirs",
                "choice": choice,
                "reason": item["ruled"]["reason"],
            })

    kept = [cand for cand in candidates if cand["field_id"] not in drop_ids]
    merged_fields = _clean_fields(kept, version_label)

    all_items = field_items + name_items
    items = [item for item in all_items if item["ruled"] is None]
    orphan = sorted(key for key in rulings if key not in used_keys)

    return {
        "auto": auto,
        "field_conflicts": field_items,
        "name_conflicts": name_items,
        "all_items": all_items,
        "items": items,
        "pending": [item["item_key"] for item in items],
        "merged_fields": merged_fields,
        "dropped": dropped,
        "deleted": deleted_by_rule,
        "applied": applied,
        "orphan_rulings": orphan,
        "counts": {
            "ancestor": len(ancestor),
            "ours": len(ours),
            "theirs": len(theirs),
            "merged": len(merged_fields),
            "auto_kept": sum(1 for row in auto if row["action"] in KEPT_ACTIONS),
            "conflicts": len(all_items),
            "ruled": len(applied),
            "pending": len(items),
            "dropped": len(dropped),
            "deleted": len(deleted_by_rule),
        },
    }
