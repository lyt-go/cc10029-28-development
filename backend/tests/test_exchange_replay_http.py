"""历史重放接口层的端到端验证：凭据、转交、执行、回执与审批。

直接以 ASGI 语义调用应用，验证固定路径接口排在通用 `/api/{module}` 之前，
并且真实执行后目标端数据、待审批事项与回执资格保持一致。
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import anyio
import pytest

from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def cleanup():
    from app.seed import SEED_ROWS
    store._tables = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    yield


def call(method: str, path: str, *, query: dict[str, str] | None = None,
         payload: dict | None = None) -> tuple[int, dict]:
    """不依赖 httpx 的最小 ASGI 调用器，用于验证真实路由匹配。"""
    raw_query = urlencode(query or {}).encode()
    body = json.dumps(payload).encode() if payload is not None else b""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": raw_query,
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    anyio.run(app, scope, receive, send)

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    raw = b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(raw) if raw else {}


def _seed_sources(batch_a: str, batch_b: str) -> None:
    store.rows("transfer_lineage").extend([
        {
            "id": 931, "status": "已生效", "链路编号": "LIN-HTTP-A",
            "源端编号": "SOUR-A", "抽取窗口": "EXTR-SOUR-A",
            "传输批次": batch_a, "目标编号": "TARG-OLD",
            "字段契约": "FIEL-0003", "血缘版本": "v1.0", "采集时间": "2026-11-20",
        },
        {
            "id": 932, "status": "已生效", "链路编号": "LIN-HTTP-B",
            "源端编号": "SOUR-B", "抽取窗口": "EXTR-SOUR-B",
            "传输批次": batch_b, "目标编号": "TARG-OLD",
            "字段契约": "FIEL-0003", "血缘版本": "v1.0", "采集时间": "2026-11-20",
        },
    ])


def _create_archive(batch_a: str, batch_b: str, archive_code: str) -> None:
    status, _ = call("POST", "/api/history_archive", payload={"values": {
        "归档编号": archive_code, "批次范围": f"{batch_a}-{batch_b}",
        "归档路径": f"/archive/{archive_code}/",
    }})
    assert status == 200


def test_fixed_replay_routes_registered_before_generic_module_route():
    paths = list(app.openapi()["paths"])
    assert paths.index("/api/exchange_replay/credentials") < paths.index("/api/{module}")
    assert paths.index("/api/exchange_replay/tasks") < paths.index("/api/{module}")
    assert "/api/exchange_replay/actions" not in paths
    # 固定路径不会被通用模块路由当成模块名吞掉。
    status, body = call("GET", "/api/exchange_replay/credentials")
    assert status == 200
    assert set(body) == {"items", "total"}


def test_http_replay_credential_flow():
    stamp = str(int(time.time() * 1000))
    batch_a, batch_b = f"TRAN-HA-{stamp}", f"TRAN-HB-{stamp}"
    archive_code = f"HIST-HHTTP-{stamp}"
    _seed_sources(batch_a, batch_b)
    _create_archive(batch_a, batch_b, archive_code)

    status, credential = call("POST", "/api/exchange_replay/credentials", payload={
        "archive_code": archive_code, "receiver": "TARG-OLD"})
    assert status == 200
    assert set(credential["members"]) == {batch_a, batch_b}
    assert {item["源端编号"] for item in credential["member_details"]} == {"SOUR-A", "SOUR-B"}

    status, handover = call("POST", "/api/exchange_replay/handovers", payload={
        "archive_code": archive_code, "batch_code": batch_b,
        "from_receiver": "TARG-OLD", "to_receiver": "TARG-NEW",
        "credential_code": credential["credential_code"],
    })
    assert status == 200
    assert handover["old_credential_status"] == "有效"
    assert handover["old_credential_revoked"] is False

    status, task = call("POST", "/api/exchange_replay/tasks", payload={
        "archive_code": archive_code, "replay_range": f"{batch_a}-{batch_b}",
        "credential_code": credential["credential_code"],
    })
    assert status == 200

    status, result = call("POST", f"/api/exchange_replay/{task['replay_id']}/execute")
    assert status == 200
    assert result["authorized_members"] == [batch_a]
    assert [item["批次编号"] for item in result["denied_members"]] == [batch_b]

    status, target = call(
        "GET", f"/api/exchange_replay/{task['replay_id']}/target-data",
        query={"target_code": "TARG-OLD"},
    )
    assert status == 200
    assert target["count"] == result["records_replayed"]
    assert {row["replay_member"] for row in target["records"]} == {batch_a}

    status, delivery = call("GET", f"/api/exchange_replay/{task['replay_id']}/delivery")
    assert status == 200
    assert delivery["approval_count"] == 1
    assert delivery["receipt_entitlement"]["原凭据有效范围"] == [batch_a]

    status, receipt = call(
        "GET", f"/api/exchange_replay/receipts/{result['receipt_code']}")
    assert status == 200
    assert receipt["当时凭据状态"] == "有效"
    assert set(receipt["当时可回取范围"]) == {batch_a, batch_b}
    assert receipt["需重新许可成员"] == [batch_b]


def test_http_managed_archive_rejects_replay_without_credential():
    stamp = str(int(time.time() * 1000))
    batch_a = f"TRAN-HC-{stamp}"
    archive_code = f"HIST-HHTTPN-{stamp}"
    _seed_sources(batch_a, f"TRAN-HD-{stamp}")
    _create_archive(batch_a, f"TRAN-HD-{stamp}", archive_code)
    call("POST", "/api/exchange_replay/credentials", payload={
        "archive_code": archive_code, "receiver": "TARG-OLD"})

    status, task = call("POST", "/api/exchange_replay/tasks", payload={
        "archive_code": archive_code, "replay_range": batch_a,
        "target_code": "TARG-OLD",
    })
    assert status == 200
    status, body = call("POST", f"/api/exchange_replay/{task['replay_id']}/execute")
    assert status == 400
    assert "没有可用凭据" in body["detail"]

    status, target = call(
        "GET", f"/api/exchange_replay/{task['replay_id']}/target-data",
        query={"target_code": "TARG-OLD"},
    )
    assert status == 200
    assert target["count"] == 0


def test_http_issue_credential_rejects_non_owner():
    stamp = str(int(time.time() * 1000))
    batch_a = f"TRAN-HE-{stamp}"
    archive_code = f"HIST-HHTTPX-{stamp}"
    _seed_sources(batch_a, f"TRAN-HF-{stamp}")
    _create_archive(batch_a, f"TRAN-HF-{stamp}", archive_code)

    status, body = call("POST", "/api/exchange_replay/credentials", payload={
        "archive_code": archive_code, "receiver": "TARG-NEW"})
    assert status == 400
    assert "当前归属" in body["detail"]
