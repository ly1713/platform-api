"""长视频分块与时间偏移（纯函数）。

这些算术错了不会报错，只会让报告里的时间全部错位——所以逐条测。
"""

import pytest

from app.modules import video_chunking as vc


def test_short_video_needs_no_chunking():
    assert vc.needs_chunking(120, threshold_seconds=600) is False
    assert vc.needs_chunking(601, threshold_seconds=600) is True
    assert vc.needs_chunking(0) is False


def test_plan_chunks_covers_whole_video():
    """最后一块必须严格到片尾——否则片尾内容被静默丢掉。"""
    chunks = vc.plan_chunks(1094.6, target_seconds=300, overlap_seconds=15)
    assert chunks[0].start == 0.0
    assert chunks[-1].end == pytest.approx(1094.6)
    # 中间不能有缝
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur.start < prev.end, "相邻块必须重叠，不能留缝"


def test_plan_chunks_empty_for_invalid_duration():
    assert vc.plan_chunks(0) == []
    assert vc.plan_chunks(-5) == []
    assert vc.plan_chunks(None) == []


def test_plan_chunks_respects_max_chunks():
    """几小时的视频不能切出上百块——按上限放大单块时长。"""
    chunks = vc.plan_chunks(4 * 3600, target_seconds=300, max_chunks=12)
    assert len(chunks) <= 12
    assert chunks[-1].end == pytest.approx(4 * 3600)


def test_overlap_larger_than_target_is_clamped():
    """重叠配置错（>= 目标时长）时不能产生空块或死循环。"""
    chunks = vc.plan_chunks(100, target_seconds=10, overlap_seconds=20, max_chunks=20)
    assert chunks
    assert all(c.duration > 0 for c in chunks)


def test_shift_timeline_adds_chunk_offset():
    """模型报的是块内相对秒数，落库前必须加回块起点。"""
    timeline = [{"t": 3, "event": "钩子"}, {"t": 12, "event": "展开"}]
    shifted = vc.shift_timeline(timeline, 300)
    assert [x["t"] for x in shifted] == [303, 312]
    # 不修改原对象
    assert timeline[0]["t"] == 3


def test_shift_timeline_survives_bad_t():
    """t 非法时保留原样，不能静默丢掉这条事件。"""
    shifted = vc.shift_timeline([{"t": "abc", "event": "x"}, {"event": "无 t"}], 100)
    assert len(shifted) == 2
    assert shifted[0]["t"] == "abc"


def test_shift_timeline_ignores_non_list():
    assert vc.shift_timeline(None, 10) == []
    assert vc.shift_timeline({"t": 1}, 10) == []


def test_merge_timelines_sorts_and_dedupes_overlap():
    """重叠区同一事件会被两块各报一次，合并时要按 t 去重。"""
    part_a = [{"t": 10, "event": "a"}, {"t": 290, "event": "接缝"}]
    part_b = [{"t": 291, "event": "接缝（另一块）"}, {"t": 400, "event": "b"}]
    merged = vc.merge_timelines([part_a, part_b])
    assert [x["t"] for x in merged] == [10, 290, 400]


def test_merge_timelines_limits_output():
    parts = [[{"t": i, "event": str(i)}] for i in range(0, 200, 3)]
    merged = vc.merge_timelines(parts, limit=10)
    assert len(merged) == 10
