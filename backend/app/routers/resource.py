"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import (
    ActionPayload,
    ActionResult,
    EntryPayload,
    FieldDiscoveryPayload,
    PageResult,
)
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


def require_metadata_service():
    service = require_service("metadata_index")
    return service


@router.post("/metadata_index/discovery", response_model=dict)
def discover_fields(payload: FieldDiscoveryPayload) -> dict[str, Any]:
    """提交一批列定义：按资产+来源去重后并入资产字典。

    同一批次可混收多个来源的声明；``batch_id`` 为空时新建字段发现批次，
    传入已有批次编号则把本次声明并入该批次（同一来源重复导入只保留一条
    采集依据）。
    """
    service = require_metadata_service()
    result = service.discover_batch(
        payload.asset_code,
        [item.model_dump() for item in payload.declarations],
        source=payload.source,
        batch_id=payload.batch_id,
        submitted_by=payload.submitted_by,
        observed_at=payload.observed_at,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("message", "字段发现失败"))
    return result


@router.get("/metadata_index/discovery", response_model=dict)
def list_discovery_batches(asset_code: str | None = Query(default=None)) -> dict[str, Any]:
    """字段发现批次清单，可按资产过滤。"""
    service = require_metadata_service()
    return {"items": service.list_batches(asset_code)}


@router.get("/metadata_index/discovery/{batch_no}", response_model=dict)
def get_discovery_batch(batch_no: str) -> dict[str, Any]:
    """批次明细：批次统计、去重后的列声明和采集依据。"""
    service = require_metadata_service()
    batch = service.get_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"字段发现批次 {batch_no} 不存在")
    asset_code = batch.get("所属资产", "")
    return {
        "batch": batch,
        "details": service.get_details(asset_code=asset_code),
        "evidence": service.get_evidence(asset_code=asset_code),
        "dictionary": service.get_asset_dictionary(asset_code),
        "reconcile": service.reconcile_catalog(asset_code),
    }


@router.get("/metadata_index/dictionary/{asset_code}", response_model=dict)
def get_asset_dictionary(asset_code: str) -> dict[str, Any]:
    """资产字典：按资产汇总去重后的字段定义及类型冲突结论。"""
    service = require_metadata_service()
    return {
        "asset_code": asset_code,
        "items": service.get_asset_dictionary(asset_code),
        "details": service.get_details(asset_code=asset_code),
        "evidence": service.get_evidence(asset_code=asset_code),
        "reconcile": service.reconcile_catalog(asset_code),
    }


@router.get("/metadata_index/dictionary/{asset_code}/catalog-check", response_model=dict)
def check_catalog(asset_code: str) -> dict[str, Any]:
    """编目清单与字段发现明细的逐字段核对。"""
    service = require_metadata_service()
    return service.reconcile_catalog(asset_code)


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
