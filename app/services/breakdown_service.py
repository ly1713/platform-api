"""爆款拆解服务：拼装输入 → 调 LLM → 算 data_context → 落库。

与 `understand_service` 同构：服务层负责 DB 读 / 组装 / 落库，LLM 调用与提示词在
`app/modules/breakdown.py`（纯模块层，不碰 DB）。对外能力：

- `async run_breakdown(db, asset_id, *, force=False) -> ViralBreakdown`：跑完整链路。
- `_data_context(db, content, asset)`：**确定性**计算互动上下文（相对中位数倍数/互动率/分位），
  数字由后端算好交给 LLM 只做定性引用，模型不重算、不杜撰（设计 §2.2 / 红线）。
- `_comment_digest(db, content_id, top_n=30)`：按 like_count 取 Top-N 评论供 LLM 归纳观众洞察。

错误语义（对齐 understand_service）：
- `BreakdownInputError`：素材/内容缺失等数据问题——不可重试，worker 直接失败。
- 其余异常：落 `viral_breakdown.status="error"` + `error` 后**原样抛出**（worker 重试）。
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events
from app.core.breakdown_settings import get_breakdown_settings
from app.models import (
    Comment,
    ContentItem,
    ContentMetricSnapshot,
    MediaAsset,
    Transcript,
    VideoUnderstanding,
    ViralBreakdown,
)
from app.modules import breakdown, prefilter

logger = logging.getLogger("platform.breakdown")

_PROMPT_VERSION = "v2"
# 分段不再限条数：时间戳是拆解定位钩子/结构的依据，砍掉尾部等于让模型
# 在「前半段有内容、后半段空白」的材料上写结论。预算由提示词层的字符上限统一控制。


class BreakdownInputError(ValueError):
    """输入/数据问题导致拆解不可执行（如资产无关联内容）——不应被当作可重试故障。

    继承 ValueError（调用方/测试可用 ValueError 捕获）。worker 侧据此不重试。
    """


# --------------------------------------------------------------------------- 确定性数字


def _median(values: list[float]) -> float:
    """稳健中位数：空列表返回 0；偶数个取中间两值平均。"""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (float(vals[mid - 1]) + float(vals[mid])) / 2.0


def _percentile(values: list[float], x: float) -> float:
    """x 在 values 中的分位（0-1）：中位秩法 (小于数 + 0.5×相等数) / 总数。"""
    n = len(values)
    if n == 0:
        return 0.0
    less = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return max(0.0, min(1.0, (less + 0.5 * equal) / n))


def _latest_metrics(db: Session, content_ids: list[int]) -> dict[int, ContentMetricSnapshot]:
    """每个 content 的最新一条互动快照（无 -> 不出现）。"""
    if not content_ids:
        return {}
    rows = db.scalars(
        select(ContentMetricSnapshot)
        .where(ContentMetricSnapshot.content_id.in_(content_ids))
        .order_by(
            ContentMetricSnapshot.content_id,
            ContentMetricSnapshot.captured_at.desc(),
            ContentMetricSnapshot.id.desc(),
        )
    ).all()
    latest: dict[int, ContentMetricSnapshot] = {}
    for row in rows:
        latest.setdefault(row.content_id, row)
    return latest


def _author_content_ids(db: Session, content: ContentItem) -> list[int]:
    """该作者（同平台、同 author_id）的全部内容 id；author_id 为空时退化为本条。"""
    author_id = (content.author_id or "").strip()
    if not author_id:
        return [content.id]
    return list(
        db.scalars(
            select(ContentItem.id).where(
                ContentItem.author_id == author_id,
                ContentItem.platform == content.platform,
            )
        ).all()
    )


def _data_context(db: Session, content: ContentItem, asset: MediaAsset) -> dict:
    """确定性计算互动上下文：当前值 + 相对该作者的倍数 / 互动率 / 分位。

    数字**全部在此计算**（不是 LLM 产出）。除数为 0 时相关倍数 / 比率一律取 0，避免除零。
    """
    ids = _author_content_ids(db, content)
    if content.id not in ids:
        ids.append(content.id)
    latest = _latest_metrics(db, ids)

    me = latest.get(content.id)
    m_play = me.play if me and me.play is not None else None
    m_like = me.like if me and me.like is not None else None
    m_comment = me.comment if me and me.comment is not None else None
    m_favorite = me.favorite if me and me.favorite is not None else None
    m_share = me.share if me and me.share is not None else None

    # 该作者全部内容的最新播放/点赞（本条也含在内）。
    # 中位数口径：只统计「有快照」的内容——未拍到的作品不以 0 计入分母，
    # 否则会拉低中位数、虚高倍数/分位（T3 review Minor①，Task 4 定稿）。
    snapshot_ids = [i for i in ids if latest.get(i) is not None]
    plays = [float(latest[i].play) for i in snapshot_ids if latest[i].play is not None]
    likes = [float(latest[i].like) for i in snapshot_ids if latest[i].like is not None]
    median_play = _median(plays)
    median_like = _median(likes)

    engagement_rate = (
        (m_like + m_comment + m_favorite + m_share) / m_play
        if m_play and None not in (m_like, m_comment, m_favorite, m_share)
        else (0.0 if m_play == 0 else None)
    )

    return {
        "play": m_play,
        "like": m_like,
        "comment": m_comment,
        "favorite": m_favorite,
        "share": m_share,
        "median_play": median_play,
        "median_like": median_like,
        # Zero denominator keeps the legacy numeric 0; unknown numerator stays None.
        "play_vs_median": (m_play / median_play) if m_play is not None and median_play else (0.0 if m_play == 0 else None),
        "like_vs_median": (m_like / median_like) if m_like is not None and median_like else (0.0 if m_like == 0 else None),
        "engagement_rate": engagement_rate,
        "percentile": _percentile(plays, float(m_play)) if m_play is not None else None,
    }


# --------------------------------------------------------------------------- 输入拼装


def _comment_digest(db: Session, content_id: int, top_n: int = 30) -> list[dict]:
    """按 like_count 取 Top-N 一级评论（昵称 / 点赞数 / 正文）。"""
    if top_n <= 0:
        return []
    rows = db.scalars(
        select(Comment)
        .where(Comment.content_id == content_id)
        .order_by(Comment.like_count.desc(), Comment.id.asc())
        .limit(top_n)
    ).all()
    return [
        {"nickname": c.nickname, "like_count": c.like_count, "content": c.content}
        for c in rows
    ]


def _comment_lines(digest: list[dict]) -> list[str]:
    """把评论摘要渲染成提示词行：``昵称（N赞）：正文``。"""
    lines = []
    for c in digest:
        body = (c.get("content") or "").strip()
        if not body:
            continue
        lines.append(f"{c.get('nickname') or '匿名'}（{c.get('like_count', 0)}赞）：{body}")
    return lines


def _format_ts(value: object) -> str:
    """秒数 -> ``m:ss``；无法解析时原样返回，避免静默丢时间信息。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    if seconds != seconds or seconds < 0:  # NaN / 负数
        return str(value)
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _transcript_text(db: Session, asset_id: int) -> str:
    """转录正文，带时间戳。

    之前是把 ``full_text`` 和前 20 个分段**同时**拼进提示词：同一段话出现两遍，
    既浪费上下文，又让「找钩子台词/时间定位」这件事变成在两个版本里对齐。
    现在只用分段时间戳版（模型定位更准），分段缺失时才退回 ``full_text``。
    """
    tr = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    if tr is None:
        return ""
    segments = tr.segments if isinstance(tr.segments, list) else []
    seg_lines = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        body = str(seg.get("text", "")).strip()
        if not body:
            continue
        seg_lines.append(f"[{_format_ts(seg.get('start'))}-{_format_ts(seg.get('end'))}] {body}")
    return "\n".join(seg_lines) if seg_lines else (tr.full_text or "").strip()


def _understanding(db: Session, asset_id: int) -> dict | None:
    """复用 L3 视频理解结果（summary/dimensions/timeline）；无有效内容返回 None。"""
    row = db.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id))
    if row is None:
        return None
    if not (row.summary or row.dimensions or row.timeline):
        return None
    return {
        "summary": row.summary or "",
        "dimensions": row.dimensions or {},
        "timeline": row.timeline or [],
        "category": row.category or "",
        # 让拆解区分「看清了」和「猜的」，低置信时报告应体现不确定性而不是照单全收。
        "confidence": float(row.understanding_confidence or 0),
        "low_confidence": bool(row.low_confidence),
        # 证据类别：拆解要据此决定「从话里找爆点还是从画面/反应里找」。
        "evidence_class": row.evidence_class or "",
        # 分块理解时模型只看过局部，整体结论来自汇总，置信度需要打折看待。
        "chunk_count": int(row.chunk_count or 1),
    }


def _evidence_class(db: Session, asset_id: int) -> str:
    """取本片的证据类别；无理解结果时按 words 兜底（最安全的默认）。"""
    row = db.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id))
    return (row.evidence_class if row is not None else "") or "words"


def _moments(db: Session, asset_id: int) -> list[dict]:
    row = db.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id))
    data = row.moments if row is not None else None
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def _funnel_config(db: Session) -> dict:
    """漏斗相关配置（存 process_settings，拆解这里只借用三个键）。"""
    from app.core.process_settings import get_process_settings

    s = get_process_settings(db)
    return {
        "prefilter_enabled": s.get("prefilter_enabled", False),
        "prefilter_min_chars": s.get("prefilter_min_chars", 3000),
        "prefilter_timeout_sec": s.get("prefilter_timeout_sec", 120),
    }


async def _funnel_transcript(db: Session, asset_id: int, s: dict, call_model) -> tuple[str, dict]:
    """按开关跑两级漏斗，返回 ``(转录文本, 统计)``；未启用/失败一律回退全文。"""
    if not s.get("prefilter_enabled", False):
        return _transcript_text(db, asset_id), {"enabled": False}
    tr = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    if tr is None or not isinstance(tr.segments, list) or not tr.segments:
        return _transcript_text(db, asset_id), {"enabled": False, "fallback_reason": "无分段"}

    async def llm(system: str, body: str) -> str:
        result = await call_model(
            request={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": body},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "timeout": float(s.get("prefilter_timeout_sec", 120) or 120),
            },
            parse_result=lambda raw: raw,
        )
        data = result.data
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    kept, stats = await prefilter.select_segments_async(
        tr.segments,
        llm=llm,
        min_chars=int(s.get("prefilter_min_chars", 3000) or 3000),
    )
    text = "\n".join(
        f"[{_format_ts(seg.get('start'))}-{_format_ts(seg.get('end'))}] {str(seg.get('text') or '').strip()}"
        for seg in kept
        if isinstance(seg, dict) and str(seg.get("text") or "").strip()
    )
    info = stats.as_dict()
    if not text.strip():
        return _transcript_text(db, asset_id), {**info, "fallback_reason": "初筛结果为空，回退全文"}
    return text, info


def _assemble_payload(
    db: Session,
    content: ContentItem,
    asset: MediaAsset,
    prompt: str,
    *,
    transcript_limit: int = 0,
    s_criteria: list | None = None,
    transcript: str | None = None,
) -> dict:
    """把元数据 + 转录 + L3 + 评论 + 确定性数字拼成提示词 payload。"""
    data_context = _data_context(db, content, asset)
    understanding = _understanding(db, asset.id)
    return {
        "prompt": prompt,
        "transcript_limit": transcript_limit or None,
        "platform": content.platform,
        "title": content.title,
        "author": content.author_name,
        "duration_sec": content.duration_sec,
        "published_at": content.published_at.isoformat() if content.published_at else None,
        "play": data_context["play"],
        "like": data_context["like"],
        "comment": data_context["comment"],
        "data_context": data_context,
        "transcript": _transcript_text(db, asset.id) if transcript is None else transcript,
        "timeline": (understanding or {}).get("timeline", []),
        "understanding": understanding,
        "comments": _comment_lines(_comment_digest(db, content.id)),
        "hook_criteria": s_criteria,
        "evidence_class": _evidence_class(db, asset.id),
        "moments": _moments(db, asset.id),
    }


# --------------------------------------------------------------------------- 结果落库


def _clamp_score(value: object) -> float:
    """把模型评分收敛到 0-100；非法 / NaN 记 0。"""
    if isinstance(value, bool):
        return 0.0
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if num != num:
        return 0.0
    return max(0.0, min(100.0, num))


def _sections(parsed: dict) -> dict:
    """模型定性产出中除 verdict/score/comment_insights 外的各板块。"""
    return {
        "hook": parsed.get("hook"),
        "structure": parsed.get("structure"),
        "pacing": parsed.get("pacing"),
        "script": parsed.get("script"),
        "psych_triggers": parsed.get("psych_triggers"),
        "cta": parsed.get("cta"),
        "blueprint": parsed.get("blueprint"),
        "replication_tips": parsed.get("replication_tips"),
    }


def _hook_matches(parsed: dict) -> list[dict]:
    """规整判据命中列表：只保留可核对的三元组，过滤模型自由发挥的字段。

    每条至少要有 ``criterion`` 和 ``evidence``——只有 id 没有依据的命中
    等于「模型说它命中了」，无法核对，不写进报告。
    """
    raw = parsed.get("hook_matches")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        criterion = item.get("criterion")
        evidence = item.get("evidence")
        if not isinstance(criterion, str) or not criterion.strip():
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            continue
        entry = {"criterion": criterion.strip(), "evidence": evidence.strip()[:300]}
        segment = item.get("segment")
        if isinstance(segment, str) and segment.strip():
            entry["segment"] = segment.strip()[:40]
        out.append(entry)
        if len(out) >= 12:
            break
    return out


def _str_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _dict_or_empty(value: object) -> dict:
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- 组合服务


async def run_breakdown(
    db: Session, asset_id: int, *, force: bool = False, job_id: int | None = None
) -> ViralBreakdown:
    """跑完整爆款拆解链路，返回并落库 `ViralBreakdown`。

    - 已 `done` 且 `force=False`：直接返回既有行（幂等，不重复花 token）。
    - `force=True`：即使已 `done` 也重跑（upsert，按 asset_id 唯一，不产生重复行）。
    - 依赖约束：调用方应先通过 ``workflow_engine`` 放行；这里再做一次硬校验，
      没有已完成转录与视频理解时拒绝调用模型（不产出降级空报告）。

    Raises:
        BreakdownInputError: 素材不存在 / 无关联内容（数据问题，不可重试）。
        Exception: 拆解链路任何失败——落 `status=error` 后原样抛出（worker 负责重试）。
    """
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise BreakdownInputError(f"素材不存在: {asset_id}")

    transcript = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    if transcript is None or not (transcript.full_text or "").strip():
        raise BreakdownInputError("拆解前置未完成：需要先完成转录")
    understanding = db.scalar(
        select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id)
    )
    if understanding is None or understanding.status != "done":
        raise BreakdownInputError("拆解前置未完成：需要先完成视频理解")

    row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
    if row is not None and row.status == "done" and not force:
        return row


    if row is None:
        row = ViralBreakdown(asset_id=asset_id)
        db.add(row)
    row.status = "doing"
    row.error = ""
    events.publish("breakdown_status", {"asset_id": asset_id, "status": "doing"}, db=db)
    db.commit()
    db.refresh(row)

    try:
        content = db.get(ContentItem, asset.content_id)
        if content is None:  # 数据不一致（孤儿资产）：显式报错，不静默产出空报告
            raise BreakdownInputError(
                f"素材 #{asset_id} 未关联内容（content_id={asset.content_id}）"
            )

        s = get_breakdown_settings(db)
        # 漏斗配置与拆解配置分属两个 KV 模块，这里合并成一份视图。
        s = {**s, **_funnel_config(db)}

        from app.services.model_access_service import ModelAccessService

        gateway = ModelAccessService()

        async def call_model(*, request, parse_result, **kwargs):
            return await gateway.call_with_fallback(
                db,
                work_type="text_analysis",
                operation="breakdown",
                request=request,
                parse_result=parse_result,
                validate_result=kwargs.get("validate_result"),
                asset_id=asset_id,
                job_id=job_id,
            )

        async def call_prefilter(*, request, parse_result, **kwargs):
            # 初筛走独立的 prefilter 槽位：它要的是便宜的小模型，与拆解用的
            # 大模型是两回事。混用一个槽位会让「省钱」变成「多花一次大模型钱」。
            return await gateway.call_with_fallback(
                db,
                work_type="prefilter",
                operation="prefilter",
                request=request,
                parse_result=parse_result,
                asset_id=asset_id,
                job_id=job_id,
            )

        funnel: dict = {"enabled": False}
        transcript_for_prompt = None
        if s.get("prefilter_enabled", False):
            transcript_for_prompt, funnel = await _funnel_transcript(
                db, asset_id, s, call_prefilter
            )

        payload = _assemble_payload(
            db,
            content,
            asset,
            s["prompt"],
            transcript_limit=int(s.get("transcript_limit") or 0),
            s_criteria=s.get("hook_criteria"),
            transcript=transcript_for_prompt,
        )
        system, user = breakdown.build_breakdown_prompt(payload)

        # 带推理的模型输出 2k 字符 JSON 实测 80~240 秒；写死 90 秒会把「正在思考」
        # 误判成失败，白跑一次主备切换还把结果记成 error。
        result = await breakdown.run_llm(
            system,
            user,
            call_model=call_model,
            timeout=float(s.get("timeout_sec") or 300),
        )
        parsed = result.get("data") or {}
        if not isinstance(parsed, dict):
            raise ValueError("拆解返回不是 JSON 对象")

        row.score = _clamp_score(parsed.get("score"))
        row.verdict = _str_or_empty(parsed.get("verdict"))
        row.sections = _sections(parsed)
        row.comment_insights = _dict_or_empty(parsed.get("comment_insights"))
        # 判据命中是结构化结果：它回答「这条凭什么算爆点」，比自由文本的 verdict 更可核对。
        row.sections["hook_matches"] = _hook_matches(parsed)
        row.data_context = payload["data_context"]
        row.understanding_available = payload["understanding"] is not None
        row.model = result.get("model") or ""
        row.prompt_version = _PROMPT_VERSION
        row.usage = result.get("usage") or {}
        if result.get("routing"):
            routing = dict(result["routing"])
            # 首轮不是 JSON、靠修复轮救回来的，必须在结果里留痕：这个模型
            # 「能干活但不守格式」，换一条素材仍可能失败，不该显示成一切正常。
            if result.get("format_repair"):
                routing["format_repair"] = result["format_repair"]
            row.routing = {"text_analysis": routing}
        if funnel.get("enabled"):
            # 漏斗是「送了多少给大模型」的证据：不解开它就说不清为什么报告
            # 只覆盖了部分内容，也无法判断它到底省了多少。
            row.routing = {**(row.routing or {}), "prefilter": funnel}
        row.status = "done"
        row.error = ""
        events.publish("breakdown_status", {"asset_id": asset_id, "status": "done"}, db=db)
        db.commit()
        db.refresh(row)
        return row
    except Exception as exc:
        db.rollback()
        row = db.scalar(select(ViralBreakdown).where(ViralBreakdown.asset_id == asset_id))
        if row is None:
            row = ViralBreakdown(asset_id=asset_id)
            db.add(row)
        row.status = "error"
        row.error = str(exc)[:512]
        events.publish("breakdown_status", {"asset_id": asset_id, "status": "error"}, db=db)
        db.commit()
        raise
