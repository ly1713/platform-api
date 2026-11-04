"""爆点判据表：把「什么算爆点」做成可配置、可排序的清单。

为什么独立成表：之前这些判断散在拆解提示词的各个字段里，模型只能从
「请分析 hook / structure / pacing」这样的描述里自己揣测什么值得说。
成熟做法（AI-Youtube-Shorts-Generator 等）都把它写成**按影响力排序的判据**，
模型按表逐条对照，输出也更可比。

红线（同 process_settings）：判据不写死在业务代码里，有内置默认、可在线改。
"""

from __future__ import annotations

# 每条判据：id / 名称 / 说明。顺序即影响力排序（越靠前越是爆点主因）。
DEFAULT_HOOK_CRITERIA: list[dict[str, str]] = [
    {
        "id": "hook",
        "name": "钩子时刻",
        "ask": "开头就制造好奇或反差的语句，让人立刻想知道后面讲什么",
    },
    {
        "id": "emotion_peak",
        "name": "情绪峰值",
        "ask": "真实的惊讶、大笑、愤怒、脆弱或兴奋，未经修饰的反应",
    },
    {
        "id": "opinion_bomb",
        "name": "观点炸弹",
        "ask": "强烈、有争议或反直觉的判断，让人想赞同或反驳",
    },
    {
        "id": "revelation",
        "name": "反转/揭秘",
        "ask": "出人意料的事实、数据或自白，改变观众原本的认知",
    },
    {
        "id": "conflict",
        "name": "冲突/张力",
        "ask": "分歧、反驳，或一个被正面处理的问题",
    },
    {
        "id": "quotable",
        "name": "可摘金句",
        "ask": "单独拿出来就能当引语卡的一句话",
    },
    {
        "id": "story_peak",
        "name": "故事高潮",
        "ask": "一段叙事的转折点或报酬时刻",
    },
    {
        "id": "practical",
        "name": "实用价值",
        "ask": "观众能立刻上手的具体技巧、方法或洞察",
    },
]

# 校验：id 唯一、字段齐全、非空。
def valid_criteria(data: object) -> bool:
    if not isinstance(data, list) or not data:
        return False
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            return False
        if not all(isinstance(item.get(k), str) and item.get(k).strip() for k in ("id", "name", "ask")):
            return False
        if item["id"] in seen:
            return False
        seen.add(item["id"])
    return True


def criteria_prompt_block(criteria: list[dict[str, str]]) -> str:
    """把判据表渲染成提示词段落（按影响力从高到低编号）。"""
    lines = ["【爆点判据】（按影响力从高到低；逐条对照，命中的写进 hook_matches）"]
    for index, item in enumerate(criteria or DEFAULT_HOOK_CRITERIA, start=1):
        lines.append(f"{index}. {item['name']}：{item['ask']}")
    return "\n".join(lines)


def hook_matches_instruction(criteria: list[dict[str, str]]) -> str:
    """告诉模型在输出里怎么回报命中的判据。"""
    ids = ", ".join(item["id"] for item in (criteria or DEFAULT_HOOK_CRITERIA))
    return (
        '"hook_matches": [{"criterion": "判据 id（' + ids + '）", '
        '"segment": "在哪一段（如 0-3s）", "evidence": "片中的原话或画面依据"}]'
    )
