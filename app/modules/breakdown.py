"""爆款拆解模块：提示词构造 + LLM 调用 + JSON 解析。

职责（纯模块层，不碰 DB；DB 拼装/落库在 `app/services/breakdown_service.py`）：

- `build_breakdown_prompt(payload) -> (system, user)`：把「元数据 + 转录 + 视频理解(L3) +
  评论摘要 + 后端算好的互动数字」拼成 LLM 的 system/user，纯函数便于单测。
- `run_llm(system, user, *, call_model, timeout) -> dict`：经统一 ``ModelAccessService``
  发起请求，强制 JSON 输出，容错解析严格 JSON；非法输出抛异常（由 worker 落
  `status=error` / 重试，不静默降级）。

红线：
- 数字（相对中位数倍数/互动率/分位）由后端确定性计算后作为**输入**交给模型，模型只做定性，
  禁止重算或杜撰（提示词里已写明）。
- 供应商与模型名来自「模型接入」，由网关注入，不硬编码。

模型请求只经调用方注入的网关回调；本模块不读取供应商配置、不直连 HTTP。
"""

import json
import logging
import re
from typing import Callable

from app.core.breakdown_contract import render_repair_schema, truncate_transcript
from app.core.breakdown_settings import DEFAULT_BREAKDOWN_PROMPT
from app.core.evidence_class import evidence_guidance
from app.core.hook_criteria import (
    DEFAULT_HOOK_CRITERIA,
    criteria_prompt_block,
    hook_matches_instruction,
)

logger = logging.getLogger("platform.breakdown")

# 转录送入拆解的默认最大字符数。历史上写死 8000，一条 8 分钟的口播就吃满上限，
# 结尾的转折和 CTA 直接丢失，但报告仍要填 ``structure`` / ``cta``。现在由
# ``breakdown_transcript_limit`` 配置项控制（见 ``app/core/breakdown_settings.py``）。
_TRANSCRIPT_LIMIT = 24000
_COMMENT_LIMIT = 30
# 视频理解自评低于该值时，提示词里要显式标注不确定性。
_LOW_CONFIDENCE = 0.6

# 去掉 ```json ... ``` / ``` ... ``` 代码围栏（与 vision.py 同款，保持容错一致）。
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


class BreakdownFormatError(ValueError):
    """模型（含修复轮）仍未返回合法 JSON 对象。

    继承 ``ValueError`` 保持既有调用方语义（worker 据此不按网络错误重试）。
    ``error_class`` 供调用审计使用：这类失败以前落成 ``unknown``，按
    error_class 排查时完全看不见「模型没按格式回」和「网络挂了」的区别。
    """

    error_class = "invalid_json"

    def __init__(self, message: str, *, first_error: BaseException | None = None):
        super().__init__(message)
        self.first_error = first_error


def _fmt(value: object, fallback: str = "（无）") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _dump(value: object) -> str:
    """紧凑 JSON 文本（中文不转义）；非 JSON 可序列化对象退化为 str。"""
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def build_breakdown_prompt(payload: dict) -> tuple[str, str]:
    """拼装拆解提示词，返回 ``(system, user)``。

    ``payload`` 字段（均容错，缺失按「（无）」/空处理）::

        platform, title, author, duration_sec, published_at,
        play, like, comment,
        data_context,      # 后端确定性算好的数字（相对中位数倍数/互动率/分位）
        transcript,        # transcript.full_text
        timeline,          # 视频理解 timeline（L3）
        understanding,     # 视频理解 {summary, dimensions, ...}（L3）
        comments,          # Top-N 评论正文（list[str]）
        hook_criteria,     # 爆点判据表（list[{id,name,ask}]，可配置）
        evidence_class,    # words/reaction/visual，决定「该信哪路证据」
        moments,           # 视听信号融合出的候选片段（客观测量，非模型产出）

    system 提示词优先取 payload["prompt"]（服务层从设置读取，可在线改），否则用内置默认。
    判据表与证据类别**追加**在 system 之后：用户的自定义提示词仍然是最主要的约束，
    这两段只补「按什么标准找爆点」「这类素材该信哪路证据」两件模型无法自己知道的事。
    """
    system = payload.get("prompt")
    if not isinstance(system, str) or not system.strip():
        system = DEFAULT_BREAKDOWN_PROMPT

    raw_criteria = payload.get("hook_criteria")
    criteria = raw_criteria if isinstance(raw_criteria, list) and raw_criteria else DEFAULT_HOOK_CRITERIA
    system += "\n\n" + criteria_prompt_block(criteria)
    system += "\n输出 JSON 中增加字段：" + hook_matches_instruction(criteria)

    evidence_class = payload.get("evidence_class")
    if isinstance(evidence_class, str) and evidence_class:
        system += "\n\n【本片证据优先级】" + evidence_guidance(evidence_class)

    # 互动数字：既列当前值，也列后端算好的上下文（相对中位数倍数/互动率）。
    metric_lines = [
        f"播放：{_fmt(payload.get('play'), '未知')}",
        f"点赞：{_fmt(payload.get('like'), '未知')}",
        f"评论：{_fmt(payload.get('comment'), '未知')}",
    ]
    data_context = payload.get("data_context")
    if isinstance(data_context, dict) and data_context:
        metric_lines.append(f"确定性上下文（后端算好，只引用不重算）：{_dump(data_context)}")

    limit = payload.get("transcript_limit")
    limit = int(limit) if isinstance(limit, int) and limit > 0 else _TRANSCRIPT_LIMIT
    raw_transcript = payload.get("transcript")
    # 截断与「已截断」提示由契约层统一渲染：外部料包用同一个函数，
    # 两条路径看到的材料才逐字节相同。
    transcript, _truncation = truncate_transcript(raw_transcript, limit)
    comment_lines = [str(c) for c in (payload.get("comments") or []) if str(c).strip()][:_COMMENT_LIMIT]

    understanding = payload.get("understanding")
    if isinstance(understanding, dict) and understanding:
        understanding_text = _dump(understanding)
        confidence = understanding.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < _LOW_CONFIDENCE:
            # 低置信的画面描述不能和「确实看清了」等价对待，否则报告会把猜测写成事实。
            understanding_text += (
                f"\n（注意：视频理解模型自评置信度仅 {confidence:.2f}，"
                "画面结论可能不准；引用画面时请标注不确定性，勿当作确凿证据。）"
            )
    else:
        understanding_text = "（无视频理解结果，请仅依据转录与评论分析，不要臆测画面）"

    timeline = payload.get("timeline")
    timeline_text = _dump(timeline) if timeline else "（无）"

    moments = payload.get("moments")
    moment_lines = []
    for m in moments if isinstance(moments, list) else []:
        if not isinstance(m, dict):
            continue
        try:
            start = int(round(float(m.get("start", 0))))
            end = int(round(float(m.get("end", 0))))
        except (TypeError, ValueError):
            continue
        drivers = m.get("drivers")
        why = "/".join(drivers.keys()) if isinstance(drivers, dict) and drivers else "综合"
        moment_lines.append(f"- {start}-{end}s（{why}，强度 {m.get('score', 0)}）")

    user = (
        "【作品元数据】\n"
        f"平台：{_fmt(payload.get('platform'))}\n"
        f"标题：{_fmt(payload.get('title'))}\n"
        f"作者：{_fmt(payload.get('author'))}\n"
        f"时长（秒）：{_fmt(payload.get('duration_sec'), '未知')}\n"
        f"发布时间：{_fmt(payload.get('published_at'))}\n"
        "\n【互动数据】\n"
        + "\n".join(metric_lines)
        + "\n\n【转录文案】\n"
        + (transcript or "（无转录，请仅依据其他材料分析）")
        + "\n\n【视频理解（L3）】\n"
        + understanding_text
        + "\n\n【时间轴】\n"
        + timeline_text
        + "\n\n【系统测得的候选片段】\n"
        + (
            "以下由 ffmpeg 测量响度/镜头切换/运动量融合得出，是客观测量而非模型推测；\n"
            "可作为「高能区在哪」的定位依据，但**不能替代**你对内容价值的判断：\n"
            + "\n".join(moment_lines)
            if moment_lines
            else "（无）"
        )
        + "\n\n【评论区 Top 摘要】\n"
        + ("\n".join(f"- {c}" for c in comment_lines) if comment_lines else "（无评论摘要）")
    )
    return system, user


def parse_structured(text: str) -> dict:
    """容错解析模型返回的 JSON 对象。

    依次尝试：直接解析 -> 去 Markdown 代码围栏后解析 -> 截取第一个 `{` 到最后一个 `}`。
    全部失败抛 `ValueError`（调用方决定落 error / 重试）。
    """
    if not isinstance(text, str):
        raise ValueError(f"拆解返回文本类型非法: {type(text).__name__}")
    raw = text.strip()
    candidate = _FENCE_RE.sub("", raw).strip()
    for attempt in (raw, candidate):
        try:
            parsed = json.loads(attempt)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("拆解返回内容不是合法 JSON 对象")


async def run_llm(
    system: str,
    user: str,
    *,
    call_model: Callable | None = None,
    timeout: float = 300,
    repair: bool = True,
) -> dict:
    """经统一模型网关执行拆解，返回 ``{"data", "usage", "model", "routing"}``。

    Args:
        system/user: `build_breakdown_prompt` 产出的提示词。
        call_model: 统一模型网关回调；未注入即视为编程错误。
        timeout: 单次请求超时秒数（拆解为长文本；带推理的模型实测需 80~240s）。
        repair: 首次返回不是合法 JSON 时，是否原地让模型改写。
            ``response_format`` 只是「请求」，不是「保证」：实测部分网关/模型
            会直接无视它并输出 Markdown 报告（deepseek-v4.1-flash、MiniMax-M3
            均如此），一次就把整个拆解判死。把模型自己的输出连同一份严格
            schema 再喂回去，两者都能稳定产出合法 JSON。

    Raises:
        BreakdownFormatError: 首轮与修复轮都不是合法 JSON 对象。
    """
    if call_model is None:
        raise RuntimeError("爆款拆解必须经 ModelAccessService 调用，禁止直连模型接口")
    if repair:
        # 修复轮要看到首轮的原文才能改写，所以解析器只记录不抛错。
        captured: dict[str, str] = {}

        def _capture(text: str) -> dict:
            captured["text"] = text
            return parse_structured(text)

        first_result = None
        try:
            result = await call_model(
                request={
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.4,
                    "response_format": {"type": "json_object"},
                    "timeout": timeout,
                },
                parse_result=_capture,
            )
        except Exception as exc:  # noqa: BLE001 只为判断能否修复，其余原样上抛
            # 网关把「解析失败」包成异常抛出，首轮的用量挂在异常上（若提供）。
            first_result = getattr(exc, "result", None)
            raw = captured.get("text") or ""
            if not raw.strip():
                raise
            repaired = await _repair_json(
                call_model, system, user, raw, timeout=timeout
            )
            if repaired is None:
                raise BreakdownFormatError(
                    f"模型两次都未返回合法 JSON：{_summarize_raw(raw)}",
                    first_error=exc,
                ) from exc
            logger.warning("拆解首轮不是 JSON，修复轮成功（原文 %d 字符）", len(raw))
            out = _as_result(repaired)
            # 修复轮成功不代表首轮没出问题：路由信息里要留下痕迹，否则从结果页
            # 看这个模型「一切正常」，下次换素材失败时又会从头查起。
            out["format_repair"] = {
                "repaired": True,
                "first_round_chars": len(raw),
                "first_round_head": _summarize_raw(raw, 80),
            }
            # 两轮都真的调了模型、都计费，用量必须相加；只记修复轮会少算一次。
            out["usage"] = _merge_usage(_usage_of(first_result), out["usage"])
            return out
        return _as_result(result)

    result = await call_model(
        request={
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "timeout": timeout,
        },
        parse_result=parse_structured,
    )
    return _as_result(result)


def _as_result(result) -> dict:
    return {
        "data": result.data if hasattr(result, "data") else result,
        "usage": getattr(result, "usage", {}) or {},
        "model": getattr(result, "used_model_key", ""),
        "routing": result.to_routing() if hasattr(result, "to_routing") else {},
    }


def _usage_of(result) -> dict:
    usage = getattr(result, "usage", None)
    return usage if isinstance(usage, dict) else {}


def _merge_usage(first: dict, second: dict) -> dict:
    """两轮用量相加（两轮都真实调用了模型、都计费）。"""
    out = dict(second or {})
    for key, value in (first or {}).items():
        if isinstance(value, (int, float)) and isinstance(out.get(key), (int, float)):
            out[key] = out[key] + value
        elif key not in out:
            out[key] = value
    return out


def _summarize_raw(text: str, limit: int = 120) -> str:
    """把模型原文压成一行摘要，供错误消息与日志定位。"""
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


_REPAIR_SCHEMA = render_repair_schema()

_REPAIR_INSTRUCTION = (
    "你上一条回复不是合法 JSON，系统无法解析。\n"
    "请把上一条回复里的分析结论改写成一个 JSON 对象。硬性要求：\n"
    "1. 只输出 JSON 本身，不要解释、不要 Markdown、不要代码围栏；\n"
    "2. **必须**使用下面结构里的英文键名，不要用中文键名，不要额外嵌套层级；\n"
    "3. 上一条回复里没有的信息填 null 或空数组，不要临时编造。\n"
    "目标结构：\n" + _REPAIR_SCHEMA
)


async def _repair_json(
    call_model: Callable,
    system: str,
    user: str,
    raw: str,
    *,
    timeout: float,
) -> object | None:
    """让模型把自己的非 JSON 输出改写成合法 JSON。

    返回网关的完整结果对象（含 model/usage/routing），不是裸数据——
    只返回数据会让修复成功的这条记录丢掉真实模型名，落库时保留上一次
    运行的路由，排查时会被彻底带偏（实测：deepseek 跑的报告显示成 gpt-6-sol）。
    仍失败返回 None。
    """
    try:
        result = await call_model(
            request={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": _REPAIR_INSTRUCTION},
                ],
                # 改写是「照抄结论换格式」，不需要也不应该再有创造性。
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "timeout": timeout,
            },
            parse_result=parse_structured,
        )
    except Exception:  # noqa: BLE001 修复失败要保留首轮原文的错误摘要
        return None
    return result
