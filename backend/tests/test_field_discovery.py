"""字段发现批次测试：整批去重、多来源采集依据、类型冲突回查和字典同步。"""
import app.store as store_mod
from app.services.metadata_index import MetadataIndexService


def _service():
    store_mod.store.reset()
    return MetadataIndexService()


def _dict_entry(service, asset, field_name):
    for row in service.get_asset_fields(asset):
        if row.get("字段名称") == field_name:
            return row
    return None


def _evidence(service, asset, field_name, source):
    rows = service.get_evidence(asset_code=asset, source=source, field_name=field_name)
    assert len(rows) == 1, f"{asset}.{field_name}/{source} 采集依据应唯一，实际 {len(rows)}"
    return rows[0]


class TestBatchDeduplication:

    def test_one_batch_with_repeated_columns_only_keeps_one_evidence(self):
        s = _service()
        result = s.discover_batch("FB-001", [
            {"name": "sku", "type": "字符型"},
            {"name": "sku", "type": "字符型"},
            {"name": "net", "type": "数值"},
        ], source="loader_a")

        assert result["ok"] is True
        assert result["summary"]["声明条数"] == 3
        assert result["summary"]["批内重复条数"] == 1
        assert result["summary"]["字典列数"] == 2
        assert len(s.get_evidence(asset_code="FB-001", source="loader_a")) == 2
        assert len(s.get_details(asset_code="FB-001", source="loader_a")) == 2
        assert len(s.get_asset_dictionary("FB-001")) == 2

    def test_reimporting_same_source_reuses_the_same_evidence(self):
        s = _service()
        first = s.discover_batch("FB-002", [
            {"name": "sku", "type": "字符型"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], source="loader_a")
        batch_no = first["batch"]["批次编号"]

        second = s.discover_batch("FB-002", [
            {"name": "sku", "type": "字符型"},
        ], source="loader_a", batch_id=batch_no)

        assert second["summary"]["重复导入条数"] == 1
        assert len(s.get_evidence(asset_code="FB-002", source="loader_a", field_name="sku")) == 1
        assert len(s.get_evidence(asset_code="FB-002", source="loader_a")) == 3
        assert len(s.get_asset_dictionary("FB-002")) == 3
        evidence = _evidence(s, "FB-002", "sku", "loader_a")
        assert evidence["命中次数"] == 2
        assert evidence["首次批次"] == batch_no

    def test_same_field_from_different_sources_keeps_two_evidence_rows(self):
        s = _service()
        s.discover_batch("FB-003", [{"name": "sku", "type": "字符型"}], source="loader_a")
        s.discover_batch("FB-003", [
            {"name": "sku", "type": "字符型"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], source="loader_b")

        assert len(s.get_evidence(asset_code="FB-003", field_name="sku")) == 2
        assert len(s.get_details(asset_code="FB-003", field_name="sku")) == 2
        entry = _dict_entry(s, "FB-003", "sku")
        assert entry["来源数"] == 2
        assert entry["类型冲突"] is False
        assert len(s.get_asset_dictionary("FB-003")) == 3


class TestMixedSourceBatch:

    def test_batch_can_mix_declarations_from_two_sources(self):
        s = _service()
        result = s.discover_batch("FB-004", [
            {"name": "sku", "type": "字符型", "source": "loader_a"},
            {"name": "sku", "type": "整数", "source": "loader_b"},
            {"name": "net", "type": "数值", "source": "loader_b"},
            {"name": "tax", "type": "数值", "source": "loader_b"},
        ], source="batch_import")

        assert result["summary"]["冲突列数"] == 1
        assert result["conflicts"] == ["sku"]
        assert len(s.get_asset_dictionary("FB-004")) == 3
        assert len(s.get_evidence(asset_code="FB-004", source="loader_a")) == 1
        assert len(s.get_evidence(asset_code="FB-004", source="loader_b")) == 3

    def test_primary_key_only_submission_then_full_schema_then_resubmit(self):
        """一次提交主键清单和全列结构，再重送主键清单，字典仍按来源对齐。"""
        s = _service()
        first = s.discover_batch("FB-005", [
            {"name": "order_id", "type": "bigint", "source": "loader_a"},
            {"name": "order_id", "type": "bigint", "source": "loader_b"},
            {"name": "sku", "type": "字符型", "source": "loader_b"},
            {"name": "net", "type": "decimal(12,2)", "source": "loader_b"},
            {"name": "tax", "type": "decimal(12,2)", "source": "loader_b"},
        ], source="batch_import")
        assert first["summary"]["字典列数"] == 4
        assert first["summary"]["冲突列数"] == 0

        second = s.discover_batch("FB-005", [
            {"name": "order_id", "type": "bigint"},
        ], source="loader_a")
        assert second["summary"]["重复导入条数"] == 1
        assert len(s.get_asset_dictionary("FB-005")) == 4
        assert len(s.get_evidence(asset_code="FB-005")) == 5  # 2 来源的 order_id + B 的三列
        assert len(s.get_details(asset_code="FB-005")) == 5
        assert s.reconcile_catalog("FB-005")["一致"] is True


class TestTypeConflictTraceability:

    def test_conflicting_type_keeps_both_declarations_queryable(self):
        s = _service()
        s.discover_batch("FB-010", [{"name": "sku", "type": "字符型"}], source="loader_a")
        result = s.discover_batch("FB-010", [
            {"name": "sku", "type": "整数"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], source="loader_b")

        assert result["conflicts"] == ["sku"]
        entry = _dict_entry(s, "FB-010", "sku")
        assert entry["类型冲突"] is True
        assert entry["status"] == "解析异常"
        assert entry["abnormal"] is True
        # 既有的公开发布类型不被不兼容声明覆盖
        assert entry["字段类型"] == "字符型"

        declared = {
            (d["来源"], d["类型"]) for d in __import__("json").loads(entry["冲突声明"])
        }
        assert declared == {("loader_a", "字符型"), ("loader_b", "整数")}

        for source, expected_type in (("loader_a", "字符型"), ("loader_b", "整数")):
            detail = s.get_details(asset_code="FB-010", source=source, field_name="sku")[0]
            assert detail["是否冲突"] is True
            assert detail["声明类型"] == expected_type
            evidence = s.get_evidence(
                asset_code="FB-010", source=source, field_name="sku")[0]
            assert evidence["声明类型"] == expected_type
            assert detail["依据编号"] == evidence["依据编号"]

    def test_second_submission_from_same_source_keeps_dispute_and_tax_evidence(self):
        """第二次收到 A 的结构时，B 的 tax 列和 sku 争议仍有出处。"""
        s = _service()
        s.discover_batch("FB-011", [{"name": "sku", "type": "字符型"}], source="loader_a")
        s.discover_batch("FB-011", [
            {"name": "sku", "type": "整数"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], source="loader_b")
        tax_before = _evidence(s, "FB-011", "tax", "loader_b")

        resend = s.discover_batch("FB-011", [{"name": "sku", "type": "字符型"}], source="loader_a")

        assert resend["conflicts"] == ["sku"]
        assert resend["summary"]["重复导入条数"] == 1
        assert resend["summary"]["字典列数"] == 3

        tax_after = s.get_evidence(asset_code="FB-011", source="loader_b", field_name="tax")
        assert len(tax_after) == 1
        assert tax_after[0]["依据编号"] == tax_before["依据编号"]
        assert tax_after[0]["声明类型"] == "数值"

        entry = _dict_entry(s, "FB-011", "sku")
        assert entry["类型冲突"] is True
        assert entry["来源数"] == 2
        assert entry["字段类型"] == "字符型"

        loader_b_detail = s.get_details(
            asset_code="FB-011", source="loader_b", field_name="sku")[0]
        assert loader_b_detail["是否冲突"] is True
        assert loader_b_detail["依据编号"] == _evidence(
            s, "FB-011", "sku", "loader_b")["依据编号"]

    def test_compatible_widening_publishes_without_conflict(self):
        s = _service()
        s.discover_batch("FB-012", [{"name": "amount", "type": "integer"}], source="loader_a")
        result = s.discover_batch(
            "FB-012", [{"name": "amount", "type": "decimal(12,2)"}], source="loader_b")
        assert result["conflicts"] == []
        entry = _dict_entry(s, "FB-012", "amount")
        assert entry["类型冲突"] is False
        assert normalize(entry["字段类型"]) == "decimal"


class TestCatalogConsistency:

    def test_catalog_and_details_stay_aligned(self):
        s = _service()
        s.discover_batch("FB-020", [
            {"name": "sku", "type": "字符型"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], source="loader_a")
        s.discover_batch("FB-020", [{"name": "sku", "type": "整数"}], source="loader_b")

        reconciled = s.reconcile_catalog("FB-020")
        assert reconciled["一致"] is True
        assert reconciled["仅编目存在"] == []
        assert reconciled["仅发现存在"] == []
        sku = next(item for item in reconciled["字段"] if item["字段名称"] == "sku")
        assert sku["来源"] == ["loader_a", "loader_b"]
        assert len(sku["采集依据"]) == 2

        batches = s.list_batches("FB-020")
        assert len(batches) == 2
        assert all(batch["编目与发现一致"] is True for batch in batches)

    def test_legacy_discover_schema_still_returns_indexed_entries(self):
        s = _service()
        entries = s.discover_schema("FB-021", [
            {"name": "id", "type": "bigint"},
            {"name": "name", "type": "string"},
        ])
        assert [entry["字段名称"] for entry in entries] == ["id", "name"]
        # 重复调用不再追加重复列
        entries = s.discover_schema("FB-021", [
            {"name": "id", "type": "bigint"},
        ])
        assert [entry["字段名称"] for entry in entries] == ["id", "name"]


def normalize(value):
    from app.services.field_types import normalize_type
    return normalize_type(value)
