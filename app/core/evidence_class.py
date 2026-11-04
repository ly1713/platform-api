"""品类路由：判断一条视频的爆点主要靠哪一路证据。

为什么需要：不同品类的爆点证据几乎是反的——
- 带货/知识/访谈：爆点在话里，读文字稿就能挑；
- 游戏/户外/聊天：爆点在语气和现场反应里（怒吼、爆笑），文字稿常常只有「卧槽」；
- 跳舞/才艺/萌宠：文字稿基本是空的，全靠画面、音乐和弹幕。

用一套提示词打天下，等于逼模型在「没有可引用原话」的素材上硬凑结构。

三类的划分参考 hotclip 的 genre/evidence 设计，但类型枚举复用本项目的
判类目录（见 `process_settings.DEFAULT_TAXONOMY`），不另起一套。
"""

from __future__ import annotations

WORDS = "words"
REACTION = "reaction"
VISUAL = "visual"

EVIDENCE_CLASSES: tuple[str, ...] = (WORDS, REACTION, VISUAL)

EVIDENCE_LABELS: dict[str, str] = {
    WORDS: "靠话（转录足以判断）",
    REACTION: "靠现场反应（语气、爆发、互动）",
    VISUAL: "靠画面（动作、剪辑、视觉信息）",
}

# 判类目录（16 类）-> 证据类别。
# 映射理由逐条写在右侧，便于以后按实际素材调整。
DEFAULT_TYPE_EVIDENCE: dict[str, str] = {
    "knowledge": WORDS,      # 知识科普：论点、论据、结论都在话里
    "opinion": WORDS,        # 观点评论：立场与论据靠说
    "commerce": WORDS,       # 带货：卖点、价格、优惠靠说
    "tutorial": WORDS,       # 教程：步骤靠说（画面只是演示）
    "review": WORDS,         # 测评探店：结论靠说
    "interview": WORDS,      # 访谈：一问一答，内容全在话里
    "news": WORDS,           # 新闻纪实：事实陈述
    "film_tv": WORDS,        # 影视解说：旁白为主
    "other": WORDS,          # 未知：默认按最有普适性的文本通道
    "drama": REACTION,       # 剧情段子：效果靠包袱抖响的瞬间
    "emotion": REACTION,     # 情感鸡汤：靠情绪起伏
    "vlog": REACTION,        # 日常 vlog：靠真实反应与生活感
    "game": REACTION,        # 游戏：靠操作高光与现场反应
    "sports": REACTION,      # 体育：靠进球/犯规等高光瞬间
    "performance": VISUAL,   # 才艺舞蹈音乐：靠动作与编排
    "food": VISUAL,          # 美食：靠成品画面与制作过程
    "pet": VISUAL,           # 萌宠：靠可爱画面
}


def evidence_class_of(category: str) -> str:
    """把判类枚举映射到证据类别；未知类型按 ``words`` 兜底。

    兜底选 words 而不是 visual：文本通道对任意素材都不会因为「没有原话」
    而完全空转，是最安全的默认。
    """
    if not category:
        return WORDS
    return DEFAULT_TYPE_EVIDENCE.get(str(category), WORDS)


def evidence_guidance(evidence_class: str) -> str:
    """给提示词用的一段说明：告诉模型这类素材该信哪路证据。

    这段不是「角色扮演」，而是**明确的证据优先级**——模型如果没有这层提示，
    会习惯性地从转录里找结构，在画面类素材上就会把「没有台词」误判成
    「没有内容」。
    """
    if evidence_class == VISUAL:
        return (
            "本片属于「靠画面」的类型：转录可能很少甚至为空，这**不代表没有内容**。\n"
            "请以画面、动作、剪辑节奏、字幕与场景变化为主要依据；\n"
            "转录只作为补充（用于确认人名/品牌/价格等具体信息），\n"
            "不要因为「话少」就把结构字段填成空。"
        )
    if evidence_class == REACTION:
        return (
            "本片属于「靠现场反应」的类型：爆点通常出现在情绪爆发、语气突变、\n"
            "互动回应的瞬间，这些时刻的台词可能只有一句感叹。\n"
            "请同时关注语气/情绪与画面变化，不要只按文字信息量判断重点段落。"
        )
    return (
        "本片属于「靠话」的类型：核心信息在转录里。\n"
        "请以转录的论点、步骤、结论为主要依据，画面只作为辅助印证。"
    )
