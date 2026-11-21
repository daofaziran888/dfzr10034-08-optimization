"""元数据索引领域服务：字段级元数据解析、索引和搜索发现。

字段发现批次（field discovery batch）是资产字典的唯一写入口：
- 一次提交的多条列定义形成一个批次；
- 列声明按「资产 + 来源 + 字段」去重，同一来源重复导入只保留一条采集依据；
- 同一字段来自不同来源的声明逐条比对，兼容类型合并发布，不兼容类型保留双方声明；
- 批次明细、采集依据和元数据索引（资产字典）三者可相互回查。

接收资产注册触发，维护元数据索引的搜索和发现能力。
"""
from __future__ import annotations

import json
import time as _time
from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.services.field_types import normalize_type, types_conflict
from app.store import store

BATCH_MODULE = "field_discovery_batch"
DETAIL_MODULE = "field_discovery_detail"
EVIDENCE_MODULE = "field_discovery_evidence"

_BATCH_NO_FIELD = "批次编号"
_DETAIL_NO_FIELD = "明细编号"
_EVIDENCE_NO_FIELD = "依据编号"

# 数值类型中更宽的类型族：兼容时向宽的一侧发布，避免丢掉新来源的精度。
_WIDER_FAMILY = {"integer": "decimal", "date": "datetime"}


class MetadataIndexService(BaseModuleService):
    MODULE = "metadata_index"
    ENTITY = "元数据条目"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["条目编号", "所属资产", "字段名称", "字段类型", "业务含义", "枚举值", "样例数据", "最后解析"]
    REQUIRED_FIELDS = ["条目编号", "所属资产", "字段名称"]
    STATUS_ORDER = ["待解析", "已索引", "解析异常", "已过期"]
    ACTION_RULES = {"触发解析": "已索引", "重建索引": "解析异常", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    # ── 字段发现批次 ──────────────────────────────────────────

    def discover_schema(
        self,
        asset_code: str,
        schema_map: list[dict[str, str]],
        source: str = "schema_import",
    ) -> list[dict[str, Any]]:
        """从资产schema自动发现并索引元数据（Pipeline 1 核心方法，兼容旧调用）。

        旧调用不区分来源，统一记到 ``schema_import``；重复列不会再追加
        新的索引行，而是更新既有字段定义。需要完整批次结果时用
        :meth:`discover_batch`。
        """
        result = self.discover_batch(asset_code, schema_map, source=source)
        return result["entries"]

    def discover_batch(
        self,
        asset_code: str,
        declarations: Iterable[dict[str, Any] | str],
        source: str = "batch_import",
        *,
        batch_id: str | int | None = None,
        submitted_by: str = "system",
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """提交一批列声明，按资产和来源去重后并入资产字典。

        ``declarations`` 的每一项支持 ``name``/``type``/``source`` 或中文键
        ``字段名称``/``字段类型``/``来源``。未携带来源的列使用批次默认
        ``source``；因此同一批次可以混收多个来源的声明。

        ``batch_id`` 为空时新建一个字段发现批次；传入已有批次编号则把本次
        声明并入该批次（重复导入同一来源仍只保留一条采集依据）。
        """
        asset = str(asset_code or "").strip()
        if not asset:
            return {"ok": False, "message": "字段发现缺少所属资产", "entries": []}

        raw_items = [self._normalize_declaration(item, source) for item in declarations]
        received = len(raw_items)

        batch = self._get_or_create_batch(str(batch_id) if batch_id else None, asset, source)
        now = observed_at or _time.strftime("%Y-%m-%d %H:%M:%S")
        seen_in_submission: set[tuple[str, str]] = set()
        valid = 0
        duplicates_in_submission = 0
        duplicate_imports = 0
        new_fields = 0
        updated_fields = 0
        touched_details: list[dict[str, Any]] = []
        touched_evidence: list[dict[str, Any]] = []

        for decl in raw_items:
            field_name = str(decl.get("name") or "").strip()
            if not field_name:
                continue
            valid += 1
            decl_source = str(decl.get("source") or source).strip() or source
            dedupe_key = (decl_source, field_name)
            if dedupe_key in seen_in_submission:
                duplicates_in_submission += 1
                continue
            seen_in_submission.add(dedupe_key)

            evidence, evidence_created = self._upsert_evidence(
                asset, field_name, decl, decl_source, batch, now)
            if evidence_created:
                touched_evidence.append(evidence)
            else:
                duplicate_imports += 1
                touched_evidence.append(evidence)

            existing_entry = self._find_dictionary_entry(asset, field_name)
            previous_conflict = bool(existing_entry and existing_entry.get("类型冲突"))
            entry, entry_created = self._upsert_dictionary_entry(
                asset, field_name, decl, decl_source, evidence, batch, now)
            if entry_created:
                new_fields += 1
            else:
                updated_fields += 1

            detail, _ = self._upsert_detail(
                asset, field_name, decl, decl_source, evidence, entry, batch, now)
            touched_details.append(detail)

            # 该字段当前全部来源声明重新做一次冲突判定，
            # 保证 A、B 两份不兼容声明的明细都能回查到争议出处。
            conflict, published, declarations_view = self._reconcile_field_types(
                asset, field_name, entry, evidence)
            entry["字段类型"] = published
            entry["类型族"] = normalize_type(published)
            entry["类型冲突"] = conflict
            entry["冲突声明"] = json.dumps(declarations_view, ensure_ascii=False)
            entry["冲突数"] = len({(d["来源"], d["类型族"]) for d in declarations_view}) if conflict else 0
            evidence_rows = self._field_evidence(asset, field_name)
            entry["来源"] = "、".join(sorted({e.get("来源", "") for e in evidence_rows}))
            entry["来源数"] = len(evidence_rows)
            entry["采集依据"] = ",".join(e.get(_EVIDENCE_NO_FIELD, "") for e in evidence_rows)
            entry["最后批次"] = batch[_BATCH_NO_FIELD]
            entry["最后解析"] = now
            entry["status"] = "解析异常" if conflict else "已索引"
            entry["abnormal"] = conflict
            entry["pending"] = conflict

            # 明细也带上字段级冲突结论，单个来源也能看出争议。
            self._mark_field_details(asset, field_name, conflict, entry)

            if conflict and not previous_conflict:
                store.log_change(
                    self.MODULE, entry["id"], "字段类型冲突",
                    before=published, after=json.dumps(declarations_view, ensure_ascii=False),
                    source="field_discovery", scope=f"{asset}.{field_name}",
                )

        # 去重后的字段清单用于批次统计和编目核对
        field_names = sorted({d["字段名称"] for d in touched_details})
        catalog_fields = self._catalog_field_names(asset)
        detail_fields = {d["字段名称"] for d in self._asset_details(asset)}
        asset_conflicts = sorted(
            row.get("字段名称", "") for row in self.get_asset_fields(asset)
            if row.get("类型冲突")
        )
        # 同一批次可分多轮提交（例如 A、B 两个加载器先后上报），
        # 声明条数累计，其余统计反映最近一轮。
        batch.update({
            "提交来源": source,
            "提交人": submitted_by,
            "提交轮次": int(batch.get("提交轮次", 0)) + 1,
            "本轮声明条数": received,
            "声明条数": int(batch.get("声明条数", 0)) + received,
            "有效条数": valid,
            "去重条数": len(seen_in_submission),
            "批内重复条数": duplicates_in_submission,
            "重复导入条数": duplicate_imports,
            "涉及字段数": len(field_names),
            "新增列数": new_fields,
            "更新列数": updated_fields,
            "冲突列数": len(asset_conflicts),
            "冲突字段": "、".join(asset_conflicts),
            "来源": "、".join(sorted({(d.get("来源") or source) for d in touched_details})),
            "明细编号": ",".join(d.get(_DETAIL_NO_FIELD, "") for d in touched_details),
            "编目字段数": len(catalog_fields),
            "发现字段数": len(detail_fields),
            "编目与发现一致": catalog_fields == detail_fields,
            "最后提交时间": now,
            "status": "存在冲突" if asset_conflicts else "已并入",
            "pending": bool(asset_conflicts),
            "abnormal": bool(asset_conflicts),
        })

        self.emit_event("field_discovery_batch", {
            "asset_code": asset,
            "batch_no": batch[_BATCH_NO_FIELD],
            "received": received,
            "field_count": len(field_names),
            "new_fields": new_fields,
            "duplicate_imports": duplicate_imports,
            "conflict_fields": asset_conflicts,
        })

        catalog = self.get_asset_fields(asset)
        return {
            "ok": True,
            "asset_code": asset,
            "batch": batch,
            "entries": catalog,
            "details": touched_details,
            "evidence": touched_evidence,
            "conflicts": asset_conflicts,
            "summary": {
                "声明条数": received,
                "去重条数": len(seen_in_submission),
                "批内重复条数": duplicates_in_submission,
                "重复导入条数": duplicate_imports,
                "新增列数": new_fields,
                "更新列数": updated_fields,
                "冲突列数": len(asset_conflicts),
                "字典列数": len(catalog),
                "编目与发现一致": catalog_fields == detail_fields,
            },
        }

    # ── 查询与核对 ────────────────────────────────────────────

    def list_batches(self, asset_code: str | None = None) -> list[dict[str, Any]]:
        rows = list(store.rows(BATCH_MODULE))
        if asset_code:
            rows = [row for row in rows if row.get("所属资产") == asset_code]
        return rows

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get(_BATCH_NO_FIELD) == batch_no:
                return row
        return None

    def get_details(
        self,
        asset_code: str | None = None,
        source: str | None = None,
        field_name: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(DETAIL_MODULE))
        if asset_code:
            rows = [row for row in rows if row.get("所属资产") == asset_code]
        if source:
            rows = [row for row in rows if row.get("来源") == source]
        if field_name:
            rows = [row for row in rows if row.get("字段名称") == field_name]
        return rows

    def get_evidence(
        self,
        asset_code: str | None = None,
        source: str | None = None,
        field_name: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(EVIDENCE_MODULE))
        if asset_code:
            rows = [row for row in rows if row.get("所属资产") == asset_code]
        if source:
            rows = [row for row in rows if row.get("来源") == source]
        if field_name:
            rows = [row for row in rows if row.get("字段名称") == field_name]
        return rows

    def get_asset_dictionary(self, asset_code: str) -> list[dict[str, Any]]:
        """资产字典：按资产汇总去重后的字段定义。"""
        return self.get_asset_fields(asset_code)

    def reconcile_catalog(self, asset_code: str) -> dict[str, Any]:
        """核对编目清单（资产字典）与字段发现明细是否一一对应。

        除字段集合外，还逐字段核对来源数与采集依据数：同一来源重复导入
        只能对应一条依据，不兼容的多来源类型声明则必须各有一条明细和依据。
        """
        dictionary = {row.get("字段名称", ""): row for row in self.get_asset_fields(asset_code)}
        catalog = set(dictionary)
        details = self._asset_details(asset_code)
        detail_fields = {d.get("字段名称", "") for d in details}
        evidence_rows = {
            row.get(_EVIDENCE_NO_FIELD, ""): row
            for row in self.get_evidence(asset_code=asset_code)
        }
        sources_by_field: dict[str, set[str]] = {}
        evidence_ids: dict[str, set[str]] = {}
        dangling_details = []
        for detail in details:
            name = detail.get("字段名称", "")
            sources_by_field.setdefault(name, set()).add(detail.get("来源", ""))
            evidence_ids.setdefault(name, set()).add(detail.get(_EVIDENCE_NO_FIELD, ""))
            evidence = evidence_rows.get(detail.get(_EVIDENCE_NO_FIELD, ""))
            if not evidence or (
                evidence.get("来源") != detail.get("来源")
                or evidence.get("字段名称") != name
            ):
                dangling_details.append(detail.get(_DETAIL_NO_FIELD, ""))

        fields = []
        mismatched: list[dict[str, Any]] = []
        for name in sorted(catalog | detail_fields):
            entry = dictionary.get(name, {})
            detail_sources = sources_by_field.get(name, set())
            detail_evidence = evidence_ids.get(name, set())
            entry_sources = set(str(entry.get("来源", "")).replace("，", "、").split("、")) - {""}
            entry_evidence = set(str(entry.get("采集依据", "")).split(",")) - {""}
            field_ok = (
                name in catalog and name in detail_fields
                and entry_sources == detail_sources
                and entry_evidence == detail_evidence
            )
            item = {
                "字段名称": name,
                "字典类型": entry.get("字段类型", ""),
                "类型冲突": bool(entry.get("类型冲突")),
                "来源": sorted(detail_sources),
                "来源数": len(detail_sources),
                "采集依据": sorted(detail_evidence),
                "明细条数": len([d for d in details if d.get("字段名称") == name]),
                "一致": field_ok,
            }
            fields.append(item)
            if not field_ok:
                mismatched.append(item)

        return {
            "所属资产": asset_code,
            "编目字段数": len(catalog),
            "发现字段数": len(detail_fields),
            "明细条数": len(details),
            "依据条数": len(evidence_rows),
            "仅编目存在": sorted(catalog - detail_fields),
            "仅发现存在": sorted(detail_fields - catalog),
            "依据对不上的明细": dangling_details,
            "不一致字段": [item["字段名称"] for item in mismatched],
            "一致": not mismatched and not dangling_details,
            "字段": fields,
        }

    # ── 内部：批次与明细 ──────────────────────────────────────

    def _normalize_declaration(self, item: dict[str, Any] | str, default_source: str) -> dict[str, Any]:
        if isinstance(item, str):
            return {"name": item, "type": "", "source": default_source}
        return {
            "name": item.get("name", item.get("字段名称", "")),
            "type": item.get("type", item.get("字段类型", "")),
            "source": str(item.get("source", item.get("来源", default_source)) or default_source),
            "meaning": item.get("meaning", item.get("业务含义", "")),
            "enum_values": item.get("enum_values", item.get("枚举值", "")),
            "sample": item.get("sample", item.get("样例数据", item.get("样本值", ""))),
            "sampling": item.get("sampling", item.get("采样方式", "")),
            "note": item.get("note", item.get("采样说明", "")),
        }

    def _get_or_create_batch(self, batch_no: str | None, asset: str, source: str) -> dict[str, Any]:
        if batch_no:
            existing = self.get_batch(batch_no)
            # 批次只能并入同一资产，避免跨资产串用统计
            if existing and existing.get("所属资产") == asset:
                return existing
            if existing:
                # 编号已被别的资产占用，另起一个批次号，绝不共用
                batch_no = None
        batch_id = store.next_id(BATCH_MODULE)
        batch = {
            "id": batch_id,
            _BATCH_NO_FIELD: batch_no or f"FDISC-{batch_id:04d}",
            "所属资产": asset,
            "提交来源": source,
            "声明条数": 0,
            "去重条数": 0,
            "新增列数": 0,
            "更新列数": 0,
            "冲突列数": 0,
            "status": "已并入",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(BATCH_MODULE).append(batch)
        return batch

    def _upsert_evidence(
        self,
        asset: str,
        field_name: str,
        decl: dict[str, Any],
        source: str,
        batch: dict[str, Any],
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        """按 资产+来源+字段 维护唯一采集依据。"""
        for row in store.rows(EVIDENCE_MODULE):
            if (row.get("所属资产") == asset and row.get("来源") == source
                    and row.get("字段名称") == field_name):
                previous_type = str(row.get("声明类型", "") or "")
                previous_seen = row.get("最后采集", now)
                previous_batch = row.get("最后批次", "")
                row["命中次数"] = int(row.get("命中次数", 1)) + 1
                row["最后采集"] = now
                row["最后批次"] = batch[_BATCH_NO_FIELD]
                if decl.get("type") not in ("", None):
                    if previous_type and previous_type != str(decl["type"]):
                        # 同一来源改口时保留旧声明，采集依据仍只有一条。
                        history = row.setdefault("历史声明", [])
                        history.append({
                            "声明类型": previous_type,
                            "发现时间": previous_seen,
                            "批次": previous_batch,
                        })
                        row["变更次数"] = int(row.get("变更次数", 0)) + 1
                    row["声明类型"] = decl["type"]
                    row["类型族"] = normalize_type(decl["type"])
                for key, target in (("sample", "样本值"), ("sampling", "采样方式"), ("note", "采样说明")):
                    if decl.get(key):
                        row[target] = decl[key]
                return row, False

        evidence_id = store.next_id(EVIDENCE_MODULE)
        evidence = {
            "id": evidence_id,
            _EVIDENCE_NO_FIELD: f"FEVD-{evidence_id:04d}",
            "所属资产": asset,
            "字段名称": field_name,
            "来源": source,
            "声明类型": decl.get("type", ""),
            "类型族": normalize_type(decl.get("type", "")),
            "采样方式": decl.get("sampling") or "加载器列声明",
            "采样说明": decl.get("note") or f"{source} 提交的列结构声明",
            "样本值": decl.get("sample", ""),
            "采样时间": now,
            "命中次数": 1,
            "首次采集": now,
            "最后采集": now,
            "首次批次": batch[_BATCH_NO_FIELD],
            "最后批次": batch[_BATCH_NO_FIELD],
            "status": "有效",
            "pending": False,
            "abnormal": False,
            "created_at": now,
        }
        store.rows(EVIDENCE_MODULE).append(evidence)
        return evidence, True

    def _upsert_dictionary_entry(
        self,
        asset: str,
        field_name: str,
        decl: dict[str, Any],
        source: str,
        evidence: dict[str, Any],
        batch: dict[str, Any],
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        entry = self._find_dictionary_entry(asset, field_name)
        if entry:
            # 兼容合并：只有新类型与既有定义兼容时才可能调整发布类型；
            # 不兼容时不覆盖既有发布类型，交由冲突判定保留双方声明。
            published = self._merge_published_type(entry.get("字段类型", ""), decl.get("type", ""))
            entry["字段类型"] = published
            for key, target in (
                ("meaning", "业务含义"), ("enum_values", "枚举值"),
            ):
                if decl.get(key):
                    entry[target] = decl[key]
            if decl.get("sample") not in (None, ""):
                entry["样例数据"] = decl["sample"]
            entry["最后批次"] = batch[_BATCH_NO_FIELD]
            entry["最后解析"] = now
            return entry, False

        entry_id = store.next_id(self.MODULE)
        existing_numbers = {row.get("条目编号", "") for row in self.get_asset_fields(asset)}
        sequence = 1
        while f"{asset}-FLD-{sequence:03d}" in existing_numbers:
            sequence += 1
        entry = {
            "id": entry_id,
            "条目编号": f"{asset}-FLD-{sequence:03d}",
            "所属资产": asset,
            "字段名称": field_name,
            "字段类型": decl.get("type", ""),
            "类型族": normalize_type(decl.get("type", "")),
            "业务含义": decl.get("meaning", ""),
            "枚举值": decl.get("enum_values", ""),
            "样例数据": decl.get("sample", ""),
            "最后解析": now,
            "来源": source,
            "来源数": 1,
            "采集依据": evidence[_EVIDENCE_NO_FIELD],
            "类型冲突": False,
            "冲突声明": "[]",
            "冲突数": 0,
            "最后批次": batch[_BATCH_NO_FIELD],
            "status": "已索引",
            "pending": False,
            "abnormal": False,
            "created_at": now,
        }
        store.rows(self.MODULE).append(entry)
        return entry, True

    def _upsert_detail(
        self,
        asset: str,
        field_name: str,
        decl: dict[str, Any],
        source: str,
        evidence: dict[str, Any],
        entry: dict[str, Any],
        batch: dict[str, Any],
        now: str,
    ) -> tuple[dict[str, Any], bool]:
        for row in store.rows(DETAIL_MODULE):
            if (row.get("所属资产") == asset and row.get("来源") == source
                    and row.get("字段名称") == field_name):
                row["声明类型"] = decl.get("type", row.get("声明类型", ""))
                row["类型族"] = normalize_type(row["声明类型"])
                row["出现次数"] = int(row.get("出现次数", 1)) + 1
                row["最后确认"] = now
                row["最后批次"] = batch[_BATCH_NO_FIELD]
                row[_EVIDENCE_NO_FIELD] = evidence[_EVIDENCE_NO_FIELD]
                row["字典条目"] = entry.get("条目编号", "")
                row["发布类型"] = entry.get("字段类型", "")
                return row, False

        detail_id = store.next_id(DETAIL_MODULE)
        detail = {
            "id": detail_id,
            _DETAIL_NO_FIELD: f"FDET-{detail_id:04d}",
            "所属资产": asset,
            "字段名称": field_name,
            "来源": source,
            "声明类型": decl.get("type", ""),
            "类型族": normalize_type(decl.get("type", "")),
            _EVIDENCE_NO_FIELD: evidence[_EVIDENCE_NO_FIELD],
            "字典条目": entry.get("条目编号", ""),
            "发布类型": entry.get("字段类型", ""),
            "是否冲突": False,
            "冲突说明": "",
            "出现次数": 1,
            "首次发现": now,
            "最后确认": now,
            "首次批次": batch[_BATCH_NO_FIELD],
            "最后批次": batch[_BATCH_NO_FIELD],
            "status": "已收录",
            "pending": False,
            "abnormal": False,
            "created_at": now,
        }
        store.rows(DETAIL_MODULE).append(detail)
        return detail, True

    # ── 内部：类型兼容与冲突 ──────────────────────────────────

    @staticmethod
    def _merge_published_type(existing: Any, incoming: Any) -> str:
        """兼容时合并发布类型；不兼容时保持既有定义不变。"""
        current = str(existing or "").strip()
        new = str(incoming or "").strip()
        if not new:
            return current
        if not current:
            return new
        current_family = normalize_type(current)
        new_family = normalize_type(new)
        if current_family == new_family:
            return current
        if types_conflict(current, new):
            return current
        # 兼容但不同类型族：向更宽的一侧发布，既有定义仍然可用。
        if _WIDER_FAMILY.get(current_family) == new_family:
            return new
        return current

    def _reconcile_field_types(
        self,
        asset: str,
        field_name: str,
        entry: dict[str, Any],
        incoming_evidence: dict[str, Any],
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        evidence_rows = self._field_evidence(asset, field_name)
        declarations_view = [
            {
                "来源": row.get("来源", ""),
                "类型": row.get("声明类型", ""),
                "类型族": row.get("类型族", normalize_type(row.get("声明类型", ""))),
                "采集依据": row.get(_EVIDENCE_NO_FIELD, ""),
            }
            for row in evidence_rows
        ]
        conflict = False
        for idx, left in enumerate(evidence_rows):
            for right in evidence_rows[idx + 1:]:
                if types_conflict(left.get("声明类型", ""), right.get("声明类型", "")):
                    conflict = True
                    break
            if conflict:
                break

        published = str(entry.get("字段类型", "") or "")
        if not published:
            # 字典尚未发布时，取第一个有类型的声明
            for row in evidence_rows:
                if row.get("声明类型"):
                    published = str(row["声明类型"])
                    break
        if not conflict and incoming_evidence.get("声明类型"):
            published = self._merge_published_type(published, incoming_evidence["声明类型"])
        return conflict, published, declarations_view

    def _mark_field_details(
        self, asset: str, field_name: str, conflict: bool, entry: dict[str, Any]
    ) -> None:
        declarations = json.loads(entry.get("冲突声明", "[]") or "[]")
        by_source = {d.get("来源", ""): d for d in declarations}
        for row in store.rows(DETAIL_MODULE):
            if row.get("所属资产") != asset or row.get("字段名称") != field_name:
                continue
            row["发布类型"] = entry.get("字段类型", "")
            row["是否冲突"] = conflict
            if conflict:
                others = [
                    f"{d['来源']}:{d['类型']}"
                    for d in declarations
                    if d.get("来源") != row.get("来源")
                ]
                own = by_source.get(row.get("来源", ""), {})
                row["冲突说明"] = (
                    f"{row.get('来源')} 声明 {own.get('类型', row.get('声明类型', ''))}，"
                    f"与 {'、'.join(others)} 不兼容；字典暂保留发布类型 "
                    f"{entry.get('字段类型', '')}"
                )
                row["status"] = "冲突待核实"
                row["pending"] = True
                row["abnormal"] = True
            else:
                row["冲突说明"] = ""
                row["status"] = "已收录"
                row["pending"] = False
                row["abnormal"] = False

    # ── 内部：查询辅助 ────────────────────────────────────────

    def _find_dictionary_entry(self, asset: str, field_name: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("所属资产") == asset and row.get("字段名称") == field_name:
                return row
        return None

    def _field_evidence(self, asset: str, field_name: str) -> list[dict[str, Any]]:
        return [
            row for row in store.rows(EVIDENCE_MODULE)
            if row.get("所属资产") == asset and row.get("字段名称") == field_name
        ]

    def _asset_details(self, asset: str) -> list[dict[str, Any]]:
        return [row for row in store.rows(DETAIL_MODULE) if row.get("所属资产") == asset]

    def _catalog_field_names(self, asset: str) -> set[str]:
        return {row.get("字段名称", "") for row in self.get_asset_fields(asset)}

    # ── 既有索引能力 ──────────────────────────────────────────

    def rebuild_index(self, entry_id: int) -> dict[str, Any] | None:
        """重建指定元数据条目的索引。类型冲突未澄清时仍然标记异常。"""
        meta = self.find(entry_id)
        if not meta:
            return None
        conflict = bool(meta.get("类型冲突"))
        meta["最后解析"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        meta["status"] = "解析异常" if conflict else "已索引"
        meta["abnormal"] = conflict
        meta["pending"] = conflict
        store.log_change(
            self.MODULE, entry_id, "重建索引",
            before="解析异常" if conflict else "已过期",
            after=meta["status"],
            source="rebuild", scope=self.MODULE
        )
        return meta

    def find(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def search_metadata(self, keyword: str) -> list[dict[str, Any]]:
        """搜索元数据索引。"""
        results = []
        kw = keyword.casefold()
        for row in store.rows(self.MODULE):
            for field in self.LIST_FIELDS:
                if kw in str(row.get(field, "")).casefold():
                    results.append(row)
                    break
        return results

    def get_asset_fields(self, asset_code: str) -> list[dict[str, Any]]:
        """获取指定资产的字段元数据（资产字典）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("所属资产") == asset_code
        ]
