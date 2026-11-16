#!/bin/bash
# v1.0.11 升级后验收：真实数据流 + 回归。
# 用法：在 NAS 上以 bash 运行；输出可直接作为证据。
set -uo pipefail
cd /vol1/1000/docker/platform-api/src || exit 1

q() { docker compose exec -T db psql -U platform -d platform -At -F '|' -c "$1"; }

echo "===== 1. 版本与迁移一致性 ====="
curl -sS http://127.0.0.1:9100/api/system/version

echo
echo "===== 2. 容器状态 ====="
docker compose --profile updater ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'

echo
echo "===== 3. .env 是否被 pin 成目标版本 ====="
grep -n '^PLATFORM_VERSION=' .env

echo
echo "===== 4. 备份产物 ====="
docker run --rm -v platform-api_platform-backups:/b alpine sh -c 'ls -la /b/*.tar.gz 2>/dev/null | tail -5'

echo
echo "===== 4b. 迁移 0032 是否真的生效（指标列应变为可空） ====="
echo "升级前全部为 NO（不可空）。升级后 play/like/comment/favorite/share 应为 YES："
q "select column_name || '=' || is_nullable from information_schema.columns
   where table_name='content_metric_snapshot' order by ordinal_position"

echo
echo "===== 4c. 迁移 0033/0034 新增列是否存在 ====="
q "select column_name from information_schema.columns
   where table_name='video_understanding' and column_name in
   ('understanding_confidence','evidence_class','signals','moments','chunk_count','chunk_plan')
   order by column_name"

echo
echo "===== 4d. 0031 -> 0034 迁移期间有无 NULL 指标行（数据保真） ====="
q "select count(*) filter (where play is null) as play_null,
          count(*) filter (where \"like\" is null) as like_null,
          count(*) from content_metric_snapshot"

echo
echo "===== 5. 回归：数据量未丢 ====="
echo "media_asset:        $(q 'select count(*) from media_asset')"
echo "content_item:       $(q 'select count(*) from content_item')"
echo "transcript:         $(q 'select count(*) from transcript')"
echo "viral_breakdown:    $(q 'select count(*) from viral_breakdown')"
echo "platform_account:   $(q 'select count(*) from platform_account')"
echo "sync_run(success):  $(q "select count(*) from sync_run where status='success'")"

echo
echo "===== 6. 本次修复目标：faster_whisper 现在存在 ====="
docker compose exec -T platform-worker python -c 'import faster_whisper, av; print("faster_whisper", faster_whisper.__version__, "av", av.__version__)' 2>&1

echo
echo "===== 7. 任务队列 ====="
q "select job_type, status, count(*) from job_queue group by job_type, status order by job_type, 3 desc"

echo
echo "===== 8. 最近 worker 日志有无新报错 ====="
docker compose logs --since 10m platform-worker 2>&1 | grep -iE 'error|traceback|modulenotfound' | tail -10 || echo "(无新报错)"

echo
echo "===== 9. API 健康 ====="
curl -sS -o /dev/null -w 'auth/status: %{http_code}\n' http://127.0.0.1:9100/api/auth/status

echo
echo "===== 10. 真实转录链路（不只是 import） ====="
# 真跑一次 faster-whisper，用 NAS 上现成的音频文件；证明修复对生产数据成立。
docker compose exec -T platform-worker python - <<'PY' 2>&1
import glob, sys
from faster_whisper import WhisperModel
files = []
for pat in ("/mnt/douyin-archive/**/*.mp4", "/mnt/douyin-archive/**/*.m4a", "/mnt/douyin-archive/**/*.mp3"):
    files += glob.glob(pat, recursive=True)
print("候选真实素材数:", len(files))
if not files:
    print("SKIP: /mnt/douyin-archive 下没有媒体文件，无法做真实转录")
    sys.exit(0)
m = WhisperModel("tiny.en", device="cpu", compute_type="int8")
segs, info = m.transcribe(files[0], vad_filter=True)
text = " ".join(s.text for s in segs).strip()
print("文件:", files[0])
print("语言:", info.language, "时长:", round(info.duration, 2), "s")
print("识别片段数:", len(text.split()), "字样")
print("转写非空:", bool(text))
PY

