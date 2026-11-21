"""资产注册领域服务：数据治理入口底座，负责资产登记、元数据发现触发、索引编排。

Pipeline 1: Asset registration → metadata discovery → indexing
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AssetRegistryService(BaseModuleService):
    MODULE = "asset_registry"
    ENTITY = "数据资产"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["资产编号", "资产名称", "资产类型", "所属系统", "存储位置", "负责人", "保密等级", "注册时间"]
    REQUIRED_FIELDS = ["资产编号", "资产名称", "资产类型"]
    STATUS_ORDER = ["待审核", "已注册", "待更新", "已注销"]
    ACTION_RULES = {"审核注册": "已注册", "提交更新": "待更新", "注销资产": "已注销"}
    NEGATIVE_ACTIONS = []

    def register_asset(self, values: dict[str, Any]) -> dict[str, Any]:
        """注册资产并自动触发元数据发现与索引编排（Pipeline 1 入口）。"""
        entry, missing = self.create_entry(values)
        if missing:
            return {"ok": False, "missing": missing, "asset": None}

        # 自动审核注册
        self.run_action(entry["id"], "审核注册")

        # 触发元数据发现：列定义统一走字段发现批次，保证去重和采集依据一致
        schema_fields = [
            ("资产编号", "string", "资产唯一标识"),
            ("资产名称", "string", "资产业务名称"),
            ("资产类型", "string", "资产分类类型"),
            ("存储位置", "string", "物理存储路径"),
            ("保密等级", "string", "安全保密等级"),
        ]
        declarations = [
            {
                "name": field_name,
                "type": field_type,
                "meaning": biz_meaning,
                "sample": str(entry.get(field_name, "")),
                "source": self.MODULE,
            }
            for field_name, field_type, biz_meaning in schema_fields
        ]
        discovery = self._metadata_service().discover_batch(
            entry["资产编号"], declarations, source=self.MODULE,
            submitted_by=str(entry.get("负责人", "") or "system"),
        )
        meta_entries = [meta["条目编号"] for meta in discovery["entries"]]

        # 记录变更日志
        store.log_change(
            self.MODULE, entry["id"], "注册并发现元数据",
            before=None, after=f"注册资产{entry['资产编号']}，生成{len(meta_entries)}条元数据",
            source="pipeline_1", scope="asset_registry->metadata_index"
        )

        # 发出事件
        self.emit_event("asset_registered", {
            "asset_id": entry["id"],
            "asset_code": entry["资产编号"],
            "metadata_count": len(meta_entries),
        })

        return {
            "ok": True,
            "asset": entry,
            "metadata_entries": meta_entries,
            "field_discovery": {
                "batch": discovery["batch"],
                "summary": discovery["summary"],
            },
            "pipeline": "asset_registry->metadata_index",
        }

    @staticmethod
    def _metadata_service():
        """延迟导入，避免服务注册表初始化时的循环引用。"""
        from app.services.metadata_index import MetadataIndexService

        return MetadataIndexService()

    def get_asset_metadata(self, asset_id: int) -> list[dict[str, Any]]:
        """查询资产关联的元数据条目。"""
        asset = self.get_entry(asset_id)
        if not asset:
            return []
        asset_code = asset.get("资产编号", "")
        return [
            row for row in store.rows("metadata_index")
            if row.get("所属资产") == asset_code
        ]

    def get_asset_tags(self, asset_id: int) -> list[dict[str, Any]]:
        """查询资产关联的业务标签（Pipeline 9）。"""
        asset = self.get_entry(asset_id)
        if not asset:
            return []
        asset_code = asset.get("资产编号", "")
        results = []
        for row in store.rows("tag_registry"):
            if row.get("关联资产数", "") == asset_code:
                results.append(row)
        return results

    def get_asset_classifications(self, asset_id: int) -> list[dict[str, Any]]:
        """查询资产关联的分级标签。"""
        asset = self.get_entry(asset_id)
        if not asset:
            return []
        return store.find_by_field("classification", "目标字段", asset.get("资产编号", ""))

    def get_asset_quality_rules(self, asset_id: int) -> list[dict[str, Any]]:
        """查询资产关联的质量规则。"""
        asset = self.get_entry(asset_id)
        if not asset:
            return []
        asset_code = asset.get("资产编号", "")
        return store.find_by_field("quality_rule", "目标资产", asset_code)