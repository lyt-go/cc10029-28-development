"""历史重放专用接口：重放可回取范围凭据、批次转交、回执与待审批事项。

通用资源路由只覆盖登记与状态动作，凭据签发、成员转交、任务执行和回执查询
需要额外的显式入口，因此单独挂一组固定路径路由，并注册在动态
`/{module}` 路由之前，避免被通用路由抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionPayload, ActionResult
from app.services import service_for

router = APIRouter(prefix="/api/exchange_replay", tags=["历史重放"])


def _service():
    service = service_for("exchange_replay")
    if service is None:  # pragma: no cover - 注册表固定包含该模块
        raise HTTPException(status_code=404, detail="历史重放模块未注册")
    return service


def _bad_request(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))


@router.post("/credentials", response_model=dict)
def issue_credential(payload: dict[str, Any]) -> dict[str, Any]:
    """按归档卷成员签发重放可回取范围凭据。"""
    service = _service()
    try:
        return service.issue_replay_credential(
            archive_code=str(payload.get("archive_code") or payload.get("归档编号") or ""),
            receiver=str(payload.get("receiver") or payload.get("接收方") or ""),
            members=payload.get("members") or payload.get("成员批次") or None,
            one_time=bool(payload.get("one_time", True)),
            ttl_seconds=payload.get("ttl_seconds") or payload.get("有效期秒数") or None,
            note=str(payload.get("note") or ""),
        )
    except ValueError as error:
        raise _bad_request(error) from error


@router.get("/credentials", response_model=dict)
def list_credentials(
    archive_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询重放可回取范围凭据及其成员明细。"""
    service = _service()
    items = service.list_replay_credentials(archive_code=archive_code, status=status)
    return {"items": items, "total": len(items)}


@router.post("/handovers", response_model=dict)
def transfer_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """把归档卷内的成员批次转交给另一接收方，默认不撤销旧任务凭据。"""
    service = _service()
    try:
        return service.transfer_batch(
            batch_code=str(payload.get("batch_code") or payload.get("批次编号") or ""),
            from_receiver=str(payload.get("from_receiver") or payload.get("原接收方") or ""),
            to_receiver=str(payload.get("to_receiver") or payload.get("新接收方") or ""),
            archive_code=str(payload.get("archive_code") or payload.get("归档编号") or ""),
            credential_code=str(payload.get("credential_code") or payload.get("凭据编号") or ""),
            revoke_old_credential=bool(
                payload.get("revoke_old_credential")
                or payload.get("撤销旧任务凭据")
                or False
            ),
            note=str(payload.get("note") or ""),
        )
    except ValueError as error:
        raise _bad_request(error) from error


@router.get("/handovers", response_model=dict)
def list_handovers(archive_code: str | None = Query(default=None)) -> dict[str, Any]:
    """查询批次转交记录。"""
    items = _service().list_replay_handovers(archive_code=archive_code)
    return {"items": items, "total": len(items)}


@router.get("/approvals", response_model=dict)
def list_approvals(
    replay_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询受交接影响、需要补充许可的待审批事项。"""
    items = _service().list_replay_approvals(replay_code=replay_code, status=status)
    return {"items": items, "total": len(items)}


@router.get("/receipts/{receipt_code}", response_model=dict)
def get_receipt(receipt_code: str) -> dict[str, Any]:
    """读取交付回执：返回生成时冻结的当时资格。"""
    receipt = _service().get_replay_receipt(receipt_code)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"回执 {receipt_code} 不存在")
    return receipt


@router.get("/receipts", response_model=dict)
def list_receipts(replay_code: str | None = Query(default=None)) -> dict[str, Any]:
    items = _service().list_replay_receipts(replay_code=replay_code)
    return {"items": items, "total": len(items)}


@router.post("/tasks", response_model=dict)
def create_replay_task(payload: dict[str, Any]) -> dict[str, Any]:
    """按归档卷与重放凭据创建重放任务。"""
    service = _service()
    try:
        return service.create_replay(
            archive_code=str(payload.get("archive_code") or payload.get("归档编号") or ""),
            replay_range=str(
                payload.get("replay_range") or payload.get("重放范围") or ""
            ),
            mode=str(payload.get("mode") or payload.get("覆盖方式") or "append"),
            credential_code=str(
                payload.get("credential_code") or payload.get("凭据编号") or ""
            ),
            target_code=str(payload.get("target_code") or payload.get("接收方") or ""),
        )
    except ValueError as error:
        raise _bad_request(error) from error


@router.post("/{replay_id}/execute", response_model=dict)
def execute_replay(replay_id: int) -> dict[str, Any]:
    """执行历史重放：按凭据恢复获准成员，写入接收方目标产物。"""
    result = _service().execute_replay(replay_id)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/{replay_id}/delivery", response_model=dict)
def verify_delivery(replay_id: int) -> dict[str, Any]:
    """核对实际目标数据、回执资格与待补审批。"""
    result = _service().verify_target_delivery(replay_id)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/{replay_id}/target-data", response_model=dict)
def read_target_data(
    replay_id: int,
    target_code: str = Query(default=""),
) -> dict[str, Any]:
    """按接收方读取某次重放实际写入的目标数据。"""
    service = _service()
    entry = service.get_entry(replay_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重放任务 {replay_id} 不存在")
    receiver = target_code or str(entry.get("接收方", ""))
    return service.read_target_data(receiver, str(entry.get("重放编号", "")))


@router.post("/{replay_id}/actions", response_model=ActionResult)
def run_replay_action(replay_id: int, payload: ActionPayload) -> ActionResult:
    """兼容统一动作契约：执行重放走专用实现，其余动作沿用状态流转。"""
    service = _service()
    action = payload.action.strip()
    if action in ("执行重放", "启动重放"):
        result = service.execute_replay(replay_id)
        if result.get("error"):
            return ActionResult(ok=False, message=str(result["error"]))
        return ActionResult(
            ok=True,
            message=f"已按凭据重放{result.get('records_replayed', 0)}条记录",
            entry=service.get_entry(replay_id),
        )
    entry, message = service.run_action(replay_id, action)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
