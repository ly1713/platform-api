"""契约层归一化：内部链路与外部回传共用同一套校验。

为什么单独成文件：拆解外移后会有两条写入路径（内部 worker 跑模型 / 外部 agent
回传报告）。这组测试守的是**两条路径不会各有一套判定**：同一份非法输入，两边
必须给出同样的拒绝理由。任何一条变红，都意味着「报告质量」开始依赖它是谁产出的。
"""

import pytest

from app.core import breakdown_contract as contract
from app.core.hook_criteria import DEFAULT_HOOK_CRITERIA

_CRITERIA = [
    {"id": "hook", "name": "钩子时刻", "ask": "开头制造好奇"},
    {"id": "emotion_peak", "name": "情绪峰值", "ask": "真实情绪"},
]


# ============================================================
# normalize_score：评分收敛
# ============================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (88, 88.0),
        ("75", 75.0),
        (150, 100.0),  # 上溢收敛
        (-20, 0.0),  # 下溢收敛
        (0, 0.0),
        (100, 100.0),
    ],
)
def test_score_clamped_to_range(raw, expected):
    assert contract.normalize_score(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "abc", [], {}, object()])
def test_score_illegal_becomes_zero(raw):
    assert contract.normalize_score(raw) == 0.0


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf")])
def test_score_non_finite_becomes_zero(raw):
    """NaN/inf 会污染 JSON 序列化（对标报告 500），必须在这里挡掉。"""
    assert contract.normalize_score(raw) == 0.0


def test_score_boolean_is_not_coerced_to_one():
    """``float(True) == 1.0`` —— 不单独判掉的话 ``true`` 会静默变成 1 分。"""
    assert contract.normalize_score(True) == 0.0
    assert contract.normalize_score(False) == 0.0


# ============================================================
# normalize_hook_matches：判据命中
# ============================================================


def _matches(*items, criteria=_CRITERIA, rejected=None):
    return contract.normalize_hook_matches({"hook_matches": list(items)}, criteria, rejected=rejected)


def test_keeps_verifiable_match():
    out = _matches({"criterion": "hook", "segment": "0-3s", "evidence": "你有没有想过"})
    assert out == [{"criterion": "hook", "segment": "0-3s", "evidence": "你有没有想过"}]


def test_drops_match_without_evidence():
    """只有 id 没有依据 = 「模型说它命中了」，无法核对，不入报告。"""
    out = _matches({"criterion": "hook"}, {"criterion": "emotion_peak", "evidence": "笑了"})
    assert out == [{"criterion": "emotion_peak", "evidence": "笑了"}]


def test_drops_match_with_unknown_criterion_id():
    """收紧：给了判据表，就必须用表里真实存在的 id。

    外部执行方可能拿着过期判据表，编出来的 id 会静默写进报告，
    看起来像命中了某个标准，实际谁也核对不了。
    """
    rejected: list[str] = []
    out = _matches(
        {"criterion": "made_up_id", "evidence": "看起来很燃"},
        {"criterion": "hook", "evidence": "开场提问"},
        rejected=rejected,
    )
    assert [m["criterion"] for m in out] == ["hook"]
    assert any("made_up_id" in reason and "不在当前判据表内" in reason for reason in rejected)


def test_unknown_criterion_rejection_lists_available_ids():
    """拒绝理由要写明可用 id，外部只能靠这个自纠。"""
    rejected: list[str] = []
    _matches({"criterion": "nope", "evidence": "x"}, rejected=rejected)
    assert len(rejected) == 1
    assert "hook" in rejected[0] and "emotion_peak" in rejected[0]


def test_no_criteria_means_no_id_check():
    """未给判据表时保持历史行为（只校验非空），便于单独调用与回放旧数据。"""
    out = contract.normalize_hook_matches(
        {"hook_matches": [{"criterion": "anything", "evidence": "x"}]}, None
    )
    assert out == [{"criterion": "anything", "evidence": "x"}]


def test_evidence_truncated_to_300_chars():
    out = _matches({"criterion": "hook", "evidence": "啊" * 500})
    assert len(out[0]["evidence"]) == 300


def test_segment_truncated_to_40_chars():
    out = _matches({"criterion": "hook", "evidence": "x", "segment": "0" * 100})
    assert len(out[0]["segment"]) == 40


def test_segment_absent_when_blank():
    """空 segment 不能留一个空串字段，报告页会渲染成空白列。"""
    out = _matches({"criterion": "hook", "evidence": "x", "segment": "   "})
    assert "segment" not in out[0]


def test_matches_capped_at_limit():
    items = [{"criterion": "hook", "evidence": f"e{i}"} for i in range(50)]
    assert len(_matches(*items)) == contract.MAX_HOOK_MATCHES == 12


@pytest.mark.parametrize("bad", ["字符串", 123, None, ["嵌套"]])
def test_non_object_match_dropped_and_reported(bad):
    rejected: list[str] = []
    assert _matches(bad, rejected=rejected) == []
    assert len(rejected) == 1 and "不是对象" in rejected[0]


def test_hook_matches_absent_or_wrong_type_returns_empty():
    assert contract.normalize_hook_matches({}, _CRITERIA) == []
    assert contract.normalize_hook_matches({"hook_matches": "不是数组"}, _CRITERIA) == []


def test_rejected_is_optional():
    """内部链路不需要错误明细时可以不传，不能因此抛错。"""
    assert _matches({"criterion": "hook", "evidence": "x"}) == [
        {"criterion": "hook", "evidence": "x"}
    ]


# ============================================================
# normalize_sections：定性板块
# ============================================================


def test_sections_keep_none_for_missing_fields():
    """缺项保留 None（不是空数组）：报告页要能区分「未提供」和「提供了空结果」。"""
    sections = contract.normalize_sections({})
    assert sections["hook"] is None
    assert sections["structure"] is None
    assert len(sections) == 8  # 8 个定性板块，不含 hook_matches


def test_sections_do_not_coerce_types():
    """类型错误原样保留，不静默改成空值——掩盖格式问题比报错更糟。"""
    sections = contract.normalize_sections({"structure": "应该是数组但给了字符串"})
    assert sections["structure"] == "应该是数组但给了字符串"


# ============================================================
# normalize_report：完整归一化
# ============================================================


def test_normalize_report_full_roundtrip():
    raw = {
        "verdict": "钩子够狠",
        "score": "88",
        "hook": {"hook_type": "反常识"},
        "comment_insights": {"top_themes": ["求链接"]},
        "hook_matches": [{"criterion": "hook", "evidence": "开场就反问"}],
    }
    out = contract.normalize_report(raw, _CRITERIA)
    assert out["score"] == 88.0
    assert out["verdict"] == "钩子够狠"
    assert out["comment_insights"] == {"top_themes": ["求链接"]}
    assert out["sections"]["hook"] == {"hook_type": "反常识"}
    assert out["sections"]["hook_matches"] == [{"criterion": "hook", "evidence": "开场就反问"}]


def test_normalize_report_verdict_non_string_becomes_empty():
    out = contract.normalize_report({"verdict": {"nested": 1}, "score": 80}, _CRITERIA)
    assert out["verdict"] == ""


def test_normalize_report_comment_insights_non_object_becomes_empty():
    out = contract.normalize_report({"comment_insights": ["不是对象"], "score": 80}, _CRITERIA)
    assert out["comment_insights"] == {}


@pytest.mark.parametrize("bad", [None, "字符串", [1, 2], 42])
def test_normalize_report_rejects_non_object(bad):
    """非对象是格式问题，调用方据此返回 422 / 落 error，不能静默产出空报告。"""
    with pytest.raises(ValueError, match="不是 JSON 对象"):
        contract.normalize_report(bad, _CRITERIA)


def test_normalize_report_uses_default_criteria_when_configured():
    """真实链路传的是设置里的判据表，默认 8 条；用它们校验才能对上模型看到的提示词。"""
    raw = {"score": 80, "hook_matches": [{"criterion": "hook", "evidence": "x"}]}
    out = contract.normalize_report(raw, DEFAULT_HOOK_CRITERIA)
    assert len(DEFAULT_HOOK_CRITERIA) == 8
    assert out["sections"]["hook_matches"] == [{"criterion": "hook", "evidence": "x"}]
