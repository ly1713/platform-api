"""爆款拆解配置：提示词 + 爆款判定阈值，存 `system_setting` KV。

红线（同 `comment_settings.py` / `process_settings.py`）：提示词与阈值**不写死在业务代码里**，
有内置默认、可在线改、读取时取最新值。写入侧校验（长度/范围）放 API 层（后续 Task）。

- `breakdown_prompt`：拆解报告的 system 提示词（约束 JSON 结构与「禁编造」）。
- `breakdown_viral_multiple`：对标账号报告里「爆款」判定阈值（播放 >= 该账号中位数 × 倍数）。
- 自动爆款拆解的「阀门」：播放门槛 / 点赞门槛 / 互动率门槛 / 发布时间窗口 / 命中规则 /
  是否自动转录、自动视频理解、自动拆解。都存 KV，用户可在设置页按需调。
"""

import json
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.breakdown_contract import build_default_prompt
from app.core.hook_criteria import DEFAULT_HOOK_CRITERIA, valid_criteria
from app.models import SystemSetting

# system_setting key（单一事实来源；API / 服务复用此映射）。
BREAKDOWN_KEYS: dict[str, str] = {
    "prompt": "breakdown_prompt",
    "viral_multiple": "breakdown_viral_multiple",
    # 自动爆款拆解的判定阀门
    "min_play": "breakdown_min_play",
    "min_like": "breakdown_min_like",
    "min_engagement_rate": "breakdown_min_engagement_rate",
    "publish_within_days": "breakdown_publish_within_days",
    "match_rule": "breakdown_match_rule",  # any / all
    # 送入拆解的转录字符上限：写死会让长视频尾部信息丢失，见 modules/breakdown.py。
    "transcript_limit": "breakdown_transcript_limit",
    # 单次拆解的模型超时。带推理的模型输出 2k 字符 JSON 实测要 80~240 秒，
    # 90 秒默认值会把「模型在思考」误判成失败并触发主备切换。
    "timeout_sec": "breakdown_timeout_sec",
    # 自动执行链：命中阀门的爆款自动转录 / 视频理解 / 拆解
    "auto_transcribe": "breakdown_auto_transcribe",
    "auto_understand": "breakdown_auto_understand",
    "auto_breakdown": "breakdown_auto_breakdown",
    # 爆点判据表（JSON 数组）。拆解提示词据此逐条对照，输出 hook_matches。
    "hook_criteria": "breakdown_hook_criteria",
    # 执行器：谁来做拆解。``external``（外部 agent 认领）是本次改造的目的，
    # ``internal``（平台自己跑模型）保留为可回退的一键开关，不是单向切换。
    "executor": "breakdown_executor",
    # 外部租约打满仍无人回传时，平台是否自己补跑一次。
    "internal_fallback": "breakdown_internal_fallback",
    # 认领租约时长（分钟）：外部 agent 拿料后多久算失联。
    "claim_lease_min": "breakdown_claim_lease_min",
    # 一条素材最多被外部认领几次（含接管），超过即落可见 error，不允许静默卡住。
    "max_attempts": "breakdown_max_attempts",
}

# 内置默认提示词：定义报告 JSON 结构 + 各分析维度 + 禁编造约束。
# 结构对齐设计 §2.1；数字（data_context/对标）由后端确定性计算，LLM 只做定性。
#
# **文本由 `core.breakdown_contract` 渲染，不再手写。** 字段表与硬性规则收敛到
# 那一处后，内部提示词、修复轮 schema、对外契约三份产物才不会各改一半
# （历史事实：hook_matches 曾经只出现在提示词里，修复轮 schema 缺它）。
# 上面那条「不做 str.format」的注意事项随之失效：渲染由本模块之外的纯函数完成。
DEFAULT_BREAKDOWN_PROMPT = build_default_prompt()

_DEFAULT_VIRAL_MULTIPLE = 3.0

# 自动爆款判定阀门的默认值。设计取向：**默认只开"相对自身中位数"这一条**（最贴近
# 用户对"爆款"的直觉：比自己平时火），其余阀门默认 0=不限，命中规则默认 any（任一命中）。
# 这样即便用户什么都不调，也不会把所有视频都判成爆款、把加工额度烧光。
_DEFAULTS: dict[str, tuple[object, str]] = {
    "breakdown_min_play": (0, "int"),
    "breakdown_min_like": (0, "int"),
    "breakdown_min_engagement_rate": (0.0, "float"),
    "breakdown_publish_within_days": (0, "int"),
    # 24k 字符约等于 20 分钟口播；再长就由用户在设置页按自己的模型上下文放宽。
    "breakdown_transcript_limit": (24000, "int"),
    # 300 秒覆盖实测最长 239 秒；再长说明模型本身不可用，交给主备切换。
    "breakdown_timeout_sec": (300, "int"),
    "breakdown_match_rule": ("any", "str"),
    "breakdown_auto_transcribe": (True, "bool"),
    "breakdown_auto_understand": (False, "bool"),
    "breakdown_auto_breakdown": (False, "bool"),
    # 判据表不走 _coerce：是 JSON 列表，单独解析（见 _parse_criteria）。
    "breakdown_hook_criteria": (None, "str"),
    # 默认值取 external 符合本次改造目的；兜底默认开，避免外部 agent 不在线时
    # 平台彻底没有报告。这是「可回退的默认」，不是单向切换。
    "breakdown_executor": ("external", "str"),
    "breakdown_internal_fallback": (True, "bool"),
    # 30 分钟覆盖实测最长拆解（240 秒）+ 人工排查余量；再短会让正常在跑的
    # agent 被误判失联，再长则素材卡住后用户等太久才知道。
    "breakdown_claim_lease_min": (30, "int"),
    "breakdown_max_attempts": (3, "int"),
}

_MATCH_RULES = ("any", "all")
_EXECUTORS = ("internal", "external")


def _parse_criteria(raw: object) -> list[dict[str, str]]:
    """解析判据表；任何非法（坏 JSON / 缺字段 / id 重复）都回退内置默认。

    判据表决定「模型认为什么算爆点」，坏数据不能让整条拆解失败，
    但也不能静默变成空表（空表 = 没有任何判断标准），只能回退默认。
    """
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return list(DEFAULT_HOOK_CRITERIA)
    if valid_criteria(raw):
        return [dict(item) for item in raw]  # type: ignore[arg-type]
    return list(DEFAULT_HOOK_CRITERIA)


def _coerce(raw, default, kind: str):
    if raw is None or str(raw) == "":
        return default
    try:
        if kind == "int":
            return int(str(raw).strip())
        if kind == "float":
            return float(str(raw).strip())
        if kind == "bool":
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            return default
        return str(raw)
    except (TypeError, ValueError):
        return default


def resolve_breakdown_settings_from_rows(rows: dict[str, str]) -> dict:
    """从 system_setting 行映射解析拆解配置（不依赖 DB Session）。

    返回键与 ``BREAKDOWN_KEYS`` 一致；所有数值做有限性/非负校验，非法回退默认。
    """
    raw_prompt = rows.get("breakdown_prompt")
    prompt = raw_prompt if (raw_prompt and raw_prompt.strip()) else DEFAULT_BREAKDOWN_PROMPT

    raw_mult = rows.get("breakdown_viral_multiple")
    try:
        mult = float(str(raw_mult).strip()) if raw_mult not in (None, "") else _DEFAULT_VIRAL_MULTIPLE
    except (TypeError, ValueError):
        mult = _DEFAULT_VIRAL_MULTIPLE
    # 读侧兜底：手改/历史 KV 若为 nan/inf/<=0，回退默认，避免下游（对标报告 JSON 序列化）500。
    if not math.isfinite(mult) or mult <= 0:
        mult = _DEFAULT_VIRAL_MULTIPLE

    out: dict = {
        "prompt": prompt,
        "viral_multiple": mult,
        "hook_criteria": _parse_criteria(rows.get("breakdown_hook_criteria")),
    }
    for key, (default, kind) in _DEFAULTS.items():
        short = key[len("breakdown_"):]  # breakdown_min_play -> min_play
        if key == "breakdown_hook_criteria":
            continue  # 上面已单独解析
        value = _coerce(rows.get(key), default, kind)
        if kind in ("int", "float"):
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                value = default
        if key == "breakdown_match_rule" and value not in _MATCH_RULES:
            value = default
        if key == "breakdown_executor" and value not in _EXECUTORS:
            # 手改 KV 写错时回退 internal（而不是 external）：写错的人想要的
            # 几乎一定不是「平台别再产报告了」，静默停摆比报错更难被发现。
            value = "internal"
        # 0 次认领等于「永不外放」，1 分钟租约等于「刚认领就失联」。两者都是
        # 误配，回退默认而不是照单执行。
        if key == "breakdown_claim_lease_min" and value < 1:
            value = default
        if key == "breakdown_max_attempts" and value < 1:
            value = default
        out[short] = value
    if out["min_engagement_rate"] > 1:
        out["min_engagement_rate"] = 1.0  # 互动率上限 100%
    return out


def get_breakdown_settings(db: Session) -> dict:
    """返回拆解配置（短名 -> 值），缺失/非法回退内置默认。"""
    rows = {s.key: s.value for s in db.scalars(select(SystemSetting)).all()}
    return resolve_breakdown_settings_from_rows(rows)


def is_viral(data_context: dict, settings_map: dict) -> bool:
    """按配置的阀门判断一条内容是否算「爆款」（纯函数，便于测试）。

    阀门（都是"越大越像爆款"）：
    - ``viral_multiple``：播放 >= 该账号中位数 × 倍数（始终参与；中位数为 0 时不满足）；
    - ``min_play`` / ``min_like``：绝对量门槛（>0 才参与）；
    - ``min_engagement_rate``：(点赞+评论+收藏+分享)/播放 的互动率门槛（>0 才参与）；
    - ``match_rule``：``any``=任一命中即爆款；``all``=全部满足才算。

    返回 False 表示不自动拆解（阀门全部未开启时也返回 False，避免全量误判）。
    """
    checks: list[bool] = []

    mult = float(settings_map.get("viral_multiple") or 0)
    median_play = float(data_context.get("median_play") or 0)
    play = float(data_context.get("play") or 0)
    if mult > 0:
        checks.append(bool(median_play) and play >= median_play * mult)

    min_play = float(settings_map.get("min_play") or 0)
    if min_play > 0:
        checks.append(play >= min_play)

    min_like = float(settings_map.get("min_like") or 0)
    if min_like > 0:
        checks.append(float(data_context.get("like") or 0) >= min_like)

    min_rate = float(settings_map.get("min_engagement_rate") or 0)
    if min_rate > 0:
        checks.append(float(data_context.get("engagement_rate") or 0) >= min_rate)

    if not checks:
        return False
    rule = settings_map.get("match_rule") or "any"
    return all(checks) if rule == "all" else any(checks)
