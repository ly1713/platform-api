"""爆款拆解服务（Task 3）：mock LLM，覆盖落库字段 / 确定性数字 / 降级 / 幂等 / handler。

- 服务 `run_breakdown`：组装转录+L3+评论+确定性 data_context → 调 `breakdown.run_llm` → 落库。
- `_data_context`：相对中位数倍数 / 互动率 / 分位，含除零兜底（数字只由后端算）。
- 无 L3 → `understanding_available=False` 仍出报告；评论摘要在 prompt 中。
- upsert：二次运行更新同一行；`force` 语义；输入错误不可重试。
"""

import asyncio
from datetime import datetime

import pytest

import app.services.breakdown_service as svc


# ============================================================
# 夹具
# ============================================================


def _seed_content(db, *, external_id="aw1", author_id="A", platform="douyin", title="标题"):
    from app.models import ContentItem

    c = ContentItem(
        account_id=1,
        platform=platform,
        external_id=external_id,
        content_type="video",
        title=title,
        author_name="小红",
        author_id=author_id,
        duration_sec=32,
    )
    db.add(c)
    db.flush()
    return c


def _seed_asset(db, content, *, video_file="v.mp4"):
    from app.models import MediaAsset, Transcript, VideoUnderstanding

    a = MediaAsset(
        content_id=content.id,
        relative_path=f"douyin/{content.id}",
        video_file=video_file,
        download_status="done",
        transcribe_status="done",
        understand_status="done",
    )
    db.add(a)
    db.flush()
    db.add(Transcript(asset_id=a.id, full_text="测试转录", language="zh", model="base"))
    db.add(
        VideoUnderstanding(
            asset_id=a.id,
            category="other",
            summary="测试理解",
            dimensions={"visual": "画面"},
            timeline=[{"t": 1, "event": "开场"}],
            status="done",
            model="base",
        )
    )
    db.commit()
    return a


def _seed_snapshot(db, content_id, *, play, like=0, comment=0, favorite=0, share=0, when=None):
    from app.models import ContentMetricSnapshot

    s = ContentMetricSnapshot(
        content_id=content_id,
        play=play,
        like=like,
        comment=comment,
        favorite=favorite,
        share=share,
        captured_at=when or datetime.utcnow(),
    )
    db.add(s)
    db.flush()
    return s


def _llm_settings(_db):
    """Business paths no longer read llm_*; model wiring is patched at the gateway."""


def _use_internal(db):
    """把执行器切成 internal：``handle_breakdown`` 在 external 下会短路返回。"""
    from app.models import SystemSetting

    db.add(SystemSetting(key="breakdown_executor", value="internal"))
    db.commit()


_REPORT = {
    "verdict": "开头反常识 + 结尾引导下单，火在钩子",
    "score": 88,
    "hook": {"first_line": "别再买贵的了", "hook_type": "反常识", "why": "制造认知落差"},
    "structure": [{"segment": "0-3s", "role": "钩子", "summary": "反常识开场"}],
    "pacing": {"re_engagement_points": [8, 15], "emotion_curve": "先抑后扬"},
    "script": {"money_lines": ["贵的都是智商税"]},
    "psych_triggers": ["好奇", "损失厌恶"],
    "cta": {"type": "引导评论", "placement": "结尾"},
    "comment_insights": {"top_themes": ["求链接"], "controversies": [], "questions": ["多少钱"]},
    "blueprint": ["第一步：换钩子"],
    "replication_tips": ["前3秒制造落差"],
}


def _patch_llm(monkeypatch, *, report=None, usage=None, model="gw-model", capture=None):
    """patch 服务调用的 `breakdown.run_llm`，返回新契约 {"data","usage","model"}。"""

    returned = model

    async def fake_run(system, user, *, call_model, timeout=90):
        if capture is not None:
            capture["system"] = system
            capture["user"] = user
            capture["gateway_injected"] = callable(call_model)
        return {
            "data": dict(report if report is not None else _REPORT),
            "usage": usage if usage is not None else {"total_tokens": 123},
            "model": returned,
        }

    monkeypatch.setattr(svc.breakdown, "run_llm", fake_run)
    return fake_run


# ============================================================
# 正常链路：落库字段
# ============================================================


def test_run_breakdown_persists_row(monkeypatch, session):
    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    _seed_snapshot(session, c.id, play=1000, like=80, comment=12, favorite=5, share=3)
    session.commit()
    _patch_llm(monkeypatch)

    row = asyncio.run(svc.run_breakdown(session, a.id))

    assert row.asset_id == a.id
    assert row.status == "done" and row.error == ""
    assert row.score == 88
    assert row.verdict == _REPORT["verdict"]
    # sections 汇总除 verdict/score/comment_insights 外的定性板块
    assert row.sections["hook"] == _REPORT["hook"]
    assert row.sections["structure"] == _REPORT["structure"]
    assert row.sections["pacing"] == _REPORT["pacing"]
    assert row.sections["script"] == _REPORT["script"]
    assert row.sections["psych_triggers"] == _REPORT["psych_triggers"]
    assert row.sections["cta"] == _REPORT["cta"]
    assert row.sections["blueprint"] == _REPORT["blueprint"]
    assert row.sections["replication_tips"] == _REPORT["replication_tips"]
    assert row.comment_insights == _REPORT["comment_insights"]
    # 确定性数字 + 模型元数据
    assert row.data_context["play"] == 1000
    assert row.understanding_available is True  # 前置视频理解已完成
    assert row.model == "gw-model"  # 用响应里的 model，而非入参
    # v2：转录改为「只发带时间戳的分段」，并新增截断/低置信标注。
    assert row.prompt_version == "v2"
    assert row.usage == {"total_tokens": 123}


def test_run_breakdown_score_clamped(monkeypatch, session):
    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    _patch_llm(monkeypatch, report={**_REPORT, "score": 250})
    assert asyncio.run(svc.run_breakdown(session, a.id)).score == 100.0

    _patch_llm(monkeypatch, report={**_REPORT, "score": -5})
    assert asyncio.run(svc.run_breakdown(session, a.id, force=True)).score == 0.0

    _patch_llm(monkeypatch, report={**_REPORT, "score": "not-a-number"})
    assert asyncio.run(svc.run_breakdown(session, a.id, force=True)).score == 0.0


def test_run_breakdown_underscore_missing_fields_tolerated(monkeypatch, session):
    """模型漏字段：缺失项落空 / None，不抛错。"""
    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    _patch_llm(monkeypatch, report={"verdict": "只有结论"})

    row = asyncio.run(svc.run_breakdown(session, a.id))
    assert row.verdict == "只有结论"
    assert row.score == 0.0
    assert row.comment_insights == {}
    assert row.sections["hook"] is None


def test_run_breakdown_uses_understanding_when_present(monkeypatch, session):
    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    from app.models import Transcript, VideoUnderstanding

    row = session.query(VideoUnderstanding).filter_by(asset_id=a.id).one()
    row.summary = "带货视频"
    row.dimensions = {"product": "裙"}
    row.timeline = [{"t": 1}]
    transcript = session.query(Transcript).filter_by(asset_id=a.id).one()
    transcript.full_text = "全文"
    transcript.segments = [{"start": 0, "end": 3, "text": "开场"}]
    session.commit()
    _patch_llm(monkeypatch)

    row = asyncio.run(svc.run_breakdown(session, a.id))
    assert row.understanding_available is True


# ============================================================
# _data_context：确定性数字
# ============================================================


def test_data_context_math(session):
    """手算：中位数、倍数、互动率、分位、除零兜底。"""
    plays = [100, 200, 300]
    likes = [10, 20, 30]
    contents = []
    for i, (p, lk) in enumerate(zip(plays, likes)):
        c = _seed_content(session, external_id=f"c{i}", author_id="A")
        a = _seed_asset(session, c)
        # 最新快照即一行；本条目标拍 play=300/like=30
        _seed_snapshot(session, c.id, play=p, like=lk, comment=lk // 2, favorite=lk // 4, share=0)
        contents.append((c, a))
    session.commit()

    c3, a3 = contents[2]  # play=300, like=30, comment=15, favorite=7, share=0
    ctx = svc._data_context(session, c3, a3)

    assert ctx["play"] == 300 and ctx["like"] == 30
    assert ctx["median_play"] == 200.0  # 三个数中位
    assert ctx["median_like"] == 20.0
    assert ctx["play_vs_median"] == pytest.approx(300 / 200)
    assert ctx["like_vs_median"] == pytest.approx(30 / 20)
    assert ctx["engagement_rate"] == pytest.approx((30 + 15 + 7 + 0) / 300)
    # 300 是最大值：less=2, equal=1 -> (2+0.5)/3 = 0.8333
    assert ctx["percentile"] == pytest.approx(2.5 / 3)


def test_data_context_median_even_count(session):
    """偶数个样本中位数取中间两值平均。"""
    c1 = _seed_content(session, external_id="m1", author_id="B")
    c2 = _seed_content(session, external_id="m2", author_id="B")
    a2 = _seed_asset(session, c2)
    a1 = _seed_asset(session, c1)
    _seed_snapshot(session, c1.id, play=100)
    _seed_snapshot(session, c2.id, play=300)
    session.commit()
    ctx = svc._data_context(session, c2, a2)
    assert ctx["median_play"] == 200.0
    assert ctx["play_vs_median"] == pytest.approx(1.5)


def test_data_context_div_by_zero_guards(session):
    """无快照 / 中位数为 0 -> 所有倍率与互动率为 0，不抛除零。"""
    c1 = _seed_content(session, external_id="z1", author_id="Z")
    c2 = _seed_content(session, external_id="z2", author_id="Z")
    a2 = _seed_asset(session, c2)
    # c1 无快照；c2 play=0
    _seed_snapshot(session, c2.id, play=0, like=0, comment=0)
    session.commit()
    ctx = svc._data_context(session, c2, a2)
    assert ctx["play"] == 0
    assert ctx["median_play"] == 0.0
    assert ctx["play_vs_median"] == 0.0
    assert ctx["like_vs_median"] == 0.0
    assert ctx["engagement_rate"] == 0.0
    # 样本全为 0：中位秩分位定义下 = 0.5（不是除零错误）。
    assert ctx["percentile"] == 0.5


def test_data_context_isolates_author(session):
    """他人内容不进入中位数样本（按 author_id + platform 隔离）。"""
    c = _seed_content(session, external_id="s1", author_id="S")
    a = _seed_asset(session, c)
    _seed_snapshot(session, c.id, play=100)
    other = _seed_content(session, external_id="s2", author_id="OTHER")
    _seed_snapshot(session, other.id, play=100000)
    session.commit()
    ctx = svc._data_context(session, c, a)
    assert ctx["median_play"] == 100.0  # 不含 OTHER 的 100000
    assert ctx["play_vs_median"] == pytest.approx(1.0)


def test_data_context_uses_latest_snapshot(session):
    """同一 content 多条快照取 captured_at 最新一条。"""
    c = _seed_content(session, external_id="l1", author_id="L")
    a = _seed_asset(session, c)
    base = datetime.utcnow()
    _seed_snapshot(session, c.id, play=10, when=base)
    _seed_snapshot(session, c.id, play=999, when=base)
    session.commit()
    ctx = svc._data_context(session, c, a)
    assert ctx["play"] == 999


# ============================================================
# 评论摘要参与 prompt
# ============================================================


def test_comment_digest_top_n_and_order(session):
    from app.models import Comment

    c = _seed_content(session, external_id="cm1", author_id="C")
    for i, lk in enumerate([1, 99, 50, 0]):
        session.add(Comment(content_id=c.id, platform="douyin", comment_id=f"k{i}", like_count=lk, content=f"评论{i}"))
    session.commit()
    digest = svc._comment_digest(session, c.id, top_n=3)
    assert [d["like_count"] for d in digest] == [99, 50, 1]
    assert digest[0]["content"] == "评论1"


def test_run_breakdown_includes_comments_in_prompt(monkeypatch, session):
    from app.models import Comment

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.add(Comment(content_id=c.id, platform="douyin", comment_id="c1", nickname="阿明", like_count=5, content="求链接"))
    session.commit()
    cap: dict = {}
    _patch_llm(monkeypatch, capture=cap)

    asyncio.run(svc.run_breakdown(session, a.id))
    assert "求链接" in cap["user"]
    assert "阿明" in cap["user"]


def test_breakdown_runs_on_text_slot_and_never_sends_video(monkeypatch, session):
    """拆解是纯文本工作：走 text_analysis 槽位，不带任何视频/图片。

    这条测试锁住一个曾经被写错的说法——「爆款拆解必须使用多模态视觉模型」。
    拆解只吃转录 + 视频理解产出的 JSON，模型有没有视觉能力不影响它；
    真正需要视觉的是前置的视频理解步骤。
    """
    from app.models import ModelAssignment
    from app.services.model_access_service import _CAPABILITY_REQUIREMENTS

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    cap: dict = {}
    _patch_llm(monkeypatch, capture=cap)
    asyncio.run(svc.run_breakdown(session, a.id))

    # 1) 槽位要求里没有 image / video。
    assert _CAPABILITY_REQUIREMENTS["text_analysis"] == ("text", "json")
    # 2) 提示词是纯文本，模型也没收到视频参数。
    assert isinstance(cap["system"], str) and isinstance(cap["user"], str)
    assert "base64" not in cap["user"] and "video_url" not in cap["user"]
    # 3) 落库的路由只记录 text_analysis。
    from sqlalchemy import select

    from app.models import ViralBreakdown

    stored = session.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == a.id))
    assert set((stored.routing or {}).keys()) <= {"text_analysis"}


def test_prompt_marks_low_confidence_understanding(monkeypatch, session):
    """视频理解自评低时，提示词必须明说「可能不准」，不能当事实引用。"""
    from sqlalchemy import select

    from app.models import VideoUnderstanding

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    row = session.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == a.id))
    row.understanding_confidence = 0.25
    row.low_confidence = True
    session.commit()

    cap: dict = {}
    _patch_llm(monkeypatch, capture=cap)
    asyncio.run(svc.run_breakdown(session, a.id))
    assert "置信度仅 0.25" in cap["user"]


def test_long_transcript_is_truncated_with_explicit_notice(monkeypatch, session):
    """超长转录要截断，但必须显式告知模型被截断，避免它把缺失当成视频结尾。"""
    from sqlalchemy import select

    from app.models import SystemSetting, Transcript

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    long_text = "这是一句用于测试截断的转录。" * 2000  # 远超 200 字符上限
    tr = session.scalar(select(Transcript).where(Transcript.asset_id == a.id))
    tr.full_text = long_text
    tr.segments = []
    session.add(SystemSetting(key="breakdown_transcript_limit", value="200"))
    session.commit()

    cap: dict = {}
    _patch_llm(monkeypatch, capture=cap)
    asyncio.run(svc.run_breakdown(session, a.id))
    assert "转录在此处被截断" in cap["user"]


# ============================================================
# 幂等 / upsert / force
# ============================================================


def test_run_breakdown_upsert_no_duplicate(monkeypatch, session):
    """二次 force 运行更新同一行，不产生重复行。"""
    from sqlalchemy import func, select

    from app.models import ViralBreakdown

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    _patch_llm(monkeypatch, report={**_REPORT, "verdict": "第一次"})
    r1 = asyncio.run(svc.run_breakdown(session, a.id))

    _patch_llm(monkeypatch, report={**_REPORT, "verdict": "第二次"})
    r2 = asyncio.run(svc.run_breakdown(session, a.id, force=True))

    assert r1.id == r2.id
    assert r2.verdict == "第二次"
    assert session.scalar(select(func.count(ViralBreakdown.id))) == 1


def test_run_breakdown_idempotent_without_force(monkeypatch, session):
    """已 done 且 force=False：不再调 LLM（省 token），直接返回既有行。"""
    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    calls = {"n": 0}

    async def fake_run(system, user, *, call_model, timeout=90):
        calls["n"] += 1
        return {"data": dict(_REPORT), "usage": {}, "model": "m"}

    monkeypatch.setattr(svc.breakdown, "run_llm", fake_run)
    asyncio.run(svc.run_breakdown(session, a.id))
    asyncio.run(svc.run_breakdown(session, a.id))  # 第二次应短路
    assert calls["n"] == 1


# ============================================================
# 错误语义
# ============================================================


def test_run_breakdown_missing_asset_raises_input_error(session):
    with pytest.raises(svc.BreakdownInputError):
        asyncio.run(svc.run_breakdown(session, 999999))


def test_run_breakdown_orphan_asset_is_input_error(session):
    """content 缺失（孤儿资产）-> 不可重试的输入错误，且落 error 行。"""
    from app.models import MediaAsset

    a = MediaAsset(content_id=123456, relative_path="x", video_file="v.mp4")
    session.add(a)
    session.commit()
    with pytest.raises(svc.BreakdownInputError):
        asyncio.run(svc.run_breakdown(session, a.id))


def test_run_breakdown_llm_error_persists_error_and_raises(monkeypatch, session):
    from sqlalchemy import select

    from app.models import ViralBreakdown

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()

    async def boom(system, user, *, call_model, timeout=90):
        raise RuntimeError("上游 503")

    monkeypatch.setattr(svc.breakdown, "run_llm", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(svc.run_breakdown(session, a.id))

    row = session.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == a.id))
    assert row is not None and row.status == "error"
    assert "503" in row.error


def test_run_breakdown_no_model_assignment_marks_actionable_error(session):
    """没有工作分配 -> 网关给出可操作错误，落 error（不得静默出空报告）。"""
    from sqlalchemy import select

    from app.models import ViralBreakdown
    from app.services.model_access_service import ModelCallError

    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    with pytest.raises(ModelCallError) as ei:
        asyncio.run(svc.run_breakdown(session, a.id))
    assert ei.value.primary_error_class == "not_configured"
    row = session.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == a.id))
    session.refresh(row)
    assert row.status == "error"
    assert "没有配置可用模型" in row.error


# ============================================================
# handler 注册 + 委托
# ============================================================


def test_breakdown_handler_registered():
    from app.workers import handlers

    assert "breakdown" in handlers.HANDLERS


def test_handle_breakdown_delegates(monkeypatch, session):
    from app.workers import handlers

    c = _seed_content(session)
    a = _seed_asset(session, c)
    _use_internal(session)
    session.commit()
    seen = {}

    async def fake_run(db, asset_id, *, force=False, **kwargs):
        seen["asset_id"] = asset_id
        seen["force"] = force
        return object()

    monkeypatch.setattr(svc, "run_breakdown", fake_run)
    asyncio.run(handlers.handle_breakdown(session, {"asset_id": a.id, "force": True}))
    assert seen == {"asset_id": a.id, "force": True}


def test_handle_breakdown_missing_asset_is_noop(session):
    from app.workers import handlers

    asyncio.run(handlers.handle_breakdown(session, {"asset_id": 999999}))  # 不抛


def test_handle_breakdown_input_error_is_non_retryable(monkeypatch, session):
    from app.core.queue import NonRetryableJobError
    from app.workers import handlers

    c = _seed_content(session)
    a = _seed_asset(session, c)
    _use_internal(session)
    session.commit()

    async def boom(db, asset_id, *, force=False, **kwargs):
        raise svc.BreakdownInputError("素材未关联内容")

    monkeypatch.setattr(svc, "run_breakdown", boom)
    with pytest.raises(NonRetryableJobError):
        asyncio.run(handlers.handle_breakdown(session, {"asset_id": a.id}))


# ============================================================
# 归一化同源：内部链路必须与外部回传共用契约校验
# ============================================================


def test_internal_path_uses_contract_normalization(monkeypatch, session):
    """内部落库结果必须与 ``contract.normalize_report`` 逐字段一致。

    拆解外移后会有两条写入路径。这条测试是它们不会分叉的证据：内部链路的
    score / verdict / sections / hook_matches 全部来自契约层，不是另一份实现。
    """
    from app.core import breakdown_contract as contract
    from app.core.breakdown_settings import get_breakdown_settings

    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    _seed_snapshot(session, c.id, play=1000, like=80, comment=12, favorite=5, share=3)
    session.commit()
    _patch_llm(monkeypatch)

    row = asyncio.run(svc.run_breakdown(session, a.id))
    expected = contract.normalize_report(_REPORT, get_breakdown_settings(session)["hook_criteria"])

    assert row.score == expected["score"]
    assert row.verdict == expected["verdict"]
    assert row.comment_insights == expected["comment_insights"]
    for key, value in expected["sections"].items():
        assert row.sections.get(key) == value, f"sections[{key}] 与契约归一化结果不一致"


def test_internal_path_drops_hook_match_with_unknown_criterion(monkeypatch, session):
    """模型编造判据 id 时，内部链路也丢弃——与外部回传同一把尺子。"""
    _llm_settings(session)
    c = _seed_content(session)
    a = _seed_asset(session, c)
    session.commit()
    _patch_llm(
        monkeypatch,
        report={
            **_REPORT,
            "hook_matches": [
                {"criterion": "invented", "evidence": "听着就很炸"},
                {"criterion": "hook", "evidence": "开场就反问"},
            ],
        },
    )

    row = asyncio.run(svc.run_breakdown(session, a.id))
    assert row.sections["hook_matches"] == [{"criterion": "hook", "evidence": "开场就反问"}]
