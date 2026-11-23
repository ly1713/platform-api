"""外部回传落库：把 agent 交回的报告校验后写进 ``viral_breakdown``。

这是**第二条写入路径**。它与内部链路（``breakdown_service.run_breakdown``）必须
共用同一套校验——归一化在 ``core.breakdown_contract``，两边都只调它。本模块只做
三件不共用的事：

1. **不重算数字**：``data_context`` 一律由后端自己算（外部回传里的数字被忽略）。
   理由：数字是「爆款」判定与跨报告比较的基准，允许外部改写就等于让报告不可比。
2. **逐条错误**：校验失败返回每一条的原因（第几项、期望什么、实际什么），
   外部只能靠这个自纠；一句「格式错误」在外部看来等于无解。
3. **留痕**：``produced_by="external:<agent>"`` + ``contract_version``，报告页可追溯。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events
from app.core.breakdown_contract import BREAKDOWN_CONTRACT_VERSION, normalize_report
from app.core.breakdown_settings import get_breakdown_settings
from app.models import ContentItem, MediaAsset, ViralBreakdown
from app.services import breakdown_claim as claim_svc
from app.services import breakdown_service as svc


class ReportRejected(ValueError):
    """回传的报告未通过契约校验。``errors`` 是逐条原因，直接回给外部。"""

    def __init__(self, errors: list[str]):
        super().__init__("；".join(errors) if errors else "报告未通过校验")
        self.errors = errors


def validate_report_payload(
    raw: object, *, criteria: list[dict] | None = None
) -> tuple[dict, list[str]]:
    """校验 + 归一化，返回 ``(归一化报告, 被丢弃条目的原因)``。

    Raises:
        ReportRejected: 顶层不是 JSON 对象（结构性错误，其余条目无从校验）。
    """
    rejected: list[str] = []
    try:
        report = normalize_report(raw, criteria, rejected=rejected)
    except ValueError as exc:
        raise ReportRejected([str(exc)]) from exc
    return report, rejected


def submit_report(
    db: Session,
    asset_id: int,
    *,
    lease_token: str,
    agent_name: str,
    raw_report: object,
    model: str = "",
    usage: dict | None = None,
) -> tuple[ViralBreakdown, list[str]]:
    """校验并落库外部回传的报告，返回 ``(报告行, 被丢弃条目原因)``。

    先校验再改库：一份不合格的报告不能把已有报告从 done 打回 pending，
    否则外部只要反复提交坏数据就能把平台上的报告冲掉。

    Raises:
        claim_svc.ClaimConflict: 租约不匹配/已过期/无人认领。
        ReportRejected: 报告不符合契约。
        svc.BreakdownInputError: 素材缺转录/L3 等前置（与内部路径同一组校验）。
    """
    claim = claim_svc.verify_lease(db, asset_id, lease_token)

    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise svc.BreakdownInputError(f"素材不存在: {asset_id}")
    content = db.get(ContentItem, asset.content_id)
    if content is None:
        raise svc.BreakdownInputError(
            f"素材 #{asset_id} 未关联内容（content_id={asset.content_id}）"
        )

    settings_map = get_breakdown_settings(db)
    report, rejected = validate_report_payload(
        raw_report, criteria=settings_map.get("hook_criteria")
    )

    # 数字由后端算：外部回传里的 metrics 一律忽略（料包里的数字只是给它分析用的）。
    data_context = svc._data_context(db, content, asset)

    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is None:
        row = ViralBreakdown(asset_id=asset_id)
        db.add(row)

    row.score = report["score"]
    row.verdict = report["verdict"]
    row.sections = report["sections"]
    row.comment_insights = report["comment_insights"]
    row.data_context = data_context
    row.understanding_available = svc._understanding(db, asset_id) is not None
    row.model = (model or f"external:{claim.agent_name}")[:200]
    row.prompt_version = ""
    row.usage = usage if isinstance(usage, dict) else {}
    row.routing = {
        "external": {
            "agent": claim.agent_name or agent_name,
            "attempt": claim.attempts,
            "rejected_entries": rejected,
        }
    }
    row.produced_by = f"external:{(claim.agent_name or agent_name or 'unknown')[:56]}"
    row.contract_version = BREAKDOWN_CONTRACT_VERSION
    row.status = "done"
    row.error = ""
    claim_svc.complete_claim(db, asset_id)
    events.publish(
        "breakdown_status",
        {"asset_id": asset_id, "status": "done", "produced_by": row.produced_by},
        db=db,
    )
    db.commit()
    db.refresh(row)
    return row, rejected
