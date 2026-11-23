"""拆解执行器分流：把「谁来做这条拆解」收敛成一个入口。

改造后拆解有两条执行路径，**必须只有一个地方决定走哪条**：

- ``internal``：平台自己入队 ``breakdown`` job，跑模型（改造前的行为）；
- ``external``：建一条待处理记录，等外部 agent 认领（本次改造的目的）。

如果分流逻辑散在 ``auto_breakdown`` / ``api.assets.enqueue_breakdown`` /
``handle_breakdown`` 三处，用户切开关后必然出现「自动流程走外部、手动按钮还跑内部」
这类只有某条路径才暴露的偏差。本模块是唯一的判定点。

兜底（``breakdown_internal_fallback``）：外部认领次数用尽仍无人回传时，由回收流程
调用 ``fallback_to_internal`` 入队一次内部拆解，并在报告里留痕。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events
from app.core.breakdown_settings import get_breakdown_settings
from app.models import MediaAsset, ViralBreakdown

logger = logging.getLogger("platform.breakdown")

EXECUTOR_INTERNAL = "internal"
EXECUTOR_EXTERNAL = "external"


def current_executor(db: Session) -> str:
    """当前执行器（``internal`` / ``external``）。读侧已把非法值收敛为 internal。"""
    value = get_breakdown_settings(db).get("executor")
    return EXECUTOR_INTERNAL if value == EXECUTOR_INTERNAL else EXECUTOR_EXTERNAL


def is_external(db: Session) -> bool:
    return current_executor(db) == EXECUTOR_EXTERNAL


def ensure_pending(db: Session, asset_id: int, *, force: bool = False) -> ViralBreakdown:
    """确保存在一行待拆解记录（两条执行器共用）。

    已 ``done`` 且非 force 时保持原样：重跑由用户显式点「重新生成」表达，
    自动流程不该悄悄把已有报告打成 pending。``force`` 时置 pending 并清掉
    旧报告状态，让人能重做。

    两条路径都走这里，是因为它服务的是**用户可见状态**：入队后 GET 必须立刻
    读到 ``pending``，而不是等到 worker 跑起来才出现。外部模式下更是如此——
    那一刻没有 job 会来替它写这一行。

    这里**不 commit**：调用方通常处在自己的事务里（自动流程 / API 请求），
    由它决定提交时机。
    """
    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is None:
        row = ViralBreakdown(asset_id=asset_id, status="pending")
        db.add(row)
    elif row.status != "done" or force:
        # 注意这里用 ``or`` 而不是 ``elif force or row.status != "done"``：
        # 已 done 且非 force 时必须**什么都不改**（保持 done，不重复触发事件）。
        row.status = "pending"
        row.error = ""
        events.publish("breakdown_status", {"asset_id": asset_id, "status": "pending"}, db=db)
    return row


def prepare_external(db: Session, asset_id: int, *, force: bool = False) -> ViralBreakdown:
    """外部模式下的「入队」：登记一条待处理记录，不产生队列 job。"""
    return ensure_pending(db, asset_id, force=force)


def dispatch(
    db: Session, asset_id: int, *, force: bool = False, options: dict | None = None
) -> dict:
    """按执行器分流，返回 ``{"executor": ..., ...}`` 供调用方如实回报。

    外部模式不建 job，因此 ``job_id`` 为 ``None``——这是**正确结果**，不是失败。
    调用方（API）应据此告诉用户「已排入外部队列」，而不是报「任务创建失败」。

    外部模式下前置步骤（转录、视频理解）**仍然由平台跑**：料包必须有转录与 L3
    才可分析，把前置也外移等于让 agent 干它拿不到材料的活。所以这里登记前置
    工作流，只是不登记 ``breakdown`` 本身。
    """
    from app.services.workflow_engine import (
        WorkflowInputError,
        breakdown_prerequisites_done,
        register_workflow,
    )

    if is_external(db):
        prereq_note = "prerequisites_queued"
        try:
            if not breakdown_prerequisites_done(db, asset_id):
                register_workflow(
                    db, asset_id, ["understand_video"], options={"force": False}, commit=True
                )
        except WorkflowInputError as exc:
            # 素材还没下载完：先记下待处理，前置由下载完成后的自动流程补齐。
            # 这里不抛错——用户看到的应该是「已排入外部队列」，而不是 422。
            logger.info("拆解外部排队但前置未就绪 asset=%s: %s", asset_id, exc)
            prereq_note = "prerequisites_missing"
        row = prepare_external(db, asset_id, force=force)
        db.commit()
        db.refresh(row)
        logger.info("拆解走外部执行器 asset=%s status=%s", asset_id, row.status)
        return {
            "executor": EXECUTOR_EXTERNAL,
            "status": row.status,
            "job_id": None,
            "prerequisites": prereq_note,
        }

    # 内部模式：沿用改造前的完整工作流（含前置依赖展开）。
    # 先落下 pending 行：read 接口必须立刻可读，而不是等 worker 起来才有这一行。
    ensure_pending(db, asset_id, force=force)
    merged = {"force": force, **(options or {})}
    result = register_workflow(
        db,
        asset_id,
        ["breakdown"],
        options=merged,
        force=["breakdown"] if force else (),
        commit=True,
    )
    from app.models import PipelineStep

    step = db.scalar(
        select(PipelineStep).where(
            PipelineStep.run_id == result.run.id,
            PipelineStep.step == "breakdown",
        )
    )
    return {
        "executor": EXECUTOR_INTERNAL,
        "status": step.status if step is not None else "waiting_prerequisite",
        "job_id": step.job_id if step is not None else None,
        "run_id": result.run.id,
    }


def fallback_to_internal(db: Session, asset_id: int, *, reason: str = "") -> bool:
    """外部认领用尽仍无人回传时，平台自己补跑一次。

    返回是否**已安排**兜底。``breakdown_internal_fallback`` 关闭时返回 False，
    由调用方把状态落成可见的 error，让用户知道「没人做，也没兜底」。

    留痕方式：在报告行上先写 ``error``（说明为什么兜底），内部链路跑完会把
    ``status`` 改成 done 并清空 error；兜底标记存在 ``routing`` 里由内部链路
    追加（见 ``breakdown_service.run_breakdown`` 的 ``fallback`` 参数）。
    """
    settings_map = get_breakdown_settings(db)
    if not settings_map.get("internal_fallback", True):
        return False
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        return False

    from app.services.workflow_engine import register_workflow

    try:
        register_workflow(
            db,
            asset_id,
            ["breakdown"],
            options={"force": True, "fallback": True, "fallback_reason": reason},
            force=["breakdown"],
            commit=True,
        )
    except Exception:  # noqa: BLE001 兜底失败不该把回收流程整条打断
        logger.exception("拆解兜底入队失败 asset=%s", asset_id)
        return False
    logger.warning("拆解改由平台兜底 asset=%s 原因=%s", asset_id, reason)
    return True


def mark_external_exhausted(db: Session, asset_id: int, *, attempts: int) -> ViralBreakdown | None:
    """把「外部反复认领但无人回传」落成一个**可见**的 error。

    不允许静默停在 pending：那条素材会永远显示「等待外部 Agent」，用户既不知道
    发生了什么，也不知道该做什么。
    """
    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is None:
        row = ViralBreakdown(asset_id=asset_id)
        db.add(row)
    row.status = "error"
    row.error = f"外部认领 {attempts} 次仍无人回传（可重新触发或改回内部分析）"[:512]
    events.publish(
        "breakdown_status", {"asset_id": asset_id, "status": "error"}, db=db
    )
    return row
