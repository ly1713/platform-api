"""分块理解 / 信号接入 / 汇总降级的服务层行为。

重点覆盖「静默错误」——这类问题不会抛异常，只会让报告看起来正常但内容是错的：
- 分块后 timeline 不加偏移 → 时间全错但看起来正常；
- 汇总失败整条重跑 → 白烧一次视频理解和 N 次切块；
- 信号采集失败拖垮理解主链路。
"""

import asyncio

import pytest

import app.services.understand_service as svc


@pytest.fixture(autouse=True)
def _chdir_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import app.core.storage as storage_mod

    monkeypatch.setattr(
        storage_mod,
        "get_default_root",
        lambda _db: type("Root", (), {"path": str(tmp_path), "id": 1})(),
    )
    yield


def _seed(db, *, duration=1200):
    from app.models import ContentItem, MediaAsset, Transcript

    c = ContentItem(
        account_id=1,
        platform="douyin",
        external_id="chunk1",
        content_type="video",
        title="18分钟长口播",
        author_name="长视频作者",
        duration_sec=duration,
    )
    db.add(c)
    db.flush()
    a = MediaAsset(
        content_id=c.id,
        relative_path="douyin/a/1",
        video_file="v.mp4",
        download_status="done",
    )
    db.add(a)
    db.flush()
    db.add(Transcript(asset_id=a.id, full_text="长口播内容", language="zh", model="medium"))
    db.commit()
    return c, a


def _dims():
    return [{"key": "visual", "name": "画面内容", "ask": "主体场景"}]


def test_chunked_understand_shifts_timeline_to_source_seconds(monkeypatch, tmp_path):
    """每块模型报的是块内相对秒数；不偏移就会出现「第 3 秒的钩子实际在第 10 分钟」。"""
    calls = []

    async def fake_understand(path, prompt, *, provider, call_model):
        calls.append(prompt)
        index = len(calls)
        # 每块都报「第 5 秒」——块内相对时间。
        return {
            "text": '{"summary":"这段","dimensions":{"visual":"画面"},"timeline":[{"t":5,"event":"钩子"}],"confidence":0.8}',
            "usage": {"video_tokens": 100},
            "model": "qwen3.8-flash",
        }

    async def fake_call_model(*, request, parse_result, **kwargs):
        class R:
            data = {"summary": "整体", "dimensions": {"visual": "整体画面"}, "confidence": 0.9}
            usage = {"prompt_tokens": 50}
            used_model_key = "text-model"
            text = ""

        return R()

    monkeypatch.setattr(svc, "_protocol_provider_label", lambda _db, _wt: "qwen")
    monkeypatch.setattr(svc.vision, "understand", fake_understand)
    monkeypatch.setattr(
        svc, "_cut_chunk", lambda path, chunk, work_dir, *, max_mb: str(tmp_path / f"c{chunk.index}.mp4")
    )
    monkeypatch.setattr(svc, "_ensure_inline", lambda p, mb, wd: (p, {"compressed": False}))

    plan = [
        svc.video_chunking.ChunkRange(index=0, start=0.0, end=300.0),
        svc.video_chunking.ChunkRange(index=1, start=285.0, end=600.0),
    ]
    parsed, timeline, usage = asyncio.run(
        svc._understand_video(
            None,
            asset_id=1,
            video_path=str(tmp_path / "v.mp4"),
            work_dir=str(tmp_path),
            max_mb=15,
            chunk_plan=plan,
            duration=600.0,
            category="knowledge",
            label="知识科普",
            focus="论点",
            dimensions=_dims(),
            transcript="t",
            evidence="words",
            moments=[],
            call_model=fake_call_model,
        )
    )

    # 第 1 块 t=5 → 5；第 2 块 t=5 → 290。合并后按时间升序。
    times = [item["t"] for item in timeline]
    assert times == [5.0, 290.0]
    # 汇总用的是汇总结果，不是最后一块
    assert parsed["summary"] == "整体"
    assert parsed["dimensions"] == {"visual": "整体画面"}
    # 用量必须累加：两次视频理解 + 一次汇总
    assert usage["video_tokens"] == 200
    assert usage["prompt_tokens"] == 50
    # 每块提示词都要带分段说明，否则模型把 5 分钟当整条视频
    assert all("第" in p and "段" in p for p in calls)


def test_chunked_understand_degrades_when_rollup_fails(monkeypatch, tmp_path):
    """汇总失败不能毁掉已经花钱跑完的分块理解，只能降级。"""
    async def fake_understand(path, prompt, *, provider, call_model):
        return {
            "text": '{"summary":"段A","dimensions":{"visual":"甲"},"confidence":0.7}',
            "usage": {"video_tokens": 10},
            "model": "m",
        }

    async def boom(**kwargs):
        raise RuntimeError("汇总模型挂了")

    monkeypatch.setattr(svc, "_protocol_provider_label", lambda _db, _wt: "qwen")
    monkeypatch.setattr(svc.vision, "understand", fake_understand)
    monkeypatch.setattr(
        svc, "_cut_chunk", lambda path, chunk, work_dir, *, max_mb: str(tmp_path / "c.mp4")
    )
    monkeypatch.setattr(svc, "_ensure_inline", lambda p, mb, wd: (p, {"compressed": False}))

    plan = [svc.video_chunking.ChunkRange(index=0, start=0.0, end=300.0)]
    parsed, timeline, usage = asyncio.run(
        svc._understand_video(
            None,
            asset_id=1,
            video_path=str(tmp_path / "v.mp4"),
            work_dir=str(tmp_path),
            max_mb=15,
            chunk_plan=plan,
            duration=300.0,
            category="other",
            label="其他",
            focus="x",
            dimensions=_dims(),
            transcript="t",
            evidence="words",
            moments=[],
            call_model=boom,
        )
    )
    assert parsed["summary"] == "段A"  # 降级为拼接各块摘要
    assert parsed["dimensions"] == {"visual": "甲"}
    assert timeline == []


def test_short_video_uses_single_call_path(monkeypatch, tmp_path):
    """未触发分块时行为必须与旧路径一致：一次调用、不做汇总。"""
    calls = []

    async def fake_understand(path, prompt, *, provider, call_model):
        calls.append(prompt)
        return {
            "text": '{"summary":"短","dimensions":{},"timeline":[],"confidence":0.9}',
            "usage": {"video_tokens": 7},
            "model": "m",
        }

    async def fail_if_called(**kwargs):
        raise AssertionError("短视频不应走汇总")

    monkeypatch.setattr(svc, "_protocol_provider_label", lambda _db, _wt: "qwen")
    monkeypatch.setattr(svc.vision, "understand", fake_understand)
    monkeypatch.setattr(svc, "_ensure_inline", lambda p, mb, wd: (p, {"compressed": False}))

    parsed, timeline, usage = asyncio.run(
        svc._understand_video(
            None,
            asset_id=1,
            video_path=str(tmp_path / "v.mp4"),
            work_dir=str(tmp_path),
            max_mb=15,
            chunk_plan=[],
            duration=60.0,
            category="other",
            label="其他",
            focus="x",
            dimensions=_dims(),
            transcript="t",
            evidence="words",
            moments=[],
            call_model=fail_if_called,
        )
    )
    assert len(calls) == 1
    assert "分段说明" not in calls[0]
    assert parsed["summary"] == "短"
    assert usage == {"video_tokens": 7}


def test_shift_moments_clips_to_chunk_window():
    """全局 moments 必须裁到块范围并转成块内相对时间，否则模型会去找看不到的秒数。"""
    moments = [
        {"start": 10.0, "end": 40.0, "score": 1.0, "drivers": {"loud": 1.0}},
        {"start": 280.0, "end": 320.0, "score": 2.0, "drivers": {"motion": 1.0}},
        {"start": 700.0, "end": 730.0, "score": 3.0, "drivers": {"cut": 1.0}},
    ]
    shifted = svc._shift_moments(moments, offset=285.0, start=285.0, end=600.0)
    # 第一条完全在块外；第二条跨块被裁剪到 0-35；第三条在块外
    assert len(shifted) == 1
    assert shifted[0]["start"] == 0.0
    assert shifted[0]["end"] == 35.0
    assert shifted[0]["score"] == 2.0


def test_merge_usage_ignores_non_numeric():
    merged = svc._merge_usage([
        {"video_tokens": 10, "model": "x"},
        {"video_tokens": 5, "prompt_tokens": 3, "cached": True},
        None,
    ])
    assert merged == {"video_tokens": 15, "prompt_tokens": 3}


def test_video_too_large_is_not_value_error():
    """继承 RuntimeError 而不是 ValueError：避免被 except ValueError 顺手吞掉。"""
    assert issubclass(svc.VideoTooLarge, RuntimeError)
    assert not issubclass(svc.VideoTooLarge, ValueError)


def test_bitrate_plan_keeps_audio_and_video_budget():
    """音频占 25%、夹在 24~96kbps；视频不低于 48kbps。"""
    video, audio = svc._bitrate_plan(15, 300)
    # 15MiB × 0.9 × 8 / 300s ≈ 377 kbps 总量；音频 25% = 94，其余给视频
    assert audio == int(15 * svc._MB * 0.9 * 8 / 300 / 1000 * 0.25)
    assert video + audio == int(15 * svc._MB * 0.9 * 8 / 300 / 1000)
    assert 24 <= audio <= 96
    # 长视频把总量压到极低时，视频不会跌破 48
    video_long, audio_long = svc._bitrate_plan(15, 4000)
    assert video_long == 48
    assert audio_long == 24


def test_cut_chunk_encodes_once_at_target_bitrate(monkeypatch, tmp_path):
    """切块必须一次编码到位，不能再「先切再压」跑两遍全量转码。

    早期实现用 -crf 23 切块再走 _ensure_inline：一条 300 秒竖屏块切出 27MB、
    再压成 13MB，同一段画面编码两次，18 分钟视频切 4 块要 375 秒。
    """
    calls = []

    def fake_run(args, capture_output=True, text=True):
        calls.append(list(args))
        with open(args[-1], "wb") as fh:
            fh.write(b"\x00" * 10)

        class _P:
            returncode = 0
            stderr = ""

        return _P()

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    chunk = svc.video_chunking.ChunkRange(index=2, start=570.0, end=870.0)
    svc._cut_chunk("src.mp4", chunk, str(tmp_path), max_mb=15)

    assert len(calls) == 1  # 只编码一次
    args = calls[0]
    video_kbps, audio_kbps = svc._bitrate_plan(15, 300.0)
    assert args[args.index("-b:v") + 1] == f"{video_kbps}k"
    assert args[args.index("-b:a") + 1] == f"{audio_kbps}k"
    # 不使用 CRF：CRF 无法钉住体积
    assert "-crf" not in args
    # seek 在输入侧（-i 之前）：最后一块不必从文件开头逐帧解码
    assert args.index("-ss") < args.index("-i")
    # 时长用 -t 精确框定
    assert args[args.index("-t") + 1] == "300.000"
