"""历史重放可回取范围凭据的端到端验证。

覆盖两源合卷、按成员签发凭据、单批次转交、执行时的按归属授权、
待审批事项、目标端实际数据与历史回执的当时资格。
"""
from __future__ import annotations

import time

import pytest

from app.services.exchange_replay import ExchangeReplayService
from app.services.history_archive import HistoryArchiveService
from app.store import store


@pytest.fixture(autouse=True)
def cleanup():
    """每个测试前重置 store，避免不同测试的归档卷与凭据互相串味。"""
    from app.seed import SEED_ROWS
    store._tables = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    yield


def _add_lineage(batch_code: str, source_code: str, target_code: str, lineage_code: str, row_id: int):
    store.rows("transfer_lineage").append({
        "id": row_id,
        "status": "已生效",
        "链路编号": lineage_code,
        "源端编号": source_code,
        "抽取窗口": f"EXTR-{source_code}",
        "传输批次": batch_code,
        "目标编号": target_code,
        "字段契约": "FIEL-0003",
        "血缘版本": "v1.0",
        "采集时间": "2026-11-20",
        "pending": False,
        "abnormal": False,
    })


def _write_batch_records(svc: ExchangeReplayService, batch_code: str, count: int = 4):
    filepath = svc._data_dir("batches") / f"{batch_code}.jsonl"
    filepath.write_text("", encoding="utf-8")
    svc._write_jsonl(filepath, [
        {
            "batch_code": batch_code,
            "line_no": index + 1,
            "record_source": batch_code,
        }
        for index in range(count)
    ])


@pytest.fixture
def two_source_archive():
    """同一归档卷混有两条不同源端的批次，且都归旧接收方所有。"""
    suffix = str(int(time.time() * 1000))
    batch_a = f"TRAN-A-{suffix}"
    batch_b = f"TRAN-B-{suffix}"
    _add_lineage(batch_a, "SOUR-A", "TARG-OLD", f"LIN-A-{suffix}", 901)
    _add_lineage(batch_b, "SOUR-B", "TARG-OLD", f"LIN-B-{suffix}", 902)

    svc = ExchangeReplayService()
    _write_batch_records(svc, batch_a, count=4)
    _write_batch_records(svc, batch_b, count=3)

    archive = HistoryArchiveService()
    created = archive.create_archive([batch_a, batch_b])
    executed = archive.execute_archive(created["archive_id"])
    return {
        "archive_code": created["archive_code"],
        "batch_a": batch_a,
        "batch_b": batch_b,
        "archive_result": executed,
        "svc": svc,
    }


def test_archive_registers_per_member_lineage(two_source_archive):
    """归档卷应保留卷内成员各自的源端血缘，而不是整卷只记一个源端。"""
    row = next(
        item for item in store.rows("history_archive")
        if item["归档编号"] == two_source_archive["archive_code"]
    )
    details = {item["批次编号"]: item for item in row["成员明细"]}
    assert details[two_source_archive["batch_a"]]["源端编号"] == "SOUR-A"
    assert details[two_source_archive["batch_b"]]["源端编号"] == "SOUR-B"


def test_credential_covers_all_members_of_mixed_archive(two_source_archive):
    svc = two_source_archive["svc"]
    credential = svc.issue_replay_credential(two_source_archive["archive_code"], "TARG-OLD")

    assert credential["credential_code"]
    assert set(credential["members"]) == {
        two_source_archive["batch_a"], two_source_archive["batch_b"]}
    detail_sources = {
        item["批次编号"]: item["源端编号"] for item in credential["member_details"]}
    assert detail_sources[two_source_archive["batch_a"]] == "SOUR-A"
    assert detail_sources[two_source_archive["batch_b"]] == "SOUR-B"


def test_credential_cannot_be_issued_to_non_owner(two_source_archive):
    svc = two_source_archive["svc"]
    with pytest.raises(ValueError):
        svc.issue_replay_credential(two_source_archive["archive_code"], "TARG-OTHER")


def test_handover_does_not_revoke_old_credential(two_source_archive):
    svc = two_source_archive["svc"]
    credential = svc.issue_replay_credential(two_source_archive["archive_code"], "TARG-OLD")
    handover = svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=two_source_archive["archive_code"],
        credential_code=credential["credential_code"],
    )

    assert handover["old_credential_revoked"] is False
    assert handover["old_credential_status"] == "有效"
    stored = svc.list_replay_credentials(
        archive_code=two_source_archive["archive_code"])[0]
    assert stored["状态"] == "有效"


def test_execute_only_delivers_members_still_owned(two_source_archive):
    """两源合卷签一份授权，转交其中一批后执行：只写入仍归旧接收方的成员。"""
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=archive_code, credential_code=credential["credential_code"],
    )
    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        mode="append",
        credential_code=credential["credential_code"],
    )
    result = svc.execute_replay(replay["replay_id"])

    assert result["records_replayed"] == 4
    assert result["authorized_members"] == [two_source_archive["batch_a"]]
    assert [item["批次编号"] for item in result["denied_members"]] == [
        two_source_archive["batch_b"]]

    old_target = svc.read_target_data("TARG-OLD", replay["replay_code"])
    new_target = svc.read_target_data("TARG-NEW", replay["replay_code"])
    assert old_target["count"] == 4
    assert {record["replay_member"] for record in old_target["records"]} == {
        two_source_archive["batch_a"]}
    # 被转交的批次没有落到旧接收方，也没有落到新接收方（新接收方尚未取得许可）。
    assert new_target["count"] == 0


def test_conflicting_member_left_for_approval(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=archive_code, credential_code=credential["credential_code"],
    )
    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    result = svc.execute_replay(replay["replay_id"])

    approvals = svc.list_replay_approvals(replay_code=replay["replay_code"], status="待审批")
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["受影响成员"] == two_source_archive["batch_b"]
    assert approval["现接收方"] == "TARG-NEW"
    assert "转交" in approval["未取原因"]
    assert approval["审批编号"] in result["审批编号"]


def test_receipt_keeps_entitlement_at_delivery_time(two_source_archive):
    """交接前签出的进行中任务：回执说明原凭据仍有效到哪一步、何时重新许可。"""
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=archive_code, credential_code=credential["credential_code"],
    )
    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    result = svc.execute_replay(replay["replay_id"])
    receipt = svc.get_replay_receipt(result["receipt_code"])

    assert receipt["当时凭据状态"] == "有效"
    assert receipt["当时签发对象"] == "TARG-OLD"
    assert set(receipt["当时可回取范围"]) == {
        two_source_archive["batch_a"], two_source_archive["batch_b"]}
    assert receipt["原凭据有效范围"] == [two_source_archive["batch_a"]]
    assert receipt["需重新许可成员"] == [two_source_archive["batch_b"]]
    assert "重新许可" in receipt["重新许可说明"]

    # 后续不再改动这条历史回执：再签一份新凭据并执行也不回写旧回执。
    svc.issue_replay_credential(archive_code, "TARG-NEW", members=[two_source_archive["batch_b"]])
    again = svc.get_replay_receipt(result["receipt_code"])
    assert again == receipt


def test_new_owner_replay_after_relicense(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    old_credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=archive_code, credential_code=old_credential["credential_code"],
    )
    new_credential = svc.issue_replay_credential(
        archive_code, "TARG-NEW", members=[two_source_archive["batch_b"]])
    replay = svc.create_replay(
        archive_code, two_source_archive["batch_b"], mode="append",
        credential_code=new_credential["credential_code"],
    )
    result = svc.execute_replay(replay["replay_id"])

    assert result["authorized_members"] == [two_source_archive["batch_b"]]
    assert result["records_replayed"] == 3
    assert svc.read_target_data("TARG-NEW", replay["replay_code"])["count"] == 3


def test_one_time_credential_not_reused_for_same_member(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    first = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    svc.execute_replay(first["replay_id"])

    second = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    result = svc.execute_replay(second["replay_id"])

    assert result["records_replayed"] == 0
    assert {item["批次编号"] for item in result["denied_members"]} == {
        two_source_archive["batch_a"], two_source_archive["batch_b"]}
    assert all("一次性" in item["未取原因"] for item in result["denied_members"])


def test_replay_without_credential_on_managed_archive_writes_nothing(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    svc.issue_replay_credential(archive_code, "TARG-OLD")
    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        target_code="TARG-OLD",
    )
    result = svc.execute_replay(replay["replay_id"])

    assert result["records_replayed"] == 0
    # 没有凭据的任务不写入任何目标产物，也不产生归属之外的审批事项。
    target = svc.read_target_data("TARG-OLD", replay["replay_code"])
    assert target["count"] == 0
    assert target["artifact_paths"] == []


def test_verify_target_delivery_reports_gap(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=archive_code, credential_code=credential["credential_code"],
    )
    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    result = svc.execute_replay(replay["replay_id"])
    delivery = svc.verify_target_delivery(replay["replay_id"])

    assert delivery["target_code"] == "TARG-OLD"
    assert delivery["target_count"] == 4
    assert delivery["target_count"] == result["records_replayed"]
    assert delivery["approval_count"] == 1
    assert delivery["receipt_entitlement"]["当时凭据状态"] == "有效"


def test_expired_credential_requires_relicense(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD", ttl_seconds=-1)

    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    result = svc.execute_replay(replay["replay_id"])

    assert result["records_replayed"] == 0
    assert all("过期" in item["未取原因"] for item in result["denied_members"])


def test_handover_with_explicit_revoke_blocks_old_credential(two_source_archive):
    svc = two_source_archive["svc"]
    archive_code = two_source_archive["archive_code"]
    credential = svc.issue_replay_credential(archive_code, "TARG-OLD")
    result = svc.transfer_batch(
        two_source_archive["batch_b"], "TARG-OLD", "TARG-NEW",
        archive_code=archive_code, credential_code=credential["credential_code"],
        revoke_old_credential=True,
    )

    assert result["old_credential_revoked"] is True
    assert result["old_credential_status"] == "已撤销"

    replay = svc.create_replay(
        archive_code,
        f"{two_source_archive['batch_a']}-{two_source_archive['batch_b']}",
        credential_code=credential["credential_code"],
    )
    executed = svc.execute_replay(replay["replay_id"])
    assert executed["records_replayed"] == 0
