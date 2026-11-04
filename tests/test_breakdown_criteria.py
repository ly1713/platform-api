"""拆解新增能力：爆点判据注入 / 证据类别 / 候选片段 / hook_matches 规整。"""

import pytest

from app.core.breakdown_settings import resolve_breakdown_settings_from_rows
from app.core.hook_criteria import DEFAULT_HOOK_CRITERIA
from app.modules import breakdown


def test_criteria_block_injected_into_system_prompt():
    system, _ = breakdown.build_breakdown_prompt(
        {"title": "t", "hook_criteria": DEFAULT_HOOK_CRITERIA}
    )
    assert "【爆点判据】" in system
    # 按影响力排序，第一条是钩子
    assert "1. 钩子时刻" in system
    assert "hook_matches" in system


def test_default_criteria_used_when_not_configured():
    system, _ = breakdown.build_breakdown_prompt({"title": "t"})
    assert "1. 钩子时刻" in system
    assert len(DEFAULT_HOOK_CRITERIA) == 8


def test_evidence_class_guidance_injected():
    visual, _ = breakdown.build_breakdown_prompt({"title": "t", "evidence_class": "visual"})
    assert "靠画面" in visual
    words, _ = breakdown.build_breakdown_prompt({"title": "t", "evidence_class": "words"})
    assert "靠话" in words
    assert "靠画面" not in words


def test_moments_listed_as_objective_measurement():
    _, user = breakdown.build_breakdown_prompt(
        {
            "title": "t",
            "moments": [
                {"start": 12.4, "end": 40.0, "score": 1.5, "drivers": {"motion": 0.8, "cut": 0.4}},
                {"start": 100.0, "end": 130.0, "score": 0.9, "drivers": {}},
            ],
        }
    )
    assert "【系统测得的候选片段】" in user
    assert "12-40s" in user
    assert "motion/cut" in user
    # 空 drivers 不能渲染成 "（，强度"这种破碎文本
    assert "综合" in user
    assert "客观测量" in user


def test_moments_absent_renders_placeholder():
    _, user = breakdown.build_breakdown_prompt({"title": "t"})
    assert "【系统测得的候选片段】\n（无）" in user


def test_hook_matches_normalized_and_rejects_unsupported():
    out = breakdown_service_hook_matches(
        {
            "hook_matches": [
                {"criterion": "hook", "segment": "0-3s", "evidence": "你有没有想过…"},
                {"criterion": "emotion_peak"},  # 没有证据 → 不可核对，丢弃
                {"evidence": "没有判据 id"},  # 丢弃
                {"criterion": 123, "evidence": "x"},  # 类型错，丢弃
                "字符串",  # 丢弃
            ]
        }
    )
    assert out == [{"criterion": "hook", "segment": "0-3s", "evidence": "你有没有想过…"}]


def breakdown_service_hook_matches(parsed):
    from app.services.breakdown_service import _hook_matches

    return _hook_matches(parsed)


def test_hook_matches_capped():
    raw = [{"criterion": f"c{i}", "evidence": "e"} for i in range(50)]
    out = breakdown_service_hook_matches({"hook_matches": raw})
    assert len(out) == 12


def test_hook_criteria_setting_parsing():
    # 未配置 → 内置默认
    assert len(resolve_breakdown_settings_from_rows({})["hook_criteria"]) == 8
    # 坏 JSON → 回退默认（不能静默变成空表）
    assert len(resolve_breakdown_settings_from_rows(
        {"breakdown_hook_criteria": "{not json"}
    )["hook_criteria"]) == 8
    # 合法自定义 → 生效
    custom = resolve_breakdown_settings_from_rows(
        {"breakdown_hook_criteria": '[{"id":"a","name":"自定义","ask":"说明"}]'}
    )
    assert custom["hook_criteria"] == [{"id": "a", "name": "自定义", "ask": "说明"}]
    # id 重复 → 判为非法，回退默认
    dup = resolve_breakdown_settings_from_rows(
        {"breakdown_hook_criteria": '[{"id":"a","name":"n","ask":"x"},{"id":"a","name":"m","ask":"y"}]'}
    )
    assert len(dup["hook_criteria"]) == 8
