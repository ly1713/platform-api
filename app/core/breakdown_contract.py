"""爆款拆解输出契约：字段表 + 硬性规则的唯一真相。

为什么单独成模块：拆解结构此前有两份手写副本——``breakdown_settings`` 里的
``DEFAULT_BREAKDOWN_PROMPT`` JSON 字面量，和 ``modules/breakdown`` 里的
``_REPAIR_SCHEMA``。两份很容易只改一边（历史事实：``hook_matches`` 只在提示词里
出现，修复轮的 schema 里没有）。要把拆解交给外部执行时还会出现第三份
（MCP resource / 对外文档），所以先把字段表收敛到一处，再由它渲染三种产物：

- ``render_prompt_schema()``：给**内部** system 提示词用的带说明字面量
  （说明文字不进 JSON，是给人读的，例如 ``0-100 整数（爆款潜力评分）``）；
- ``render_repair_schema()``：给**修复轮**用的严格 JSON 样例（可被模型直接照抄）；
- ``render_agent_schema()``：给**外部执行方**（MCP resource / 文档）用的结构说明。

红线：本模块不导入任何业务模块，保持无依赖，避免与 ``breakdown_settings`` /
``modules.breakdown`` 形成循环导入；外部执行方也能单独取用这份契约。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 契约版本：外部执行方据此判断「我拿到的输入/输出结构是不是我认识的那一版」。
# 改动字段名、增删必填项都必须递增；只是改说明文字不需要。
BREAKDOWN_CONTRACT_VERSION = "ext-v1"


@dataclass(frozen=True)
class OutputField:
    """报告里的一个顶层字段。

    ``prompt_literal``：写进内部提示词的「人读版」字面量，可以带中文说明；
    ``example``：修复轮 schema 与对外文档用的真实样例值，必须是合法 JSON 值。

    两个表示分开而不是共用一份：提示词需要告诉模型「score 是 0-100 整数」，
    但修复轮的 schema 必须是模型能直接照抄的合法 JSON，两者天生不同。真正的
    漂移风险是「某个字段只加在一处」，由 ``test_breakdown_contract`` 的键集
    一致性断言守住。
    """

    key: str
    prompt_literal: str
    example: Any


# 字段顺序即报告阅读顺序，也是三种渲染的公共顺序。改顺序会影响提示词文本。
OUTPUT_FIELDS: tuple[OutputField, ...] = (
    OutputField(
        key="verdict",
        prompt_literal='"一句话结论：这条为什么火"',
        example="一句话结论：这条为什么火",
    ),
    OutputField(
        key="score",
        prompt_literal="0-100 整数（爆款潜力评分）",
        example=85,
    ),
    OutputField(
        key="hook",
        prompt_literal=(
            '{"first_line": "前3秒台词", "first_frame": "首帧画面", '
            '"hook_type": "好奇缺口/反常识/清单体/转变叙事/痛点直呼/其他", "why": "为什么抓人"}'
        ),
        example={
            "first_line": "前3秒台词",
            "first_frame": "首帧画面",
            "hook_type": "好奇缺口/反常识/清单体/转变叙事/痛点直呼/其他",
            "why": "为什么抓人",
        },
    ),
    OutputField(
        key="structure",
        prompt_literal='[{"segment": "0-3s", "role": "钩子/展开/转折/高潮/CTA", "summary": "段落概要"}]',
        example=[{"segment": "0-3s", "role": "钩子", "summary": "段落概要"}],
    ),
    OutputField(
        key="pacing",
        prompt_literal=(
            '{"pattern_interrupts": ["每3-5秒的变化"], "re_engagement_points": [秒数], '
            '"emotion_curve": "情绪曲线描述"}'
        ),
        example={
            "pattern_interrupts": ["每3-5秒的变化"],
            "re_engagement_points": [0],
            "emotion_curve": "情绪曲线描述",
        },
    ),
    OutputField(
        key="script",
        prompt_literal='{"reusable_phrases": ["可复用句式"], "money_lines": ["金句/记忆点"]}',
        example={"reusable_phrases": ["可复用句式"], "money_lines": ["金句"]},
    ),
    OutputField(
        key="psych_triggers",
        prompt_literal='["好奇", "认同", "损失厌恶"]',
        example=["好奇"],
    ),
    OutputField(
        key="cta",
        prompt_literal='{"type": "引导方式", "placement": "位置"}',
        example={"type": "引导方式", "placement": "位置"},
    ),
    OutputField(
        key="comment_insights",
        prompt_literal=(
            '{"top_themes": ["观众最关心"], "controversies": ["争议点"], '
            '"questions": ["可回复/可做下一条的问题"]}'
        ),
        example={
            "top_themes": ["观众最关心"],
            "controversies": ["争议点"],
            "questions": ["可回复的问题"],
        },
    ),
    OutputField(
        key="blueprint",
        prompt_literal='["复刻步骤 1", "复刻步骤 2"]',
        example=["复刻步骤"],
    ),
    OutputField(
        key="replication_tips",
        prompt_literal='["给创作者的落地建议"]',
        example=["给创作者的落地建议"],
    ),
)

# ``hook_matches`` 不写进上面两份渲染：它由 ``hook_criteria`` 动态决定判据 id 清单，
# 静态字面量写不出正确提示，由 ``core.hook_criteria.hook_matches_instruction`` 生成。
# 这里登记它，是为了让「报告有哪些顶层字段」这件事仍然只有一处真相。
DYNAMIC_FIELDS: tuple[str, ...] = ("hook_matches",)


@dataclass(frozen=True)
class HardRule:
    """一条硬性规则：既要进内部提示词，也要原样交给外部执行方。"""

    id: str
    text: str


HARD_RULES: tuple[HardRule, ...] = (
    HardRule(
        id="R1",
        text=(
            "只依据给定材料分析，**禁止编造**任何看不到的细节；材料缺失的字段一律填 null 或空数组。"
        ),
    ),
    HardRule(
        id="R2",
        text=(
            "数字（播放/点赞/相对中位数倍数/互动率）已由系统算好，你**只引用、不重算**，更不许杜撰新数字。"
        ),
    ),
    HardRule(id="R3", text="只输出 JSON，不要解释、不要 Markdown 代码块。"),
)

PROMPT_PREAMBLE = (
    "你是短视频爆款拆解分析师。根据给定的作品元数据、转录文案、视频理解结果、"
    "评论区摘要与后端已算好的互动数字，产出一份「为什么火 + 怎么复刻」的结构化拆解报告。"
)


def output_field_keys() -> tuple[str, ...]:
    """报告顶层字段的完整清单（静态字段 + 动态字段），顺序稳定。"""
    return tuple(f.key for f in OUTPUT_FIELDS) + DYNAMIC_FIELDS


def render_prompt_schema() -> str:
    """内部提示词用的结构字面量：``{ "verdict": "...", ... }``，带中文说明。

    刻意不是合法 JSON——``score`` 一类字段写的是「0-100 整数」这种约束，
    比给个具体数字更能约束模型。最后一行不带逗号，便于模型照抄。
    """
    lines = [f'  "{f.key}": {f.prompt_literal}' for f in OUTPUT_FIELDS]
    return "{\n" + ",\n".join(lines) + "\n}"


def render_repair_schema() -> str:
    """修复轮用的严格 JSON 样例（``indent=1``，与历史输出保持一致）。"""
    return json.dumps(
        {f.key: f.example for f in OUTPUT_FIELDS},
        ensure_ascii=False,
        indent=1,
    )


def render_agent_schema() -> str:
    """对外执行方用的结构说明：合法 JSON 样例 + 每个顶层字段的类型。

    与 ``render_repair_schema`` 的区别是这里额外给出类型表，外部不必从样例反推
    「structure 是不是数组」。内容由 ``schema_description`` 渲染，MCP resource 与
    料包里的结构化输出契约因此不会各说一套。
    """
    described = schema_description()
    field_lines = "\n".join(f"- {f['key']}: {f['type']}" for f in described["fields"])
    return (
        f"# 拆解报告结构（{BREAKDOWN_CONTRACT_VERSION}）\n\n"
        f"顶层字段：\n{field_lines}\n\n"
        f"样例（所有字段都可能为 null 或空数组，表示材料里没有这条信息）：\n"
        f"{render_repair_schema()}\n"
    )


# 顶层字段的类型表。key 必须在 ``output_field_keys()`` 里（有测试守着），
# 类型字符串是给外部执行方看的，不是运行时校验（校验见 ``normalize_report``）。
FIELD_TYPES: dict[str, str] = {
    "verdict": "string",
    "score": "number 0-100",
    "hook": "object",
    "structure": "array<object>",
    "pacing": "object",
    "script": "object",
    "psych_triggers": "array<string>",
    "cta": "object",
    "comment_insights": "object",
    "blueprint": "array<string>",
    "replication_tips": "array<string>",
    # 刻意不写成 array<{"criterion": ...}>：这段文本里出现花括号会让「从第一个
    # { 到最后一个 } 取样例」的解析（外部脚手架与测试都这么做）取错区间。
    "hook_matches": "array<object> 每项为 criterion / segment / evidence",
}


def schema_description() -> dict:
    """结构化版本的输出契约（料包 ``rules.output_schema`` 用）。

    ``render_agent_schema`` 是给人/agent 读的文本，这里是给程序读的同一份内容。
    两者都从 ``output_field_keys`` + ``FIELD_TYPES`` 派生，因此「多一个字段只加
    在一处」的漂移由同一组测试挡住。
    """
    return {
        "contract_version": BREAKDOWN_CONTRACT_VERSION,
        "fields": [
            {"key": key, "type": FIELD_TYPES.get(key, "any")} for key in output_field_keys()
        ],
        "example": {f.key: f.example for f in OUTPUT_FIELDS},
        "notes": [
            f"hook_matches 最多保留 {MAX_HOOK_MATCHES} 条，缺 criterion 或 evidence 的条目会被丢弃。",
            "材料里没有的信息填 null 或空数组，不要编造。",
            "score 超出 0-100 会被收敛到边界，非法值记 0。",
        ],
    }


def hard_rule_descriptions() -> list[dict]:
    """硬性规则的结构化清单（料包 / MCP resource 用），与提示词同源。"""
    return [{"id": rule.id, "text": rule.text} for rule in HARD_RULES]


def render_hard_rules() -> str:
    """把硬性规则渲染成内部提示词里的编号列表（含结尾换行）。"""
    return "".join(f"{index}. {rule.text}\n" for index, rule in enumerate(HARD_RULES, start=1))


def build_default_prompt() -> str:
    """内置 system 提示词： preamble + 硬性要求 + 输出结构。

    **逐字节**等价于迁移前 ``breakdown_settings.DEFAULT_BREAKDOWN_PROMPT`` 的文本；
    改动这里会改变所有内部拆解的行为，必须同时递增 ``_PROMPT_VERSION``。
    """
    return (
        PROMPT_PREAMBLE
        + "\n硬性要求：\n"
        + render_hard_rules()
        + "输出 JSON 结构：\n"
        + render_prompt_schema()
    )


# ============================================================
# 输入截断：内部提示词与外部料包必须看到同一份转录
# ============================================================

# 截断提示语。R4（见设计 §2.2）要求显式告知：不告知的话模型会把「材料到此为止」
# 当成「视频就此结束」，仍然自信地填 structure / cta。
TRANSCRIPT_TRUNCATION_NOTICE = (
    "\n\n（转录在此处被截断：原文 {original} 字符，仅提供前 {limit} 字符）"
)


def truncate_transcript(text: object, limit: int) -> tuple[str, dict]:
    """按字符上限截断转录，返回 ``(送入分析的文本, 结构化截断元信息)``。

    内部提示词与外部料包**都走这一个函数**：拆解外移后要比对「同一素材在两条
    路径上的结论是否可比」，前提是两边看到的材料逐字节相同。各写一份截断逻辑的话，
    差异不会立刻暴露，只会在某条长视频上悄悄分成两份材料。

    ``limit <= 0`` 视为不截断（调用方负责给默认值）。

    元信息里的 ``provided_chars`` 是**裁剪后、加提示语前**的长度，外部据此判断
    「我拿到的是全部还是一部分」，不会被提示语的字数带偏。
    """
    raw = text.strip() if isinstance(text, str) else ""
    if limit is None or limit <= 0:
        return raw, {"truncated": False, "original_chars": len(raw), "provided_chars": len(raw)}
    provided = raw[:limit]
    truncated = len(raw) > limit
    out = provided
    if truncated:
        out += TRANSCRIPT_TRUNCATION_NOTICE.format(original=len(raw), limit=limit)
    return out, {
        "truncated": truncated,
        "original_chars": len(raw),
        "provided_chars": len(provided),
    }


# ============================================================
# 结果归一化：内部链路与外部回传走同一套校验
# ============================================================
#
# 为什么归一化放在契约层而不是服务层：拆解要交给外部执行时会出现两条写入路径
# （内部 worker 跑模型 / 外部 agent 回传报告）。两条路径各写一份校验，迟早会出现
# 「同一份非法输入，内部被拒、外部入库」这类不可解释的分歧。这里是唯一入口。

# 单条判据命中最多保留多少条。太多意味着模型在凑数，报告的「为什么火」会失焦。
MAX_HOOK_MATCHES = 12


def normalize_score(value: object) -> float:
    """把评分收敛到 0-100；非法 / NaN / 布尔记 0。

    布尔单独判掉是必要的：``float(True) == 1.0``，模型返回 ``true`` 时会静默
    变成 1 分，看起来像「模型认为这条不火」而不是「模型没给分」。
    """
    import math

    if isinstance(value, bool):
        return 0.0
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(num):
        return 0.0
    return max(0.0, min(100.0, num))


def normalize_sections(parsed: dict) -> dict:
    """定性板块：除 verdict/score/comment_insights 之外的各段原样保留。

    这里刻意不做类型强转——各板块结构由提示词约束，强行规整会把「模型给错类型」
    悄悄改成空值，掩盖真实的格式问题。缺项保留 ``None``，报告页据此显示「未提供」。
    """
    return {
        "hook": parsed.get("hook"),
        "structure": parsed.get("structure"),
        "pacing": parsed.get("pacing"),
        "script": parsed.get("script"),
        "psych_triggers": parsed.get("psych_triggers"),
        "cta": parsed.get("cta"),
        "blueprint": parsed.get("blueprint"),
        "replication_tips": parsed.get("replication_tips"),
    }


def normalize_hook_matches(
    parsed: dict,
    criteria: list[dict] | None = None,
    *,
    rejected: list[str] | None = None,
) -> list[dict]:
    """规整判据命中列表，只保留可核对的条目。

    每条必须同时有 ``criterion`` 和 ``evidence``——只有 id 没有依据的命中
    等于「模型说它命中了」，无法核对，不写进报告。

    ``criteria`` 非空时**额外要求** ``criterion`` 是判据表中真实存在的 id。
    这条收紧是在拆解外移时补的：内部提示词里判据表就在眼前，模型编 id 的概率低；
    外部执行方可能拿着过期的判据表（用户在两次调用之间改了配置），编出来的 id
    会静默写进报告，看起来像是命中了某个标准。给了判据表却不校验，等于放弃这层保护。

    ``rejected`` 若传入，会把被丢弃的原因追加进去（供外部回传的错误响应使用：
    只说「格式错误」外部无法自纠）。
    """
    raw = parsed.get("hook_matches")
    if not isinstance(raw, list):
        return []
    known = {str(item.get("id")) for item in (criteria or []) if isinstance(item, dict)}
    out: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            if rejected is not None:
                rejected.append(f"hook_matches[{index}]: 不是对象")
            continue
        criterion = item.get("criterion")
        evidence = item.get("evidence")
        if not isinstance(criterion, str) or not criterion.strip():
            if rejected is not None:
                rejected.append(f"hook_matches[{index}]: 缺少 criterion")
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            if rejected is not None:
                rejected.append(f"hook_matches[{index}] ({criterion.strip()}): 缺少 evidence")
            continue
        key = criterion.strip()
        if known and key not in known:
            if rejected is not None:
                rejected.append(
                    f"hook_matches[{index}]: criterion '{key}' 不在当前判据表内"
                    f"（可用: {', '.join(sorted(known))}）"
                )
            continue
        entry = {"criterion": key, "evidence": evidence.strip()[:300]}
        segment = item.get("segment")
        if isinstance(segment, str) and segment.strip():
            entry["segment"] = segment.strip()[:40]
        out.append(entry)
        if len(out) >= MAX_HOOK_MATCHES:
            break
    return out


def normalize_report(
    parsed: object,
    criteria: list[dict] | None = None,
    *,
    rejected: list[str] | None = None,
) -> dict:
    """把模型/外部给出的原始对象归一化成可落库的报告字段。

    返回 ``{"score", "verdict", "sections", "comment_insights"}``；
    ``sections`` 里已含 ``hook_matches``。

    Raises:
        ValueError: 输入不是 JSON 对象（这是格式问题，调用方据此返回 422 或落 error）。
    """
    if not isinstance(parsed, dict):
        raise ValueError("拆解结果不是 JSON 对象")
    sections = normalize_sections(parsed)
    sections["hook_matches"] = normalize_hook_matches(parsed, criteria, rejected=rejected)
    comment_insights = parsed.get("comment_insights")
    return {
        "score": normalize_score(parsed.get("score")),
        "verdict": parsed.get("verdict") if isinstance(parsed.get("verdict"), str) else "",
        "sections": sections,
        "comment_insights": comment_insights if isinstance(comment_insights, dict) else {},
    }
