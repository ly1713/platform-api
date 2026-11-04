"""faster-whisper 转录封装。

红线：faster-whisper 不直接暴露为 API，只能经队列 + worker 调用（本模块由
workers/handlers.py 的 handle_transcribe 调用）。模型按需懒加载并进程内缓存，
避免每个任务重复加载权重。

依赖：faster-whisper（pyproject 主依赖）+ ffmpeg（PATH）。CPU 默认 int8 量化。
"""

import asyncio
import logging
import subprocess
from pathlib import Path

from app.config import settings

logger = logging.getLogger("platform.transcription")

_model = None  # 进程内缓存的 WhisperModel
_model_key: tuple | None = None


def _resolve_device(device: str) -> tuple[str, str]:
    """auto 时探测有无 CUDA，返回 (device, compute_type)。"""
    if device != "auto":
        return device, settings.whisper_compute_type
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _load_model(model_name: str, device: str):
    global _model, _model_key
    dev, compute = _resolve_device(device)
    key = (model_name, dev, compute)
    if _model is not None and _model_key == key:
        return _model
    from faster_whisper import WhisperModel

    logger.info("加载 whisper 模型 %s (device=%s compute=%s)", model_name, dev, compute)
    _model = WhisperModel(model_name, device=dev, compute_type=compute)
    _model_key = key
    return _model


def _decode_audio(media_path: str) -> "np.ndarray":
    """用 ffmpeg 把音轨解成 16kHz 单声道 float32 数组，直接喂给 faster-whisper。

    为什么要自己解：faster-whisper 1.2.1 的 ``decode_audio`` 调
    ``av.open(..., metadata_errors="ignore")``，这个参数在当前安装的 PyAV 19
    里已被移除，任何路径（连 wav 文件）都会抛
    ``TypeError: open() got an unexpected keyword argument 'metadata_errors'``——
    **所有转录都会失败**。``WhisperModel.transcribe`` 只有收到 numpy 数组时
    才跳过 ``decode_audio``，所以这里直接把音频解成数组。

    16kHz 单声道是 Whisper 的原生采样率；用 s16le 读出再除以 32768 得到
    float32，与库内部 ``decode_audio`` 的转换方式一致。
    """
    import numpy as np

    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", media_path,
            "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-acodec", "pcm_s16le",
            "-",
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or b"ffmpeg failed").decode("utf-8", "replace").strip().splitlines()[-2:]
        raise RuntimeError("解码音频失败: " + " | ".join(tail))
    audio = np.frombuffer(proc.stdout, dtype=np.int16)
    if audio.size == 0:
        raise RuntimeError("解码音频失败: ffmpeg 没有输出音频数据（该文件可能没有音轨）")
    return audio.astype(np.float32) / 32768.0


def _transcribe_sync(media_path: str, options: dict) -> dict:
    if not Path(media_path).exists():
        raise FileNotFoundError(f"待转录文件不存在: {media_path}")
    model_name = options.get("model") or settings.whisper_model
    device = options.get("device") or settings.whisper_device
    model = _load_model(model_name, device)
    return _run_model(model, model_name, _decode_audio(media_path), options)


def _run_model(model, model_name: str, audio, options: dict) -> dict:
    segments, info = model.transcribe(
        audio,
        language=options.get("language"),
        word_timestamps=options.get("word_timestamps", False),
        vad_filter=options.get("vad_filter", True),
    )
    seg_list = []
    texts = []
    for seg in segments:
        seg_list.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
        texts.append(seg.text.strip())
    return {
        "language": info.language,
        "model": model_name,
        "segments": seg_list,
        "full_text": "\n".join(texts),
    }


async def transcribe(media_path: str, options: dict | None = None) -> dict:
    """异步入口：把阻塞的转录丢到线程池，避免卡住 worker 事件循环。"""
    return await asyncio.to_thread(_transcribe_sync, media_path, options or {})
