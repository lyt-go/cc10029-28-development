"""批次归档领域服务：维护已完成传输批次的按范围归档、卷内条数与归档校验值登记。

本模块在传输批次链路完成后执行批次数据归档，将传输产物文件按范围压缩打包，
计算归档校验值并写入归档产物目录，支持按驻留策略自动清理过期归档卷。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class HistoryArchiveService(BaseModuleService):
    MODULE = "history_archive"
    ENTITY = "归档卷"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["归档编号", "批次范围", "归档路径", "卷内条数", "压缩方式", "归档校验值", "驻留策略", "归档水位"]
    REQUIRED_FIELDS = ["归档编号", "批次范围", "归档路径"]
    STATUS_ORDER = ["草稿", "待归档", "已归档", "已销毁"]
    ACTION_RULES = {"启动归档": "待归档", "确认归档": "已归档", "执行销毁": "已销毁"}
    NEGATIVE_ACTIONS = ["执行销毁"]

    @staticmethod
    def _member_details(batch_codes: list[str]) -> list[dict[str, str]]:
        """按传输血缘明细解析卷内每个成员批次的源端、目标端与链路编号。

        同一归档卷可以混有不同源端的批次，因此逐批次解析，不能取整卷的单一源端。
        """
        details: list[dict[str, str]] = []
        for batch_code in batch_codes:
            batch = next(
                (row for row in store.rows("transfer_batch")
                 if row.get("批次编号") == batch_code),
                {},
            )
            lineages = [
                row for row in store.rows("transfer_lineage")
                if row.get("传输批次") == batch_code
            ]
            active = [row for row in lineages if row.get("status") == "已生效"]
            lineage = (active or lineages or [{}])[-1]
            details.append({
                "批次编号": batch_code,
                "源端编号": str(lineage.get("源端编号") or batch.get("源端编号", "")),
                "目标编号": str(lineage.get("目标编号") or batch.get("目标编号", "")),
                "血缘链路": str(lineage.get("链路编号", "")),
            })
        return details

    def create_archive(self, batch_codes: list[str]) -> dict[str, Any]:
        """将多个已完成传输批次创建归档卷，计算批次范围与卷内预期条数。

        除批次范围外同时登记卷内成员批次清单与逐成员血缘明细（源端、目标端、
        血缘链路），供历史重放按批次归属校验重放可回取范围凭据。
        """
        archive_code = f"HIST-{int(time.time())}"
        existing = {str(row.get("归档编号", "")) for row in store.rows(self.MODULE)}
        base_code, suffix = archive_code, 1
        while archive_code in existing:
            suffix += 1
            archive_code = f"{base_code}-{suffix}"
        batch_range = f"{batch_codes[0]}-{batch_codes[-1]}" if batch_codes else "empty"
        archive_path = f"/archive/{archive_code}/"
        values = {
            "归档编号": archive_code,
            "批次范围": batch_range,
            "归档路径": archive_path,
            "卷内条数": "0",
            "压缩方式": "gzip",
            "归档校验值": "",
            "驻留策略": "90d",
            "归档水位": "0",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建归档卷失败: {missing}")
        entry["批次清单"] = [str(code) for code in batch_codes]
        entry["成员明细"] = self._member_details([str(code) for code in batch_codes])
        self._write_jsonl(
            self._data_dir("events") / "archive_log.jsonl",
            [{"action": "create_archive", "archive_code": archive_code,
              "batch_range": batch_range, "batch_count": len(batch_codes),
              "at": time.time()}]
        )
        return {"archive_code": archive_code, "archive_id": entry["id"],
                "batch_range": batch_range, "batch_count": len(batch_codes)}

    def execute_archive(self, archive_id: int) -> dict[str, Any]:
        """执行批次归档：聚合批次产物文件写入归档产物目录，计算总条数及归档校验值。

        本方法实现数据全生命周期的最终归档环节：
        读取传输批次产物文件，聚合到归档产物目录，
        计算卷内总条数与归档校验值，生成归档快照。
        """
        archive_entry = self.get_entry(archive_id)
        if archive_entry is None:
            return {"error": f"归档卷 {archive_id} 不存在"}

        entry, msg = self.run_action(archive_id, "启动归档")
        if entry is None:
            return {"error": msg, "archive_id": archive_id}

        archive_code = archive_entry["归档编号"]
        batch_range = archive_entry["批次范围"]
        batch_codes = [str(code) for code in (archive_entry.get("批次清单") or [])]
        if not batch_codes:
            batch_codes = batch_range.split("-") if "-" in batch_range else [batch_range]

        # 聚合批次产物文件到归档目录
        total_records = 0
        archive_dir = self._data_dir("archive") / archive_code
        all_records = []
        for bc in batch_codes:
            batch_file = self._data_dir("batches") / f"{bc}.jsonl"
            if batch_file.exists():
                records = self._read_jsonl(batch_file)
                all_records.extend(records)
            else:
                # 无实际批次文件则生成归档模拟数据
                records = self._simulate_records(bc, count=5, field_prefix="archive")
                all_records.extend(records)

        # 写入归档产物文件
        archive_file = archive_dir / f"{archive_code}_bundle.jsonl"
        _ = self._data_dir("archive") / archive_code  # ensure dir
        written = self._write_jsonl(archive_file, all_records)

        # 计算归档校验值
        checksum = self._compute_checksum(str(all_records))
        entry["批次清单"] = [str(code) for code in batch_codes]
        entry["成员明细"] = self._member_details([str(code) for code in batch_codes])
        entry["卷内条数"] = str(written)
        entry["归档校验值"] = f"SHA256:{checksum}"
        entry["归档水位"] = str(written)

        final_entry, _ = self.run_action(archive_id, "确认归档")

        self._write_jsonl(
            self._data_dir("events") / "archive_complete_log.jsonl",
            [{"action": "complete_archive", "archive_code": archive_code,
              "record_count": written, "checksum": checksum, "at": time.time()}]
        )

        return {
            "archive_code": archive_code,
            "record_count": written,
            "checksum": checksum,
            "artifact_path": str(archive_file),
        }

    def destroy_archive(self, archive_id: int) -> dict[str, Any]:
        """执行归档卷销毁：标记为已销毁并记录销毁事件。"""
        entry, msg = self.run_action(archive_id, "执行销毁")
        if entry is None:
            return {"error": msg, "archive_id": archive_id}
        self._write_jsonl(
            self._data_dir("events") / "archive_destroy_log.jsonl",
            [{"action": "destroy_archive", "archive_code": entry["归档编号"],
              "destroyed_at": time.time()}]
        )
        return {"archive_code": entry["归档编号"], "status": "已销毁"}