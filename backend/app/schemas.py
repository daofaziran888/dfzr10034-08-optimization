"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class FieldDeclaration(BaseModel):
    """一条列声明；来源可省略，省略时使用批次的提交来源。"""

    name: str
    type: str = ""
    source: str | None = None
    meaning: str = ""
    enum_values: str = ""
    sample: str = ""
    sampling: str = ""
    note: str = ""


class FieldDiscoveryPayload(BaseModel):
    asset_code: str
    declarations: list[FieldDeclaration] = Field(default_factory=list)
    source: str = "batch_import"
    batch_id: str | None = None
    submitted_by: str = "system"
    observed_at: str | None = None
