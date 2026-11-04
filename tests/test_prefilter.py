"""两级漏斗：切块、解析、扩窗、fail-open。

最重要的一条是 fail-open：漏斗是省钱手段，它挂了必须回退全文，
绝不能因为「初筛不可用」让一条本来能跑的任务失败。
"""

import pytest

from app.modules import prefilter as pf


def _segments(count: int, chars: int = 60) -> list[dict]:
    return [{"start": i, "end": i + 1, "text": "内容" * chars} for i in range(count)]


def test_strip_think_blocks():
    assert pf.strip_think_blocks("响应") == "响应"
    assert pf.strip_think_blocks("输出{\"a\":1}") == '输出{"a":1}'


def test_strip_think_blocks_removes_paired_block():
    text = "输出思考过程</think>{\"windows\":[]}"
    assert pf.strip_think_blocks(text) == '{"windows":[]}'


def test_strip_think_blocks_removes_truncated_tail():
    """上游截断时只剩收尾标签：它之前的内容都是思考残留。"""
    assert pf.strip_think_blocks("一堆思考内容</think>{\"windows\":[]}") == '{"windows":[]}' 


def test_chunk_segments_keeps_whole_sentences():
    segs = [{"text": "a" * 100}, {"text": "b" * 100}, {"text": "c" * 100}]
    chunks = pf.chunk_segments(segs, target_chars=150)
    assert [len(c) for c in chunks] == [1, 1, 1]
    # 不拆句：每块的文本都是完整的原始片段
    assert all(item["text"] in ("a" * 100, "b" * 100, "c" * 100) for c in chunks for item in c)


def test_parse_windows_rejects_unknown_ids():
    windows = pf.parse_windows(
        '{"windows":[{"start":0,"end":3},{"start":90,"end":95}]}', valid_ids={0, 1, 2, 3}
    )
    assert windows == [pf.Window(0, 3)]


def test_parse_windows_raises_on_garbage():
    with pytest.raises(ValueError):
        pf.parse_windows("not json", valid_ids={0})
    with pytest.raises(ValueError):
        pf.parse_windows('{"other": 1}', valid_ids={0})


def test_expand_windows_pads_and_merges():
    windows = [pf.Window(5, 6), pf.Window(8, 9)]
    merged = pf.expand_windows(windows, total=20, pad=2)
    # 5-6 扩到 3-8，8-9 扩到 6-11 → 重叠合并为 3-11
    assert merged == [pf.Window(3, 11)]


def test_select_segments_short_input_skips_funnel():
    segs = _segments(2, chars=10)
    kept, stats = pf.select_segments(segs, llm=lambda s, u: "{}")
    assert kept == segs
    assert stats.enabled is False
    assert "低于" in stats.fallback_reason


def test_select_segments_fails_open_on_bad_json():
    """初筛输出不可解析 → 回退全文，任务照常跑。"""
    segs = _segments(40)  # 40*120=4800 字符，超过 3000 门槛
    kept, stats = pf.select_segments(segs, llm=lambda s, u: "这不是 JSON")
    assert kept == segs
    assert "回退全文" in stats.fallback_reason


def test_select_segments_fails_open_on_exception():
    """初筛端点超时/不可达 → 回退全文（漏斗只省钱，不背锅）。"""
    segs = _segments(40)

    def boom(system, user):
        raise TimeoutError("本地端点超时")

    kept, stats = pf.select_segments(segs, llm=boom)
    assert kept == segs
    assert "超时" in stats.fallback_reason


def test_select_segments_without_llm_returns_all():
    segs = _segments(20)
    kept, stats = pf.select_segments(segs, llm=None)
    assert kept == segs
    assert stats.enabled is False


def test_select_segments_keeps_only_selected_windows():
    segs = _segments(40)
    kept, stats = pf.select_segments(
        segs, llm=lambda s, u: '{"windows":[{"start":10,"end":12}]}'
    )
    assert stats.enabled is True
    assert stats.windows == 1
    assert stats.kept_segments < len(segs)
    # 保留的片段里必须包含被圈中的那几句
    texts = [item.get("text") for item in kept]
    assert segs[11]["text"] in texts


def test_select_segments_adds_elision_marker():
    """跳过的部分要有省略标记，否则模型会以为上下文是连续的。"""
    segs = _segments(40)
    kept, _ = pf.select_segments(segs, llm=lambda s, u: '{"windows":[{"start":30,"end":31}]}')
    assert any("略" in str(item.get("text")) for item in kept)
