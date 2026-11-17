"""M8 入口：合并会话创建、逐项裁定、出批与回读。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import errors, web
from app.modules.m8_merge import service

router = APIRouter()

ALLOWED_CREATE_KEYS = {
    "ancestor_file", "ours_file", "theirs_file", "refs_file",
    "base_version_seq", "effective_from", "effective_to", "version_label",
}

DEFAULT_FILES = dict(service.DEFAULT_FILES)


@router.get("/merge", response_class=HTMLResponse)
def merge_page(request: Request, merge_id: str | None = None):
    sessions = service.listing()
    detail = None
    if merge_id:
        detail = service.get(merge_id)
    return web.page_or_404(
        request,
        "merge.html",
        "merge",
        None,
        sessions=sessions,
        empty=not sessions,
        detail=detail,
        defaults=DEFAULT_FILES,
    )


@router.post("/api/merges")
def create_merge(payload: dict | None = None):
    body = payload or {}
    unknown = set(body) - ALLOWED_CREATE_KEYS
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    view = service.create(body)
    return {
        "merge_id": view["merge_id"],
        "reused": bool(view.get("reused")),
        "status": view["status"],
        "head_version_seq": view["head_version_seq"],
        "counts": view["counts"],
        "pending": view["pending"],
    }


@router.get("/api/merges")
def list_merges():
    return {"merges": service.listing()}


@router.get("/api/merges/{merge_id}")
def get_merge(merge_id: str):
    return service.get(merge_id)


@router.post("/api/merges/{merge_id}/rulings")
def submit_rulings(merge_id: str, payload: dict | None = None):
    view = service.submit_rulings(merge_id, payload or {})
    return {
        "merge_id": view["merge_id"],
        "status": view["status"],
        "counts": view["counts"],
        "pending": view["pending"],
        "rulings": view["rulings"],
        "orphan_rulings": view["orphan_rulings"],
    }


@router.post("/api/merges/{merge_id}/batch")
def generate_batch(merge_id: str, payload: dict | None = None):
    body = payload or {}
    unknown = set(body) - {"base_version_seq"}
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    view = service.generate_batch(merge_id, body)
    return {
        "merge_id": view["merge_id"],
        "status": view["status"],
        "reused": bool(view.get("reused")),
        "recovered": bool(view.get("recovered")),
        "run_id": (view.get("result") or {}).get("run_id"),
        "version_seq": (view.get("result") or {}).get("version_seq"),
        "counts": view["counts"],
        "ledger": (view.get("result") or {}).get("ledger") or {},
    }
