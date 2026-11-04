"""高级档组合服务：判类 → 选类型提示词 → 视频理解 → 落库。

两遍式视频理解的服务层（设计 §3、计划 Task 7）：

    content/transcript ──①判类(文本)──▶ 类型 ──②选提示词──▶ ③vision 理解 ──▶ video_understanding

- 判类失败 → 回退 `other` + 通用模板，**不阻断**理解（设计 §3「失败隔离」）；置信度低于阈值
  则标 `low_confidence` 供人工复核（设计 §4）。
- `use_cover` 开启时按 `content.cover_url` 下载封面到 `_cover/` 再送入分类器（设计 §12-4）；
  下载失败/超限回退纯文本，不阻断。
- 提示词按类型区分：`build_prompt` 拼「角色/关注点/维度/输出 JSON/禁止编造」。
- 体积超限先用 ffmpeg 逐级降质压缩（`_ensure_inline`），压缩后仍超限交给 vision 抛错由 worker 重试。
- 判类与理解只经 ``ModelAccessService``；供应商、密钥、模型与主备路由均来自
  「模型接入」的 ``AiProvider`` / ``ModelAssignment``，不再读取 ``llm_*`` 或
  加工页里的模型名。旧配置由启动时的 ``legacy_llm_migration`` 一次性认领。

对外能力：
- `build_prompt(...)`：纯函数，拼装类型专属理解提示词。
- `async run_understand(db, asset_id, *, force_category=None, use_cover=None)`：跑完整链路，返回
  `VideoUnderstanding`；理解失败会把 `status` 落 `error` 并**原样抛出**（worker 负责重试）。
- `UnderstandInputError`：素材/内容缺失等数据问题——不可重试，worker 应直接失败。
"""

import json
import logging
import os
import subprocess

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import events
from app.core.process_settings import (
    get_common_dimensions,
    get_process_settings,
    get_taxonomy,
    get_type_prompts,
)
from app.core.evidence_class import evidence_class_of, evidence_guidance
from app.core.storage import resolve_asset_dir
from app.models import ContentItem, MediaAsset, Transcript, VideoUnderstanding
from app.modules import prefilter, understand_rollup, video_chunking, video_signals, vision
from app.modules.classify import classify

logger = logging.getLogger("platform.understand")

_MB = 1024 * 1024
_PROMPT_VERSION = "v1"
# 转录送入视频理解的截断长度（理解默认已「看视频」，转录只作补充）。
_TRANSCRIPT_LIMIT = 2000
# 封面下载超时（秒）。
_COVER_TIMEOUT = 20


class VideoTooLarge(RuntimeError):
    """压缩到可用画质下限仍超过内联体积上限。

    继承 RuntimeError 以免被 ``except ValueError`` 顺手接走；调用方（worker）
    要把它当**不可重试**——同样的输入重跑三遍只会得到同样的失败。
    """


class UnderstandInputError(ValueError):
    """输入/数据问题导致理解不可执行（如资产无关联内容）——不应被当作可重试故障。

    继承 ValueError（调用方/测试可用 ValueError 捕获）。worker 侧据此不重试。
    """


# --------------------------------------------------------------------------- 提示词


def build_prompt(
    category: str,
    label: str,
    focus: str,
    dimensions: list[dict],
    transcript: str,
    *,
    compression: dict | None = None,
    evidence_class: str = "words",
    chunk_note: str = "",
    moments: list[dict] | None = None,
) -> str:
    """按类型拼装理解提示词（纯函数）。

    Args:
        category: 类型枚举（写进输出 JSON 的 ``type``）。
        label: 类型中文名（角色描述用）。
        focus: 该类型的「关注点」段落（类型区分的灵魂）。
        dimensions: 该类型维度清单（通用 + 专属），每项 ``{key,name,ask}``。
        transcript: 转录全文（可为空）。
        compression: `_ensure_inline` 的压缩信息；画面被压过时要把这件事告诉模型，
            否则它会把「小字看不清」当成「视频里没有字」，把缺失描述成不存在。
        evidence_class: 这类素材的爆点靠哪路证据（words/reaction/visual）。
            不告诉模型的话，它会在「没有台词」的跳舞视频上把结构字段填成空。
        chunk_note: 分块说明（这段是原片的哪一段）。缺了它模型会以为自己看到的
            是完整视频，把局部结论写成整体结论。
        moments: 由视听信号融合出的候选片段，作为客观证据给模型参考。
    """
    dim_lines = "\n".join(f"- {d['key']}（{d['name']}）：{d['ask']}" for d in dimensions)
    body = (
        f"【角色】你是短视频内容分析师，专长「{label}」类视频。\n"
        f"【任务】观看视频，输出该类型的内容理解结果。\n"
        f"【本类型关注点】{focus}\n"
        f"【维度定义】逐条判断下列维度：\n{dim_lines}\n"
        "【输出格式】只输出 JSON，不要解释、不要 Markdown 代码块：\n"
        f'{{"type": "{category}", "summary": "<100字内整体概括>", '
        '"dimensions": {"<维度key>": "<值>"}, '
        '"timeline": [{"t": <秒>, "event": "<发生了什么>"}], "confidence": 0.0}\n'
        "【约束】只依据视频与给定转录；不确定或看不到的字段填 null；"
        "不要编造品牌、人名、价格等视频中不存在的细节。"
    )
    body += "\n【证据优先级】" + evidence_guidance(evidence_class)
    if chunk_note:
        body += "\n【分段说明】" + chunk_note
    if moments:
        # 信号是**客观测量**，不是模型猜的；明确标注来源，避免模型把它当成
        # 「自己看到的」而与我们后面的话术冲突。
        lines = []
        for m in moments[:8]:
            try:
                start = int(round(float(m.get("start", 0))))
                end = int(round(float(m.get("end", 0))))
            except (TypeError, ValueError):
                continue
            drivers = m.get("drivers") or {}
            why = "/".join(drivers.keys()) if isinstance(drivers, dict) and drivers else "综合"
            lines.append(f"- {start}s-{end}s（主要依据：{why}）")
        if lines:
            body += (
                "\n【系统测得的重点时段】以下是 ffmpeg 从音轨与画面测出的高能区间"
                "（响度/镜头切换/运动量），不是猜测；可作为判断节奏与重点位置的客观依据：\n"
                + "\n".join(lines)
            )
    info = compression or {}
    if info.get("compressed"):
        # 压缩档位是确定性已知的，直接报给模型。实测竖屏 1080x1920 按短边压到
        # 720 时小字仍可读，压到 480 时 24px 字基本读不出——模型需要知道
        # 「看不清是画质所致」，才不会把没读到的东西写成「不存在」。
        body += (
            f"\n【画质说明】为控制请求体积，画面已压缩到短边 {info.get('scale')} 像素、"
            f"约 {info.get('kbps')} kbps。小字、角标、细小的 UI 文字可能已经糊掉；"
            "看不清的字段一律填 null，并在 summary 里注明「受画质限制未能确认」，"
            "不要据此断言画面里没有该内容。"
        )
    text = (transcript or "").strip()[:_TRANSCRIPT_LIMIT]
    if text:
        body += f"\n【参考转录】\n{text}"
    return body


def _select_prompt(db: Session, category: str) -> tuple[str, str, str, list[dict]]:
    """返回该类型的 (category, label, focus, dimensions)；未知类型归一为 other。"""
    taxonomy = get_taxonomy(db)
    prompts = get_type_prompts(db)
    if category not in taxonomy:
        category = "other"
    if category not in prompts:
        category = "other"
    label = taxonomy.get(category, "其他")
    focus = prompts[category].get("focus", "")
    dimensions = get_common_dimensions(db) + list(prompts[category].get("dimensions", []))
    return category, label, focus, dimensions


async def _classify_category(
    db: Session,
    content: ContentItem,
    transcript: str,
    *,
    use_cover: bool,
    cover_path: str | None,
    call_model=None,
) -> tuple[str, float, str]:
    """判类；失败回退 other 且不抛（失败隔离）。返回 (category, confidence, reason)。"""
    try:
        result = await classify(
            content.title,
            content.author_name,
            content.duration_sec,
            transcript,
            get_taxonomy(db),
            cover_path=cover_path,
            use_cover=use_cover,
            call_model=call_model,
        )
        cat = result.get("type") or "other"
        conf = _coerce_conf(result.get("confidence"))
        reason = result.get("reason") or ""
        return cat, conf, reason
    except Exception as exc:
        # A missing/disabled/incapable model is not a transient classification
        # miss: retrying the rest of the pipeline with the generic template
        # would hide the configuration defect and report a successful run.
        from app.services.model_access_service import ModelConfigError

        if isinstance(exc, ModelConfigError):
            raise
        logger.warning("判类失败，回退 other: %s", exc)
        return "other", 0.0, f"判类失败，回退通用：{exc}"


def _coerce_conf(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if num != num:
        return 0.0
    return max(0.0, min(1.0, num))


# --------------------------------------------------------------------------- 路径 / 压缩


def _asset_video_path(db: Session, asset: MediaAsset) -> str:
    """素材物理视频路径 = storage_root + relative_path + video_file。仅后端内部使用。"""
    from app.core.storage import resolve_asset_file_path

    return resolve_asset_file_path(resolve_asset_dir(db, asset), asset.video_file or "")


def _guess_ext(url: str, content_type: str) -> str:
    """按响应 Content-Type 猜封面扩展名，兜底 .jpg。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(ct, ".jpg")


async def _ensure_cover(content: ContentItem, work_dir: str, max_mb: float) -> str | None:
    """下载 `content.cover_url` 缓存到素材目录，返回本地路径；无封面/不可用返回 None。

    为什么在服务层做：设计 §12-4 用户拍板「封面可选，开启才额外送图片模型」，
    但封面此前从未落盘，判类永远拿不到封面（Task 7 review Important#1）。

    失败处理：下载失败 / 体积超 `max_mb` / 空内容 → 记 warning 并返回 None（回退纯文本判类，
    不阻断理解）；已在本地缓存则直接复用，避免重复下载。
    """
    url = (content.cover_url or "").strip()
    if not url:
        return None
    os.makedirs(work_dir, exist_ok=True)
    try:
        from app.core.egress import fetch_image

        image = await fetch_image(
            url,
            purpose="understand_cover",
            platform=getattr(content, "platform", "") or "",
            timeout=_COVER_TIMEOUT,
            max_bytes=int(max_mb * _MB) if max_mb and max_mb > 0 else 20 * _MB,
        )
        if image.status_code != 200:
            logger.warning("封面下载失败 HTTP %s，跳过封面判类: %s", image.status_code, url)
            return None
        raw = image.content
        if not raw:
            logger.warning("封面下载为空，跳过封面判类: %s", url)
            return None
        if max_mb and max_mb > 0 and len(raw) > max_mb * _MB:
            logger.warning("封面体积 %.2fMB 超过上限 %.2fMB，跳过封面判类", len(raw) / _MB, max_mb)
            return None
        path = os.path.join(work_dir, "cover" + _guess_ext(url, image.content_type))
        with open(path, "wb") as fh:
            fh.write(raw)
        logger.info("封面已缓存: %s -> %s", url, path)
        return path
    except Exception as exc:  # noqa: BLE001 - 封面是可选增强，失败一律回退纯文本
        logger.warning("封面下载失败，回退纯文本判类: %s", exc)
        return None


def _short_side_filter(short_side: int) -> str:
    """按「短边」缩放的 ffmpeg 滤镜：横屏压高度，竖屏压宽度。

    早期写成 `scale=-2:min(720,ih)`（只限高度），竖屏 1080x1920 会被压成 406x720，
    宽度只剩 37.5%，画面文字直接糊掉。实测同一段竖屏素材：按高度压到 720 时
    模型只能读回 72% 的文字，按短边压到 720 时能读回 100%，而体积只差一倍。
    竖屏是抖音/快手/小红书的主流体裁，必须按短边算。
    """
    return (
        f"scale=w='if(gte(iw,ih),-2,min({short_side},iw))':"
        f"h='if(gte(iw,ih),min({short_side},ih),-2)'"
    )


def _video_bitrate_kbps(video_path: str) -> int:
    """视频流码率（kbps）；探测失败返回 0（不影响主流程）。"""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=bit_rate",
                "-of", "default=nw=1:nk=1", video_path,
            ],
            capture_output=True,
            text=True,
        )
        return int(str(proc.stdout).strip() or 0) // 1000
    except (OSError, ValueError):
        return 0


def _ffprobe_duration(video_path: str) -> float:
    """视频时长（秒）；探测失败返回 0。"""
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", video_path,
            ],
            capture_output=True,
            text=True,
        )
        return max(0.0, float(str(proc.stdout).strip() or 0))
    except (OSError, ValueError):
        return 0.0


def _ensure_inline(video_path: str, max_mb: float, work_dir: str) -> tuple[str, dict]:
    """体积超限时把视频压到内联上限以下。

    为什么改成「按体积反算码率」：原来用固定 CRF 逐级降质，CRF 是**恒定质量**，
    画面越复杂码率越高。18 分钟的口播视频压到 480p/CRF34 仍有 30MB 以上，
    永远压不到 15MB——三档试完还是超限，然后 vision 抛错、worker 重试三次，
    表现成 100% 失败。要命中体积目标，只能反算码率。

    分配方式：目标总码率 = 上限字节 × 0.9 / 时长（留 10% 给容器开销）；
    音频预算 = min(96, 总码率 × 0.25) kbps，其余给视频。
    视频用 `-b:v` + `-maxrate`/`-bufsize` 钉住平均码率；单帧再复杂也超不过上限。

    Returns:
        ``(可送入模型的路径, 压缩信息)``。压缩信息形如
        ``{"compressed": True, "scale": 720, "kbps": 210, "audio_kbps": 48, "crf": None}``。
        调用方要把「画面被压过」写进提示词——模型不会自己知道小字已经糊了。

    Raises:
        VideoTooLarge: 按最低可用画质压完仍超限。带上明确修复建议，
            且**不可重试**（重试只会把同一个确定失败再跑三遍）。
        RuntimeError: ffmpeg 本身失败（可重试）。
    """
    limit = float(max_mb or 0)
    if limit <= 0:
        return video_path, {"compressed": False}
    try:
        size = os.path.getsize(video_path)
    except OSError:
        return video_path, {"compressed": False}
    if size <= limit * _MB:
        return video_path, {"compressed": False}

    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, "inline.mp4")
    duration = _ffprobe_duration(video_path)
    if duration <= 0:
        # 时长探测失败：退回固定 CRF，让 vision 的体积校验兜底（至少不会卡死）。
        duration = None

    # 逐级降画质。每档都按体积反算码率；压不下才降分辨率，
    # 因为「分辨率」才是小字可读性的决定因素，码率只是体积。
    attempts: list[int] = [720, 540, 480]
    target_bytes = limit * _MB * 0.9
    last_kbps = 0
    for short_side in attempts:
        if duration:
            total_kbps = int(target_bytes * 8 / duration / 1000)
            # 极低码率下限：低于 ~80kbps 画面基本不可看，继续降分辨率已无意义。
            if total_kbps < 80:
                break
            video_kbps, audio_kbps = _bitrate_plan(limit, duration)
            rate_args = [
                "-b:v", f"{video_kbps}k",
                "-maxrate", f"{int(video_kbps * 1.5)}k",
                "-bufsize", f"{video_kbps * 2}k",
            ]
        else:
            audio_kbps, video_kbps = 96, 0
            rate_args = ["-crf", "30"]
        last_kbps = video_kbps
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", video_path,
                "-vf", _short_side_filter(short_side),
                "-c:v", "libx264", "-preset", "veryfast", *rate_args,
                "-c:a", "aac", "-b:a", f"{audio_kbps}k", out,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not os.path.exists(out):
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError("ffmpeg 压缩视频失败: " + " | ".join(tail))
        if os.path.getsize(out) <= limit * _MB:
            info = {
                "compressed": True,
                "scale": short_side,
                "kbps": _video_bitrate_kbps(out) or video_kbps,
                "audio_kbps": audio_kbps,
            }
            logger.info("视频内联压缩: %s -> %s (%s)", video_path, out, info)
            return out, info

    actual_mb = os.path.getsize(out) / _MB if os.path.exists(out) else size / _MB
    raise VideoTooLarge(
        f"视频 {actual_mb:.1f}MB 压到下限仍超过内联上限 {limit:g}MB（时长 {duration or 0:.0f}s）。"
        "建议：调大「视频体积上限」，或把「分块阈值」调小让长视频分块理解，"
        "或改用支持更大内联体积的模型。"
    )


def _bitrate_plan(max_mb: float, duration: float) -> tuple[int, int]:
    """按体积上限与时长反算 (视频 kbps, 音频 kbps)。

    体积目标 = 上限 × 0.9（留 10% 给 mp4 容器与索引开销）；
    音频预算 = 总量 × 25%，夹在 24~96 kbps（低于 24k 人声就糊，高于 96k 浪费）；
    其余给视频，且不低于 48 kbps（再低画面完全不可看，不如直接报错）。
    """
    total_kbps = int(float(max_mb) * _MB * 0.9 * 8 / max(duration, 1.0) / 1000)
    audio_kbps = max(24, min(96, int(total_kbps * 0.25)))
    video_kbps = max(48, total_kbps - audio_kbps)
    return video_kbps, audio_kbps


def _cut_chunk(
    video_path: str,
    chunk: "video_chunking.ChunkRange",
    work_dir: str,
    *,
    max_mb: float,
) -> str:
    """切出一块并**直接按内联上限编码**，返回可直接送模型的路径。

    为什么一步到位而不是「先切再压」：早期实现是 `-crf 23` 切块再走一遍
    `_ensure_inline`，一条 300 秒的竖屏块切出 27MB、再压成 13MB——同一段画面
    被编码两次。实测 18 分钟视频切 4 块要跑 375 秒（2 块），几乎全部耗在
    第二次全量转码上。按目标码率一次编码即可，画质由码率决定，不受影响。

    为什么用输出侧 `-t` + **输入侧** `-ss`：`-ss` 放 `-i` 之前会跳到最近的关键帧，
    但 ffmpeg 在转码时默认开着 `-accurate_seek`，会解码并按输出时间丢弃多出的小段，
    切点仍然精确；而 `-ss` 放 `-i` 之后需要从文件开头逐帧解码，最后一块要白白
    解码 855 秒的素材。转码场景下前者又快又准。
    """
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, f"chunk_{chunk.index:02d}.mp4")
    video_kbps, audio_kbps = _bitrate_plan(max_mb, chunk.duration)
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-ss", f"{chunk.start:.3f}", "-i", video_path,
            "-t", f"{chunk.duration:.3f}",
            "-vf", _short_side_filter(720),
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k",
            "-maxrate", f"{int(video_kbps * 1.5)}k",
            "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", f"{audio_kbps}k",
            out,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not os.path.exists(out):
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg 切块失败: " + " | ".join(tail))
    return out


def _chunk_dict(chunk: "video_chunking.ChunkRange") -> dict:
    return {"index": chunk.index, "start": chunk.start, "end": chunk.end}


def _chunk_note(chunk: "video_chunking.ChunkRange", total: int, duration: float) -> str:
    """分块说明。缺了它模型会把自己看到的 5 分钟当成完整视频。"""
    return (
        f"这是原片（总长 {_mmss(duration)}）的第 {chunk.index + 1}/{total} 段，"
        f"对应原片 {_mmss(chunk.start)}-{_mmss(chunk.end)}。"
        "请只依据这一段内容作答；timeline 里的 t 用**本段相对秒数**（从 0 开始），"
        "系统会自动加回原片偏移。不要在 summary 里把这一段当成整条视频。"
    )


def _mmss(value: float) -> str:
    try:
        total = int(round(float(value)))
    except (TypeError, ValueError):
        return "?"
    if total < 0:
        return "?"
    return f"{total // 60}:{total % 60:02d}"


# --------------------------------------------------------------------------- 理解执行


def _shift_moments(moments: list[dict], *, offset: float, start: float, end: float) -> list[dict]:
    """把全局 moments 裁到本块范围内并转成块内相对时间。

    提示词里给的 moments 是原片绝对秒数，而模型看到的块从 0 开始。
    不转换的话模型会认为「第 800 秒」在它看的这 5 分钟里——它看不到，
    于是把客观证据当成幻觉忽略掉。
    """
    out: list[dict] = []
    for m in moments or []:
        try:
            m_start = float(m.get("start", 0))
            m_end = float(m.get("end", 0))
        except (TypeError, ValueError):
            continue
        if m_end <= start or m_start >= end:
            continue
        item = dict(m)
        item["start"] = max(0.0, m_start - offset)
        item["end"] = min(end - start, m_end - offset)
        out.append(item)
    return out


def _merge_usage(parts: list[dict]) -> dict:
    """合并多次调用的 usage（分块理解会调用 N+1 次，账单必须累加）。"""
    total: dict = {}
    for usage in parts:
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            total[key] = total.get(key, 0) + value
    return total


async def _understand_video(
    db: Session,
    *,
    asset_id: int,
    video_path: str,
    work_dir: str,
    max_mb: float,
    chunk_plan: list["video_chunking.ChunkRange"],
    duration: float,
    category: str,
    label: str,
    focus: str,
    dimensions: list[dict],
    transcript: str,
    evidence: str,
    moments: list[dict],
    call_model,
) -> tuple[dict, list[dict], dict]:
    """单次理解或「分块理解 + 汇总」。返回 ``(parsed, timeline, usage)``。

    为什么长视频必须分块：整片压到内联上限时，18 分钟的视频只剩几十 kbps，
    画面完全不可用（详见 CFG-28）。分块后每块 5 分钟、720p，画质可用且总成本可控。

    短视频（未触发分块）走原路径，行为与之前完全一致——分块只在真正需要时增加调用。
    """
    provider = _protocol_provider_label(db, "video_understanding")

    if not chunk_plan:
        inline_path, compression = _ensure_inline(video_path, max_mb, work_dir)
        prompt = build_prompt(
            category, label, focus, dimensions, transcript,
            compression=compression,
            evidence_class=evidence,
            moments=moments,
        )
        result = await vision.understand(
            inline_path, prompt, provider=provider, call_model=call_model
        )
        parsed = vision.parse_structured(result.get("text", ""))
        timeline = parsed.get("timeline") if isinstance(parsed.get("timeline"), list) else []
        parsed["_model"] = result.get("model", "")
        return parsed, timeline, result.get("usage") or {}

    total = len(chunk_plan)
    parts: list[dict] = []
    timelines: list[list[dict]] = []
    usages: list[dict] = []
    models: list[str] = []

    for chunk in chunk_plan:
        # 切块时已按上限编码，画质信息就是实际码率——不再走第二遍压缩。
        inline_path = _cut_chunk(video_path, chunk, work_dir, max_mb=max_mb)
        video_kbps, audio_kbps = _bitrate_plan(max_mb, chunk.duration)
        compression = {
            "compressed": True,
            "scale": 720,
            "kbps": video_kbps,
            "audio_kbps": audio_kbps,
        }
        prompt = build_prompt(
            category, label, focus, dimensions, transcript,
            compression=compression,
            evidence_class=evidence,
            chunk_note=_chunk_note(chunk, total, duration),
            moments=_shift_moments(moments, offset=chunk.start, start=chunk.start, end=chunk.end),
        )
        result = await vision.understand(
            inline_path, prompt, provider=provider, call_model=call_model
        )
        parsed = vision.parse_structured(result.get("text", ""))
        timelines.append(
            video_chunking.shift_timeline(parsed.get("timeline"), chunk.start)
        )
        parts.append(
            {
                "index": chunk.index,
                "start": chunk.start,
                "end": chunk.end,
                "summary": parsed.get("summary") if isinstance(parsed.get("summary"), str) else "",
                "dimensions": parsed.get("dimensions") if isinstance(parsed.get("dimensions"), dict) else {},
                "confidence": _coerce_conf(parsed.get("confidence")),
            }
        )
        usages.append(result.get("usage") or {})
        if result.get("model"):
            models.append(result["model"])
        # 切块是临时文件，用完即删——长视频每块几十 MB，堆着会把磁盘吃满。
        try:
            os.remove(inline_path)
        except OSError:
            pass

    merged_timeline = video_chunking.merge_timelines(timelines)
    rollup, rollup_usage = await _rollup(
        parts, dimensions=dimensions, duration=duration, call_model=call_model
    )
    usages.append(rollup_usage)

    parsed = {
        "type": category,
        "summary": rollup.get("summary") or "",
        "dimensions": rollup.get("dimensions") or {},
        "confidence": rollup.get("confidence"),
        "_model": " + ".join(dict.fromkeys(models)) if models else "",
        "_chunk_parts": [
            {"index": p["index"], "start": p["start"], "end": p["end"], "summary": p["summary"]}
            for p in parts
        ],
    }
    return parsed, merged_timeline, _merge_usage(usages)


async def _rollup(
    parts: list[dict],
    *,
    dimensions: list[dict],
    duration: float,
    call_model,
) -> tuple[dict, dict]:
    """把各块结论汇总成整片结论。

    汇总失败**不致命**：降级为「各块摘要按时间拼接 + 维度取第一块的非空值」，
    因为理解本身已经跑完花钱了，不该因为最后一次文本调用失败而整条重跑。
    """
    prompt = understand_rollup.build_rollup_prompt(parts, dimensions=dimensions, duration_sec=duration)
    body = {
        "messages": [
            {"role": "system", "content": understand_rollup.ROLLUP_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        result = await call_model(
            request=body,
            parse_result=lambda raw: understand_rollup.parse_rollup(raw),
            stage="text_analysis",
        )
        data = result.data if hasattr(result, "data") else result
        usage = getattr(result, "usage", {}) or {}
        if isinstance(data, dict) and (data.get("summary") or data.get("dimensions")):
            return data, usage
        logger.warning("分块汇总返回为空，降级为拼接式汇总")
    except Exception as exc:  # noqa: BLE001 汇总失败不毁链路
        logger.warning("分块汇总失败，降级为拼接式汇总: %s", exc)

    dims: dict = {}
    for part in parts:
        for key, value in (part.get("dimensions") or {}).items():
            if key not in dims and value not in (None, ""):
                dims[key] = value
    summaries = [p.get("summary") for p in parts if p.get("summary")]
    return (
        {
            "summary": " ".join(summaries)[:300],
            "dimensions": dims,
            "confidence": min((p.get("confidence") or 0) for p in parts) if parts else 0,
        },
        {},
    )


# --------------------------------------------------------------------------- 组合服务


def _transcript_text(db: Session, asset_id: int) -> str:
    tr = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    return tr.full_text if tr and tr.full_text else ""


async def _prefiltered_transcript(
    db: Session, asset_id: int, s: dict, call_model
) -> tuple[str, dict]:
    """按开关跑两级漏斗，返回 ``(要送进提示词的转录, 漏斗统计)``。

    漏斗是**可选的省钱手段**，默认关闭：它需要一个额外的（通常是本地的）小模型端点。
    任何失败都回退全文，绝不因此让理解或拆解失败。
    """
    if not s.get("prefilter_enabled", False):
        return _transcript_text(db, asset_id), {"enabled": False}

    tr = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    segments = tr.segments if tr is not None and isinstance(tr.segments, list) else []
    if not segments:
        return _transcript_text(db, asset_id), {"enabled": False, "fallback_reason": "无分段"}

    async def llm(system: str, body: str) -> str:
        result = await call_model(
            request={
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": body},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "timeout": float(s.get("prefilter_timeout_sec", 120) or 120),
            },
            parse_result=lambda raw: raw,
            stage="prefilter",
        )
        data = result.data
        if isinstance(data, str):
            return data
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(data)

    kept, stats = await prefilter.select_segments_async(
        segments,
        llm=llm,
        min_chars=int(s.get("prefilter_min_chars", 3000) or 3000),
    )
    text = "\n".join(
        str(seg.get("text") or "").strip() for seg in kept if str(seg.get("text") or "").strip()
    )
    info = stats.as_dict()
    if not text.strip():
        # 反常情况（窗口选空）也回退全文——宁可多花钱，也不能让模型看到空稿。
        return _transcript_text(db, asset_id), {**info, "fallback_reason": "初筛结果为空，回退全文"}
    return text, info


def _protocol_provider_label(db: Session, work_type: str) -> str:
    """兼容视觉模块的 provider 入参；真实供应商/协议仍由网关解析。"""
    from app.services.model_access_service import resolved_transport

    return "gemini" if resolved_transport(db, work_type) == "gemini_inline" else "qwen"


async def run_understand(
    db: Session,
    asset_id: int,
    *,
    force_category: str | None = None,
    use_cover: bool | None = None,
    job_id: int | None = None,
) -> VideoUnderstanding:
    """跑完整高级档链路，返回并落库 `VideoUnderstanding`。

    Raises:
        UnderstandInputError: 素材不存在 / 无关联内容（数据问题，不可重试）。
        Exception: 理解链路任何失败——落 `status=error` 后原样抛出（worker 负责重试）。
    """
    asset = db.get(MediaAsset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise UnderstandInputError(f"素材不存在: {asset_id}")

    transcript = db.scalar(select(Transcript).where(Transcript.asset_id == asset_id))
    if transcript is None or not (transcript.full_text or "").strip():
        _record_understand_error(db, asset_id, "视频理解前置未完成：需要先完成转录")
        raise UnderstandInputError("视频理解前置未完成：需要先完成转录")

    asset.understand_status = "doing"
    events.publish("asset_status", {"asset_id": asset_id, "understand": "doing"}, db=db)
    db.commit()

    try:
        asset = db.get(MediaAsset, asset_id)
        content = db.get(ContentItem, asset.content_id)
        if content is None:  # 数据不一致（孤儿资产）：显式报错，不靠兜底 catch 静默成 other
            raise UnderstandInputError(f"素材 #{asset_id} 未关联内容（content_id={asset.content_id}）")
        s = get_process_settings(db)
        from app.services.model_access_service import ModelAccessService

        gateway = ModelAccessService()
        routed: dict[str, dict] = {}

        async def call_model(*, request, parse_result, video_request=False, stage=None, **kwargs):
            # stage 显式指定时以它为准：分块汇总是纯文本调用，走 text_analysis，
            # 不能因为「不是视频请求」就被错记成封面判类。
            stage_key = stage or ("video_understanding" if video_request else "cover_classify")
            if stage == "prefilter":
                operation = "prefilter"
            elif stage == "text_analysis":
                operation = "understand_rollup"
            else:
                operation = "understand_video" if video_request else "classify"
            try:
                result = await gateway.call_with_fallback(
                    db,
                    work_type=stage_key,
                    operation=operation,
                    request=request,
                    parse_result=parse_result,
                    validate_result=kwargs.get("validate_result"),
                    asset_id=asset_id,
                    job_id=kwargs.get("job_id") or job_id,
                    image_required=bool(kwargs.get("image_required")),
                    video_request=video_request,
                )
            except Exception as exc:  # noqa: BLE001 失败也要保留 routing，供结果页解释
                if hasattr(exc, "to_routing"):
                    failure = exc.to_routing()
                    if isinstance(failure, dict):
                        routed[stage_key] = failure
                raise
            routed[stage_key] = result.to_routing()
            return result

        transcript = _transcript_text(db, asset_id)
        # 漏斗只影响「送进提示词的转录范围」；判类仍用全文，否则类型判断会被
        # 一个有偏的片段带偏。统计落库便于复核它到底省了多少。
        funnel: dict = {"enabled": False}

        # ① 判类（可强制类型 / 可关闭）；开启封面时把封面下载到本地再送入分类器
        want_cover = s["use_cover"] if use_cover is None else use_cover
        if force_category:
            # 人工指定类型：置信度记 1.0，理由注明来源，跳过判类。
            category, conf, reason = force_category, 1.0, "人工指定类型"
        elif s.get("classify_enabled", True):
            cover_path = None
            if want_cover:
                work_dir = os.path.join(resolve_asset_dir(db, asset), "_cover")
                cover_path = await _ensure_cover(content, work_dir, float(s.get("cover_max_mb", 5)))
            category, conf, reason = await _classify_category(
                db,
                content,
                transcript,
                use_cover=want_cover,
                cover_path=cover_path,
                call_model=call_model,
            )
        else:
            category, conf, reason = "other", 0.0, "判类已关闭，使用通用模板"

        # 低置信度标记（设计 §4：低于阈值 → low_confidence 供人工复核）
        threshold = float(s.get("low_confidence_threshold", 0.6))
        low_confidence = bool(conf < threshold)

        transcript_for_prompt, funnel = await _prefiltered_transcript(db, asset_id, s, call_model)

        # ② 选提示词（未知类型归一 other）
        category, label, focus, dimensions = _select_prompt(db, category)
        # 品类路由：不同品类的爆点证据几乎是反的（话 / 反应 / 画面），
        # 用一套提示词打天下会逼模型在「没有台词」的素材上硬凑结构。
        evidence = evidence_class_of(category)

        video_path = _asset_video_path(db, asset)
        duration = float(content.duration_sec or 0)

        # ③ 视听信号（纯本地 ffmpeg，成本低）。采集失败不影响理解主链路。
        signals = video_signals.MediaSignals()
        moments: list[dict] = []
        if s.get("signals_enabled", True):
            try:
                signals = video_signals.collect_signals(video_path, duration=duration)
                if not duration:
                    duration = signals.duration
                moments = video_signals.fuse_moments(
                    signals, evidence_class=evidence, window=30.0, limit=8
                )
                # 信号是纯本地计算出来的客观证据，`fuse_moments` 的评分方向与
                # 模型评分无关；这里只做「告诉模型哪几段是高能区」，不参与排序决策。
            except Exception as exc:  # noqa: BLE001 信号是增强项
                logger.warning("视听信号采集失败（不影响理解）: %s", exc)

        # ④ 视频理解：长视频分块，短视频单次
        work_dir = os.path.join(os.path.dirname(video_path), "_inline")
        max_mb = float(s.get("video_max_mb", 15))
        threshold = int(s.get("chunk_threshold_sec", 600) or 0)
        chunk_plan: list[video_chunking.ChunkRange] = []
        if video_chunking.needs_chunking(duration, threshold_seconds=threshold):
            chunk_plan = video_chunking.plan_chunks(
                duration,
                target_seconds=int(s.get("chunk_seconds", 300) or 300),
                overlap_seconds=int(s.get("chunk_overlap_sec", 15) or 0),
            )

        parsed, timeline, usage = await _understand_video(
            db,
            asset_id=asset_id,
            video_path=video_path,
            work_dir=work_dir,
            max_mb=max_mb,
            chunk_plan=chunk_plan,
            duration=duration,
            category=category,
            label=label,
            focus=focus,
            dimensions=dimensions,
            transcript=transcript_for_prompt,
            evidence=evidence,
            moments=moments,
            call_model=call_model,
        )
        summary = parsed.get("summary") if isinstance(parsed.get("summary"), str) else ""
        dims = parsed.get("dimensions") if isinstance(parsed.get("dimensions"), dict) else {}
        dims = {str(k): v for k, v in dims.items()}
        # 提示词要求模型给出 confidence。之前解析完就丢掉了，导致拆解无法判断
        # 「这段画面描述是看清了还是猜的」，只能一律当事实引用。
        understanding_confidence = _coerce_conf(parsed.get("confidence"))

        existing = db.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id))
        if existing is None:
            existing = VideoUnderstanding(asset_id=asset_id)
            db.add(existing)
        existing.category = category
        existing.category_confidence = conf
        existing.category_reason = reason[:512]
        existing.low_confidence = low_confidence
        existing.understanding_confidence = understanding_confidence
        existing.evidence_class = evidence
        existing.signals = signals.as_dict() if signals.duration else None
        existing.moments = moments or None
        existing.chunk_count = len(chunk_plan) or 1
        existing.chunk_plan = [_chunk_dict(c) for c in chunk_plan] or None
        if funnel.get("enabled"):
            existing.routing = {**existing.routing, "prefilter": funnel}
        existing.dimensions = dims
        existing.summary = summary
        existing.timeline = timeline
        existing.model = parsed.get("_model") or ""
        existing.prompt_version = f"{_PROMPT_VERSION}:{category}"
        existing.usage = usage
        existing.routing = {
            key: value for key, value in routed.items() if isinstance(value, dict)
        }
        existing.status = "done"
        existing.error = ""

        content.auto_category = category
        content.category_confidence = conf
        asset.understand_status = "done"
        asset.process_tier = "advanced"
        # 注意：不要写 asset.error。media_asset.error 是**共享**的下载/整体错误位，
        # 视频理解失败时写进去会让「已下载成功」的素材在前端显示成失败（实测 bug）。
        # 理解自身的错误只落 VideoUnderstanding.error。
        events.publish("asset_status", {"asset_id": asset_id, "understand": "done"}, db=db)
        db.commit()
        db.refresh(existing)
        return existing
    except Exception as exc:
        db.rollback()
        _record_understand_error(db, asset_id, str(exc))
        events.publish("asset_status", {"asset_id": asset_id, "understand": "error"}, db=db)
        db.commit()
        raise


def _record_understand_error(db: Session, asset_id: int, message: str) -> None:
    """把理解失败落到理解自己的记录上，绝不污染 media_asset.error。"""
    asset = db.get(MediaAsset, asset_id)
    if asset is None:
        return
    asset.understand_status = "error"
    row = db.scalar(select(VideoUnderstanding).where(VideoUnderstanding.asset_id == asset_id))
    if row is None:
        row = VideoUnderstanding(asset_id=asset_id)
        db.add(row)
    row.status = "error"
    row.error = message[:512]
    db.commit()
