"""转录的音频解码路径。

这个测试存在的原因是一次真实故障：faster-whisper 1.2.1 的 `decode_audio`
调用 `av.open(..., metadata_errors="ignore")`，而当前安装的 PyAV 19 已经移除
该参数，导致**所有转录**抛 `TypeError`。队列里表现为任务重试三次后失败，
上游视频理解/拆解全部卡在「需要先完成转录」。
"""

import numpy as np
import pytest

from app.modules import transcription


def test_decode_audio_passes_numpy_not_path(tmp_path, monkeypatch):
    """必须解成 ndarray 传给模型。

    传路径会让 faster-whisper 走 `decode_audio`，而那条路在当前 PyAV 上必崩。
    `WhisperModel.transcribe` 只在收到 ndarray 时才跳过解码。
    """
    captured = {}

    class FakeModel:
        def transcribe(self, audio, **kwargs):
            captured["audio_type"] = type(audio).__name__
            captured["is_ndarray"] = isinstance(audio, np.ndarray)
            captured["dtype"] = str(getattr(audio, "dtype", ""))

            class Seg:
                start, end, text = 0.0, 1.0, "你好"

            class Info:
                language = "zh"

            return [Seg()], Info()

    monkeypatch.setattr(
        transcription, "_decode_audio", lambda _p: np.zeros(16000, dtype=np.float32)
    )
    result = transcription._run_model(FakeModel(), "medium", np.zeros(16, dtype=np.float32), {})
    assert captured["is_ndarray"] is True
    assert captured["dtype"] == "float32"
    assert result["full_text"] == "你好"
    assert result["language"] == "zh"


def test_decode_audio_output_scale_and_rate(tmp_path, monkeypatch):
    """解码结果必须是 16kHz 单声道 float32，且幅度归一到 [-1, 1)。"""
    import subprocess

    wav = tmp_path / "tone.wav"
    # 1 秒 440Hz 正弦。注意 lavfi 的 sine 默认幅度很小（实测峰值 0.088），
    # 这里显式给满幅，才能验证「s16le → float32 除以 32768」没有把音量压小。
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100",
            "-af", "volume=10", "-ac", "2", str(wav),
        ],
        check=True,
    )
    audio = transcription._decode_audio(str(wav))
    assert audio.dtype == np.float32
    # 1 秒 × 16kHz，允许少量误差
    assert abs(audio.size - 16000) < 200
    assert 0.5 < audio.max() <= 1.0


def test_decode_audio_raises_clear_error_without_audio_track(tmp_path):
    """无音轨时给出可诊断的错误，而不是零长度数组。"""
    import subprocess

    silent = tmp_path / "noaudio.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-an", str(silent),
        ],
        check=True,
    )
    with pytest.raises(RuntimeError, match="音频"):
        transcription._decode_audio(str(silent))
