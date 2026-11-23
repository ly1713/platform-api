"""拆解料包：把内部提示词看到的全部材料，组装成外部执行方可直接消费的一份 JSON。

为什么单独成模块而不是复用 ``_assemble_payload``：那个函数的产物是**提示词的一半**
（``build_breakdown_prompt`` 会把它渲染成中文长文本），数字、转录、评论都已被
拼进句子，外部拿不到结构化字段。料包必须是结构化的，外部才能自己决定怎么用。

红线（与内部链路一致，见设计 §2.2）：

- 数字**由后端算好**（``_data_context``），料包里只给结果，不接受外部回传修改；
- 转录截断走 ``contract.truncate_transcript``，与内部提示词**逐字节相同**；
- 规则（判据表 / 证据类别话术 / 硬性规则 / 自定义提示词）内联下发，一次调用拿全，
  避免外部漏读其中一段后「按自己的常识」拆解。

料包**不含**视频文件、不含任何凭证、不含其他账号的数据。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import breakdown_contract as contract
from app.core.breakdown_settings import get_breakdown_settings
from app.core.evidence_class import evidence_guidance
from app.core.hook_criteria import hook_matches_instruction
from app.models import (
    BreakdownClaim,
    ContentItem,
    MediaAsset,
    Transcript,
    VideoUnderstanding,
    ViralBreakdown,
)
from app.services import breakdown_service as svc

# 料包默认评论条数。与内部提示词一致（``_comment_digest`` 默认 30），
# 两条路径看到同一批评论，才谈得上「同一条素材的结论可比」。
DEFAULT_COMMENT_LIMIT = 30
# 转录上限的终极兜底：设置为 0/非法时退回 24k，与 ``modules.breakdown`` 一致。
DEFAULT_MAX_CHARS_FALLBACK = 24000


def _content_block(content: ContentItem) -> dict:
    return {
        "platform": content.platform,
        "title": content.title or "",
        "author": content.author_name or "",
        "author_id": content.author_id or "",
        "url": content.url or "",
        "duration_sec": content.duration_sec or 0,
        "published_at": content.published_at.isoformat() if content.published_at else None,
        "category": content.auto_category or "",
    }


def _metrics_block(db: Session, content: ContentItem, asset: MediaAsset) -> dict:
    """确定性数字：喂给外部之前把内部私有字段收敛成公开名字。

    ``_data_context`` 里没有 ``peer_sample_size``——中位数分母是内部实现细节，
    但外部**必须要能判断倍数可不可信**（样本 2 条和样本 200 条的「6 倍」不是
    一回事）。这里补上分母，其余字段原样透出，不重算。
    """
    ctx = svc._data_context(db, content, asset)
    ids = svc._author_content_ids(db, content)
    latest = svc._latest_metrics(db, ids)
    sample = sum(1 for i in ids if latest.get(i) is not None)
    return {**ctx, "peer_sample_size": sample}


def _transcript_block(
    db: Session, asset_id: int, max_chars: int
) -> tuple[dict, dict]:
    """返回 ``(料包里的 transcript 段, 给 rules.system_prompt 用的文本)``。

    两处都要：前者是结构化事实（语言、分段、是否截断），后者是内部提示词看到的
    那份带时间戳文本。截断由契约层统一完成，因此 ``truncated`` 与内部提示词里
    那句「转录在此处被截断」永远同步。
    """
    row = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    rendered = svc._transcript_text(db, asset_id)
    text, meta = contract.truncate_transcript(rendered, max_chars)
    segments = []
    if row is not None and isinstance(row.segments, list):
        for seg in row.segments:
            if not isinstance(seg, dict):
                continue
            body = str(seg.get("text") or "").strip()
            if not body:
                continue
            segments.append(
                {"start": seg.get("start"), "end": seg.get("end"), "text": body}
            )
    return (
        {
            "language": (row.language if row is not None else "") or "",
            # 分段是**原始全文**，不跟着 max_chars 裁剪：外部要按时间定位「0-3s 的钩子」，
            # 按字符数切分段会切出一个不存在的时间区间。
            "segments": segments,
            **meta,
        },
        {"text": text, **meta},
    )


def _understanding_block(db: Session, asset_id: int) -> dict | None:
    data = svc._understanding(db, asset_id)
    if data is None:
        return None
    row = db.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id))
    return {
        "summary": data["summary"],
        "dimensions": data["dimensions"],
        "timeline": data["timeline"],
        "category": data["category"],
        "confidence": data["confidence"],
        "low_confidence": data["low_confidence"],
        "evidence_class": data["evidence_class"] or "words",
        "chunk_count": data["chunk_count"],
        # 分类理由：外部据此判断「这类素材该信哪路证据」，只给 evidence_class
        # 会丢掉「为什么判成这一类」的依据。
        "category_reason": (row.category_reason if row is not None else "") or "",
    }


def _comments_block(db: Session, content_id: int, limit: int) -> list[dict]:
    return svc._comment_digest(db, content_id, top_n=limit)


def _rules_block(
    settings_map: dict, evidence_class: str, transcript_text: str
) -> dict:
    """规则一次给全：外部不必再调一次接口猜「还有什么约束」。

    ``system_prompt`` 直接下发设置页里那份自定义提示词：它是用户对拆解的主要
    约束，外部若不知道它，产出的报告就和内部路径不可比。
    """
    criteria = settings_map.get("hook_criteria") or []
    return {
        "output_schema": contract.schema_description(),
        "hook_criteria": criteria,
        "evidence_class": evidence_class,
        "evidence_guidance": evidence_guidance(evidence_class),
        "hard_rules": contract.hard_rule_descriptions(),
        "system_prompt": settings_map.get("prompt") or "",
        # 判据命中的输出要求（哪些 id 可填、每条要带什么）。与内部提示词同一处生成。
        "hook_matches_instruction": hook_matches_instruction(criteria),
        "transcript_provided": transcript_text,
    }


def _existing_report_block(db: Session, asset_id: int) -> dict | None:
    """已有报告摘要：让外部知道这是首做还是重做。

    只给状态与评分，不给全文——外部要做的是**重做**，看到旧结论容易被锚定，
    反而产出一份和旧报告高度相似的「新」报告。
    """
    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is None:
        return None
    return {
        "status": row.status,
        "score": row.score,
        "produced_by": row.produced_by or "",
        "contract_version": row.contract_version or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _claim_block(db: Session, asset_id: int) -> dict | None:
    row = db.scalar(select(BreakdownClaim).where(BreakdownClaim.asset_id == asset_id))
    if row is None:
        return None
    return {
        "agent_name": row.agent_name,
        "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
        "lease_until": row.lease_until.isoformat() if row.lease_until else None,
        "attempts": row.attempts,
    }


def build_packet(
    db: Session,
    asset_id: int,
    *,
    max_chars: int | None = None,
    comment_limit: int = DEFAULT_COMMENT_LIMIT,
    include_transcript_text: bool = True,
) -> dict:
    """组装一份完整料包。

    Args:
        max_chars: 转录字符上限；``None`` 取设置里的 ``transcript_limit``。
        comment_limit: 附带评论条数上限（按赞排序）。
        include_transcript_text: 是否在 ``rules`` 里附带渲染好的带时间戳转录全文。
            料包本身已含 ``transcript.segments``（原始分段）；这一项是给「要照着
            内部提示词的材料形态分析」的执行方用的。默认带上，因为内部链路看到的
            就是这份文本，不带上两条路径的材料就不一致。

    Raises:
        BreakdownInputError: 素材不存在 / 无关联内容 / 缺转录 / 缺 L3 视频理解。
            **外部路径与内部路径共用这组前置校验**：视频理解不从平台外移，
            外部 agent 也拿不到视频文件，缺它就没有可分析的材料。
    """
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise svc.BreakdownInputError(f"素材不存在: {asset_id}")
    content = db.get(ContentItem, asset.content_id)
    if content is None:
        raise svc.BreakdownInputError(
            f"素材 #{asset_id} 未关联内容（content_id={asset.content_id}）"
        )

    transcript_row = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    if transcript_row is None or not (transcript_row.full_text or "").strip():
        raise svc.BreakdownInputError("拆解前置未完成：需要先完成转录")
    understanding_row = db.scalar(
        select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id)
    )
    if understanding_row is None or understanding_row.status != "done":
        raise svc.BreakdownInputError("拆解前置未完成：需要先完成视频理解")

    settings_map = get_breakdown_settings(db)
    limit = max_chars if isinstance(max_chars, int) and max_chars > 0 else int(
        settings_map.get("transcript_limit") or DEFAULT_MAX_CHARS_FALLBACK
    )
    transcript, transcript_meta = _transcript_block(db, asset_id, limit)
    understanding = _understanding_block(db, asset_id)
    evidence_class = (understanding or {}).get("evidence_class") or "words"

    packet = {
        "contract_version": contract.BREAKDOWN_CONTRACT_VERSION,
        "asset_id": asset_id,
        "content": _content_block(content),
        "metrics": _metrics_block(db, content, asset),
        "transcript": transcript,
        "understanding": understanding,
        "signals": {"moments": svc._moments(db, asset_id)},
        "comments": _comments_block(db, content.id, comment_limit),
        "rules": _rules_block(
            settings_map,
            evidence_class,
            transcript_meta["text"] if include_transcript_text else "",
        ),
        "existing_report": _existing_report_block(db, asset_id),
        "claim": _claim_block(db, asset_id),
    }
    return packet
