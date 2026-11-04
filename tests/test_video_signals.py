"""视听信号：解析（纯函数）+ 融合（纯函数）+ 区间提取。

这些测试全部用**真实 ffmpeg 输出的片段**做输入，不是编造的假数据——
早期版本就是因为按想象写了解析器（以为响度打在 stderr 的 `t:` 行、
以为 scdet 有个固定 0-100 尺度），在真实素材上恒为空列表却单测全绿。
"""

import pytest

from app.modules import video_signals as vs


EBUR128_REAL = """frame:0    pts:0       pts_time:0
lavfi.r128.M=-120.691
lavfi.r128.S=-120.691
lavfi.r128.I=-70.000
frame:1    pts:4410    pts_time:0.1
lavfi.r128.M=-11.234
lavfi.r128.S=-11.234
lavfi.r128.I=-70.000
frame:2    pts:8820    pts_time:0.2
lavfi.r128.M=-9.876
lavfi.r128.S=-9.876
lavfi.r128.I=-70.000
"""

SCDET_REAL = """frame:0    pts:0       pts_time:0
lavfi.scd.mafd=0.000
lavfi.scd.score=0.000
frame:1    pts:512     pts_time:0.0333333
lavfi.scd.mafd=0.618
lavfi.scd.score=0.618
frame:60   pts:30720   pts_time:2.0
lavfi.scd.mafd=0.100
lavfi.scd.score=0.050
"""

SIGNALSTATS_REAL = """frame:0    pts:0       pts_time:0
lavfi.signalstats.YAVG=100.000
frame:1    pts:512     pts_time:0.0333333
lavfi.signalstats.YAVG=120.000
frame:2    pts:1024    pts_time:0.0666667
lavfi.signalstats.YAVG=110.000
"""


def test_parse_ebur128_reads_per_frame_momentary_not_summary():
    """响度必须按 frame 折叠成时间序列。

    早期实现解析 stderr 的 `t: ... M: ...` summary 行——那行整段只在结尾出现
    一次，拿不到曲线，响度峰值恒为空。
    """
    curve = vs.parse_ebur128(EBUR128_REAL)
    assert curve == [-120.691, -11.234, -9.876]


def test_parse_ebur128_empty_input_is_safe():
    assert vs.parse_ebur128("") == []
    assert vs.parse_ebur128("no frame data here") == []


def test_parse_scdet_threshold_is_percentile_not_fixed():
    """scdet 阈值必须自适应：同一素材最大 score 可能只有 0.6，也可能是 30。

    写死绝对值（早期写成 8）会让一批素材切换数恒为 0。
    """
    times = vs.parse_scdet(SCDET_REAL)
    # 最大值 0.618 在相对地板（0.618*0.4=0.247）之上，应被选中
    assert times == [pytest.approx(0.0333333, abs=1e-5)]


def test_parse_scdet_relative_floor_keeps_high_scores_only():
    """低于「本片最大分 × 40%」的帧不算切换。"""
    text = "\n".join(
        [
            "frame:0    pts:0       pts_time:0",
            "lavfi.scd.score=10.0",
            "frame:1    pts:512     pts_time:0.5",
            "lavfi.scd.score=1.0",  # 远低于 10*0.4=4
        ]
    )
    times = vs.parse_scdet(text)
    assert times == [0.0]


def test_parse_scdet_dedupes_gradient_transition():
    """一次渐变转场会在连续多帧高分，去重后只剩一个切换点。"""
    lines = []
    # 间隔 0.02s，小于 MIN_CUT_GAP_SECONDS(0.35)，应全部合并成一个切换点
    for i in range(5):
        lines.append(f"frame:{i}    pts:{i}    pts_time:{i * 0.02:.3f}")
        lines.append("lavfi.scd.score=9.0")
    times = vs.parse_scdet("\n".join(lines))
    assert len(times) == 1
    # 间隔够大的两次切换应各自保留
    far = "\n".join(
        [
            "frame:0    pts:0       pts_time:0.000",
            "lavfi.scd.score=9.0",
            "frame:1    pts:512     pts_time:2.000",
            "lavfi.scd.score=9.0",
        ]
    )
    assert len(vs.parse_scdet(far)) == 2


def test_parse_motion_turns_yavg_into_frame_diff():
    curve = vs.parse_motion(SIGNALSTATS_REAL)
    assert len(curve) == 2
    assert curve[0] == pytest.approx(20 / 255, abs=1e-6)
    assert curve[1] == pytest.approx(10 / 255, abs=1e-6)


def test_find_peaks_uses_absolute_delta_so_silence_does_not_flatten_curve():
    """静音帧（-120 dB）不能把峰值门槛推到够不着。

    实测一条口播：中位 -12.9、最大 -9.8（只差 3 dB），但几帧突刺让跨度
    到 51 dB。按跨度算门槛会得出 +6 dB，一个峰值都选不出来。
    """
    curve = [-120.0, -120.0, -13.0, -12.9, -12.8, -9.8, -12.9, -120.0]
    peaks = vs.find_peaks(curve, min_delta=2.0, min_duration=1.0, merge_gap=0.5)
    assert peaks, "有声段的中位之上出现 3dB 抬升，必须被识别为峰值"
    assert any(p.start <= 5 <= p.end for p in peaks)


def test_find_peaks_returns_empty_for_flat_curve():
    assert vs.find_peaks([-20.0] * 30) == []
    assert vs.find_peaks([]) == []


def test_detect_cut_density_is_relative_to_whole_video():
    """密集段阈值必须相对本片平均密度，不能写死「每 10 秒 4 次」。

    实测一条 45 秒口播全片只有 6 次切换，写死阈值永远够不到。
    """
    # 均匀分布：没有段落显著高于平均 → 无密集段
    even = [i * 5.0 for i in range(10)]
    assert vs.detect_cut_density(even) == []
    # 聚集：后 10 秒集中 8 次 → 该段密集
    burst = [0.0] + [30.0 + i * 0.5 for i in range(8)]
    dense = vs.detect_cut_density(burst)
    assert dense and any(r.start <= 32 <= r.end for r in dense)


def _signals(**kwargs) -> vs.MediaSignals:
    base = dict(
        duration=120.0,
        loud_peaks=[vs.TimeRange(0, 10, 5.0)],
        cut_dense=[vs.TimeRange(0, 10, 3.0)],
        motion_peaks=[vs.TimeRange(0, 10, 0.2)],
    )
    base.update(kwargs)
    return vs.MediaSignals(**base)


def test_fuse_moments_weights_differ_by_evidence_class():
    """三套权重必须产生不同的排序——否则「品类路由」等于没做。"""
    signals = _signals(
        loud_peaks=[vs.TimeRange(0, 30, 2.0)],      # 只有响度
        cut_dense=[vs.TimeRange(60, 90, 10.0)],     # 只有镜头切换
        motion_peaks=[],
    )
    words = vs.fuse_moments(signals, evidence_class="words", window=30, limit=2)
    visual = vs.fuse_moments(signals, evidence_class="visual", window=30, limit=2)
    # visual 更信镜头切换（2.2）而非响度（1.2），所以 60-90 段应排第一
    assert visual[0]["start"] == 60.0
    # 两种品类的首选项不应完全相同
    assert words[0]["start"] != visual[0]["start"] or words[0]["score"] != visual[0]["score"]


def test_fuse_moments_reports_drivers():
    signals = _signals()
    moments = vs.fuse_moments(signals, evidence_class="words", window=30, limit=1)
    assert moments
    assert "drivers" in moments[0]
    assert moments[0]["drivers"], "必须说明这个窗口是靠哪路证据被选中的"


def test_fuse_moments_empty_signals_returns_empty():
    assert vs.fuse_moments(vs.MediaSignals(), window=30) == []
    assert vs.fuse_moments(_signals(duration=0), window=30) == []


def test_signals_as_dict_is_json_safe():
    import json

    signals = _signals(duration=45.5, errors=["响度采集失败: x"])
    data = signals.as_dict()
    json.dumps(data)  # 不抛异常即通过
    assert data["errors"] == ["响度采集失败: x"]
    assert data["loud_peaks"][0]["start"] == 0.0
