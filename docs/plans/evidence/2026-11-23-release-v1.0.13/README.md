# v1.0.13 发布 + NAS 用户视角升级验收

- **版本**：v1.0.13（用户指定）
- **tag commit**：`312963528f082052e4b64a5d30a9c9c81450a9dc`
- **发布产物**：GitHub Release <https://github.com/ly1713/platform-api/releases/tag/v1.0.13>；GHCR `platform-api` 与 `platform-api-updater` 的 `v1.0.13 / v1.0 / v1 / latest`
- **NAS**：`fnos` / `192.168.2.4`，`/vol1/1000/docker/platform-api/src`，Web 端口 9100
- **路径**：v1.0.11 → v1.0.13（跨两个迁移 0035、0036）
- **验收方式**：真实浏览器 + CDP `Input` 域真实鼠标/键盘事件，全程走用户会点的按钮，不调升级接口抄近路

## 结论

发布与升级均已完成并验证通过：

| 项 | 结果 |
|---|---|
| CI | run `45273626528` success（test + build） |
| 镜像 | 两个镜像 `v1.0.13` 均已推送到 GHCR |
| Release | 已创建，非草稿非预发布 |
| NAS 版本 | `app_version=v1.0.13`，`version_source=release_image`，`db_in_sync=true` |
| 数据库 | `db_revision=0036 = db_head=0036`（迁移已跑完） |
| 数据 | 升级前后逐项一致：9266 / 9382 / 7151 / 7123 / 2 / 239 |
| 容器 | api / worker / updater 全部 v1.0.13，updater 也固定在 v1.0.13 |
| 前端功能验收 | **20/20 PASS**（`feature-result.json`） |
| 前端回归验收 | 8 个路由 + 数据 + 鉴权全部 PASS（`regress-result.json`） |

## 本次升级通过浏览器点的路径

登录 → 系统设置 → 版本与升级 → 检查更新 → 升级到 v1.0.13 → 确认框「开始升级」 → 等待页面报告「升级成功 v1.0.11 → v1.0.13」，自动备份 `backup-v1.0.13-20261123T221030Z.tar.gz`。

脚本：`upgrade.mjs`，结果：`upgrade-result.json`，截图：`shots/01..06`。

`upgrade-result.json` 里有两条 `ok:false`，都是**脚本自身判定写得不对，不是产品缺陷**，已定位：

1. `终态显示新版本` —— 正则 `v\\d+\\.\\d+\\.\\d+` 在 JS 模板字符串里被多转义了一层，永远匹配不上；同一次运行的截图 `shots/06-upgrade-done.png` 与页面文本片段都显示 `应用版本 v1.0.13`、`数据库版本 0036`、`已同步`、`提交已核验`、`升级成功`，并且服务端 `GET /api/system/version` 独立确认 v1.0.13。
2. `无页面报错` —— 抓到 `net::ERR_CONNECTION_REFUSED`，是升级过程中服务重启导致的，属预期。

## 本次改动功能的 NAS 验收（20/20）

脚本 `feature.mjs`，结果 `feature-result.json`，截图 `shots/10..15`。全部真实点击：

- 关注列表渲染 **239** 条；默认排序 = **最新关注**
- 当前列表搜索框存在（占位「在 239 个来源里找昵称 / ID」），输入「极物」筛出 1/239
- 点首条「归档」→ 确认弹层 `position=fixed`，盒子 `[478,381,484,238]` 全在 `1440x1000` 视口内（**这就是本次核心修复**）
- 确认归档 → 当前列表 239 → 238，被归档的博主不再出现，弹层关闭
- 归档箱 `position=fixed`、在视口内，含搜索框；条目显示**头像 + 昵称 + 粉丝 6.6万 + 归档于 11/23 22:15 + 复原**
- 归档箱内搜索「极物」→ 1 条
- 点「复原」→ 归档箱 1 → 0，当前列表回到 239，该博主重新出现
- 全程无页面报错

## 回归（升级后）

脚本 `regress.mjs`，结果 `regress-result.json`，截图 `shots/20..27, 29`：

- 8 个路由（overview / accounts / crawl / followed-creators / assets / pipeline / models / system）均渲染正常、无 5xx
- 素材库读到真实存量 **9266 条**
- 未带会话访问 `/api/followed-creators` → **401**（版本接口 `/api/system/version` 挂在 `public_router` 上，公开是有意设计：登录页与升级器都要读它）
- worker 心跳持续更新（`worker_last_heartbeat` 距查询时刻 34 秒）
- 升级后（22:00Z 之后）新增任务 0、新增报错 0
- 库里 405 条历史 `error` 任务（`understand_video` 196 / `transcribe` 207 / `analyze` 1 / `breakdown` 1）最新一条是 19:21Z，**早于本次升级**，原因是「封面判类没有配置可用模型」，属既有配置问题，与 v1.0.13 无关

## 升级过程中修掉的两个真问题

1. **CI 首跑失败（run 45271645992）**：`tests/test_sqlite_hardening.py` 的两个迁移测试只在本机能过——它们依赖仓库里的 `data/secret.key`（被 gitignore），CI 上不存在，`0018_model_access` 迁移直接拒绝建库。已改为显式注入一次性 `PLATFORM_CREDENTIAL_ENC_KEY` 并把 `PLATFORM_DATA_DIR` 指到 `tmp_path`，并把 `data/secret.key` 物理移走复跑验证（6 passed）。这是**测试问题不是生产问题**：compose 会传密钥，NAS `.env` 已配置。
2. **updater sidecar 不动**：compose 里 sidecar 的镜像是 `${PLATFORM_UPDATER_VERSION:-${PLATFORM_VERSION:-latest}}`，而 `.env` 把 `PLATFORM_UPDATER_VERSION` 钉在旧版本，所以只改 `PLATFORM_VERSION` 时 sidecar 原地不动。已把 `.env` 两个变量都钉到 v1.0.13。

## 文件

| 文件 | 内容 |
|---|---|
| `git-baseline.txt` | 发布前代码基线（21:37Z，head=59571cd）、v1.0.12.. 提交列表、alembic head |
| `pre-upgrade-baseline.txt` | NAS 升级前真实快照（v1.0.11 的 version JSON、`.env`、容器、数据量） |
| `release-artifacts.txt` | CI run、Release、GHCR 标签与 digest |
| `nas-upgrade-state.txt` | NAS 升级后 version JSON、`.env`、容器、数据量（与升级前逐项对照） |
| `gates.txt` | 发布后门禁复跑输出 |
| `upgrade.mjs` / `upgrade-result.json` | 浏览器升级脚本与结果 |
| `feature.mjs` / `feature-result.json` | 归档/复原/搜索/排序功能验收 |
| `regress.mjs` / `regress-result.json` | 升级后路由与鉴权回归 |
| `cdp.mjs` | 最小 CDP harness（真实 Input 事件） |
| `shots/` | 全部截图 |
