"""拆解外移 API（``/api/agent/*``）：外部 agent 取料、认领、回传。

为什么不复用 ``/api/skills`` 前缀：Agent Skills 是「程序性知识包」，这里是
「拆解任务的取料/回传通道」，两者概念无关，混在同一前缀下会让「给 AI 的接口」
变成一团按时间堆积的路由。

工序（设计 §3.2）：

```
GET  /breakdown/pending            有哪些素材等着拆
GET  /breakdown/{asset_id}/packet  不认领先看料（只读 Key 可用）
POST /breakdown/{asset_id}/claim   认领 + 拿料 + 租约（需 breakdown scope）
POST /breakdown/{asset_id}/report  回传（必须带 lease_token）
POST /breakdown/{asset_id}/release 放弃
GET  /manifest                     工具/资源/scope 清单（前端与 MCP 同源）
```

错误语义：

- ``403`` scope 不足；``404`` 素材不存在；``409`` 时机冲突（已被他人持有 /
  租约过期 / token 不匹配 / 认领次数用尽）；``422`` 报告未通过契约校验**并逐条
  给出原因**（外部只能靠这个自纠）。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import agent_manifest as catalog
from app.core import api_auth, breakdown_contract as contract
from app.core.breakdown_settings import get_breakdown_settings
from app.db import get_db
from app.models import ApiCallLog, ApiKey, BreakdownClaim, MediaAsset, ViralBreakdown
from app.services import breakdown_claim as claim_svc
from app.services import breakdown_packet, breakdown_service as svc, breakdown_submit
from app.services.storage_lifecycle import get_live_asset

router = APIRouter(prefix="/api/agent", tags=["agent"])

# scope 划分：``read`` 能取料（先看再决定接不接），认领/回传/放弃需要 ``breakdown``。
# 「看看料」和「交作业」能分别授权，只读 Key 不会因为有 read 就能改报告。
BREAKDOWN_SCOPE = catalog.BREAKDOWN_SCOPE
READ_SCOPES = ("read", "operate", "admin")
WRITE_SCOPES = (BREAKDOWN_SCOPE, "operate", "admin")

# 单次料包的转录上限护栏。外部可以调小（省上下文），但不能调大：
# 无限放大等于把整条素材塞进 agent 上下文，出来的报告质量反而更差（设计 §6）。
MAX_CHARS_HARD_CAP = 60000


def _log(db: Session, key_id: int | None, tool: str, params: str, status: str, t0: float) -> None:
    db.add(
        ApiCallLog(
            key_id=key_id,
            tool=tool,
            params_summary=params[:500],
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
        )
    )
    db.commit()


def _key_id(key) -> int | None:
    """管理员通道没有 ApiKey 行；``require_admin_or_scope`` 会返回 ``"admin"``。"""
    return key.id if isinstance(key, ApiKey) else None


def _live_asset_or_404(db: Session, asset_id: int) -> MediaAsset:
    asset = get_live_asset(db, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    return asset


def _clamp_max_chars(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise HTTPException(status_code=422, detail="max_chars 必须为正整数")
    return min(value, MAX_CHARS_HARD_CAP)


class ReportIn(BaseModel):
    """回传体。``report`` 是拆解报告本体，结构见 ``breakdown://contract``。

    ``report`` 故意声明成 ``Any`` 而不是 ``dict``：定成 dict 时，外部传了数组/
    字符串会被 pydantic 用一套**通用**的 422 信封挡掉，而契约层本来能给出
    「拆解结果不是 JSON 对象」这种可自纠的原因。同一种错误不该有两种错误格式。
    """

    lease_token: str
    report: Any = Field(description="拆解报告对象，结构见契约（contract_version 对应）")
    model: str = ""
    usage: dict = {}


class ReleaseIn(BaseModel):
    lease_token: str
    reason: str = ""


class AgentToolOut(BaseModel):
    """一个对外工具的描述。前端据此渲染清单，不再自己维护数组。"""

    name: str
    method: str
    path: str
    scope: str
    summary: str
    group: str


class AgentGroupOut(BaseModel):
    """工具分组（标题 + 一句话说明），让前端不必按路径前缀猜分类。"""

    title: str
    note: str


class AgentResourceOut(BaseModel):
    uri: str
    summary: str


class AgentManifestOut(BaseModel):
    """工具 / 资源 / scope 清单（前端与 MCP 的单一来源）。"""

    contract_version: str
    tool_count: int
    scopes: dict[str, str]
    tools: list[AgentToolOut]
    groups: dict[str, AgentGroupOut]
    resources: list[AgentResourceOut]
    settings: dict[str, Any]


# ============================================================
# 队列与料包（read）
# ============================================================


@router.get("/breakdown/pending")
def list_pending(
    request: Request,
    limit: int = Query(default=10, ge=1, le=100),
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*READ_SCOPES)),
):
    """列出等着外部处理的素材（未认领 / 租约已过期且未用尽次数）。

    判定条件刻意只看「报告未完成」与「租约不可用」，不看 ``status``：
    ``status`` 是产物状态机（pending/doing/done/error），租约才是「现在谁在做」。
    """
    t0 = time.time()
    settings_map = get_breakdown_settings(db)
    max_attempts = int(settings_map.get("max_attempts") or 3)
    now = datetime.utcnow()

    rows = db.execute(
        select(ViralBreakdown, BreakdownClaim)
        .join(MediaAsset, MediaAsset.id == ViralBreakdown.asset_id)
        .outerjoin(BreakdownClaim, BreakdownClaim.asset_id == ViralBreakdown.asset_id)
        .where(
            MediaAsset.deleted_at.is_(None),
            ViralBreakdown.status.in_(("pending", "error")),
        )
        .order_by(ViralBreakdown.created_at.asc())
        .limit(limit * 3)
    ).all()

    out = []
    for breakdown, claim in rows:
        if claim is not None and claim_svc.is_live(claim, now):
            continue  # 别人正在做
        if claim is not None and (claim.attempts or 0) >= max_attempts:
            continue  # 认领次数用尽，等回收流程落 error，不再外放
        out.append(
            {
                "asset_id": breakdown.asset_id,
                "status": breakdown.status,
                "attempts": claim.attempts if claim is not None else 0,
                "created_at": breakdown.created_at.isoformat() if breakdown.created_at else None,
            }
        )
        if len(out) >= limit:
            break
    _log(db, _key_id(key), "agent.list_pending_breakdowns", f"limit={limit}", "ok", t0)
    return {"items": out, "count": len(out), "max_attempts": max_attempts}


@router.get("/breakdown/{asset_id}/packet")
def get_packet(
    asset_id: int,
    request: Request,
    max_chars: int | None = Query(default=None, description="转录字符上限（不得超过 60000）"),
    comment_limit: int = Query(default=breakdown_packet.DEFAULT_COMMENT_LIMIT, ge=0, le=100),
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*READ_SCOPES)),
):
    """取料包（不认领，不建租约）。适合先看料再决定要不要接。"""
    t0 = time.time()
    _live_asset_or_404(db, asset_id)
    try:
        packet = breakdown_packet.build_packet(
            db, asset_id, max_chars=_clamp_max_chars(max_chars), comment_limit=comment_limit
        )
    except svc.BreakdownInputError as exc:
        _log(db, _key_id(key), "agent.get_breakdown_packet", f"asset={asset_id}", "error", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _log(db, _key_id(key), "agent.get_breakdown_packet", f"asset={asset_id}", "ok", t0)
    return packet


# ============================================================
# 认领 / 回传 / 放弃（breakdown scope）
# ============================================================


@router.post("/breakdown/{asset_id}/claim")
def claim(
    asset_id: int,
    request: Request,
    agent_name: str = Query(default="", description="执行方名字，用于报告留痕与 UI 展示"),
    max_chars: int | None = Query(default=None),
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*WRITE_SCOPES)),
):
    """认领一条素材：返回租约 + 完整料包（一次调用拿全，避免二次往返丢上下文）。"""
    t0 = time.time()
    _live_asset_or_404(db, asset_id)
    name = (agent_name or getattr(key, "name", "") or "admin").strip() or "unknown"
    try:
        row, _ok = claim_svc.claim_breakdown(db, asset_id, agent_name=name)
    except claim_svc.ClaimExhausted as exc:
        _log(db, _key_id(key), "agent.claim_breakdown", f"asset={asset_id}", "denied", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except claim_svc.ClaimConflict as exc:
        _log(db, _key_id(key), "agent.claim_breakdown", f"asset={asset_id}", "denied", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        packet = breakdown_packet.build_packet(db, asset_id, max_chars=_clamp_max_chars(max_chars))
    except svc.BreakdownInputError as exc:
        # 认领了却给不出料包（缺转录/缺 L3）：立刻释放，别让素材被一个
        # 拿不到材料的租约占着。
        claim_svc.release_claim(
            db, asset_id, row.lease_token, reason=f"料包不可用：{exc}"
        )
        _log(db, _key_id(key), "agent.claim_breakdown", f"asset={asset_id}", "error", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _log(db, _key_id(key), "agent.claim_breakdown", f"asset={asset_id} agent={name}", "ok", t0)
    return {
        "asset_id": asset_id,
        "agent_name": row.agent_name,
        "lease_token": row.lease_token,
        "lease_until": row.lease_until.isoformat(),
        "attempts": row.attempts,
        "contract_version": contract.BREAKDOWN_CONTRACT_VERSION,
        "packet": packet,
    }


@router.post("/breakdown/{asset_id}/report")
def submit_report(
    asset_id: int,
    body: ReportIn,
    request: Request,
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*WRITE_SCOPES)),
):
    """回传报告：必须带认领时拿到的 ``lease_token``。"""
    t0 = time.time()
    _live_asset_or_404(db, asset_id)
    try:
        row, rejected = breakdown_submit.submit_report(
            db,
            asset_id,
            lease_token=body.lease_token,
            agent_name=getattr(key, "name", "") or "",
            raw_report=body.report,
            model=body.model,
            usage=body.usage,
        )
    except claim_svc.ClaimConflict as exc:
        _log(db, _key_id(key), "agent.submit_breakdown_report", f"asset={asset_id}", "denied", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except breakdown_submit.ReportRejected as exc:
        _log(db, _key_id(key), "agent.submit_breakdown_report", f"asset={asset_id}", "error", t0)
        # 逐条错误：外部只有这一份反馈可以自纠。
        raise HTTPException(
            status_code=422,
            detail={"message": "报告未通过契约校验", "errors": exc.errors},
        ) from exc
    except svc.BreakdownInputError as exc:
        _log(db, _key_id(key), "agent.submit_breakdown_report", f"asset={asset_id}", "error", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _log(db, _key_id(key), "agent.submit_breakdown_report", f"asset={asset_id}", "ok", t0)
    return {
        "asset_id": asset_id,
        "status": row.status,
        "score": row.score,
        "produced_by": row.produced_by,
        "contract_version": row.contract_version,
        # 部分条目被丢弃不算失败：报告已入库，但要让外部知道哪几条没被采纳。
        "dropped_entries": rejected,
    }


@router.post("/breakdown/{asset_id}/release")
def release(
    asset_id: int,
    body: ReleaseIn,
    request: Request,
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*WRITE_SCOPES)),
):
    """主动放弃认领：素材回到待处理队列，次数保留。"""
    t0 = time.time()
    _live_asset_or_404(db, asset_id)
    try:
        claim_svc.release_claim(db, asset_id, body.lease_token, reason=body.reason)
    except claim_svc.ClaimConflict as exc:
        _log(db, _key_id(key), "agent.release_breakdown", f"asset={asset_id}", "denied", t0)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _log(db, _key_id(key), "agent.release_breakdown", f"asset={asset_id}", "ok", t0)
    return {"asset_id": asset_id, "released": True}


# ============================================================
# 规则（read）：MCP resource 的内容由这里给，不在 server.py 复制一份
# ============================================================


@router.get("/contract", response_class=PlainTextResponse)
def get_contract(
    request: Request,
    key=Depends(api_auth.require_admin_or_scope(*READ_SCOPES)),
):
    """渲染好的输出契约（文本）。MCP ``breakdown://contract`` 直接转发它。

    存在的理由：契约真相在 ``core/breakdown_contract.py``。如果 MCP 侧再抄一份
    字段表，用户看到的说明与实际校验就会各自漂移，而失败原因会被误认为
    「agent 不守格式」。这里由服务端渲染，客户端只做转发。
    """
    return contract.render_agent_schema()


@router.get("/hook-criteria")
def get_hook_criteria(
    request: Request,
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*READ_SCOPES)),
):
    """当前生效的判据表（用户在设置页改过就以平台为准）。"""
    settings_map = get_breakdown_settings(db)
    criteria = settings_map.get("hook_criteria") or []
    lines = ["当前生效的爆点判据（按影响力从高到低）："]
    for index, item in enumerate(criteria, start=1):
        lines.append(f"{index}. [{item['id']}] {item['name']}：{item['ask']}")
    lines.append("")
    lines.append("hook_matches 里的 criterion 必须是上面方括号中的 id，其余会被丢弃。")
    return {
        "criteria": criteria,
        "max_matches": contract.MAX_HOOK_MATCHES,
        "text": "\n".join(lines),
    }


# ============================================================
# 清单（read）：前端与 MCP 的单一来源
# ============================================================


@router.get("/manifest", response_model=AgentManifestOut)
def manifest(
    request: Request,
    db: Session = Depends(get_db),
    key=Depends(api_auth.require_admin_or_scope(*READ_SCOPES)),
):
    """工具 / 资源 / scope 清单。

    存在的理由是**去掉手写副本**：AI 接入页曾把工具清单写死成一个数组，MCP
    server 又写一份，提示词里再写一句「14 个工具」——三处只要漏改一处，用户看到
    的就是假的。这里由服务端给出唯一清单，前端直接渲染。
    """
    settings_map = get_breakdown_settings(db)
    tools = catalog.all_tools()
    return {
        "contract_version": contract.BREAKDOWN_CONTRACT_VERSION,
        "tool_count": len(tools),
        "scopes": catalog.SCOPES,
        "tools": tools,
        "groups": catalog.GROUPS,
        "resources": [dict(r) for r in catalog.RESOURCES],
        "settings": {
            "executor": settings_map.get("executor"),
            "internal_fallback": settings_map.get("internal_fallback"),
            "claim_lease_min": settings_map.get("claim_lease_min"),
            "max_attempts": settings_map.get("max_attempts"),
            "max_chars_hard_cap": MAX_CHARS_HARD_CAP,
        },
    }
