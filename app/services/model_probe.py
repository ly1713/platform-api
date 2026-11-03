"""模型能力探测：文本 JSON、图片真看、视频真看与手工覆盖。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import shutil
import subprocess
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.crypto import decrypt_credential, redact_text
from app.models import AiModel, AiProvider, ModelAssignment
from app.modules import model_protocols

logger = logging.getLogger("platform.model_probe")

# v2：视频探针从「2 秒纯色块」升级为「三帧彩色 + 画面文字序列」。
# v1 只能证明「模型返回了红蓝」，纯图片模型、只读首帧的模型、甚至不看视频
# 只猜颜色的模型都能蒙混过关；而拆解真正需要的是「按时间顺序读到画面里的
# 内容」。存量的 v1 结论因此不再可信，见 `capabilities_of` 的版本判定。
_PROBE_VERSION = "v2"

# 探针视频的三种颜色，顺序即时间顺序。
_VIDEO_SEQUENCE = (
    ("red", (220, 30, 30)),
    ("blue", (30, 60, 220)),
    ("green", (30, 170, 80)),
)

_IMAGE_COLORS = {
    "red": (220, 30, 30),
    "blue": (30, 60, 220),
    "green": (30, 170, 80),
}

# Vision probes ask for a color; Chinese-capable models often answer 红色/蓝色/绿色
# even when instructed to use English. Accept both spellings instead of treating a
# correct visual answer as "model did not read the image".
_COLOR_ALIASES = {
    "red": "red", "红色": "red", "红": "red",
    "blue": "blue", "蓝色": "blue", "蓝": "blue",
    "green": "green", "绿色": "green", "绿": "green",
}


def _normalize_color(value: object) -> str:
    """Map an English or Chinese color answer onto red/blue/green."""
    text = str(value or "").strip().lower()
    return _COLOR_ALIASES.get(text, _COLOR_ALIASES.get(text.rstrip("色"), ""))


def _decrypt_key(provider: AiProvider) -> str:
    if not provider.api_key_encrypted:
        return ""
    return str(decrypt_credential(provider.api_key_encrypted).get("api_key") or "")


def _choose_probe_color() -> str:
    return random.choice(list(_IMAGE_COLORS))


def _red_image(color: str | None = None) -> bytes:
    """生成探测图片；默认红色，颜色可由探针运行时随机指定。"""

    from PIL import Image

    rgb = _IMAGE_COLORS.get(color or "red", _IMAGE_COLORS["red"])
    image = Image.new("RGB", (16, 16), rgb)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _probe_token() -> str:
    """随机 4 位大写字母数字：防止模型靠常识或历史会话猜出画面文字。"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(alphabet) for _ in range(4))


def _frame_png(rgb: tuple[int, int, int], token: str) -> bytes:
    """单帧：纯色底 + 大号白字 token。字号够大才能在压缩后仍可辨认。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (640, 360), rgb)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=96)
    except TypeError:  # 老版本 Pillow 没有 size 参数
        font = ImageFont.load_default()
    # 居中：不依赖字体度量，左右各留等宽，视觉上足够正中。
    draw.text((212, 128), token, font=font, fill=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _sequence_video(work_dir: str | Path, token: str, *, seconds: int = 3) -> bytes:
    """生成「红 → 蓝 → 绿」三段、每段都写着同一个 token 的测试视频。

    为什么是序列而不是单帧：单帧图像 + 纯色块无法区分「真的按时间读视频」和
    「只看了一张缩略图」；拆解要的正是时间顺序上的画面变化，探针必须能证伪。
    """
    out = Path(work_dir) / "probe.mp4"
    frames_dir = Path(work_dir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    concat_lines: list[str] = []
    for index, (_name, rgb) in enumerate(_VIDEO_SEQUENCE):
        frame = frames_dir / f"{index}.png"
        frame.write_bytes(_frame_png(rgb, token))
        concat_lines.append(f"file '{frame}'")
        concat_lines.append("duration 1")
    # concat demuxer 需要最后一条重复，否则末段时长会被吞掉。
    last_frame = frames_dir / f"{len(_VIDEO_SEQUENCE) - 1}.png"
    concat_lines.append(f"file '{last_frame}'")
    playlist = frames_dir / "list.txt"
    playlist.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")

    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(playlist),
            "-vf",
            "fps=10,format=yuv420p",
            "-c:v",
            "libx264",
            "-t",
            str(seconds),
            "-movflags",
            "+faststart",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not out.exists():
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"ffmpeg 生成视频探针失败: {tail}")
    return out.read_bytes()


def _parse_json_text(text: str) -> object:
    raw = (text or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


async def _chat(
    provider, model: str, *, request: dict, api_key: str, timeout: float, protocol: str | None = None
) -> dict:
    effective = protocol or model_protocols.resolve_protocol(None, provider)
    built = model_protocols.chat_request(
        provider, model, request, api_key=api_key, protocol=effective
    )
    data = await model_protocols.send_request(built, timeout=timeout)
    return model_protocols.parse_chat_response(effective, data)


async def _video_chat(
    provider,
    model: str,
    video_b64: str,
    prompt: str,
    *,
    transport: str,
    api_key: str,
    timeout: float,
    protocol: str | None = None,
) -> dict:
    built = model_protocols.video_request(
        provider,
        model,
        video_b64,
        prompt,
        transport=transport,
        api_key=api_key,
        protocol=protocol,
    )
    data = await model_protocols.send_request(built, timeout=timeout)
    # video transport 与对话协议可能不同；响应按 transport 对应协议解析。
    response_protocol = "gemini" if transport == "gemini_inline" else "openai"
    return model_protocols.parse_chat_response(response_protocol, data)


def _capability(
    *,
    value: bool,
    source: str,
    revision: int,
    protocol: str,
    detail: str,
    extra: dict | None = None,
) -> dict:
    out = {
        "value": bool(value),
        "source": source,
        "config_revision": revision,
        # 生效协议也写进能力记录：协议变了旧结论即失效（capabilities_of 比对）。
        "protocol": protocol,
        "probe_version": _PROBE_VERSION,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "detail": detail,
    }
    if extra:
        out.update(extra)
    return out


async def _probe_json_mode_with(
    provider,
    model: AiModel,
    api_key: str,
    prompt: str,
    timeout: float,
    *,
    protocol: str,
) -> tuple[dict, str]:
    """按指定协议完成一次 JSON 能力探测；必要时退回 prompt-only JSON 模式。"""
    response_format = {"type": "json_object"}
    try:
        result = await _chat(
            provider,
            model.model_key,
            request={
                "messages": [
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "response_format": response_format,
            },
            api_key=api_key,
            timeout=timeout,
            protocol=protocol,
        )
        parsed = _parse_json_text(result["text"])
        if not isinstance(parsed, dict):
            raise model_protocols.SchemaMismatchError("探测返回不是 JSON 对象")
        return parsed, "json_mode"
    except model_protocols.ModelProtocolError:
        raise
    except Exception as first:
        if model_protocols.classify_error(first) != "bad_request":
            raise
        result = await _chat(
            provider,
            model.model_key,
            request={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            api_key=api_key,
            timeout=timeout,
            protocol=protocol,
        )
        parsed = _parse_json_text(result["text"])
        if not isinstance(parsed, dict):
            raise model_protocols.SchemaMismatchError("探测返回不是 JSON 对象")
        return parsed, "prompt_only"


_PROBE_CANDIDATE_ORDER = ("openai", "anthropic", "gemini")


def _protocol_candidates(current: str) -> list[str]:
    """候选协议顺序：先试当前生效协议，再试其余两族（去重，保持稳定顺序）。"""
    ordered = [current] + [p for p in _PROBE_CANDIDATE_ORDER if p != current]
    seen: list[str] = []
    for name in ordered:
        if name and name not in seen:
            seen.append(name)
    return seen


async def _detect_protocol(
    provider, model: AiModel, api_key: str, *, text: bool, timeout: float
) -> tuple[str, str, dict | None, str]:
    """判定并可能固化模型协议。

    返回 ``(生效协议, 说明, 文本探测结果, 探测模式)``；文本探测结果用于复用，
    避免判定协议与记录「文字/JSON」能力各打一次上游：
    - ``manual``：用户已指定，直接用，不试其他；
    - 自动：按候选顺序实测，第一个能从文本探测拿到合格 JSON 的胜出并固化为 ``auto``；
      若胜出者恰好是继承协议，则保持 ``inherit``（跟随供应商，不 pin）；
    - 全部失败：保持继承协议（能力在后续子探测里照常记为失败）。
    """
    inherited = model_protocols.resolve_protocol(None, provider)
    source = str(getattr(model, "protocol_source", "") or "inherit")

    if source == "manual" and getattr(model, "protocol", None):
        manual = model_protocols.validate_protocol(model.protocol)
        return manual, "用户指定协议", None, ""

    if not text:
        # 未做文本探测（例如只测视频）时无法判定，沿用继承协议。
        return inherited, "未做文本探测，沿用供应商协议", None, ""

    prompt = '只输出 JSON：{"ok": true, "echo": "workbench"}'
    last_error = ""
    for candidate in _protocol_candidates(inherited):
        try:
            parsed, mode = await _probe_json_mode_with(
                provider, model, api_key, prompt, timeout, protocol=candidate
            )
        except Exception as exc:  # noqa: BLE001 试错协议：一个失败就换下一个
            last_error = redact_text(str(exc), extra_secrets=(api_key,))[:200]
            continue
        if parsed.get("ok") is True and parsed.get("echo") == "workbench":
            if candidate == inherited:
                # 继承协议即可用：不 pin，跟随供应商后续变化。
                model.protocol = None
                model.protocol_source = "inherit"
                return candidate, "沿用供应商协议", parsed, mode
            model.protocol = candidate
            model.protocol_source = "auto"
            return candidate, f"自动判定为 {candidate}", parsed, mode

    if last_error:
        logger.info("协议自动判定未成功，沿用供应商协议 %s：%s", inherited, last_error)
    return inherited, "未能自动判定，沿用供应商协议", None, ""


async def probe_model(
    db: Session,
    model_id: int,
    *,
    text: bool = True,
    image: bool = True,
    video: bool = True,
) -> AiModel:
    """探测模型能力并写回 ``ai_model``；单项失败不影响其他项。

    协议默认自动判定：手动的（``protocol_source=manual``）直接用；否则按候选顺序
    实测挑第一个能返回合格 JSON 的协议并固化（``auto``）。探测得出的协议一致用于
    图片/视频子探测，避免「文本通了、看图却按另一个协议发」。
    """
    model = db.get(AiModel, model_id)
    if model is None:
        raise ValueError(f"模型不存在: {model_id}")
    provider = db.get(AiProvider, model.provider_id)
    if provider is None:
        model.probe_status = "failed"
        model.probe_error = "供应商不存在，无法探测"
        model.probe_started_at = None
        db.commit()
        raise ValueError(f"模型所属供应商不存在: {model.provider_id}")
    api_key = _decrypt_key(provider)
    if not api_key:
        model.probe_status = "failed"
        model.probe_error = "供应商没有可用访问密钥，无法探测"
        model.probe_started_at = None
        db.commit()
        raise ValueError("供应商没有可用访问密钥，无法探测")

    revision = int(provider.config_revision or 1)
    caps = dict(model.capabilities or {})
    statuses: list[str] = []
    # 先定协议：文本探测既产出能力，也用来判定协议。
    effective_protocol, protocol_detail, text_probe, text_mode = await _detect_protocol(
        provider, model, api_key, text=text, timeout=30
    )
    cap_extra = {"protocol_detail": protocol_detail}

    if text:
        if text_probe is not None:
            # 协议判定阶段已经打过一次文本探测，直接复用，不重复计费。
            ok = True
            detail = f"通过文本与 JSON 输出探测（{protocol_detail}）"
            caps["text"] = _capability(
                value=True, source="probe", revision=revision,
                protocol=effective_protocol, detail=detail, extra=cap_extra,
            )
            caps["json"] = _capability(
                value=True, source="probe", revision=revision,
                protocol=effective_protocol,
                detail=f"JSON 输出可用（{text_mode}）",
                extra={"mode": text_mode, **cap_extra},
            )
            statuses.append("ok")
        else:
            prompt = '只输出 JSON：{"ok": true, "echo": "workbench"}'
            try:
                parsed, mode = await _probe_json_mode_with(
                    provider, model, api_key, prompt, 30, protocol=effective_protocol
                )
                ok = parsed.get("ok") is True and parsed.get("echo") == "workbench"
                detail = "通过文本与 JSON 输出探测" if ok else "JSON 内容不符合要求"
                caps["text"] = _capability(
                    value=ok, source="probe", revision=revision,
                    protocol=effective_protocol, detail=detail, extra=cap_extra,
                )
                caps["json"] = _capability(
                    value=ok, source="probe", revision=revision,
                    protocol=effective_protocol,
                    detail=f"JSON 输出{'可用' if ok else '不符合要求'}（{mode}）",
                    extra={"mode": mode, **cap_extra},
                )
                statuses.append("ok" if ok else "failed")
            except Exception as exc:  # noqa: BLE001 单项失败不能中断其他探测
                detail = redact_text(str(exc), extra_secrets=(api_key,))[:512]
                caps["text"] = _capability(
                    value=False, source="probe", revision=revision,
                    protocol=effective_protocol, detail=detail,
                )
                caps["json"] = _capability(
                    value=False, source="probe", revision=revision,
                    protocol=effective_protocol, detail=detail,
                )
                statuses.append("failed")

    if image:
        color = _choose_probe_color()
        try:
            image_bytes = _red_image(color)
            import base64

            data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
            result = await _chat(
                provider,
                model.model_key,
                request={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": '看图后只输出 JSON：{"has_image": true|false, "color": "颜色"}。只依据图片内容。',
                                },
                                {"type": "image_url", "image_url": {"url": data_uri}},
                            ],
                        }
                    ],
                    "temperature": 0,
                },
                api_key=api_key,
                timeout=45,
                protocol=effective_protocol,
            )
            parsed = _parse_json_text(result["text"])
            answer = _normalize_color((parsed or {}).get("color"))
            ok = bool((parsed or {}).get("has_image")) and answer == color
            detail = "通过图片颜色探测" if ok else "模型未正确识别图片"
            caps["image"] = _capability(
                value=ok, source="probe", revision=revision,
                protocol=effective_protocol, detail=detail, extra=cap_extra,
            )
            statuses.append("ok" if ok else "failed")
        except Exception as exc:  # noqa: BLE001
            caps["image"] = _capability(
                value=False,
                source="probe",
                revision=revision,
                protocol=effective_protocol,
                detail=redact_text(str(exc), extra_secrets=(api_key,))[:512],
            )
            statuses.append("failed")

    if video:
        transport = model_protocols.normalize_transport(effective_protocol, None)
        video_extra = {
            "transport": transport,
            "auth_style": model_protocols.auth_style_for_transport(transport),
            **cap_extra,
        }
        if shutil.which("ffmpeg") is None:
            caps["video"] = _capability(
                value=False,
                source="probe",
                revision=revision,
                protocol=effective_protocol,
                detail="未安装 ffmpeg，无法生成视频探针",
                extra=video_extra,
            )
            statuses.append("skipped")
        else:
            try:
                import base64

                token = _probe_token()
                with tempfile.TemporaryDirectory(prefix="model-probe-") as work_dir:
                    video_bytes = await asyncio.to_thread(_sequence_video, work_dir, token)
                video_b64 = base64.b64encode(video_bytes).decode("ascii")
                result = await _video_chat(
                    provider,
                    model.model_key,
                    video_b64,
                    "这是一段按时间顺序播放的视频。只依据画面内容输出 JSON："
                    '{"colors": ["三个颜色按出现顺序"], "text": "画面里出现的文字"}。'
                    "不要猜，看不清就填 null。",
                    transport=transport,
                    api_key=api_key,
                    timeout=300,
                    protocol=effective_protocol,
                )
                parsed = _parse_json_text(result["text"]) or {}
                colors = [_normalize_color(c) for c in (parsed.get("colors") or [])]
                expected = [name for name, _rgb in _VIDEO_SEQUENCE]
                # 只认前缀匹配：多报比少报好，但顺序必须对。
                order_ok = colors[: len(expected)] == expected
                text_ok = str(parsed.get("text") or "").strip().upper().find(token) >= 0
                ok = order_ok and text_ok
                if ok:
                    detail = "通过视频序列探测（三色顺序 + 画面文字均正确）"
                elif not order_ok:
                    detail = (
                        "模型未按时间顺序读到画面：期望 "
                        + "/".join(expected)
                        + f"，实际 {colors or '空'}"
                    )
                else:
                    detail = "模型读到了颜色但没读到画面文字：该模型可能只做低分辨率抽帧或转述字幕"
                caps["video"] = _capability(
                    value=ok,
                    source="probe",
                    revision=revision,
                    protocol=effective_protocol,
                    detail=detail,
                    extra={
                        **video_extra,
                        # 拆解用的正是「时间顺序 + 画面文字」，把这两项单独记账，
                        # 失败时页面能直接告诉用户是哪一项不达标。
                        "sequence_ok": order_ok,
                        "text_ok": text_ok,
                    },
                )
                statuses.append("ok" if ok else "failed")
            except Exception as exc:  # noqa: BLE001
                caps["video"] = _capability(
                    value=False,
                    source="probe",
                    revision=revision,
                    protocol=effective_protocol,
                    detail=redact_text(str(exc), extra_secrets=(api_key,))[:512],
                    extra=video_extra,
                )
                statuses.append("failed")

    model.capabilities = caps
    if "ok" in statuses and ("failed" in statuses or "skipped" in statuses):
        model.probe_status = "partial"
    elif "ok" in statuses:
        model.probe_status = "passed"
    elif "skipped" in statuses and all(s == "skipped" for s in statuses):
        model.probe_status = "skipped"
    else:
        model.probe_status = "failed"
    model.probe_error = "; ".join(
        redact_text(str((caps.get(k) or {}).get("detail", "")), extra_secrets=(api_key,))
        for k in ("text", "json", "image", "video")
        if (caps.get(k) or {}).get("value") is False
    )[:2000]
    model.probe_started_at = None
    model.probed_at = datetime.utcnow()
    # Only a successful real probe of the legacy model clears the pending flag.
    # A failed/partial probe must keep the warning visible until the gap is fixed
    # or the user explicitly overrides the capability.
    if model.probe_status in ("passed", "partial"):
        for assignment in db.scalars(
            select(ModelAssignment).where(ModelAssignment.primary_model_id == model.id)
        ).all():
            assignment.legacy_pending_validation = False
    db.commit()
    db.refresh(model)
    return model


async def set_manual_video_capability(
    db: Session,
    model_id: int,
    *,
    value: bool,
    transport: str | None = None,
    reason: str = "",
) -> AiModel:
    """用户手工确认视频能力；来源固定为 manual_override。"""
    model = db.get(AiModel, model_id)
    if model is None:
        raise ValueError(f"模型不存在: {model_id}")
    provider = db.get(AiProvider, model.provider_id)
    if provider is None:
        raise ValueError(f"模型所属供应商不存在: {model.provider_id}")
    effective_protocol = model_protocols.resolve_protocol(model, provider)
    normalized = model_protocols.normalize_transport(effective_protocol, transport)
    caps = dict(model.capabilities or {})
    caps["video"] = _capability(
        value=bool(value),
        source="manual_override",
        revision=int(provider.config_revision or 1),
        protocol=effective_protocol,
        detail=reason or ("用户手工确认支持视频" if value else "用户手工确认不支持视频"),
        extra={
            "transport": normalized,
            "auth_style": model_protocols.auth_style_for_transport(normalized),
            "reason": reason,
        },
    )
    caps["manual_override"] = {"video": bool(value), "reason": reason}
    model.capabilities = caps
    for assignment in db.scalars(
        select(ModelAssignment).where(ModelAssignment.primary_model_id == model.id)
    ).all():
        assignment.legacy_pending_validation = False
    db.commit()
    db.refresh(model)
    return model
