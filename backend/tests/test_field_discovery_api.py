"""字段发现接口测试：批次提交、重复导入、类型争议和字典核对。"""
import json

import pytest

from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def _reset():
    store.reset()
    yield
    store.reset()


def _call(method: str, path: str, payload: dict | None = None):
    """在当前环境没有 httpx 时，用 ASGI 消息直接驱动路由。"""
    import asyncio

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 123),
        "server": ("test", 80),
    }
    body = json.dumps(payload or {}).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def run():
        await app(scope, receive, send)

    asyncio.run(run())
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(raw or b"{}")


def _submit(asset, source, declarations, batch_id=None):
    payload = {"asset_code": asset, "source": source, "declarations": declarations}
    if batch_id:
        payload["batch_id"] = batch_id
    return _call("POST", "/api/metadata_index/discovery", payload)


class TestFieldDiscoveryApi:

    def test_submit_conflicting_sources_then_resubmit_key_list(self):
        status, first = _submit("api.orders", "loader_a", [
            {"name": "sku", "type": "字符型"},
        ])
        assert status == 200
        batch_no = first["batch"]["批次编号"]

        status, second = _submit("api.orders", "loader_b", [
            {"name": "sku", "type": "整数"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], batch_id=batch_no)
        assert status == 200
        assert second["summary"]["冲突列数"] == 1
        assert second["summary"]["字典列数"] == 3

        # 重送 A 的主键清单：只更新采集依据，不新增列
        status, third = _submit("api.orders", "loader_a", [
            {"name": "sku", "type": "字符型"},
        ], batch_id=batch_no)
        assert status == 200
        assert third["summary"]["重复导入条数"] == 1
        assert third["summary"]["字典列数"] == 3
        assert third["batch"]["声明条数"] == 5  # 1 + 3 + 1
        assert third["batch"]["提交轮次"] == 3

    def test_dictionary_endpoint_exposes_conflict_and_evidence(self):
        _, first = _submit("api.dict", "loader_a", [{"name": "sku", "type": "字符型"}])
        _submit("api.dict", "loader_b", [
            {"name": "sku", "type": "整数"},
            {"name": "net", "type": "数值"},
            {"name": "tax", "type": "数值"},
        ], batch_id=first["batch"]["批次编号"])

        status, dictionary = _call("GET", "/api/metadata_index/dictionary/api.dict")
        assert status == 200
        sku = next(item for item in dictionary["items"] if item["字段名称"] == "sku")
        assert sku["类型冲突"] is True
        assert sku["字段类型"] == "字符型"
        assert sku["来源数"] == 2
        assert len(sku["采集依据"].split(",")) == 2
        assert dictionary["reconcile"]["一致"] is True

        declarations = {
            (d["来源"], d["类型"]) for d in json.loads(sku["冲突声明"])
        }
        assert declarations == {("loader_a", "字符型"), ("loader_b", "整数")}

        status, batch = _call("GET", f"/api/metadata_index/discovery/{first['batch']['批次编号']}")
        assert status == 200
        tax_details = [d for d in batch["details"] if d["字段名称"] == "tax"]
        assert len(tax_details) == 1
        assert tax_details[0]["来源"] == "loader_b"
        assert batch["reconcile"]["一致"] is True

    def test_missing_asset_is_rejected(self):
        status, _ = _submit("", "loader_a", [{"name": "sku", "type": "字符型"}])
        assert status == 400
