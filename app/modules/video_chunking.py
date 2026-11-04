"""长视频分块与时间偏移（纯函数，可单测）。

为什么必须分块：视频理解的输入是「整段视频内联进一次请求」，18 分钟的原片
压进 15MB 后码率只剩几十 kbps，画面里的字幕和 UI 全糊。与其把整片压烂，
不如切成若干段，每段都有足够的码率预算。

参考实现（hotclip / AI-Youtube-Shorts-Generator）都采用「分块 + 重叠 + 偏移」：
- 分块：每块独立送模型，避免单次请求过大；
- 重叠：块与块的接缝处内容不丢，跨块的一段话能在一侧完整出现；
- 偏移：模型报的是「块内相对秒数」，落库前必须加回块起点，否则下游
  timeline / 拆解的时间定位全部错位。

红线：这里只做算术，不碰 ffmpeg 也不发请求。切文件在服务层做。
"""

from __future__ import annotations

from dataclasses import dataclass

# 单块默认目标时长。300 秒是平衡点：短视频平台的长口播切成 5 分钟一段，
# 每段在 15MB 预算下仍能保住短边 720；再长就开始掉画质。
DEFAULT_TARGET_SECONDS = 300
# 块间重叠。接缝处的一句话经常跨块，留 15 秒让两侧都能读到完整语义。
DEFAULT_OVERLAP_SECONDS = 15
# 低于该时长不分块：单次请求就够，分块反而多花一次调用。
DEFAULT_THRESHOLD_SECONDS = 600

# 分块上限：防止一条几小时的视频切出上百块把队列和账单打爆。
# 超长视频按「块数上限」自动放大单块时长，而不是无限切。
MAX_CHUNKS = 12


@dataclass(frozen=True)
class ChunkRange:
    """一块在**原片**里的时间范围（秒）。"""

    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def plan_chunks(
    duration: float,
    *,
    target_seconds: int = DEFAULT_TARGET_SECONDS,
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
    max_chunks: int = MAX_CHUNKS,
) -> list[ChunkRange]:
    """把 ``[0, duration)`` 切成若干带重叠的块。

    Args:
        duration: 原片时长（秒）。<=0 返回空列表（调用方按「无需分块」处理）。
        target_seconds: 单块目标时长；块数因此超过 ``max_chunks`` 时自动放大。
        overlap_seconds: 相邻块的重叠秒数；必须小于 target，否则会被夹到一半。
        max_chunks: 块数上限。

    Returns:
        按时间升序的块列表。最后一块的 ``end`` 严格等于 ``duration``，
        保证片尾不会被切掉。
    """
    if duration is None or duration <= 0 or target_seconds <= 0:
        return []

    overlap = max(0.0, float(overlap_seconds))
    target = float(target_seconds)
    # 重叠不能吃掉整块，否则会出现 start >= end 的空块。
    if overlap >= target:
        overlap = target / 2

    max_chunks = max(1, int(max_chunks))
    # 块数超上限就放大单块时长（而不是让块数无限增长）。
    min_target = duration / max_chunks
    step = max(target - overlap, target / 2)
    if step <= 0:
        step = target
    estimated = 1 if duration <= target else 1 + int((duration - target) / step)
    if estimated > max_chunks:
        # 按上限重算步长：块数 = ceil((duration - overlap) / step) <= max_chunks
        step = max((duration - target) / (max_chunks - 1), target / 2) if max_chunks > 1 else duration

    chunks: list[ChunkRange] = []
    start = 0.0
    index = 0
    while start < duration and index < max_chunks:
        end = min(duration, start + target)
        chunks.append(ChunkRange(index=index, start=round(start, 3), end=round(end, 3)))
        if end >= duration:
            break
        index += 1
        start = end - overlap
        # 浮点保护：start 没前进就强行推进，避免死循环。
        if start <= chunks[-1].start:
            start = chunks[-1].start + step

    # 收尾：保证覆盖到片尾（块数上限已用完时，把最后一块拉到结尾）。
    if chunks and chunks[-1].end < duration:
        last = chunks[-1]
        chunks[-1] = ChunkRange(index=last.index, start=last.start, end=round(duration, 3))
    return chunks


def needs_chunking(duration: float, *, threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS) -> bool:
    """是否值得分块。短于阈值的视频单次请求即可，分块只是多花钱。"""
    if duration is None or duration <= 0 or threshold_seconds <= 0:
        return False
    return duration > threshold_seconds


def shift_timeline(timeline: object, offset: float) -> list[dict]:
    """把块内相对时间轴的 ``t`` 加回块起点。

    模型看到的是从 0 开始的块，它报的 ``t`` 是相对块起点的；而转录、拆解、
    前端时间轴用的都是原片的绝对秒数。不加偏移会出现「第 3 秒的钩子」
    实际在原片第 10 分钟这种静默错位——报告看起来完全正常，时间全错。
    """
    out: list[dict] = []
    if not isinstance(timeline, list):
        return out
    for item in timeline:
        if not isinstance(item, dict):
            continue
        entry = dict(item)
        try:
            entry["t"] = round(float(entry.get("t")) + offset, 2)
        except (TypeError, ValueError):
            # t 非法时保留原样：宁可让下游看到原始值，也不静默丢弃这条事件。
            pass
        out.append(entry)
    return out


def merge_timelines(parts: list[list[dict]], *, limit: int = 80) -> list[dict]:
    """合并多块时间轴：按 t 排序并按 t 去重（重叠区的同一条事件只留一条）。"""
    flat: list[dict] = []
    for part in parts or []:
        if isinstance(part, list):
            flat.extend(item for item in part if isinstance(item, dict))
    flat.sort(key=lambda item: _as_time(item.get("t")))
    merged: list[dict] = []
    for item in flat:
        # 去重阈值：重叠区里同一事件被两块各报一次，t 会相差几百毫秒到几秒。
        # 取 2 秒是经验值——真实的两条独立事件间隔通常远大于 2 秒。
        if merged and abs(_as_time(item.get("t")) - _as_time(merged[-1].get("t"))) <= 2.0:
            continue
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def _as_time(value: object) -> float:
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float("inf")
    return num if num == num else float("inf")
