"""视听信号采集与融合：响度峰值 / 镜头切换 / 运动量。

为什么需要它：整条链路原本是「模型引用转录原话 → 反查时间轴」。这对带货、
讲课、访谈成立，但对跳舞、才艺、户外根本不成立——那些视频的转录可能是空的，
峰值时刻也只有一句「卧槽」。没有可引用的原话就等于一条候选都出不来。

这里补上一条不依赖文字的路：ffmpeg 直接算出客观的视听证据，融合成一条
时间轴热度曲线，选段时用它给候选窗口打分并驱动排序。

分层（成本从低到高）：
- 响度（ebur128）：情绪爆发、笑声、喊叫，纯音频解码，最便宜；
- 镜头切换（scdet）：画面节奏，需要解码视频；
- 运动量（帧差）：动作强度，需要解码视频。

红线：
- 解析函数是纯函数（拿 ffmpeg 的文本输出做输入），便于单测、不依赖 ffmpeg；
- 只有 `collect_signals` 碰 subprocess，且所有超时/失败一律降级为「无该路信号」，
  不让信号采集失败拖垮整条视频理解（信号是增强项，不是前置条件）。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger("platform.video_signals")

# 单条视频的信号采集总时限（秒）。超时降级，不阻塞理解主链路。
COLLECT_TIMEOUT_SECONDS = 600
# 分析用的低分辨率宽度：信号只看「变化」，不需要清晰度。缩到 320 宽能把
# 解码成本压到 1/10 左右，且对帧差/场景切换的判定几乎没有影响。
ANALYSIS_WIDTH = 320
# scdet 的 score 尺度**取决于画面内容**，不是一个固定的 0..100：
# 实测同一条口播视频最高只有 6.95，而切换频繁的片子能到 30+。写死阈值
# （早期写成 8）在一半的素材上恒为空列表。这里改成自适应：取分位数。
# 顶部 0.5% 的帧当切换点，既不会漏掉明显转场，也不会把渐变算成十几次。
SCDET_PERCENTILE = 0.995
# 自适应阈值的地板是**相对值**：取本片最大分数的 40%。
# 绝对地板（早期写死 1.0）会在一批素材上失效——实测慢节奏口播全片最大
# score 只有 0.625，绝对地板 1.0 把每一条都过滤掉，切换数恒为 0。
SCDET_RELATIVE_FLOOR = 0.4
# scdet 滤镜自身的 threshold 参数（未达该分数的帧不输出 score 行）。
# 设成 0 才能看到完整分布，由上面的分位数逻辑决定最终取舍。
SCDET_FILTER_THRESHOLD = 0.0
# 相邻切换点的最小间隔（秒）：一次渐变转场会在多帧上连续高分，
# 不去重会把一次转场算成十几次。
MIN_CUT_GAP_SECONDS = 0.35
# 运动量曲线的聚合桶宽（秒）。单帧尖峰按这个粒度聚合成「这半秒动得多厉害」。
MOTION_BUCKET_SECONDS = 0.5
# 低于该值的响度视为静音（ebur128 对无声段给 -120 dB）。
# 不排掉这些点，整条曲线的跨度会被撑大、峰值阈值失效。
SILENCE_FLOOR = -70.0


@dataclass(frozen=True)
class TimeRange:
    start: float
    end: float
    score: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class MediaSignals:
    """一条视频的客观视听信号。缺哪一路就是空列表，调用方按「无该证据」处理。"""

    duration: float = 0.0
    # 持续高于节目整体中位响度的区间（情绪爆发/笑声/喊叫）。
    loud_peaks: list[TimeRange] = field(default_factory=list)
    # 镜头切换密集的区间（快节奏剪辑）。
    cut_dense: list[TimeRange] = field(default_factory=list)
    # 运动量峰值区间（动作/走位/手势剧烈）。
    motion_peaks: list[TimeRange] = field(default_factory=list)
    # 每秒响度（LUFS 近似值），供画时间轴用。
    loudness_curve: list[float] = field(default_factory=list)
    # 镜头切换时刻（秒）。
    cut_times: list[float] = field(default_factory=list)
    # 每秒运动量分数 0..1。
    motion_curve: list[float] = field(default_factory=list)
    # 采集失败原因（成功为空）。落库供诊断，不阻断。
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "duration": round(self.duration, 2),
            "loud_peaks": [_range_dict(r) for r in self.loud_peaks],
            "cut_dense": [_range_dict(r) for r in self.cut_dense],
            "motion_peaks": [_range_dict(r) for r in self.motion_peaks],
            "cut_times": [round(t, 2) for t in self.cut_times[:400]],
            "loudness_curve": [round(v, 1) for v in self.loudness_curve],
            "motion_curve": [round(v, 3) for v in self.motion_curve],
            "errors": list(self.errors),
        }


def _range_dict(r: TimeRange) -> dict:
    return {"start": round(r.start, 2), "end": round(r.end, 2), "score": round(r.score, 3)}


# --------------------------------------------------------------------------- 解析（纯函数）


def parse_ebur128_series(text: str) -> tuple[list[float], float]:
    """解析瞬时响度序列，返回 ``(逐点 M 值, 相邻点间隔秒数)``。

    实测该滤镜的输出形如（每次 frame 后跟一组 lavfi.r128.*）::

        frame:0    pts:0       pts_time:0
        lavfi.r128.M=-120.691
        frame:1    pts:4410    pts_time:0.1
        lavfi.r128.M=-120.691

    **采样间隔不是 1 秒**：实测 ebur128 每 100 ms 输出一个 M 值（pts 步进 4410
    @44.1kHz）。早期实现把每个点当成 1 秒，38 秒的视频被读成 383 秒，峰值区间
    落到片外——不报错，只是候选片段全部消失。所以这里直接从 ``pts_time`` 反算
    真实步长，而不是假定。

    拿不到 pts_time 时退回 0.1 秒（ebur128 的固定元数据周期）。

    取 ``M``（momentary，瞬时响度）。注意早期写法靠解析 ``t: ... M: ...`` 的
    summary 行是错的——那行只在结尾出现一次，拿不到时间序列。这里做的是
    「按 frame 折叠」：每个 frame 的 M 值收成一个点。

    解析不出来返回 ``([], 0.0)``——信号是增强项，解析失败不该报错。
    """
    values: list[float] = []
    times: list[float] = []
    pending: float | None = None
    pending_t = 0.0
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            # 新的一帧开始：把上一帧攒到的值落下来
            if pending is not None:
                values.append(pending)
                times.append(pending_t)
                pending = None
            pending_t = _pts_time_of(line, pending_t)
            continue
        if "lavfi.r128.M=" in line:
            try:
                pending = float(line.split("=", 1)[1])
            except (IndexError, ValueError):
                pending = None
    if pending is not None:
        values.append(pending)
        times.append(pending_t)

    step = 0.0
    if len(times) >= 2:
        deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
        if deltas:
            deltas.sort()
            step = deltas[len(deltas) // 2]
    return values, round(step, 4) if step > 0 else 0.1


def _pts_time_of(frame_line: str, fallback: float) -> float:
    """从 ``frame:N  pts:..  pts_time:1.2`` 里取 pts_time；取不到沿用上一个值。"""
    marker = "pts_time:"
    index = frame_line.find(marker)
    if index < 0:
        return fallback
    tail = frame_line[index + len(marker):].strip()
    token = tail.split()[0] if tail else ""
    try:
        return float(token)
    except ValueError:
        return fallback


def parse_ebur128(text: str) -> list[float]:
    """只要响度序列时的便捷入口（等价于 ``parse_ebur128_series(text)[0]``）。"""
    return parse_ebur128_series(text)[0]


def parse_scdet(text: str) -> list[float]:
    """从 ``scdet,metadata=print`` 的输出里抽出镜头切换时刻（秒）。

    实测输出形如::

        frame:12   pts:...  pts_time:0.4
        lavfi.scd.score=0.618

    scdet 的 score 是帧间差分的强度（0..100）。只收超过阈值的帧作为切换点。
    注意：scdet 的 score 与「老化程度」无关，连续高分往往是一次渐变转场的
    多帧，所以切换点之间做一个最小间隔去重（见 ``detect_cut_density``）。
    """
    times: list[float] = []
    current_time: float | None = None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            match = re.search(r"pts_time:\s*([0-9.]+)", line)
            current_time = float(match.group(1)) if match else None
            continue
        if "lavfi.scd.score=" in line and current_time is not None:
            try:
                score = float(line.split("=", 1)[1])
            except (IndexError, ValueError):
                continue
            times.append((current_time, score))
    return _dedupe_times(_threshold_cuts(times), min_gap=MIN_CUT_GAP_SECONDS)


def _threshold_cuts(pairs: list[tuple[float, float]]) -> list[float]:
    """按分位数挑出真正的镜头切换时刻。

    为什么不能写死阈值：scdet 的 score 尺度随画面内容变化（实测同一素材
    最高 6.95，切换频繁的能到 30+），写死 8 会让一批视频永远没有切换点。
    """
    if not pairs:
        return []
    scores = sorted(score for _t, score in pairs)
    index = min(len(scores) - 1, int(len(scores) * SCDET_PERCENTILE))
    # 地板取「本片最大分数」的固定比例：scdet 的绝对尺度随画面内容变化很大，
    # 用一个跨素材固定的数字必然在某类素材上全灭。
    floor = scores[-1] * SCDET_RELATIVE_FLOOR
    cutoff = max(floor, scores[index])
    return [t for t, score in pairs if score >= cutoff]


def _dedupe_times(times: list[float], *, min_gap: float) -> list[float]:
    """相邻切换点间隔小于 ``min_gap`` 时只保留第一个。

    一次渐变转场会在连续多帧上都给出高分；不去重的话「镜头切换密度」
    会把一次转场算成十几次切换，快节奏段落被严重高估。
    """
    out: list[float] = []
    for t in times:
        if out and t - out[-1] < min_gap:
            continue
        out.append(t)
    return out


def parse_motion_series(text: str) -> tuple[list[float], float]:
    """从 ``signalstats,metadata=print`` 输出解析运动量曲线，返回 ``(逐点帧差, 步长秒)``。

    输入是 ``lavfi.signalstats.YAVG=<0-255>`` 序列。返回相邻帧亮度均值差的归一化分数（0..1）。

    **必须同时返回步长**：signalstats 是逐帧统计，25fps 素材每点 0.04 秒、60fps
    每点 0.0167 秒。当成「每秒一个点」会把 38 秒视频读成 1149 秒，运动峰落到
    821 秒（实测），融合时按片长切窗口就一个候选都选不出来。
    步长从 ``pts_time`` 中位数反算，取不到时退回 1/30 秒。
    """
    values: list[float] = []
    times: list[float] = []
    current_t = 0.0
    current: float | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("frame:"):
            if current is not None:
                values.append(current)
                times.append(current_t)
                current = None
            current_t = _pts_time_of(stripped, current_t)
            continue
        match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", stripped)
        if match:
            try:
                current = float(match.group(1)) / 255.0
            except ValueError:
                current = None
    if current is not None:
        values.append(current)
        times.append(current_t)

    step = 0.0
    if len(times) >= 2:
        deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
        if deltas:
            deltas.sort()
            step = deltas[len(deltas) // 2]
    if len(values) < 2:
        return [], round(step, 4) if step > 0 else 0.0
    diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    return diffs, round(step, 4) if step > 0 else 1 / 30


def parse_motion(stderr_text: str) -> list[float]:
    """只要运动量曲线时的便捷入口（等价于 ``parse_motion_series(text)[0]``）。"""
    return parse_motion_series(stderr_text)[0]


# --------------------------------------------------------------------------- 区间提取（纯函数）


def find_peaks(
    curve: list[float],
    *,
    min_delta: float = 2.0,
    min_duration: float = 1.0,
    merge_gap: float = 2.0,
    limit: int = 12,
    step: float = 1.0,
) -> list[TimeRange]:
    """在逐秒曲线上找「持续显著高于中位」的区间。

    判据用**绝对增量**而不是倍数或相对跨度：
    - 响度是对数尺度（dB），「高 2 倍」没有物理意义，但「高 3 dB」有——那约等于
      能量翻倍；
    - 用相对跨度（max-min）算门槛会被个别异常帧拖垮：实测一条口播的响度
      中位 -12.9、最大 -9.8（只差 3 dB），但跨度因几帧突刺达到 51 dB，
      门槛被推到 +6 dB，结果一个峰值都选不出来。

    Args:
        curve: 一条等间隔采样曲线。
        min_delta: 相对中位的绝对增量门槛（响度单位是 dB，默认 2 dB）。
        min_duration: 区间最短时长（秒），过滤单帧噪点。
        merge_gap: 相邻区间间隔小于它时合并（秒）。
        limit: 最多返回多少个，按得分降序。
        step: **每个采样点代表多少秒**。

            这个参数不能省：ebur128 实测每 0.1 秒输出一个 M 值、signalstats
            逐帧输出（30fps 就是每点 0.033 秒）。早期实现把它们都当成「每秒一个点」，
            于是一条 38 秒的视频算出 383 个采样点、峰值区间落到 250~337 秒——
            时间轴整体被拉长 10 倍，远超片长。这种错不会报错，只会让下游
            「候选片段」全部落在片外（融合时按片长切窗口，结果一个都选不出来）。
    """
    if not curve:
        return []
    clean = [v for v in curve if v == v and v > SILENCE_FLOOR]
    if not clean:
        return []
    median = sorted(clean)[len(clean) // 2]
    threshold = median + abs(float(min_delta))
    if max(clean) < threshold:
        return []

    step = max(1e-6, float(step))
    ranges: list[TimeRange] = []
    start: int | None = None
    for index, value in enumerate(curve):
        hot = value == value and value > SILENCE_FLOOR and value >= threshold
        if hot and start is None:
            start = index
        elif not hot and start is not None:
            ranges.append(
                TimeRange(
                    start * step,
                    index * step,
                    _peak_score(curve, start, index, median),
                )
            )
            start = None
    if start is not None:
        ranges.append(
            TimeRange(
                start * step,
                len(curve) * step,
                _peak_score(curve, start, len(curve), median),
            )
        )

    merged: list[TimeRange] = []
    for item in ranges:
        if merged and item.start - merged[-1].end <= merge_gap:
            prev = merged[-1]
            merged[-1] = TimeRange(prev.start, item.end, max(prev.score, item.score))
        else:
            merged.append(item)
    merged = [r for r in merged if r.duration >= min_duration]
    merged.sort(key=lambda r: r.score, reverse=True)
    return merged[:limit]


def _peak_score(curve: list[float], start: int, end: int, baseline: float) -> float:
    """区间内的峰值相对基线的增量。start/end 是**采样点索引**，不是秒。"""
    window = curve[int(start) : int(end)]
    if not window:
        return 0.0
    peak = max(window)
    return round(float(peak - baseline), 3)


def resample_curve(curve: list[float], *, step: float, target_step: float) -> tuple[list[float], float]:
    """把高采样率曲线按时间桶聚合成低采样率，返回 ``(新曲线, 新步长)``。

    为什么要这一步：signalstats 是逐帧输出（30fps→每点 0.033 秒），而运动量
    本身是**单帧尖峰**——实测一条口播 599 个点里中位数只有 0.0002，峰值 0.157，
    两者差 800 倍。直接在这个曲线上按「持续 0.5 秒」找峰，一个都找不到，
    因为真正的高值只有一两帧。按 0.5 秒桶取**最大值**聚合后，语义才对：
    「这半秒内画面动得最厉害到什么程度」。

    取最大值而不是均值：均值会把一闪而过的动作（恰恰是要找的高光）摊平。

    Args:
        curve: 原始曲线。
        step: 原始每个点代表的秒数。
        target_step: 目标每桶秒数。

    Returns:
        ``(聚合后的曲线, 实际桶宽)``；输入为空或参数非法时原样返回。
    """
    if not curve or step <= 0 or target_step <= 0:
        return list(curve or []), step
    if step >= target_step:
        return list(curve), step
    size = max(1, int(round(target_step / step)))
    out: list[float] = []
    for i in range(0, len(curve), size):
        bucket = curve[i : i + size]
        out.append(max(bucket))
    return out, step * size


def detect_cut_density(
    cut_times: list[float],
    *,
    window: float = 10.0,
    limit: int = 12,
) -> list[TimeRange]:
    """把镜头切换时刻聚成「切换密集」的区间。

    快节奏剪辑（单位时间内切换多）通常对应高信息密度段落，这是纯像素级证据。

    阈值是**相对整条视频**的：绝对次数（早期写死「10 秒 4 次」）在慢节奏
    素材上恒为空——实测一条 45 秒口播全片只有 6 次切换，永远够不到 4 次/10 秒。
    这里改成「达到本片平均密度的 2 倍以上」才算密集段，跨素材可比。
    """
    if not cut_times:
        return []
    ordered = sorted(cut_times)
    span = max(1.0, ordered[-1])
    avg_per_window = len(ordered) / span * window
    min_cuts = max(1.0, avg_per_window * 2.0)
    duration = ordered[-1] + window
    ranges: list[TimeRange] = []
    step = window / 2
    t = 0.0
    while t < duration:
        count = sum(1 for c in ordered if t <= c < t + window)
        if count >= min_cuts:
            ranges.append(TimeRange(t, t + window, float(count)))
        t += step
    # 合并重叠窗口
    merged: list[TimeRange] = []
    for item in sorted(ranges, key=lambda r: r.start):
        if merged and item.start <= merged[-1].end:
            prev = merged[-1]
            merged[-1] = TimeRange(prev.start, max(prev.end, item.end), max(prev.score, item.score))
        else:
            merged.append(item)
    merged.sort(key=lambda r: r.score, reverse=True)
    return merged[:limit]


# --------------------------------------------------------------------------- 融合（纯函数）

# 三套证据权重：不同品类的爆点证据几乎是反的。
# - words：话里有内容（带货/知识/访谈），信号只是辅助；
# - reaction：靠现场反应（游戏/户外/聊天），响度与爆发是主证据；
# - visual：靠画面（跳舞/才艺/萌宠），镜头切换与运动是主证据，语气基本没用。
DEFAULT_MOMENT_WEIGHTS: dict[str, dict[str, float]] = {
    "words": {"loud": 0.6, "cut": 0.6, "motion": 0.4},
    "reaction": {"loud": 2.0, "cut": 1.0, "motion": 1.5},
    "visual": {"loud": 1.2, "cut": 2.2, "motion": 2.8},
}


def fuse_moments(
    signals: MediaSignals,
    *,
    evidence_class: str = "words",
    window: float = 30.0,
    limit: int = 8,
    weights: dict[str, float] | None = None,
) -> list[dict]:
    """把多路信号融合成候选片段，按热度降序返回。

    Returns:
        ``[{"start","end","score","drivers":{"loud":...,"cut":...,"motion":...}}]``
        ``drivers`` 说明这个窗口是靠哪路证据被选中的，便于人工核对与前端展示。
    """
    table = weights or DEFAULT_MOMENT_WEIGHTS.get(evidence_class) or DEFAULT_MOMENT_WEIGHTS["words"]
    if signals.duration <= 0 or window <= 0:
        return []

    buckets: list[dict[str, float]] = []
    steps = max(1, int(signals.duration // window) + 1)
    for i in range(steps):
        start = i * window
        end = min(signals.duration, start + window)
        if end <= start:
            break
        buckets.append(
            {
                "start": start,
                "end": end,
                "loud": _overlap_score(signals.loud_peaks, start, end),
                "cut": _overlap_score(signals.cut_dense, start, end),
                "motion": _overlap_score(signals.motion_peaks, start, end),
            }
        )

    # 每路单独归一到 0..1，避免量纲差把某一路压死（响度差是 dB，切换数是次数）。
    for key in ("loud", "cut", "motion"):
        peak = max((b[key] for b in buckets), default=0.0)
        if peak > 0:
            for b in buckets:
                b[key] = b[key] / peak

    scored = []
    for b in buckets:
        score = sum(b.get(k, 0.0) * float(table.get(k, 0.0)) for k in ("loud", "cut", "motion"))
        drivers = {k: round(b.get(k, 0.0), 3) for k in ("loud", "cut", "motion") if b.get(k, 0.0) >= 0.5}
        scored.append(
            {
                "start": round(b["start"], 2),
                "end": round(b["end"], 2),
                "score": round(score, 3),
                "drivers": drivers,
            }
        )
    scored.sort(key=lambda x: x["score"], reverse=True)
    return [item for item in scored[:limit] if item["score"] > 0]


def _overlap_score(ranges: list[TimeRange], start: float, end: float) -> float:
    """区间内各信号得分的加权和（按重叠时长折算，越长的重叠贡献越大）。"""
    total = 0.0
    for r in ranges:
        overlap = min(end, r.end) - max(start, r.start)
        if overlap > 0:
            total += r.score * (overlap / max(1e-6, r.duration))
    return total


# --------------------------------------------------------------------------- 采集（唯一碰 subprocess 的地方）


def collect_signals(video_path: str, *, duration: float | None = None) -> MediaSignals:
    """采集一条视频的视听信号。

    任何一路失败都只记录 ``errors`` 并继续——信号是增强项，采集失败不能
    阻塞视频理解。整条链路有总时限，超时返回已采到的部分。
    """
    signals = MediaSignals(duration=float(duration or 0))
    if not shutil.which("ffmpeg"):
        signals.errors.append("ffmpeg 不可用，跳过信号采集")
        return signals

    try:
        if signals.duration <= 0:
            signals.duration = _probe_duration(video_path)
        loudness = _run_ffmpeg(
            [
                "-i", video_path, "-map", "0:a:0",
                "-af", "ebur128=metadata=1,ametadata=print:file=-",
                "-f", "null", "-",
            ]
        )
        curve, step = parse_ebur128_series(loudness)
        if curve:
            signals.loudness_curve = curve
            # 步长是**实测**的（ebur128 每 100ms 一个点），不是「1 点 = 1 秒」。
            # 传错步长会让峰值区间整体落在片外，候选片段一个都出不来。
            signals.loud_peaks = find_peaks(
                curve, step=step or 1.0, min_duration=1.0, merge_gap=2.0
            )
    except Exception as exc:  # noqa: BLE001 信号是增强项
        signals.errors.append(f"响度采集失败: {exc}"[:200])

    try:
        scale = f"scale={ANALYSIS_WIDTH}:-2"
        cuts_out = _run_ffmpeg(
            [
                "-i", video_path,
                "-vf", f"{scale},scdet=threshold={SCDET_FILTER_THRESHOLD},metadata=print:file=-",
                "-an", "-f", "null", "-",
            ]
        )
        cuts = parse_scdet(cuts_out)
        if cuts:
            signals.cut_times = cuts
            signals.cut_dense = detect_cut_density(cuts)
    except Exception as exc:  # noqa: BLE001
        signals.errors.append(f"镜头切换采集失败: {exc}"[:200])

    try:
        scale = f"scale={ANALYSIS_WIDTH}:-2"
        motion_out = _run_ffmpeg(
            [
                "-i", video_path,
                "-vf", f"{scale},signalstats,metadata=print:file=-",
                "-an", "-f", "null", "-",
            ]
        )
        motion, motion_step = parse_motion_series(motion_out)
        if motion:
            # 运动量的绝对尺度取决于素材（静止机位 max 可能只有 0.007，
            # 手持运镜能到 0.3）。门槛按本片最大值的比例给，跨素材才可比。
            peak = max(motion)
            delta = max(0.005, peak * 0.5)
            # 步长是帧间隔（25fps→0.04s），不是 1 秒。运动量是单帧尖峰，
            # 先按 0.5 秒桶取最大值再找峰——否则「持续 0.5 秒」的条件
            # 会把所有一闪而过的动作全部滤掉（实测 599 点里一个峰都没有）。
            bucketed, bucketed_step = resample_curve(
                motion, step=motion_step or (1 / 30), target_step=MOTION_BUCKET_SECONDS
            )
            signals.motion_curve = bucketed
            peak = max(bucketed) if bucketed else 0.0
            delta = max(0.005, peak * 0.5)
            # min_duration 必须小于桶宽：单个桶就是「这半秒动得多厉害」的完整
            # 语义，要求持续两个桶等于要求动作持续 1 秒，短促高光会被全部滤掉
            # （实测 30 秒素材两个最强峰都因此消失）。
            signals.motion_peaks = find_peaks(
                bucketed, min_delta=delta, min_duration=bucketed_step * 0.9,
                step=bucketed_step, merge_gap=MOTION_BUCKET_SECONDS * 2,
            )
    except Exception as exc:  # noqa: BLE001
        signals.errors.append(f"运动量采集失败: {exc}"[:200])

    return signals


def _probe_duration(video_path: str) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", video_path],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return 0.0
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def _run_ffmpeg(args: list[str]) -> str:
    """跑一次 ffmpeg 并返回它的 stdout。

    ``metadata=print:file=-`` 把逐帧统计写到 stdout，所以这里取 stdout；
    stderr 只留错误信息，不参与解析（早期实现误从 stderr 解析，结果是空的）。
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        timeout=COLLECT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise RuntimeError((proc.stderr or "ffmpeg 失败").strip().splitlines()[-1][:200])
    return proc.stdout or ""
