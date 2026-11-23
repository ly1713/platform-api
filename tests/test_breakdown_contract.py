"""拆解输出契约的单一真相守卫。

守三件事，任何一条失守都意味着「同一个报告结构」开始有多份真相：

1. **同源**：内部提示词、修复轮 schema、对外契约三份产物都由 ``OUTPUT_FIELDS``
   渲染，不存在只加在一处的字段。
2. **合法**：修复轮 schema 与对外契约必须能被 ``json.loads`` 解析——它们要让模型
   或外部执行方直接照抄，不是给人看的伪代码。
3. **稳定**：内置提示词的文本是行为契约，改动会改变所有内部拆解；迁移期的
   ``DEFAULT_BREAKDOWN_PROMPT`` 必须与契约渲染结果逐字节一致。

历史背景（为什么需要这份测试）：结构曾有两份手写副本
（``DEFAULT_BREAKDOWN_PROMPT`` 与 ``_REPAIR_SCHEMA``），``hook_matches`` 一度
只出现在前者里，导致修复轮把模型刚写对的字段抹掉。
"""

import json

import pytest

from app.core import breakdown_contract as contract


# ============================================================
# 1. 同源：三份产物共享同一张字段表
# ============================================================


def test_every_static_field_appears_in_prompt_schema():
    """静态字段必须出现在内部提示词的结构字面量中。

    动态字段（``hook_matches``）不在此列：它的判据 id 清单来自运行期配置，
    由 ``build_breakdown_prompt`` 追加，静态字面量写不出正确提示。
    """
    rendered = contract.render_prompt_schema()
    missing = [f.key for f in contract.OUTPUT_FIELDS if f'"{f.key}"' not in rendered]
    assert not missing, f"字段未进入内部提示词: {missing}"


def test_every_field_appears_in_repair_schema():
    """修复轮 schema 必须包含全部静态字段——模型照抄它改写，缺字段等于抹掉结论。"""
    parsed = json.loads(contract.render_repair_schema())
    missing = [f.key for f in contract.OUTPUT_FIELDS if f.key not in parsed]
    assert not missing, f"字段未进入修复轮 schema: {missing}"
    # 反向：不能有字段表之外的多余键（例如手写的历史残留）。
    extra = [key for key in parsed if key not in {f.key for f in contract.OUTPUT_FIELDS}]
    assert not extra, f"修复轮 schema 出现未知字段: {extra}"


def test_every_field_appears_in_agent_schema():
    """对外契约要列出全部顶层字段，外部执行方据此自检输出是否完整。"""
    rendered = contract.render_agent_schema()
    missing = [key for key in contract.output_field_keys() if key not in rendered]
    assert not missing, f"字段未进入对外契约: {missing}"


def test_output_field_keys_are_unique_and_ordered():
    keys = contract.output_field_keys()
    assert len(keys) == len(set(keys)), "字段名重复会让三份渲染互相覆盖"
    # 顺序是提示词文本的一部分，重排等于改行为。静态字段按报告阅读顺序排列，
    # 动态字段（hook_matches）追加在末尾，因为它由运行期判据表决定。
    assert keys[0] == "verdict"
    assert keys[: len(contract.OUTPUT_FIELDS)] == tuple(f.key for f in contract.OUTPUT_FIELDS)
    assert keys[-1] == contract.DYNAMIC_FIELDS[-1]


def test_hook_matches_is_dynamic_not_static():
    """hook_matches 的判据 id 是运行期配置，不能写死进静态 schema。

    它若进了静态字段表，用户自定义判据表后提示词就会要求一套不存在的 id。
    """
    assert "hook_matches" in contract.DYNAMIC_FIELDS
    assert "hook_matches" not in {f.key for f in contract.OUTPUT_FIELDS}
    assert "hook_matches" not in json.loads(contract.render_repair_schema())
    # 但它仍算报告字段，对外契约要登记。
    assert "hook_matches" in contract.render_agent_schema()


# ============================================================
# 2. 合法：可被机器解析
# ============================================================


def test_repair_schema_is_valid_json():
    parsed = json.loads(contract.render_repair_schema())
    assert isinstance(parsed, dict)
    # score 必须是数字样例，否则模型可能回 "85%" 这类字符串。
    assert isinstance(parsed["score"], (int, float))


def test_prompt_schema_is_intentionally_not_json():
    """内部提示词的结构字面量带中文约束说明，刻意不是合法 JSON。

    这条测试是**反直觉的守卫**：如果有人「顺手」把它改成合法 JSON，
    `score` 的取值约束（0-100 整数）就丢了，模型会开始返回任意数字。
    """
    with pytest.raises(json.JSONDecodeError):
        json.loads(contract.render_prompt_schema())
    assert "0-100 整数" in contract.render_prompt_schema()


def test_agent_schema_embeds_parseable_example():
    """对外契约里的样例段必须能单独解析出来（外部靠它生成脚手架）。"""
    rendered = contract.render_agent_schema()
    start = rendered.index("{")
    end = rendered.rindex("}")
    parsed = json.loads(rendered[start : end + 1])
    assert parsed["verdict"]


# ============================================================
# 3. 稳定：默认提示词逐字节固定
# ============================================================


def test_default_prompt_is_rendered_from_contract():
    """``DEFAULT_BREAKDOWN_PROMPT`` 必须是契约的渲染结果，不能是另一份手写文本。"""
    from app.core.breakdown_settings import DEFAULT_BREAKDOWN_PROMPT

    assert DEFAULT_BREAKDOWN_PROMPT == contract.build_default_prompt()


def test_repair_schema_is_rendered_from_contract():
    """``modules.breakdown._REPAIR_SCHEMA`` 必须与契约渲染结果一致。"""
    from app.modules.breakdown import _REPAIR_SCHEMA

    assert _REPAIR_SCHEMA == contract.render_repair_schema()


def test_default_prompt_contains_all_hard_rules():
    prompt = contract.build_default_prompt()
    for rule in contract.HARD_RULES:
        assert rule.text in prompt, f"硬性规则 {rule.id} 未进入提示词"
    # 规则必须编号，模型靠编号对齐「硬性要求第几条」。
    assert "1. " in prompt and "2. " in prompt and "3. " in prompt


def test_default_prompt_shape_is_pinned():
    """固定开头与结尾，防止无意间改变提示词语义。

    只钉首尾而不钉全文：中间字段说明可以改（那是给人读的），但
    「你是谁 / 最后一句是什么」一变，所有历史报告的可比性就没了。
    """
    prompt = contract.build_default_prompt()
    assert prompt.startswith(contract.PROMPT_PREAMBLE)
    assert prompt.endswith("}")


def test_contract_version_is_declared():
    """外部执行方靠版本号判断契约是否认识，不能是空串。"""
    assert contract.BREAKDOWN_CONTRACT_VERSION
    assert contract.BREAKDOWN_CONTRACT_VERSION in contract.render_agent_schema()
