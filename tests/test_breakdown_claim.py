"""认领租约服务：纯逻辑层测试（不经 HTTP）。

为什么单独测：租约的边界条件（幂等、接管、次数上限、token 校验）一旦错，
表现为「报告被覆盖」或「素材永久卡住」——两者都很难从 UI 上看出来。
这里把每条语义钉成一条断言。
"""

from datetime import datetime, timedelta

import pytest

from app.services import breakdown_claim as claim_svc


def _seed(session):
    from app.models import ContentItem, MediaAsset

    c = ContentItem(
        account_id=1, platform="douyin", external_id="c1", content_type="video", author_id="A"
    )
    session.add(c)
    session.flush()
    a = MediaAsset(content_id=c.id, relative_path="d/1", video_file="v.mp4", download_status="done")
    session.add(a)
    session.commit()
    return a


# ``_ensure_breakdown_row`` 会建行，所以这里不需要预先插入 ViralBreakdown。


def test_claim_creates_lease_with_token(session):
    a = _seed(session)
    claim, ok = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    assert ok is True
    assert claim.lease_token and len(claim.lease_token) == 32
    assert claim.attempts == 1
    assert claim_svc.is_live(claim)


def test_claim_marks_report_doing(session):
    """报告状态用既有的 doing：UI 与 workflow_engine 不必认识「租约」这个概念。"""
    from app.models import ViralBreakdown

    a = _seed(session)
    claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "doing"


def test_same_agent_reclaim_keeps_token(session):
    a = _seed(session)
    first, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    token = first.lease_token
    second, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    assert second.lease_token == token
    assert second.attempts == 1


def test_other_agent_blocked_with_remaining_minutes(session):
    a = _seed(session)
    claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    with pytest.raises(claim_svc.ClaimConflict) as err:
        claim_svc.claim_breakdown(session, a.id, agent_name="a2")
    assert "已被 a1 认领" in str(err.value)
    assert "分钟" in str(err.value)


def test_takeover_after_expiry_rotates_token(session):
    a = _seed(session)
    first, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    old = first.lease_token
    now = datetime.utcnow() + timedelta(minutes=31)
    second, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a2", now=now)
    assert second.lease_token != old
    assert second.attempts == 2
    assert second.agent_name == "a2"


def test_exhausted_after_max_attempts(session):
    a = _seed(session)
    now = datetime.utcnow()
    for i in range(3):
        claim_svc.claim_breakdown(session, a.id, agent_name=f"a{i}", now=now + timedelta(hours=i))
    with pytest.raises(claim_svc.ClaimExhausted):
        claim_svc.claim_breakdown(session, a.id, agent_name="late", now=now + timedelta(hours=9))


def test_verify_lease_rejects_wrong_and_expired(session):
    a = _seed(session)
    claim, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    with pytest.raises(claim_svc.ClaimConflict):
        claim_svc.verify_lease(session, a.id, "nope")
    with pytest.raises(claim_svc.ClaimConflict):
        claim_svc.verify_lease(
            session, a.id, claim.lease_token, now=datetime.utcnow() + timedelta(hours=2)
        )
    # 正确的 token 且未过期才放行
    assert claim_svc.verify_lease(session, a.id, claim.lease_token).id == claim.id


def test_verify_lease_when_nobody_claims(session):
    a = _seed(session)
    with pytest.raises(claim_svc.ClaimConflict) as err:
        claim_svc.verify_lease(session, a.id, "anything")
    assert "无人认领" in str(err.value)


def test_release_clears_token_and_restores_pending(session):
    from app.models import ViralBreakdown

    a = _seed(session)
    claim, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    claim_svc.release_claim(session, a.id, claim.lease_token, reason="做不了")
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    session.refresh(row)
    assert row.status == "pending"
    assert row.error == "做不了"
    # token 已作废：不能再拿它回传
    with pytest.raises(claim_svc.ClaimConflict):
        claim_svc.verify_lease(session, a.id, claim.lease_token)


def test_release_keeps_attempts_count(session):
    """释放不该把「交给外部几次」抹掉：它是历史事实，抹掉后无法判断该不该兜底。"""
    a = _seed(session)
    claim, _ = claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    claim_svc.release_claim(session, a.id, claim.lease_token)
    again = claim_svc.get_claim(session, a.id)
    assert again.attempts == 1


def test_expired_claims_lists_only_live_tokens(session):
    a = _seed(session)
    claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    assert claim_svc.expired_claims(session) == []  # 还没过期
    future = datetime.utcnow() + timedelta(hours=1)
    assert [c.asset_id for c in claim_svc.expired_claims(session, now=future)] == [a.id]


def test_claim_on_missing_asset_raises(session):
    with pytest.raises(claim_svc.ClaimConflict):
        claim_svc.claim_breakdown(session, 9999, agent_name="a1")
