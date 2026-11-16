# v1.0.11 发布与 NAS 真实升级验收

日期：2026-11-16
请求：发布新版本，并在 NAS 生产实例上模拟用户真实触发的更新。

## 发布前状态

| 项 | 事实 |
|---|---|
| NAS 已部署 | `v1.0.9`（commit `061ef74`，build 2026-10-20T20:25:39Z） |
| NAS DB 版本 | `0031`，`db_in_sync=true` |
| 本地 main | `388c4e3`，领先 origin/main 6 个提交，纯快进 |
| origin tags | 无（v1.0.0~v1.0.10 全为本地 tag，从未推送） |
| origin Releases | 无 |
| Actions 运行记录 | 0 次 |
| GHCR 镜像 | 无（ly1713/* 404） |
| 历史镜像来源 | 全部本机 `docker build`，未经 CI |

## 发现的发布阻塞

1. **NAS compose 指向已消失的账号**：镜像源为 `ghcr.io/lianying1716/*`，而 `github.com/lianying1716` 返回 404。新版本无论怎么发都无法被 NAS 拉到。
   处置：备份后改为 `ghcr.io/ly1713/*`。
2. **NAS `.env` 的检查更新仓库仍是旧账号**：`PLATFORM_UPDATE_REPO=lianying1716/platform-api`，检查更新会 404。
   处置：备份后改为 `ly1713/platform-api`。
3. **v1.0.9 镜像缺 `faster_whisper`**：镜像构建时间 2026-10-20T20:35:32Z 早于 Dockerfile 加 `--extra transcribe` 的 2026-10-21 22:58:48。生产有 172 个 transcription 任务因此失败。

## 发布内容（v1.0.9 → v1.0.11，15 个提交）

见本目录 `release-commits.txt`。

## 发布过程

### 第一次尝试（tag @ 388c4e3，run 43797798882）—— CI 失败

`test` 任务在**收集阶段**报错：

```
ERROR collecting tests/test_transcription_decode.py
E   ModuleNotFoundError: No module named 'numpy'
Interrupted: 1 error during collection
```

根因：该测试 `import numpy`，而 CI 只装 `--extra postgres --extra dev`。
缺 numpy 时 pytest 收集即退出，**这个守护音频解码回归的测试从加进来到现在
在 CI 里一次都没执行过**。本地能过只因本机 venv 恰好装了 numpy。

修复（commit 23d5097）：把 `numpy>=1.26` 加进 `dev` extra。测试 fake 模型，
不需要真实转录栈；真实 faster-whisper 由镜像内 smoke 覆盖。

验证（CI 等价环境，只装 postgres+dev）：
```
uv run pytest tests/test_transcription_decode.py -q  → 3 passed
uv run pytest tests/ -q                             → 1289 passed, 3 skipped
```

### 第二次尝试（tag @ 23d5097，run 43798909026）—— 3 个失败，2 个是真缺陷

`build` 被 skip（依赖 test），故 Release 从未产出，tag 可干净移动。

1. `FileNotFoundError: 'ffmpeg'`（`test_transcription_decode.py` 两例）
   —— CI runner 没有 ffmpeg。这个测试守护的是「16kHz 单声道 float32 解码」，
   曾经一个解码 bug 让**全部转录**失效，不能 skip。修复（commit 3e975c4）：
   test 任务 `apt-get install ffmpeg`。
2. `KeyError: 'Node'`（`test_runtime_capabilities.py`）—— **真实产品缺陷**。
   `app/api/system.py` 的诊断接口把 Node 这一行包在 `try: import playwright` 里，
   可选包缺失时整行消失。前端读 `optional_failures` 决定是否告警，行缺失导致
   同一部署向运维和客户端报告不同形状。修复（commit 68dcb9a）：Node 行无条件返回。

### 第三次尝试（tag @ 68dcb9a，run 43799952401）—— test 通过

```
test  job: success
  ✓ 安装 ffmpeg（转录解码测试需要真实 ffmpeg）
  ✓ 安装依赖
  ✓ 确认 PostgreSQL 就绪（防止迁移测试静默 skip）
  ✓ 运行测试（含迁移/版本门禁专项）
  ✓ 断言迁移/加锁/演练测试确实执行（未 skip）
build job:
  ✓ 构建 amd64 候选镜像（暂不推送）
  ✓ 镜像内真实转录 smoke      ← 证明 faster_whisper 修复在生产镜像内成立
```

镜像内 smoke 跑的是 `ffmpeg 生成音频 → av 解码 → WhisperModel("tiny.en") 转写
→ 断言 info.language`，不是 `import` 检查。这是 v1.0.9「缺 faster_whisper
导致 207 个转录任务失败」那类问题唯一能在发布前拦住的关口。

### 三个此前从未跑过的测试

第一次失败的 numpy 收集错误，暴露的是：`tests/test_transcription_decode.py`
自加入起在 CI 里**一次都没执行过**（收集即中断），本地能过只因开发机 venv
恰好装了 numpy。本轮把它和随之暴露的 ffmpeg、Node 缺陷一起修掉，
CI 才算真正覆盖了音频解码回归。

## 发布结果

run 43799952401 两段全绿，两个镜像已推到 GHCR（匿名可拉），GitHub Release `v1.0.11` 已创建：

| 产物 | 引用 |
|---|---|
| 主镜像 | `ghcr.io/ly1713/platform-api:v1.0.11` digest `sha256:7a57328bec44f5a9ce31c75d8c2c31426d8210317704948f8c31bf1e6c3331c3` |
| 边车镜像 | `ghcr.io/ly1713/platform-api-updater:v1.0.11` digest `sha256:3e73ca6bb585cd99218939f0db26e993a776f21c1a478f3ff4b72143d8438ecb` |
| Release | https://github.com/ly1713/platform-api/releases/tag/v1.0.11 |

## NAS 真实升级验收：一键升级失败，已定位根因并修复

### 升级前的两道 NAS 侧阻塞（已排除）

1. **`PLATFORM_UPDATE_REPO` 没传进容器**。NAS 的旧 compose 没有这一项，进程内用的是镜像
   编译期默认值 `lianying1716/platform-api`（该账号已 404）。表现是 Release 明明存在，
   网页却报「未找到正式 Release」。备份后补上 passthrough 并重建 api 容器，同一按钮
   立刻显示「发现新版本 · 最新 v1.0.11」（截图 `02-check-update-found-v1.0.11.png`）。
2. **新命名空间没有旧版本镜像**。`ly1713` 仓库只有 `v1.0.11`/`v1`/`latest`，
   `v1.0.9`、`v1.0.10` 均 404（`docker manifest inspect` 实测）。把本地旧镜像 retag
   到新命名空间，保证回滚有目标。

### 真实点击升级 → 失败

用真实浏览器（CDP 真实鼠标事件）点「检查更新 → 升级到 v1.0.11 → 确认」，
updater 任务在 `pull` 步骤失败并把服务停掉：

```
backup  ok    /backups/backup-v1.0.11-20261116T040248Z.tar.gz（389,971,294 字节）
pull    error  platform-worker Skipped - Image is already being pulled by platform-api
               platform-api Error denied
               Error response from daemon: denied
rollback error 回滚也失败：up 失败(... container platform-api-platform-api-1 is unhealthy)
               拉取也失败(... Error denied ...)，请用备份手动恢复
```

完整 job JSON：`upgrade-job-failed.json`。服务随即不可用，手工
`docker compose --profile updater up -d --no-deps platform-api` 恢复到 v1.0.9，
确认数据完好后再手工完成到 v1.0.11。

### 根因：**不是**镜像仓库、网络或凭据，而是边车读到了冻结的 compose

事故当下我曾判断为「GHCR 匿名取令牌并发竞态」。**这个判断是错的**，用下面这组对照实验推翻：

```
# 同一条 compose pull，在 NAS 宿主上连续 6 次：全部成功
platform-worker Skipped - Image is already being pulled by platform-api
platform-api Pulled / 退出码=0

# 完全相同的命令，在 updater 边车容器内连续 3 次：全部失败
platform-api Error denied / Error response from daemon: denied / 退出码=1
```

差异来自**边车看到的是另一个文件**：

```
容器内 /compose/docker-compose.yml   image: ghcr.io/lianying1716/platform-api:...  size=6422  mtime=2026-10-21
宿主真实 docker-compose.yml           image: ghcr.io/ly1713/platform-api:...       size=6690  mtime=2026-11-16
容器内 /compose/.env   PLATFORM_UPDATE_REPO=lianying1716/platform-api  PLATFORM_VERSION=v1.0.9
宿主 .env              PLATFORM_UPDATE_REPO=ly1713/platform-api       PLATFORM_VERSION=v1.0.11
```

`docker-compose.yml` 与 `.env` 都是以**单文件** bind mount 挂进边车的。单文件绑定的是
inode：宿主一旦用「写临时文件 + rename」改写文件（编辑器、`cp`、`git checkout`、
安装脚本都这么做），容器就永远停在旧 inode 上。`lianying1716` 命名空间下的镜像
不存在 → 匿名拉取返回 `denied` → 重建与回滚都指向不存在的镜像 → 服务被停掉且回不来。

最小复现（在 NAS 上真跑，非推理）：

```
挂单文件：容器初始 old-content → 宿主 rename 成 new-content → 容器仍读到 old-content
挂目录  ：容器初始 old2        → 宿主 rename 成 new2        → 容器读到 new2
容器内对冻结单文件写入能读回自己写的内容，但宿主文件不变 —— pin 会写进孤儿 inode
```

### 修复

1. **挂载方式**（`docker-compose.yml`）：`./docker-compose.yml` 与 `./.env` 两个单文件
   挂载改为 `./:/compose` 目录挂载，路径不变。改为可写：pin 需要写回 `.env`，而 `ro`
   在「边车已持有 docker.sock」的前提下不构成真实边界。
2. **`_do_pull` 的成败判定**：改为「目标镜像是否真的在本机」（`_compose_images()` +
   `_image_exists_locally()`），不再依赖 compose 退出码。`Skipped - Image is already
   being pulled by <svc>` 这类多服务共享镜像的良性输出不再被判成失败。
3. **新增 `_stale_compose_hint()`**：比对「运行中容器用的镜像仓库」与「compose 解析
   出的仓库」，不一致时在**拉取失败的错误信息里**追加「多半读到了过期 compose，请
   `--force-recreate` 边车」的诊断，而不是只丢一句 `denied`。诊断在 pull **之后**才
   追加、且只在失败时追加：用户主动把镜像仓库从 A 迁到 B 是合法操作，pull 之前就拦
   会把正常迁移误判成故障（有 `test_pull_success_ignores_repo_migration` 锁住）。
4. **`_do_recreate` / `_do_rollback`**：目标镜像不在本机时**不执行 `up`**。事故里正是
   `up` 顺手去拉、拉失败前先把容器停掉，才把「升级失败」放大成「整站不可用」。

### 修复后复验（同一台 NAS，同一命令）

```
# 边车重建后读到宿主当前文件
docker exec platform-api-platform-updater-1 grep image: /compose/docker-compose.yml
  → image: ghcr.io/ly1713/platform-api:${PLATFORM_VERSION:-latest}

# 边车容器内，事故时 3/3 失败的那条命令
docker exec -e PLATFORM_VERSION=v1.0.11 ... docker compose -f /compose/docker-compose.yml pull platform-api platform-worker
--- 第 1 次 --- platform-worker Pulled   退出码=0
--- 第 2 次 --- platform-api Pulled      退出码=0
--- 第 3 次 --- platform-worker Pulled   退出码=0
```

`denied` 消失。回归测试见 `tests/test_updater_sidecar.py` 新增的 6 条（`Skipped` 且
镜像在本机算成功、退出码 0 但镜像缺失算失败、拉取失败时追加过期 compose 诊断、
拉取成功时不因仓库名变化误判、镜像缺失时不执行 up、回滚目标不可用时保持现有容器
不动）；另有 `tests/test_upgrade_engine.py` 的 3 条覆盖回滚目标选择（见下）。

## 升级后验收（v1.0.11 + DB 0031→0034，真实生产数据）

脚本：`verify-after-upgrade.sh`；原始输出 `post-upgrade-verify.txt`。

| 项 | 结果 |
|---|---|
| 版本 | `app_version=v1.0.11`、commit `68dcb9a`、`version_source=release_image` |
| 迁移 | `db_revision=0034` = `db_head=0034`，`db_in_sync=true` |
| 0032 生效 | 指标列 `play/like/comment/favorite/share` 由 `NO` 变 `YES`（可空） |
| 0033/0034 新列 | `understanding_confidence`/`evidence_class`/`signals`/`moments`/`chunk_count`/`chunk_plan` 均存在 |
| 迁移保真 | NULL 指标行 `play_null=0`、`like_null=0`，未凭空制造空值 |
| 数据回归 | `media_asset 9266 / content_item 9370 / transcript 7148 / viral_breakdown 7123 / platform_account 2 / sync_run 392`，与升级前逐项相同 |
| 运行库 | worker 内 `faster_whisper 1.2.1 | av 17.1.0` |
| 真实转录 | 生产 worker 内对真实素材转写成功：`语言: en`、`时长 170.18s`、`转写非空: True` |
| 健康 | 无新 worker 报错；`/api/system/version`、`/api/auth/status` 均 200 |

## 仍然未解决 / 未验证

- **OPS-20（已修）**：回滚目标版本的选择逻辑。事故中回滚用的 `from_version`
  与目标 tag 在 GHCR 均不存在，回滚必然失败。现在 `_abort_with_rollback()` 先取
  `target = job.from_version.strip()`，为空即终态 `failed` 并提示人工恢复，
  **不再用 `job.version` 兜底**（那正是刚刚失败的升级目标）。同时 `_do_rollback()`
  在目标镜像不在本机时不执行 `compose up`，改为明确回报「现有容器未改动」。仍未做：
  回滚前的**迁移可回退性**校验——v1.0.7 的迁移链停在 `0022`，而事故时 DB 已在
  `0034`，即使镜像拉到也会被版本守卫拒绝启动。这一点已记入 OPS-20 的已知边界。
- **未在修复后重跑一次真实 UI 一键升级**：本轮修复的验证是「同一命令在边车容器内
  3/3 成功」＋单元回归，不是端到端再点一次按钮。该端到端复验留待下轮（需要再准备
  一个可升级的相邻版本，且要接受一次真实停服风险）。
- **边车镜像 tag 仍随 `PLATFORM_VERSION` 走**：`.env` 没有固定
  `PLATFORM_UPDATER_IMAGE` 的版本，因此边车镜像版本始终等于主版本号。CI 每次发版
  都会同时推两个镜像，所以正常路径没问题；但若某版本只推了主镜像，边车就会
  `manifest unknown`（本轮已实测复现 `platform-updater Error manifest unknown`）。
  未修。

