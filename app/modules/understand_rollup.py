"""分块理解的汇总：把各块的结论合成一份整体描述。

分块理解解决了「长视频压不下体积」的问题，但每块单独看只能说出「这一段讲了
什么」，说不出「整条视频的结构是什么」。这一步用纯文本模型（不需要视频）
把各块结论合并成整体 summary + 各维度结论。

为什么单独一个模块：它是纯函数（拼提示词 + 解析），便于单测；
调用走统一网关，不在这里发请求。
"""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")

ROLLUP_SYSTEM = (
    "你是短视频内容分析师。下面是一条视频被切成若干段后，各段分别得出的理解结论。\n"
    "请把它们合并成**整条视频**的结论。\n"
    "硬性要求：\n"
    "1. 只依据给出的分段结论，不得编造分段里没有的细节；\n"
    "2. 如果各段结论互相矛盾，以多数段为准，并在 summary 里注明分歧；\n"
    "3. 只输出 JSON，不要解释、不要 Markdown 代码块。\n"
    '输出结构：{"summary": "<150字内整体概括>", '
    '"dimensions": {"<维度key>": "<值>"}, "confidence": 0.0}'
)


def build_rollup_prompt(
    parts: list[dict],
    *,
    dimensions: list[dict],
    duration_sec: float = 0,
) -> str:
    """把各块结论拼成汇总请求。

    Args:
        parts: 每块的 ``{"index","start","end","summary","dimensions","confidence"}``。
        dimensions: 需要产出的维度清单（与理解阶段同一份）。
        duration_sec: 原片总时长，用于让模型知道整条视频的规模。
    """
    dim_lines = "\n".join(f"- {d['key']}（{d['name']}）：{d['ask']}" for d in dimensions)
    blocks = []
    for part in parts or []:
        head = f"【第 {int(part.get('index', 0)) + 1} 段 {_fmt_time(part.get('start'))}-{_fmt_time(part.get('end'))}】"
        body = {
            "summary": part.get("summary") or "",
            "dimensions": part.get("dimensions") or {},
            "confidence": part.get("confidence"),
        }
        blocks.append(head + "\n" + json.dumps(body, ensure_ascii=False))
    return (
        f"【整条视频时长】{_fmt_time(duration_sec)}\n"
        f"【需要合并的分段结论】\n" + "\n\n".join(blocks) + "\n\n"
        f"【维度定义】\n{dim_lines}\n"
        + ROLLUP_SYSTEM
    )


def _fmt_time(value: object) -> str:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if seconds != seconds or seconds < 0:
        return "?"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def parse_rollup(text: str) -> dict:
    """容错解析汇总结果；缺字段按空处理，不抛异常（汇总失败不应毁掉整条链路）。"""
    raw = (text or "").strip()
    candidate = _FENCE_RE.sub("", raw).strip()
    parsed = None
    for attempt in (raw, candidate):
        try:
            parsed = json.loads(attempt)
            break
        except (TypeError, ValueError):
            continue
    if parsed is None:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(candidate[start : end + 1])
            except (TypeError, ValueError):
                parsed = None
    if not isinstance(parsed, dict):
        return {}
    dims = parsed.get("dimensions")
    return {
        "summary": parsed.get("summary") if isinstance(parsed.get("summary"), str) else "",
        "dimensions": {str(k): v for k, v in dims.items()} if isinstance(dims, dict) else {},
        "confidence": parsed.get("confidence"),
    }
