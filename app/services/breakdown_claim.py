"""拆解认领租约：外部 agent 的「我在做这条」凭据与回收规则。

为什么需要租约而不是「谁回传算谁的」：外部 agent 拿料后可能失联、可能超时、
也可能两个 agent 同时在做同一条。没有租约时这些问题都以「报告莫名其妙被覆盖」
的形式暴露，事后无从追溯。租约把这三件事变成可查询的事实：

- **谁在做**：``agent_name`` + ``claimed_at``；
- **做到什么时候**：``lease_until``，过期即可被接管；
- **试过几次**：``attempts``，超过上限就不再外放，落成**可见**的 error。

回传必须带 ``lease_token``：A 的租约过期后 B 接手，A 迟到的回传不能覆盖 B 的结果。

红线：本模块只碰 ``breakdown_claim`` 与 ``viral_breakdown.status``；报告内容
的归一化在 ``core.breakdown_contract``，料包组装在 ``services.breakdown_packet``。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events
from app.core.breakdown_settings import get_breakdown_settings
from app.models import BreakdownClaim, MediaAsset, ViralBreakdown

# 租赁冲突 / 过期 / 已达上限。全部由 API 层翻译成 409（可重试的状态冲突），
# 而不是 422——格式没错，是时机不对，外部据此重新认领而不是改报告。
class ClaimConflict(Exception):
    """认领/回传的时机冲突（已被他人持有、租约过期、token 不匹配等）。"""


class ClaimExhausted(ClaimConflict):
    """该素材的认领次数已用尽，不再外放。"""


def _now() -> datetime:
    return datetime.utcnow()


def _lease_deadline(minutes: int, now: datetime | None = None) -> datetime:
    return (now or _now()) + timedelta(minutes=max(1, int(minutes)))


def get_claim(db: Session, asset_id: int) -> BreakdownClaim | None:
    return db.scalar(select(BreakdownClaim).where(BreakdownClaim.asset_id == asset_id))


def is_live(claim: BreakdownClaim | None, now: datetime | None = None) -> bool:
    """租约是否仍然有效（有人正在做，别人不该抢）。

    ``lease_until`` 为 ``None`` 时按「无效」处理：那说明行写坏了，把它当成
    有效租约会让素材永久锁死，而按无效处理最坏只是被重复认领。
    """
    if claim is None or not claim.lease_token or claim.lease_until is None:
        return False
    return claim.lease_until > (now or _now())


def _ensure_breakdown_row(db: Session, asset_id: int) -> ViralBreakdown:
    """确保存在一行待拆解记录（外部模式下的入队产物）。"""
    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is None:
        row = ViralBreakdown(asset_id=asset_id, status="pending")
        db.add(row)
    return row


def claim_breakdown(
    db: Session,
    asset_id: int,
    *,
    agent_name: str,
    settings_map: dict | None = None,
    now: datetime | None = None,
) -> tuple[BreakdownClaim, bool]:
    """认领一条素材，返回 ``(认领行, 是否新建/续租成功)``。

    语义（设计 §3.2）：

    - 无人认领或租约已过期 → 建/续租约，``attempts += 1``，返回**新** token。
    - **同一 agent** 在有效租约内重复认领 → 返回**同一** token（幂等，重试不丢租约）。
    - **别的 agent** 在有效租约内认领 → ``ClaimConflict``（附剩余分钟数）。
    - ``attempts`` 已达上限 → ``ClaimExhausted``。

    ``attempts`` 每次「拿到新租约」都 +1，幂等重复认领不 +1：它是「平台把这条
    素材交给外部的次数」，不是「agent 重试了几次」。
    """
    current = now or _now()
    s = settings_map if settings_map is not None else get_breakdown_settings(db)
    lease_min = int(s.get("claim_lease_min") or 30)
    max_attempts = int(s.get("max_attempts") or 3)

    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise ClaimConflict(f"素材不存在: {asset_id}")

    name = (agent_name or "").strip()[:120] or "unknown"
    claim = get_claim(db, asset_id)

    if claim is not None and is_live(claim, current):
        if claim.agent_name == name:
            # 幂等：同一 agent 在租约内重复认领，续租但不换 token。
            # 换 token 会让它上一次调用拿到的 token 立刻失效，这是纯粹的坑。
            claim.lease_until = _lease_deadline(lease_min, current)
            _ensure_breakdown_row(db, asset_id)
            db.commit()
            db.refresh(claim)
            return claim, True
        remaining = max(0, int((claim.lease_until - current).total_seconds() // 60))
        raise ClaimConflict(
            f"素材 {asset_id} 已被 {claim.agent_name} 认领，租约剩余约 {remaining} 分钟"
        )

    if claim is not None and claim.attempts >= max_attempts:
        raise ClaimExhausted(
            f"素材 {asset_id} 已被认领 {claim.attempts} 次仍无人回传，不再外放"
        )

    if claim is None:
        claim = BreakdownClaim(asset_id=asset_id)
        db.add(claim)
    claim.agent_name = name
    claim.lease_token = secrets.token_hex(16)
    claim.claimed_at = current
    claim.lease_until = _lease_deadline(lease_min, current)
    claim.attempts = (claim.attempts or 0) + 1

    row = _ensure_breakdown_row(db, asset_id)
    # doing 表示「有人在做」：UI 与 workflow_engine 都认这个状态，不必认识租约。
    if row.status in ("pending", "error"):
        row.status = "doing"
        row.error = ""
    events.publish(
        "breakdown_claim",
        {"asset_id": asset_id, "agent": name, "lease_until": claim.lease_until.isoformat()},
        db=db,
    )
    db.commit()
    db.refresh(claim)
    return claim, True


def verify_lease(
    db: Session, asset_id: int, lease_token: str, *, now: datetime | None = None
) -> BreakdownClaim:
    """回传前的租约校验：token 必须匹配且未过期。"""
    claim = get_claim(db, asset_id)
    if claim is None or not claim.lease_token:
        raise ClaimConflict(f"素材 {asset_id} 当前无人认领，不接受回传")
    if not lease_token or not secrets.compare_digest(claim.lease_token, lease_token):
        raise ClaimConflict(
            f"素材 {asset_id} 的租约凭据不匹配（可能已被他人接管，请重新认领）"
        )
    if not is_live(claim, now):
        raise ClaimConflict(
            f"素材 {asset_id} 的租约已过期（{claim.lease_until.isoformat()}），请重新认领"
        )
    return claim


def release_claim(
    db: Session, asset_id: int, lease_token: str, *, reason: str = "", now: datetime | None = None
) -> None:
    """主动放弃：清空 token（保留 attempts 计数），报告回到 pending 等人接。"""
    claim = verify_lease(db, asset_id, lease_token, now=now)
    claim.lease_token = ""
    claim.agent_name = ""
    claim.lease_until = now or _now()
    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is not None and row.status == "doing":
        row.status = "pending"
        row.error = reason[:512] if reason else ""
    db.commit()


def complete_claim(db: Session, asset_id: int) -> None:
    """回传成功后释放租约（保留 attempts：它记的是「交给外部几次」，是历史事实）。"""
    claim = get_claim(db, asset_id)
    if claim is None:
        return
    claim.lease_token = ""
    claim.agent_name = ""


def expired_claims(db: Session, *, now: datetime | None = None) -> list[BreakdownClaim]:
    """所有「有 token 但已过期」的认领（回收流程用）。"""
    current = now or _now()
    rows = db.scalars(
        select(BreakdownClaim).where(
            BreakdownClaim.lease_token != "", BreakdownClaim.lease_until <= current
        )
    ).all()
    return list(rows)


def reconcile_claims(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    """回收过期租约：还回队列，或用尽次数后落成可见 error / 兜底。

    这是「外部 agent 认领后失联」的唯一出口。没有它，那条素材会永远显示
    「等待外部 Agent 处理」，用户既不知道发生了什么，也没法处理。

    返回 ``{"released": n, "exhausted": m, "fallback": k}``。
    """
    from app.services import breakdown_dispatch as dispatch

    current = now or _now()
    settings_map = get_breakdown_settings(db)
    max_attempts = int(settings_map.get("max_attempts") or 3)
    out = {"released": 0, "exhausted": 0, "fallback": 0}

    for claim in expired_claims(db, now=current):
        attempts = claim.attempts or 0
        agent = claim.agent_name or "unknown"
        claim.lease_token = ""
        claim.agent_name = ""
        claim.lease_until = current

        row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == claim.asset_id))
        if row is not None and row.status == "done":
            db.commit()
            continue  # 已有人回传过（迟到的过期行），不动结果

        if attempts < max_attempts:
            if row is not None and row.status == "doing":
                row.status = "pending"
            out["released"] += 1
            db.commit()
            continue

        reason = f"外部 agent「{agent}」认领 {attempts} 次均未回传"
        if dispatch.fallback_to_internal(db, claim.asset_id, reason=reason):
            out["fallback"] += 1
            db.commit()
            continue
        dispatch.mark_external_exhausted(db, claim.asset_id, attempts=attempts)
        out["exhausted"] += 1
        db.commit()

    return out
