"""执行器分流 + 租约回收：external / internal 两条路径的边界。

为什么单独测：``dispatch`` 是「谁来做这条拆解」的唯一判定点。它错了的表现
不是报错，而是**静默**——用户切到外部后平台还在偷偷烧模型钱，或者切回内部后
素材永远停在「等待外部 Agent」。两条都不能靠肉眼从 UI 上看出来，只能钉成断言。

覆盖：外部模式不建 job 但立即可读 / 前置仍由平台排队 / 内部模式行为不变 /
回收流程「还回队列 → 用尽后兜底或可见 error」。
"""

from datetime import datetime, timedelta

import pytest

from app.services import breakdown_claim as claim_svc
from app.services import breakdown_dispatch as dispatch_svc


def _seed(session, *, downloaded=True):
    from app.models import ContentItem, MediaAsset

    c = ContentItem(
        account_id=1, platform="douyin", external_id="c1", content_type="video", author_id="A"
    )
    session.add(c)
    session.flush()
    a = MediaAsset(
        content_id=c.id,
        relative_path="d/1",
        video_file="v.mp4",
        download_status="done" if downloaded else "queued",
    )
    session.add(a)
    session.commit()
    return a


def _set(session, **kv):
    from app.models import SystemSetting

    for short, value in kv.items():
        key = f"breakdown_{short}"
        row = session.get(SystemSetting, key)
        if row is None:
            session.add(SystemSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
    session.commit()


# ============================================================
# 分流本身
# ============================================================


def test_executor_defaults_to_external(session):
    assert dispatch_svc.current_executor(session) == dispatch_svc.EXECUTOR_EXTERNAL
    assert dispatch_svc.is_external(session) is True


def test_invalid_executor_falls_back_to_internal(session):
    """手改 KV 写错时回退 internal：写错的人想要的不是「平台别再产报告」。"""
    _set(session, executor="extrenal")
    assert dispatch_svc.current_executor(session) == dispatch_svc.EXECUTOR_INTERNAL


def test_external_dispatch_creates_no_breakdown_job(session):
    """外部模式不建 breakdown job（前置 transcribe 例外，见下一条）。"""
    from app.models import JobQueue, ViralBreakdown

    a = _seed(session)
    out = dispatch_svc.dispatch(session, a.id)

    assert out["executor"] == "external"
    assert out["job_id"] is None
    assert out["status"] == "pending"
    assert session.query(JobQueue).filter_by(job_type="breakdown").count() == 0
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "pending"


def test_external_dispatch_queues_prerequisites_not_breakdown(session):
    """前置（转录/视频理解）仍由平台跑：料包必须有转录与 L3 才能分析。"""
    from app.models import JobQueue

    a = _seed(session)
    dispatch_svc.dispatch(session, a.id)

    types = {j.job_type for j in session.query(JobQueue).all()}
    assert "transcribe" in types
    assert "breakdown" not in types


def test_external_dispatch_tolerates_undownloaded_asset(session):
    """素材还没下载完不应 422：用户看到的是「已排入外部队列」，不是报错。"""
    a = _seed(session, downloaded=False)
    out = dispatch_svc.dispatch(session, a.id)

    assert out["executor"] == "external"
    assert out["prerequisites"] == "prerequisites_missing"


def test_internal_dispatch_keeps_pending_row_readable(session):
    """内部模式也要先落 pending 行：入队后 GET 必须立刻读到，不等 worker。"""
    from app.models import ViralBreakdown

    _set(session, executor="internal")
    a = _seed(session)
    out = dispatch_svc.dispatch(session, a.id)

    assert out["executor"] == "internal"
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "pending"


def test_external_dispatch_keeps_done_without_force(session):
    from app.models import ViralBreakdown

    a = _seed(session)
    session.add(ViralBreakdown(asset_id=a.id, status="done", verdict="旧"))
    session.commit()

    dispatch_svc.dispatch(session, a.id, force=False)
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "done"
    assert row.verdict == "旧"


def test_external_dispatch_force_resets_done(session):
    from app.models import ViralBreakdown

    a = _seed(session)
    session.add(ViralBreakdown(asset_id=a.id, status="done", verdict="旧"))
    session.commit()

    dispatch_svc.dispatch(session, a.id, force=True)
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "pending"


# ============================================================
# handler 在外部模式下不得偷偷跑模型
# ============================================================


def test_handle_breakdown_external_does_not_call_model(session, monkeypatch):
    """历史遗留 job / 手动重跑落到 handler 时，外部模式下也不能花模型钱。"""
    import asyncio

    from app.workers import handlers

    a = _seed(session)
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("app.services.breakdown_service.run_breakdown", fake_run)
    asyncio.run(handlers.handle_breakdown(session, {"asset_id": a.id, "force": True}))
    assert calls == []


def test_handle_breakdown_fallback_payload_still_runs_internal(session, monkeypatch):
    """兜底链路必须真的跑：那条 payload 是平台自己安排的补做。"""
    import asyncio

    from app.workers import handlers

    a = _seed(session)
    calls = []

    async def fake_run(db, asset_id, *, force=False, fallback=False, fallback_reason="", **kw):
        calls.append({"asset_id": asset_id, "fallback": fallback, "reason": fallback_reason})
        return object()

    monkeypatch.setattr("app.services.breakdown_service.run_breakdown", fake_run)
    asyncio.run(
        handlers.handle_breakdown(
            session,
            {"asset_id": a.id, "force": True, "fallback": True, "fallback_reason": "没人回传"},
        )
    )
    assert calls == [{"asset_id": a.id, "fallback": True, "reason": "没人回传"}]


# ============================================================
# 租约回收：外部认领后失联的唯一出口
# ============================================================


def _expire(session, asset_id, *, minutes=31):
    """把租约拨到过去，模拟 agent 拿料后失联。"""
    claim = claim_svc.get_claim(session, asset_id)
    claim.lease_until = datetime.utcnow() - timedelta(minutes=minutes)
    session.commit()


def test_reconcile_returns_expired_lease_to_queue(session):
    a = _seed(session)
    claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    _expire(session, a.id)

    out = claim_svc.reconcile_claims(session)

    assert out["released"] == 1
    claim = claim_svc.get_claim(session, a.id)
    assert claim.lease_token == ""
    from app.models import ViralBreakdown

    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "pending"


def test_reconcile_falls_back_to_internal_after_max_attempts(session):
    """次数用尽且兜底开着：入队一次内部拆解，而不是静默停在 pending。"""
    a = _seed(session)
    now = datetime.utcnow()
    for i in range(3):
        claim_svc.claim_breakdown(session, a.id, agent_name=f"a{i}", now=now + timedelta(hours=i))
    _expire(session, a.id, minutes=1)

    out = claim_svc.reconcile_claims(session, now=now + timedelta(hours=9))

    assert out["fallback"] == 1
    # 兜底已登记成 breakdown 步骤，"fallback" 必须落在步骤 payload 上：
    # 否则 worker 跑到这一步会按外部模式再登记一次待处理，兜底静默失效。
    from app.models import PipelineStep

    step = session.query(PipelineStep).filter_by(step="breakdown").one()
    assert step.payload.get("fallback") is True
    assert step.payload.get("fallback_reason")
    assert step.status in ("waiting_prerequisite", "queued")


def test_reconcile_marks_visible_error_when_fallback_disabled(session):
    """兜底关掉时不允许静默：落成一次用户看得见的 error。"""
    _set(session, internal_fallback="false")
    a = _seed(session)
    now = datetime.utcnow()
    for i in range(3):
        claim_svc.claim_breakdown(session, a.id, agent_name=f"a{i}", now=now + timedelta(hours=i))
    _expire(session, a.id, minutes=1)

    out = claim_svc.reconcile_claims(session, now=now + timedelta(hours=9))

    assert out["exhausted"] == 1
    from app.models import ViralBreakdown

    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    assert row.status == "error"
    assert "3 次" in row.error


def test_reconcile_never_touches_done_report(session):
    """迟到的过期租约不得覆盖已经回传成功的报告。"""
    from app.models import ViralBreakdown

    a = _seed(session)
    claim_svc.claim_breakdown(session, a.id, agent_name="a1")
    _expire(session, a.id)
    row = session.query(ViralBreakdown).filter_by(asset_id=a.id).one()
    row.status = "done"
    row.verdict = "已回传"
    session.commit()

    out = claim_svc.reconcile_claims(session)

    assert out["released"] == 0
    session.refresh(row)
    assert row.status == "done"
    assert row.verdict == "已回传"


def test_reconcile_ignores_live_lease(session):
    a = _seed(session)
    claim_svc.claim_breakdown(session, a.id, agent_name="a1")

    out = claim_svc.reconcile_claims(session)

    assert out == {"released": 0, "exhausted": 0, "fallback": 0}
    assert claim_svc.get_claim(session, a.id).lease_token


def test_manual_reclaim_after_exhaustion_is_refused(session):
    a = _seed(session)
    now = datetime.utcnow()
    for i in range(3):
        claim_svc.claim_breakdown(session, a.id, agent_name=f"a{i}", now=now + timedelta(hours=i))
    _expire(session, a.id, minutes=1)
    with pytest.raises(claim_svc.ClaimExhausted):
        claim_svc.claim_breakdown(session, a.id, agent_name="late", now=datetime.utcnow())
