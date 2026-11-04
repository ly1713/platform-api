"""两级漏斗：本地小模型先通读全文圈「可能有爆点」的区间，云端大模型只精读入围部分。

为什么要它：长视频的转录动辄几万字，全量送云端模型既贵又慢。先用本机小模型
（Ollama / llama.cpp 等 OpenAI 兼容端点）扫一遍，把送云端的内容压到一个量级。

**铁律是 fail-open**：本地端点不可达、超时、输出不可解析，一律静默回退全文直发。
漏斗只省钱，不背锅——它绝不能让一条本来能跑的任务失败。

参考实现（hotclip/prefilter.ts）的几个工程细节此处复用：
- 按字符数切块，但**以整句为单位**，不拆句；
- 入围窗口每侧扩几句，防止把钩子的铺垫切没；
- 整条初筛有总时限，慢不能拖垮主链路。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger("platform.prefilter")

# 全文低于该字符数不启用漏斗——短稿直发云端反而更准更快。
MIN_CHARS = 3000
# 每块喂给小模型的目标字符数（小模型上下文短，块要小）。
CHUNK_TARGET_CHARS = 2600
# 入围窗口每侧扩展的句数（给云端留上下文，防止把钩子的铺垫切没）。
WINDOW_PAD_SEGMENTS = 2

PREFILTER_SYSTEM = (
    "你是短视频素材的初筛助手。给你一段逐句转录，请圈出「可能有爆点」的句子区间——\n"
    "判断标准：制造好奇的开场、情绪爆发、反直觉观点、意外反转、或强实用价值。\n"
    "只输出 JSON：{\"windows\": [{\"start\": <起始句序号>, \"end\": <结束句序号>}]}\n"
    "句序号从 0 开始。不要输出解释、不要 Markdown。"
)


@dataclass(frozen=True)
class Window:
    """入围区间：句序号（闭区间）。"""

    start: int
    end: int


@dataclass
class FunnelStats:
    """漏斗统计，落库/日志用，便于判断它到底省了多少。"""

    enabled: bool = False
    chunks: int = 0
    windows: int = 0
    kept_segments: int = 0
    total_segments: int = 0
    fallback_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "chunks": self.chunks,
            "windows": self.windows,
            "kept_segments": self.kept_segments,
            "total_segments": self.total_segments,
            "fallback_reason": self.fallback_reason,
        }


def strip_think_blocks(text: str) -> str:
    """剥掉推理模型的思考块（qwen3 等会先思考再给 JSON）。

    两种形态都要处理：
    - 完整的 ``<think ...>...</think>`` 对；
    - 只有收尾 ``*/`` 的残缺块（上游被截断时常见）——把收尾标记之前的部分一起丢掉。
    """
    cleaned = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", text or "", flags=re.S | re.I)
    # 残缺的收尾：只剩 </think>，把它及之前的内容视为思考残留
    if "</think" in cleaned.lower():
        idx = cleaned.lower().index("</think")
        end = cleaned.find(">", idx)
        cleaned = cleaned[end + 1 :] if end != -1 else ""
    return cleaned.strip()


def chunk_segments(segments: list[dict], target_chars: int = CHUNK_TARGET_CHARS) -> list[list[dict]]:
    """按累计字符数切块，整句为单位。

    切句而不是切字符：把一句话劈成两半，模型读到的是残句，判据全乱。
    """
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "")
        if cur and chars + len(text) > target_chars:
            chunks.append(cur)
            cur = []
            chars = 0
        cur.append(seg)
        chars += len(text)
    if cur:
        chunks.append(cur)
    return chunks


def parse_windows(content: str, *, valid_ids: set[int]) -> list[Window]:
    """解析小模型的入围区间；只留两端序号都真实存在的窗口。

    Raises:
        ValueError: 输出不可解析 / 缺 windows 字段（调用方据此 fail-open）。
    """
    cleaned = strip_think_blocks(content)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    try:
        parsed = json.loads(match.group(0) if match else cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError("初筛输出不是合法 JSON") from exc
    raw = parsed.get("windows") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        raise ValueError("初筛输出缺少 windows 数组")
    out: list[Window] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start > end:
            start, end = end, start
        if start not in valid_ids and end not in valid_ids:
            continue
        out.append(Window(max(0, start), max(0, end)))
    return out


def expand_windows(windows: list[Window], *, total: int, pad: int = WINDOW_PAD_SEGMENTS) -> list[Window]:
    """每侧扩几句，并把重叠/相邻的窗口合并。"""
    if not windows:
        return []
    padded = [Window(max(0, w.start - pad), min(total - 1, w.end + pad)) for w in windows]
    padded.sort(key=lambda w: w.start)
    merged = [padded[0]]
    for item in padded[1:]:
        last = merged[-1]
        if item.start <= last.end + 1:
            merged[-1] = Window(last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return merged


async def _select(
    segments: list[dict],
    *,
    call: Callable[[str, str], Awaitable[str]],
    min_chars: int,
    chunk_chars: int,
) -> tuple[list[dict], FunnelStats]:
    """漏斗主体（async）。所有失败路径都返回原文——fail-open。"""
    stats = FunnelStats(enabled=True, total_segments=len(segments or []))
    total_chars = sum(len(str(s.get("text") or "")) for s in segments or [] if isinstance(s, dict))
    if total_chars < min_chars:
        stats.enabled = False
        stats.fallback_reason = f"全文 {total_chars} 字符，低于 {min_chars}，不启用初筛"
        return list(segments or []), stats

    chunks = chunk_segments(segments, chunk_chars)
    stats.chunks = len(chunks)
    all_windows: list[Window] = []
    index_base = 0
    for chunk in chunks:
        valid = set(range(index_base, index_base + len(chunk)))
        body_lines = [
            f"{index_base + i}: {str(seg.get('text') or '').strip()}"
            for i, seg in enumerate(chunk)
            if str(seg.get("text") or "").strip()
        ]
        if not body_lines:
            index_base += len(chunk)
            continue
        try:
            raw = await call(PREFILTER_SYSTEM, "\n".join(body_lines))
            windows = parse_windows(raw, valid_ids=valid)
        except Exception as exc:  # noqa: BLE001 漏斗失败必须回退全文
            stats.fallback_reason = f"初筛失败，回退全文：{str(exc)[:120]}"
            logger.info("初筛失败，回退全文直发：%s", exc)
            return list(segments or []), stats
        all_windows.extend(windows)
        index_base += len(chunk)

    if not all_windows:
        stats.fallback_reason = "初筛没有圈出任何区间，回退全文"
        return list(segments or []), stats

    merged = expand_windows(all_windows, total=len(segments))
    stats.windows = len(merged)
    kept: list[dict] = []
    cursor = 0
    for window in merged:
        echo = {"start": None, "end": None, "text": "…（前文略）"}
        if window.start > cursor:
            kept.append(echo)
        kept.extend(segments[window.start : window.end + 1])
        cursor = window.end + 1
    stats.kept_segments = len(kept)
    return kept, stats


async def select_segments_async(
    segments: list[dict],
    *,
    llm: Callable[[str, str], Awaitable[str]] | None = None,
    min_chars: int = MIN_CHARS,
    chunk_chars: int = CHUNK_TARGET_CHARS,
) -> tuple[list[dict], FunnelStats]:
    """跑一次初筛（异步网关版），返回 ``(保留的分段, 统计)``。

    **任何失败都返回原文**（fail-open）：漏斗是省钱手段，不是正确性前提。
    """
    if llm is None:
        stats = FunnelStats(enabled=False, total_segments=len(segments or []))
        stats.fallback_reason = "未配置初筛模型"
        return list(segments or []), stats
    return await _select(segments, call=llm, min_chars=min_chars, chunk_chars=chunk_chars)


def select_segments(
    segments: list[dict],
    *,
    llm: Callable[[str, str], str] | None = None,
    min_chars: int = MIN_CHARS,
    chunk_chars: int = CHUNK_TARGET_CHARS,
    timeout_hint: str = "",
) -> tuple[list[dict], FunnelStats]:
    """同步版本（测试/离线脚本用）。生产链路请用 ``select_segments_async``。"""
    if llm is None:
        stats = FunnelStats(enabled=False, total_segments=len(segments or []))
        stats.fallback_reason = "未配置初筛模型"
        return list(segments or []), stats

    async def call(system: str, body: str) -> str:
        return llm(system, body)

    return asyncio.run(_select(segments, call=call, min_chars=min_chars, chunk_chars=chunk_chars))


def parse_windows(content: str, *, valid_ids: set[int]) -> list[Window]:
    """解析小模型的入围区间；只留两端序号都真实存在的窗口。

    Raises:
        ValueError: 输出不可解析 / 缺 windows 字段（调用方据此 fail-open）。
    """
    cleaned = strip_think_blocks(content)
    match = re.search(r"\{[\s\S]*\}", cleaned)
    try:
        parsed = json.loads(match.group(0) if match else cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError("初筛输出不是合法 JSON") from exc
    raw = parsed.get("windows") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        raise ValueError("初筛输出缺少 windows 数组")
    out: list[Window] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start > end:
            start, end = end, start
        if start not in valid_ids and end not in valid_ids:
            continue
        out.append(Window(max(0, start), max(0, end)))
    return out


def expand_windows(windows: list[Window], *, total: int, pad: int = WINDOW_PAD_SEGMENTS) -> list[Window]:
    """每侧扩几句，并把重叠/相邻的窗口合并。"""
    if not windows:
        return []
    padded = [Window(max(0, w.start - pad), min(total - 1, w.end + pad)) for w in windows]
    padded.sort(key=lambda w: w.start)
    merged = [padded[0]]
    for item in padded[1:]:
        last = merged[-1]
        if item.start <= last.end + 1:
            merged[-1] = Window(last.start, max(last.end, item.end))
        else:
            merged.append(item)
    return merged


def select_segments(
    segments: list[dict],
    *,
    llm: Callable[[str, str], str] | None = None,
    min_chars: int = MIN_CHARS,
    chunk_chars: int = CHUNK_TARGET_CHARS,
    timeout_hint: str = "",
) -> tuple[list[dict], FunnelStats]:
    """跑一次初筛，返回 ``(保留的分段, 统计)``。

    **任何失败都返回原文**（fail-open）：漏斗是省钱手段，不是正确性前提。
    """
    stats = FunnelStats(enabled=True, total_segments=len(segments or []))
    if llm is None:
        stats.enabled = False
        stats.fallback_reason = "未配置初筛模型"
        return list(segments or []), stats

    total_chars = sum(len(str(s.get("text") or "")) for s in segments or [] if isinstance(s, dict))
    if total_chars < min_chars:
        stats.enabled = False
        stats.fallback_reason = f"全文 {total_chars} 字符，低于 {min_chars}，不启用初筛"
        return list(segments or []), stats

    chunks = chunk_segments(segments, chunk_chars)
    stats.chunks = len(chunks)
    all_windows: list[Window] = []
    index_base = 0
    for chunk in chunks:
        valid = set(range(index_base, index_base + len(chunk)))
        body_lines = [
            f"{index_base + i}: {str(seg.get('text') or '').strip()}"
            for i, seg in enumerate(chunk)
            if str(seg.get("text") or "").strip()
        ]
        if not body_lines:
            index_base += len(chunk)
            continue
        try:
            raw = llm(PREFILTER_SYSTEM, "\n".join(body_lines))
            windows = parse_windows(raw, valid_ids=valid)
        except Exception as exc:  # noqa: BLE001 漏斗失败必须回退全文
            stats.fallback_reason = f"初筛失败，回退全文：{str(exc)[:120]}"
            logger.info("初筛失败，回退全文直发：%s", exc)
            return list(segments or []), stats
        all_windows.extend(windows)
        index_base += len(chunk)

    if not all_windows:
        stats.fallback_reason = "初筛没有圈出任何区间，回退全文"
        return list(segments or []), stats

    merged = expand_windows(all_windows, total=len(segments))
    stats.windows = len(merged)
    kept: list[dict] = []
    cursor = 0
    for window in merged:
        echo = {"start": None, "end": None, "text": "…（前文略）"}
        if window.start > cursor:
            kept.append(echo)
        kept.extend(segments[window.start : window.end + 1])
        cursor = window.end + 1
    stats.kept_segments = len(kept)
    return kept, stats
