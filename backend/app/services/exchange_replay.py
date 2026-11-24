"""历史重放领域服务：按归档批次血缘明细校验卷内成员权限并执行可回取范围重放。

除了按范围回放归档产物外，本模块维护「重放可回取范围凭据」：
凭据按归档卷成员批次签发，记录每个成员的源端血缘、当时归属的接收方与一次性回取资格。
卷内批次发生转交后，旧凭据不会被自动撤销；执行时按批次当前归属重新判定，
只把本次确认有权取得的实际记录写入目标端，受交接影响的成员进入待审批队列，
交付回执保留生成当时的资格快照，后续变更不会回写历史回执。
"""
from __future__ import annotations

import copy
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 以下账本只在本模块内部使用，不进入业务模块清单和运营概览。
CREDENTIAL_TABLE = "_replay_credentials"
HANDOVER_TABLE = "_replay_handovers"
RECEIPT_TABLE = "_replay_receipts"
APPROVAL_TABLE = "_replay_approvals"

ACTIVE_CREDENTIAL_STATUSES = ("有效", "部分有效", "需重新许可")


def _now() -> float:
    return time.time()


def _iso(ts: float | int | str | None) -> str:
    if ts in (None, ""):
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class ExchangeReplayService(BaseModuleService):
    MODULE = "exchange_replay"
    ENTITY = "重放任务"
    OPERATION_TERM = "归档"
    LIST_FIELDS = ["重放编号", "归档编号", "重放范围", "起始批次", "截止批次", "重放速率", "覆盖方式", "重放结果"]
    REQUIRED_FIELDS = ["重放编号", "归档编号", "重放范围"]
    STATUS_ORDER = ["草稿", "待重放", "重放中", "已完成", "已取消"]
    ACTION_RULES = {"启动重放": "重放中", "标记完成": "已完成", "取消重放": "已取消"}
    NEGATIVE_ACTIONS = ["取消重放"]

    # ------------------------------------------------------------------
    # 凭据、交接与回执账本
    # ------------------------------------------------------------------

    def _credential_rows(self) -> list[dict[str, Any]]:
        return store.rows(CREDENTIAL_TABLE)

    def _handover_rows(self) -> list[dict[str, Any]]:
        return store.rows(HANDOVER_TABLE)

    def _receipt_rows(self) -> list[dict[str, Any]]:
        return store.rows(RECEIPT_TABLE)

    def _approval_rows(self) -> list[dict[str, Any]]:
        return store.rows(APPROVAL_TABLE)

    def _find_credential(self, credential_code: str | None) -> dict[str, Any] | None:
        if not credential_code:
            return None
        for row in self._credential_rows():
            if row.get("凭据编号") == credential_code:
                return row
        return None

    def _latest_handover(self, batch_code: str) -> dict[str, Any] | None:
        candidates = [row for row in self._handover_rows() if row.get("批次编号") == batch_code]
        if not candidates:
            return None
        return max(candidates, key=lambda row: float(row.get("交接时间", 0)))

    def _credential_for_member(self, batch_code: str, receiver: str) -> str:
        """找到覆盖该成员、签给原接收方且仍有效的凭据，用于登记“旧任务凭据”。"""
        candidates = [
            row for row in self._credential_rows()
            if row.get("签发对象") == receiver
            and row.get("状态") in ACTIVE_CREDENTIAL_STATUSES
            and batch_code in (row.get("可回取范围") or [])
            and batch_code not in (row.get("已回取成员") or [])
        ]
        if not candidates:
            return ""
        latest = max(candidates, key=lambda row: float(row.get("签发时间", 0)))
        return str(latest.get("凭据编号", ""))

    def _lineage_for_member(self, batch_code: str) -> dict[str, Any] | None:
        """按批次编号查传输血缘明细，优先取已生效链路。"""
        rows = [row for row in store.rows("transfer_lineage") if row.get("传输批次") == batch_code]
        if not rows:
            return None
        active = [row for row in rows if row.get("status") == "已生效"]
        return (active or rows)[-1]

    def _member_lineage(self, batch_code: str) -> dict[str, Any]:
        lineage = self._lineage_for_member(batch_code)
        batch = next(
            (row for row in store.rows("transfer_batch") if row.get("批次编号") == batch_code),
            None,
        )
        return {
            "批次编号": batch_code,
            "源端编号": (lineage or {}).get("源端编号") or (batch or {}).get("源端编号", ""),
            "目标编号": (lineage or {}).get("目标编号") or (batch or {}).get("目标编号", ""),
            "血缘链路": (lineage or {}).get("链路编号", ""),
        }

    def _current_owner(self, batch_code: str, lineage_target: str = "") -> str:
        """批次当前归属接收方：最近一次转交优先，其次取血缘登记的目标端。"""
        handover = self._latest_handover(batch_code)
        if handover is not None:
            return str(handover.get("新接收方", ""))
        return lineage_target or str(self._member_lineage(batch_code).get("目标编号", ""))

    def _archive_entry(self, archive_code: str) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows("history_archive") if row.get("归档编号") == archive_code),
            None,
        )

    def _archive_members(self, archive_code: str) -> list[str]:
        archive = self._archive_entry(archive_code)
        if archive is None:
            return []
        explicit = archive.get("批次清单") or []
        if explicit:
            return [str(code) for code in explicit]
        # 兼容只登记了批次范围的历史归档卷：优先按血缘明细反查，再退回按连接符切分。
        batch_range = str(archive.get("批次范围", ""))
        lineage_members = [
            str(row.get("传输批次"))
            for row in store.rows("transfer_lineage")
            if row.get("传输批次") and str(row.get("传输批次")) in batch_range
        ]
        if lineage_members:
            return list(dict.fromkeys(lineage_members))
        if not batch_range or batch_range == "empty":
            return []
        return [part for part in batch_range.split("-") if part]

    def _archive_member_details(self, archive_code: str) -> list[dict[str, Any]]:
        archive = self._archive_entry(archive_code) or {}
        details = archive.get("成员明细") or []
        if details:
            return [dict(item) for item in details]
        return [self._member_lineage(code) for code in self._archive_members(archive_code)]

    def _read_archive_records(self, archive_code: str) -> list[dict[str, Any]]:
        """读取归档卷聚合产物；产物缺失时按卷内成员生成模拟记录。"""
        archive_dir = self._data_dir("archive")
        for candidate in archive_dir.iterdir():
            # 归档编号可能互为前缀（HIST-123 与 HIST-123-2），必须精确匹配目录名。
            if candidate.is_dir() and candidate.name == archive_code:
                for artifact in sorted(candidate.glob("*.jsonl")):
                    records = self._read_jsonl(artifact)
                    if records:
                        return records
        records: list[dict[str, Any]] = []
        for member in self._archive_members(archive_code):
            records.extend(self._simulate_records(member, count=5, field_prefix="replay"))
        return records

    @staticmethod
    def _record_belonging(record: dict[str, Any], batch_code: str) -> bool:
        return batch_code in {
            str(record.get("batch_code", "")),
            str(record.get("batch_key", "")),
            str(record.get("批次编号", "")),
        }

    def _append_receipt(self, receipt: dict[str, Any]) -> dict[str, Any]:
        rows = self._receipt_rows()
        receipt["id"] = _next_id(rows)
        receipt["回执编号"] = f"RCPT-{receipt['重放编号']}-{receipt['id']}"
        rows.append(receipt)
        return receipt

    # ------------------------------------------------------------------
    # 重放可回取范围凭据
    # ------------------------------------------------------------------

    def issue_replay_credential(
        self,
        archive_code: str,
        receiver: str,
        members: list[str] | None = None,
        one_time: bool = True,
        ttl_seconds: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """签发重放可回取范围凭据：按归档卷成员逐项登记血缘归属与一次性回取资格。

        凭据只签发给成员的当前归属接收方；卷内任一成员已转交他人时签发直接失败，
        避免用一份凭据覆盖不属于该接收方的成员。归档时的源端血缘仍原样保留在
        成员明细里，当前归属另记在「签发时归属」字段。
        """
        archive = self._archive_entry(archive_code)
        if archive is None:
            raise ValueError(f"归档卷 {archive_code} 不存在，无法签发重放凭据")

        member_codes = [str(code) for code in (members or self._archive_members(archive_code))]
        if not member_codes:
            raise ValueError(f"归档卷 {archive_code} 没有可签发凭据的成员批次")

        archive_details = {
            str(item.get("批次编号")): item for item in self._archive_member_details(archive_code)
        }
        details: list[dict[str, Any]] = []
        for batch_code in member_codes:
            lineage = dict(archive_details.get(batch_code) or self._member_lineage(batch_code))
            lineage.setdefault("批次编号", batch_code)
            owner = self._current_owner(batch_code, str(lineage.get("目标编号", "")))
            if owner and owner != receiver:
                raise ValueError(
                    f"批次 {batch_code} 当前归属 {owner}，不能签发给 {receiver} 的重放凭据"
                )
            details.append({
                "批次编号": batch_code,
                "源端编号": lineage.get("源端编号", ""),
                "目标编号": lineage.get("目标编号", ""),
                "血缘链路": lineage.get("血缘链路", ""),
                "签发时归属": owner or lineage.get("目标编号", ""),
                "成员状态": "待回取",
            })

        issued_at = _now()
        rows = self._credential_rows()
        credential = {
            "id": _next_id(rows),
            "凭据编号": f"CRED-{archive_code}-{len(rows) + 1}",
            "凭据类型": "重放可回取范围凭据",
            "归档编号": archive_code,
            "可回取范围": member_codes,
            "成员明细": details,
            "血缘关联": [
                {
                    "批次编号": item["批次编号"],
                    "源端编号": item.get("源端编号", ""),
                    "目标编号": item.get("目标编号", ""),
                    "血缘链路": item.get("血缘链路", ""),
                }
                for item in details
            ],
            "签发对象": receiver,
            "一次性": one_time,
            "状态": "有效",
            "已回取成员": [],
            "未回取成员": [],
            "签发时间": issued_at,
            "签发时间文本": _iso(issued_at),
            "有效期至": issued_at + ttl_seconds if ttl_seconds else "",
            "有效期至文本": _iso(issued_at + ttl_seconds) if ttl_seconds else "",
            "关联重放编号": "",
            "关联交接": [],
            "备注": note,
        }
        rows.append(credential)
        self._write_jsonl(
            self._data_dir("events") / "replay_credential_log.jsonl",
            [{
                "action": "issue_credential",
                "credential_code": credential["凭据编号"],
                "archive_code": archive_code,
                "receiver": receiver,
                "members": member_codes,
                "one_time": one_time,
                "at": issued_at,
            }],
        )
        return {
            "credential_code": credential["凭据编号"],
            "credential_id": credential["id"],
            "archive_code": archive_code,
            "receiver": receiver,
            "members": member_codes,
            "member_details": copy.deepcopy(details),
            "one_time": one_time,
            "status": credential["状态"],
            "issued_at": issued_at,
            "issued_at_text": credential["签发时间文本"],
        }

    # 兼容不同调用方对“签发凭据”的命名。
    create_replay_credential = issue_replay_credential

    def list_replay_credentials(
        self,
        archive_code: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询重放可回取范围凭据，可按归档卷和状态过滤。"""
        rows = self._credential_rows()
        if archive_code:
            rows = [row for row in rows if row.get("归档编号") == archive_code]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return copy.deepcopy(rows)

    # ------------------------------------------------------------------
    # 批次转交
    # ------------------------------------------------------------------

    def transfer_batch(
        self,
        batch_code: str,
        from_receiver: str,
        to_receiver: str,
        archive_code: str = "",
        credential_code: str = "",
        revoke_old_credential: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        """把归档卷内的一个成员批次转交给另一接收方。

        转交只登记新的归属，不会顺手撤销原任务凭据；只有显式
        ``revoke_old_credential=True`` 时才动旧凭据。这样交接前签出的
        进行中任务仍可凭原凭据执行未受影响成员，受交接影响的成员转入审批。
        """
        if not batch_code or not to_receiver:
            raise ValueError("转交必须指定批次编号和目标接收方")
        if from_receiver == to_receiver:
            raise ValueError("原接收方与目标接收方不能相同")
        if not credential_code:
            credential_code = self._credential_for_member(batch_code, from_receiver)

        if archive_code:
            members = self._archive_members(archive_code)
            if members and batch_code not in members:
                raise ValueError(f"批次 {batch_code} 不在归档卷 {archive_code} 的成员范围内")
        else:
            archive_code = next(
                (str(row.get("归档编号", "")) for row in store.rows("history_archive")
                 if batch_code in (row.get("批次清单") or [])),
                "",
            )

        lineage = self._member_lineage(batch_code)
        current_owner = self._current_owner(batch_code, str(lineage.get("目标编号", "")))
        if current_owner and current_owner != from_receiver:
            raise ValueError(
                f"批次 {batch_code} 当前归属 {current_owner}，不能由 {from_receiver} 转交"
            )

        credential = self._find_credential(credential_code)
        if credential_code and credential is None:
            raise ValueError(f"重放凭据 {credential_code} 不存在")

        timestamp = _now()
        rows = self._handover_rows()
        handover = {
            "id": _next_id(rows),
            "交接编号": "",
            "归档编号": archive_code,
            "批次编号": batch_code,
            "源端编号": lineage.get("源端编号", ""),
            "原接收方": from_receiver,
            "新接收方": to_receiver,
            "旧任务凭据": credential_code,
            "撤销旧任务凭据": bool(revoke_old_credential),
            "交接时间": timestamp,
            "交接时间文本": _iso(timestamp),
            "状态": "已转交",
            "备注": note,
        }
        handover["交接编号"] = f"HAND-{batch_code}-{handover['id']}"
        rows.append(handover)

        if credential is not None:
            credential.setdefault("关联交接", []).append(handover["交接编号"])
            if revoke_old_credential:
                credential["状态"] = "已撤销"
                for member in credential.get("成员明细", []):
                    if member.get("批次编号") == batch_code:
                        member["成员状态"] = "已撤销"
                if batch_code not in credential.setdefault("未回取成员", []):
                    credential["未回取成员"].append(batch_code)

        self._write_jsonl(
            self._data_dir("events") / "replay_handover_log.jsonl",
            [{
                "action": "transfer_batch",
                "handover_code": handover["交接编号"],
                "batch_code": batch_code,
                "archive_code": archive_code,
                "from_receiver": from_receiver,
                "to_receiver": to_receiver,
                "credential_code": credential_code,
                "old_credential_revoked": bool(revoke_old_credential),
                "at": timestamp,
            }],
        )

        old_status = credential.get("状态") if credential else "未关联"
        return {
            "handover_code": handover["交接编号"],
            "handover_id": handover["id"],
            "batch_code": batch_code,
            "archive_code": archive_code,
            "from_receiver": from_receiver,
            "to_receiver": to_receiver,
            "old_credential_code": credential_code,
            "old_credential_status": old_status,
            "old_credential_revoked": bool(revoke_old_credential),
            "status": handover["状态"],
            "handed_over_at": timestamp,
            "handed_over_at_text": handover["交接时间文本"],
        }

    # 兼容“交接”命名。
    handover_batch = transfer_batch

    def list_replay_handovers(self, archive_code: str | None = None) -> list[dict[str, Any]]:
        rows = self._handover_rows()
        if archive_code:
            rows = [row for row in rows if row.get("归档编号") == archive_code]
        return copy.deepcopy(rows)

    # ------------------------------------------------------------------
    # 重放执行
    # ------------------------------------------------------------------

    def _resolve_replay_credential(
        self,
        replay_entry: dict[str, Any],
        archive_code: str,
        receiver: str,
    ) -> dict[str, Any] | None:
        """解析任务关联的重放凭据。

        只认任务上显式登记（或凭据回写关联重放编号）的凭据，不按接收方
        猜测可用凭据：授权必须逐次确认，不能因为归档卷上恰好存在一份
        有效凭据就自动扩大本次任务的回取范围。
        """
        explicit = replay_entry.get("重放凭据") or replay_entry.get("凭据编号")
        if explicit:
            credential = self._find_credential(str(explicit))
            if credential is None:
                raise ValueError(f"重放凭据 {explicit} 不存在")
            return credential

        replay_code = str(replay_entry.get("重放编号", ""))
        linked = [
            row for row in self._credential_rows()
            if row.get("关联重放编号") == replay_code
            and row.get("状态") in ACTIVE_CREDENTIAL_STATUSES
        ]
        if not linked:
            return None
        return max(linked, key=lambda row: float(row.get("签发时间", 0)))

    @staticmethod
    def _credential_expired(credential: dict[str, Any], at: float) -> bool:
        expires_at = float(credential.get("有效期至") or 0)
        return bool(expires_at) and at > expires_at

    def _deny_reason(
        self,
        credential: dict[str, Any],
        batch_code: str,
        receiver: str,
        lineage_target: str,
        owner: str,
        handover: dict[str, Any] | None,
        now: float,
    ) -> str:
        if credential.get("状态") == "已撤销":
            return "重放凭据已撤销，需重新申请许可"
        if self._credential_expired(credential, now):
            return f"重放凭据已于 {credential.get('有效期至文本', '')} 过期，需重新申请许可"
        if credential.get("一次性") and batch_code in credential.get("已回取成员", []):
            return "该成员已按一次性凭据回取，不可重复回取"
        if handover is not None and handover.get("新接收方") != receiver:
            return (
                f"批次已转交至 {handover.get('新接收方')}，原凭据未撤销但归属已变更，"
                f"需由 {handover.get('新接收方')} 重新申请重放许可"
            )
        if owner and owner != receiver:
            return f"批次当前归属 {owner}，与本次接收方 {receiver} 不一致"
        if lineage_target and lineage_target != receiver:
            return f"批次血缘归属 {lineage_target}，与本次接收方 {receiver} 不一致"
        return "本次凭据未覆盖该成员，无法确认回取权限"

    def _create_approval(
        self,
        replay_code: str,
        archive_code: str,
        credential_code: str,
        batch_code: str,
        receiver: str,
        owner: str,
        reason: str,
        handover: dict[str, Any] | None,
    ) -> dict[str, Any]:
        for row in self._approval_rows():
            if (row.get("重放编号") == replay_code
                    and row.get("受影响成员") == batch_code
                    and row.get("状态") == "待审批"):
                return row
        rows = self._approval_rows()
        created_at = _now()
        approval = {
            "id": _next_id(rows),
            "审批编号": "",
            "重放编号": replay_code,
            "归档编号": archive_code,
            "凭据编号": credential_code,
            "受影响成员": batch_code,
            "原接收方": receiver,
            "现接收方": owner,
            "未取原因": reason,
            "需补事项": (
                f"由 {owner or '有权接收方'} 重新申请批次 {batch_code} 的重放可回取范围凭据"
            ),
            "交接编号": (handover or {}).get("交接编号", ""),
            "状态": "待审批",
            "创建时间": created_at,
            "创建时间文本": _iso(created_at),
        }
        approval["审批编号"] = f"APPR-{replay_code}-{approval['id']}"
        rows.append(approval)
        self._write_jsonl(
            self._data_dir("events") / "replay_approval_log.jsonl",
            [{
                "action": "request_approval",
                "approval_code": approval["审批编号"],
                "replay_code": replay_code,
                "member": batch_code,
                "reason": reason,
                "at": created_at,
            }],
        )
        return approval

    def execute_replay(self, replay_id: int) -> dict[str, Any]:
        """执行历史重放：按归档血缘与重放可回取范围凭据恢复获准成员。

        有凭据时逐成员校验血缘归属与当前接收方：
        - 归属一致且凭据未失效的成员才写入目标端重放产物；
        - 受转交影响或权限冲突的成员不写入，登记待审批事项并写入回执未取原因；
        - 回执冻结生成当时的凭据状态与可回取范围，之后撤销凭据不会回写历史回执。

        没有可用凭据时保留原有按归档卷整体回放的行为，供存量任务兼容。
        """
        replay_entry = self.get_entry(replay_id)
        if replay_entry is None:
            return {"error": f"重放任务 {replay_id} 不存在"}

        entry, msg = self.run_action(replay_id, "启动重放")
        if entry is None:
            return {"error": msg, "replay_id": replay_id}

        archive_code = replay_entry["归档编号"]
        replay_range = replay_entry["重放范围"]
        mode = replay_entry["覆盖方式"]
        replay_code = replay_entry["重放编号"]
        receiver = str(replay_entry.get("接收方", "") or "")
        credential = self._resolve_replay_credential(replay_entry, archive_code, receiver)
        if credential is not None:
            receiver = receiver or str(credential.get("签发对象", ""))
            return self._execute_with_credential(
                replay_entry, entry, credential, receiver,
            )
        if self.list_replay_credentials(archive_code=archive_code):
            # 该归档卷已经进入凭据授权管理，不能再按整卷兜底回放，否则会绕过归属校验。
            members = self._archive_members(archive_code)
            entry["重放结果"] = "缺少重放可回取范围凭据，未执行"
            entry["未取成员"] = "、".join(members)
            self.run_action(replay_entry["id"], "取消重放")
            return {
                "error": (
                    f"归档卷 {archive_code} 已按重放可回取范围凭据管理，"
                    "本次任务没有可用凭据，未恢复任何记录"
                ),
                "replay_code": replay_code,
                "archive_code": archive_code,
                "records_replayed": 0,
                "credential_code": "",
                "denied_members": [
                    {"批次编号": code, "未取原因": "重放任务未关联有效凭据"}
                    for code in members
                ],
            }
        return self._execute_legacy(replay_entry, entry, archive_code, replay_range, mode)

    def _execute_legacy(
        self,
        replay_entry: dict[str, Any],
        entry: dict[str, Any],
        archive_code: str,
        replay_range: str,
        mode: str,
    ) -> dict[str, Any]:
        """存量兼容路径：没有重放可回取范围凭据时，按归档卷整体回放。"""
        archive_records = self._read_archive_records(archive_code)
        if not archive_records:
            archive_records = self._simulate_records(archive_code, count=10, field_prefix="replay")

        replay_file = self._data_dir("replay") / f"{replay_entry['重放编号']}.jsonl"
        if mode == "overwrite":
            replay_file.write_text("", encoding="utf-8")
            written = self._write_jsonl(replay_file, archive_records)
        else:
            combined = self._read_jsonl(replay_file) + archive_records
            replay_file.write_text("", encoding="utf-8")
            written = self._write_jsonl(replay_file, combined)

        entry["重放结果"] = f"已重放{written}条记录"
        self.run_action(replay_entry["id"], "标记完成")
        return {
            "replay_code": replay_entry["重放编号"],
            "archive_code": archive_code,
            "mode": mode,
            "records_replayed": written,
            "artifact_path": str(replay_file),
            "credential_code": "",
        }

    def _execute_with_credential(
        self,
        replay_entry: dict[str, Any],
        entry: dict[str, Any],
        credential: dict[str, Any],
        receiver: str,
    ) -> dict[str, Any]:
        archive_code = replay_entry["归档编号"]
        replay_code = replay_entry["重放编号"]
        mode = replay_entry["覆盖方式"]
        credential_code = str(credential.get("凭据编号", ""))
        credential_snapshot = copy.deepcopy(credential)
        issued_at = float(credential.get("签发时间", 0) or 0)

        archive_details = {
            str(item.get("批次编号")): item
            for item in self._archive_member_details(archive_code)
        }
        credential_members = [str(code) for code in credential.get("可回取范围", [])]
        archive_members = self._archive_members(archive_code) or credential_members
        # 卷内成员与凭据范围的交集才是本次可能回取的集合；卷内新增成员不在凭据内。
        candidate_members = [code for code in archive_members if code in credential_members]
        for code in credential_members:
            if code not in candidate_members:
                candidate_members.append(code)

        archive_records = self._read_archive_records(archive_code)
        authorized: list[str] = []
        denied: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        approval_codes: list[str] = []
        authorized_records: list[dict[str, Any]] = []
        pre_handover_task = False
        executed_at = _now()

        for batch_code in candidate_members:
            detail = dict(archive_details.get(batch_code) or {})
            lineage_detail = self._member_lineage(batch_code)
            for key in ("源端编号", "目标编号", "血缘链路"):
                detail.setdefault(key, lineage_detail.get(key, ""))
            lineage_target = str(detail.get("目标编号", "") or "")
            handover = self._latest_handover(batch_code)
            owner = self._current_owner(batch_code, lineage_target)

            if handover is not None and float(handover.get("交接时间", 0)) > issued_at:
                pre_handover_task = True

            conflict = (
                credential.get("状态") == "已撤销"
                or self._credential_expired(credential, executed_at)
                or (credential.get("一次性") and batch_code in credential.get("已回取成员", []))
                or (handover is not None and handover.get("新接收方") != receiver)
                or (owner and owner != receiver)
                or not owner
            )
            if conflict:
                reason = self._deny_reason(
                    credential, batch_code, receiver, lineage_target, owner, handover,
                    executed_at)
                denied.append({
                    "批次编号": batch_code,
                    "源端编号": detail.get("源端编号", ""),
                    "未取原因": reason,
                    "当前归属": owner,
                })
                # 已回取的成员属于重复执行，不再产生审批事项；
                # 归属冲突和权限缺口才转给后续审批补许可。
                needs_approval = (
                    batch_code not in credential.get("已回取成员", [])
                    and credential.get("状态") != "已撤销"
                )
                if needs_approval:
                    approval = self._create_approval(
                        replay_code, archive_code, credential_code, batch_code,
                        receiver, owner, reason, handover,
                    )
                    approvals.append(copy.deepcopy(approval))
                    approval_codes.append(approval["审批编号"])
                continue

            member_records = [
                copy.deepcopy(record) for record in archive_records
                if self._record_belonging(record, batch_code)
            ]
            for record in member_records:
                record["replay_code"] = replay_code
                record["replay_target"] = receiver
                record["replay_member"] = batch_code
            authorized_records.extend(member_records)
            authorized.append(batch_code)
            if batch_code not in credential.setdefault("已回取成员", []):
                credential["已回取成员"].append(batch_code)
            for member in credential.get("成员明细", []):
                if member.get("批次编号") == batch_code:
                    member["成员状态"] = "已回取"

        # 只把本次确认有权取得的实际记录写入接收方目标产物。
        replay_dir = self._data_dir("replay") / replay_code
        replay_dir.mkdir(parents=True, exist_ok=True)
        target_file = replay_dir / f"{receiver}.jsonl"
        if mode == "overwrite":
            target_file.write_text("", encoding="utf-8")
            written = self._write_jsonl(target_file, authorized_records)
        else:
            combined = self._read_jsonl(target_file) + authorized_records
            target_file.write_text("", encoding="utf-8")
            written = self._write_jsonl(target_file, combined)

        denied_members = [item["批次编号"] for item in denied]
        credential["未回取成员"] = list(dict.fromkeys(
            [str(code) for code in credential.get("未回取成员", [])] + denied_members))
        credential["关联重放编号"] = replay_code
        if denied_members:
            credential["状态"] = "部分有效" if authorized else "需重新许可"
        elif set(credential_members).issubset(set(credential.get("已回取成员", []))):
            credential["状态"] = "已用"

        # 原凭据在交接前签发、到执行时已有成员被转交：说明还能用到哪一步、何时重新许可。
        handover_members = [
            item["批次编号"] for item in denied
            if self._latest_handover(item["批次编号"]) is not None
            and (self._latest_handover(item["批次编号"]) or {}).get("新接收方") != receiver
        ]
        if pre_handover_task:
            relicense_text = (
                f"原凭据 {credential_code} 未被撤销，仍可用于成员 "
                f"{'、'.join(authorized) if authorized else '（无）'}；"
                f"成员 {'、'.join(handover_members) if handover_members else '（无）'} 已转交，"
                "自交接时点起需由现接收方重新许可后再回取"
            )
        else:
            relicense_text = ""

        generated_at = _now()
        receipt = self._append_receipt({
            "重放编号": replay_code,
            "凭据编号": credential_code,
            "归档编号": archive_code,
            "接收方": receiver,
            "回执类型": "交接前进行中任务回执" if pre_handover_task else "历史交付回执",
            "当时凭据状态": credential_snapshot.get("状态", ""),
            "当时签发对象": credential_snapshot.get("签发对象", ""),
            "当时可回取范围": list(credential_snapshot.get("可回取范围", [])),
            "当时一次性": bool(credential_snapshot.get("一次性", False)),
            "当时签发时间": credential_snapshot.get("签发时间", ""),
            "当时签发时间文本": credential_snapshot.get("签发时间文本", ""),
            "本次回取成员": list(authorized),
            "未取成员": copy.deepcopy(denied),
            "原凭据有效范围": list(authorized),
            "需重新许可成员": list(handover_members),
            "重新许可说明": relicense_text,
            "写入记录数": written,
            "目标产物": str(target_file),
            "审批编号": approval_codes,
            "生成时间": generated_at,
            "生成时间文本": _iso(generated_at),
        })

        entry["重放结果"] = f"已重放{written}条记录"
        entry["重放凭据"] = credential_code
        entry["接收方"] = receiver
        entry["未取成员"] = "、".join(denied_members)
        self.run_action(replay_entry["id"], "标记完成")

        self._write_jsonl(
            self._data_dir("events") / "replay_log.jsonl",
            [{
                "action": "execute_with_credential",
                "replay_code": replay_code,
                "credential_code": credential_code,
                "authorized": authorized,
                "denied": denied_members,
                "records_replayed": written,
                "receipt_code": receipt["回执编号"],
                "at": generated_at,
            }],
        )

        return {
            "replay_code": replay_code,
            "archive_code": archive_code,
            "mode": mode,
            "records_replayed": written,
            "artifact_path": str(target_file),
            "target_code": receiver,
            "接收方": receiver,
            "credential_code": credential_code,
            "credential_status": credential.get("状态", ""),
            "authorized_members": list(authorized),
            "实际回取成员": list(authorized),
            "denied_members": copy.deepcopy(denied),
            "未取成员": copy.deepcopy(denied),
            "approvals": approvals,
            "审批编号": approval_codes,
            "receipt": copy.deepcopy(receipt),
            "receipt_code": receipt["回执编号"],
            "回执编号": receipt["回执编号"],
        }

    # ------------------------------------------------------------------
    # 回执、审批与目标数据核对
    # ------------------------------------------------------------------

    def get_replay_receipt(self, receipt_code: str) -> dict[str, Any] | None:
        """读取交付回执：返回生成时冻结的资格快照，不随后续凭据变更而改写。"""
        for row in self._receipt_rows():
            if row.get("回执编号") == receipt_code:
                return copy.deepcopy(row)
        return None

    get_delivery_receipt = get_replay_receipt

    def list_replay_receipts(self, replay_code: str | None = None) -> list[dict[str, Any]]:
        rows = self._receipt_rows()
        if replay_code:
            rows = [row for row in rows if row.get("重放编号") == replay_code]
        return copy.deepcopy(rows)

    def list_replay_approvals(
        self,
        replay_code: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询待审批事项：交接冲突成员的未取原因与需补办的许可。"""
        rows = self._approval_rows()
        if replay_code:
            rows = [row for row in rows if row.get("重放编号") == replay_code]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        return copy.deepcopy(rows)

    get_pending_approvals = list_replay_approvals

    def read_target_data(
        self,
        target_code: str,
        replay_code: str | None = None,
    ) -> dict[str, Any]:
        """核对目标端实际数据：按重放编号与接收方读取已写入的重放产物。"""
        files = []
        if replay_code:
            candidate = self._data_dir("replay") / replay_code / f"{target_code}.jsonl"
            if candidate.exists():
                files.append(candidate)
        else:
            replay_root = self._data_dir("replay")
            files.extend(sorted(replay_root.glob(f"*/{target_code}.jsonl")))
        records: list[dict[str, Any]] = []
        for path in files:
            records.extend(self._read_jsonl(path))
        return {
            "target_code": target_code,
            "replay_code": replay_code or "",
            "records": copy.deepcopy(records),
            "count": len(records),
            "artifact_paths": [str(path) for path in files],
        }

    get_target_delivery = read_target_data

    def verify_target_delivery(self, replay_id: int) -> dict[str, Any]:
        """按执行回执核对实际目标数据与审批缺口。"""
        replay_entry = self.get_entry(replay_id)
        if replay_entry is None:
            return {"error": f"重放任务 {replay_id} 不存在"}
        replay_code = replay_entry["重放编号"]
        receipts = [row for row in self.list_replay_receipts(replay_code)]
        latest = receipts[-1] if receipts else {}
        receiver = latest.get("接收方", replay_entry.get("接收方", ""))
        target = self.read_target_data(str(receiver), replay_code)
        approvals = self.list_replay_approvals(replay_code=replay_code, status="待审批")
        return {
            "replay_code": replay_code,
            "target_code": receiver,
            "target_count": target["count"],
            "receipt_code": latest.get("回执编号", ""),
            "receipt_entitlement": {
                "当时凭据状态": latest.get("当时凭据状态", ""),
                "当时可回取范围": latest.get("当时可回取范围", []),
                "原凭据有效范围": latest.get("原凭据有效范围", []),
            },
            "pending_approvals": approvals,
            "approval_count": len(approvals),
        }

    # ------------------------------------------------------------------
    # 任务创建
    # ------------------------------------------------------------------

    def _unique_replay_code(self, archive_code: str) -> str:
        """生成不与既有任务或已有产物目录冲突的重放编号。"""
        base = f"EXCH-{archive_code}-{int(time.time())}"
        taken = {str(row.get("重放编号", "")) for row in store.rows(self.MODULE)}
        replay_root = self._data_dir("replay")
        candidate, suffix = base, 1
        while candidate in taken or (replay_root / candidate).exists():
            suffix += 1
            candidate = f"{base}-{suffix}"
        return candidate

    def create_replay(
        self,
        archive_code: str,
        replay_range: str,
        mode: str = "append",
        credential_code: str = "",
        target_code: str = "",
        credential: str = "",
    ) -> dict[str, Any]:
        """创建历史重放任务：指定归档卷、重放范围、覆盖方式与重放凭据。"""
        credential_code = credential_code or credential
        credential_row = self._find_credential(credential_code) if credential_code else None
        if credential_code and credential_row is None:
            raise ValueError(f"重放凭据 {credential_code} 不存在")
        target_code = target_code or str((credential_row or {}).get("签发对象", ""))

        replay_code = self._unique_replay_code(archive_code)
        values = {
            "重放编号": replay_code,
            "归档编号": archive_code,
            "重放范围": replay_range,
            "起始批次": replay_range.split("-")[0] if "-" in replay_range else replay_range,
            "截止批次": replay_range.split("-")[-1] if "-" in replay_range else replay_range,
            "重放速率": "500",
            "覆盖方式": mode,
            "重放结果": "待重放",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建重放任务失败: {missing}")
        entry["重放凭据"] = credential_code
        entry["接收方"] = target_code
        self._write_jsonl(
            self._data_dir("events") / "replay_log.jsonl",
            [{"action": "create_replay", "replay_code": replay_code, "archive_code": archive_code,
              "replay_range": replay_range, "mode": mode, "credential_code": credential_code,
              "target_code": target_code, "at": time.time()}]
        )
        return {"replay_code": replay_code, "replay_id": entry["id"],
                "archive_code": archive_code, "mode": mode,
                "credential_code": credential_code, "target_code": target_code}
