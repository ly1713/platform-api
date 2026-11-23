"""料包：外部执行方拿到的材料，必须与内部提示词看到的**同一份**。

守两条：
1. 转录截断由 ``contract.truncate_transcript`` 统一完成，料包里的 ``truncated``
   与内部提示词里那句提示语永远同步（各写一份的话，长视频上两条路径会分析
   两份不同的材料，而结论差异不会立刻暴露）。
2. 规则一次给全：判据表、证据话术、硬性规则、自定义提示词、输出契约。外部漏掉
   其中任何一段，产出的报告都和内部不可比。
"""

from datetime import datetime

import pytest

from app.core import breakdown_contract as contract
from app.services import breakdown_packet as packet_svc
from app.services import breakdown_service as svc


def _seed(session, *, segments=None, full_text="你有没有想过这个反常识的结论", with_l3=True):
    from app.models import (
        ContentItem,
        ContentMetricSnapshot,
        MediaAsset,
        Transcript,
        VideoUnderstanding,
    )

    c = ContentItem(
        account_id=1,
        platform="douyin",
        external_id="p1",
        content_type="video",
        title="标题",
        author_name="小红",
        author_id="A",
        url="https://x/v",
        duration_sec=30,
        auto_category="knowledge",
        published_at=datetime(2026, 10, 1, 9, 0, 0),
    )
    session.add(c)
    session.flush()
    a = MediaAsset(content_id=c.id, relative_path="d/1", video_file="v.mp4", download_status="done")
    session.add(a)
    session.flush()
    session.add(
        Transcript(
            asset_id=a.id,
            language="zh",
            model="base",
            segments=segments if segments is not None else [{"start": 0, "end": 3, "text": full_text}],
            full_text=full_text,
        )
    )
    if with_l3:
        session.add(
            VideoUnderstanding(
                asset_id=a.id,
                category="knowledge",
                category_reason="白板讲解",
                summary="讲了三个方法",
                dimensions={"visual": "白板"},
                timeline=[{"t": 1, "event": "开场"}],
                status="done",
                model="base",
                evidence_class="words",
                moments=[{"start": 1, "end": 4, "drivers": {"loudness": 1}, "score": 0.8}],
            )
        )
    session.add(
        ContentMetricSnapshot(
            content_id=c.id,
            play=5000,
            like=400,
            comment=20,
            favorite=10,
            share=5,
            captured_at=datetime.utcnow(),
        )
    )
    session.commit()
    return c, a


# ============================================================
# 截断同源
# ============================================================


def test_truncate_transcript_keeps_short_text_untouched():
    text, meta = contract.truncate_transcript("短文本", 100)
    assert text == "短文本"
    assert meta == {"truncated": False, "original_chars": 3, "provided_chars": 3}


def test_truncate_transcript_annotates_long_text():
    text, meta = contract.truncate_transcript("a" * 50, 10)
    assert text.startswith("a" * 10)
    assert "原文 50 字符" in text and "前 10 字符" in text
    assert meta["truncated"] is True
    assert meta["provided_chars"] == 10  # 不含提示语本身


def test_truncate_transcript_zero_limit_means_no_limit():
    text, meta = contract.truncate_transcript("abc", 0)
    assert text == "abc"
    assert meta["truncated"] is False


def test_truncate_transcript_handles_non_string():
    assert contract.truncate_transcript(None, 10) == ("", {"truncated": False, "original_chars": 0, "provided_chars": 0})


def test_packet_truncation_matches_internal_prompt(session):
    """同一上限下，料包里的转录与内部提示词 payload 的转录逐字节相同。"""
    from app.modules import breakdown as module

    c, a = _seed(session, full_text="啊" * 40)
    limit = 12

    packet = packet_svc.build_packet(session, a.id, max_chars=limit, include_transcript_text=True)
    payload = svc._assemble_payload(
        session, c, a, "", transcript_limit=limit, transcript=svc._transcript_text(session, a.id)
    )
    _system, user = module.build_breakdown_prompt(payload)

    provided = packet["rules"]["transcript_provided"]
    assert provided in user
    # 截断按字符数切在带时间戳的渲染文本上：12 字符只够留下时间戳前缀。
    assert provided.startswith("[0:00-0:03]")
    assert "转录在此处被截断" in provided
    assert packet["transcript"]["provided_chars"] == limit
    assert packet["transcript"]["truncated"] is True


# ============================================================
# 料包内容
# ============================================================


def test_packet_rules_are_complete(session):
    _, a = _seed(session)
    packet = packet_svc.build_packet(session, a.id)
    rules = packet["rules"]
    assert rules["hook_criteria"] and all("id" in x for x in rules["hook_criteria"])
    assert rules["evidence_guidance"]
    assert [r["id"] for r in rules["hard_rules"]] == [r.id for r in contract.HARD_RULES]
    assert rules["system_prompt"]
    assert rules["output_schema"]["contract_version"] == contract.BREAKDOWN_CONTRACT_VERSION
    assert "hook_matches" in [f["key"] for f in rules["output_schema"]["fields"]]


def test_packet_metrics_include_sample_size(session):
    """中位数分母必须暴露：样本量决定「倍数」可不可信。"""
    _, a = _seed(session)
    metrics = packet_svc.build_packet(session, a.id)["metrics"]
    assert metrics["play"] == 5000
    assert metrics["peer_sample_size"] == 1


def test_packet_understanding_carries_uncertainty(session):
    _, a = _seed(session)
    und = packet_svc.build_packet(session, a.id)["understanding"]
    assert und["confidence"] == 0.0  # 夹具未设置信度 -> 0，外部应据此谨慎
    assert und["category_reason"] == "白板讲解"
    assert und["evidence_class"] == "words"


def test_packet_segments_are_raw_not_truncated(session):
    """分段不跟 max_chars 裁剪：按字符切分段会切出不存在的秒数区间。"""
    long_text = "字" * 20
    _, a = _seed(session, full_text=long_text, segments=[{"start": 0, "end": 3, "text": long_text}])
    packet = packet_svc.build_packet(session, a.id, max_chars=5)
    assert packet["transcript"]["segments"][0]["text"] == long_text
    assert packet["transcript"]["provided_chars"] == 5


def test_packet_has_no_secrets_or_paths(session):
    """料包是发给外部 agent 的：不能带本地路径、不能带凭证字段。"""
    _, a = _seed(session)
    dumped = str(packet_svc.build_packet(session, a.id))
    assert "v.mp4" not in dumped
    assert "/d/1" not in dumped
    for banned in ("cookie", "token", "password", "secret"):
        assert banned not in dumped.lower()


def test_packet_rejects_asset_without_transcript(session):
    _, a = _seed(session)
    from app.models import Transcript

    session.query(Transcript).filter_by(asset_id=a.id).delete()
    session.commit()
    with pytest.raises(svc.BreakdownInputError, match="转录"):
        packet_svc.build_packet(session, a.id)


def test_packet_rejects_asset_without_understanding(session):
    _, a = _seed(session, with_l3=False)
    with pytest.raises(svc.BreakdownInputError, match="视频理解"):
        packet_svc.build_packet(session, a.id)


def test_packet_rejects_soft_deleted_asset(session):
    _, a = _seed(session)
    a.deleted_at = datetime.utcnow()
    session.commit()
    with pytest.raises(svc.BreakdownInputError, match="素材不存在"):
        packet_svc.build_packet(session, a.id)
