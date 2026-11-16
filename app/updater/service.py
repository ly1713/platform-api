"""updater 边车 FastAPI：升级编排入口。

端点：
- GET  /health           存活探测
- POST /upgrade          触发升级（需密钥）→ 立即返回 accepted，后台线程执行
- GET  /upgrade/status    轮询进度（只读，无需密钥）

为什么后台执行 + 状态放这里：
升级会重建 platform-api 自身，主 API 进程随之退出。状态必须存活在不会重启的边车里，
前端才能在升级过程中持续轮询进度、看到"备份→拉取→重建→校验"的每一步。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, field_validator

from app.config import settings
from app.updater.engine import UpgradeBusy, UpgradeEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("platform.updater")

app = FastAPI(title="platform-updater", version="2.0.0")

# 只允许重建这两个服务；绝不含 db / updater 自己。
_UPGRADABLE_SERVICES = ["platform-api", "platform-worker"]
_COMPOSE_FILE = os.environ.get("UPDATER_COMPOSE_FILE", "/compose/docker-compose.yml")
_DATA_DIR = os.environ.get("UPDATER_DATA_DIR", "/data")
_BACKUP_DIR = os.environ.get("UPDATER_BACKUP_DIR", "/backups")
_ENV_FILE = os.environ.get("UPDATER_ENV_FILE", "/compose/.env")
_API_HEALTH_URL = os.environ.get("UPDATER_API_HEALTH_URL", "http://platform-api:9100/api/system/version")
_POSTGRES_SERVICE = os.environ.get("UPDATER_DB_SERVICE", "db")


def _compose(*args: str, env: dict | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", _COMPOSE_FILE, *args],
        capture_output=True, text=True, timeout=timeout, env=env or os.environ.copy(), check=False,
    )


def _compose_images(version: str, services: list[str] | None = None) -> list[str]:
    """解析给定服务在目标版本下会用的镜像引用。

    只取升级器真正会重建的那些服务——`config --images` 会把 db 等依赖也带出来，
    而它们与升级无关。用于升级前自检：compose 内容与目标版本对不上时（典型症状是
    边车读到过期快照，见 OPS-19），提前发现比事后对着 `denied` 猜要好。
    """
    targets = services or list(_UPGRADABLE_SERVICES)
    r = _compose("config", "--format", "json", env=dict(os.environ, PLATFORM_VERSION=version))
    if r.returncode != 0:
        return []
    import json

    try:
        config = json.loads(r.stdout or "{}")
    except ValueError:
        return []
    out: list[str] = []
    for name in targets:
        image = str(((config.get("services") or {}).get(name) or {}).get("image") or "").strip()
        if image and image not in out:
            out.append(image)
    return out


def _image_exists_locally(ref: str) -> bool:
    """本机是否已有该镜像引用（含 digest 已解析的本地副本）。"""
    if not ref:
        return False
    r = subprocess.run(
        ["docker", "image", "inspect", ref], capture_output=True, text=True, timeout=60, check=False
    )
    return r.returncode == 0


def _repo_of(ref: str) -> str:
    """去掉 tag/digest，只留 registry/repo，用于比对「是不是同一个仓库」。"""
    ref = (ref or "").strip().split("@", 1)[0]
    if not ref:
        return ""
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    # 冒号在最后一个斜杠之前说明那是端口（registry:5000/repo），不是 tag 分隔符。
    if colon > slash:
        ref = ref[:colon]
    return ref


def _running_api_image() -> str:
    """通过 compose 找到正在运行的 platform-api 容器，读它实际用的镜像引用。"""
    ids = _compose("ps", "-q", "platform-api")
    if ids.returncode != 0:
        return ""
    container = next((line.strip() for line in ids.stdout.splitlines() if line.strip()), "")
    if not container:
        return ""
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.Config.Image}}", container],
        capture_output=True, text=True, timeout=30, check=False,
    )
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _stale_compose_hint(version: str) -> str:
    """compose 解析出的仓库与「正在运行的容器」不一致时，给出可执行的诊断。

    边车把 compose 以**单文件** bind mount 挂进来，宿主用「写临时文件 + rename」
    改写后，容器仍读旧 inode——升级会拿着过期的 compose 去 pull/重建，表象是
    `denied`（指向已退役仓库）或 `manifest unknown`（指向不存在的 tag）。
    在动手前就比对「正在运行容器用的镜像仓库」与「compose 现在解析出的仓库」，
    不一致就停下并给出明确修法，而不是让用户对着一句 daemon 错误猜。
    """
    refs = [ref for ref in _compose_images(version) if not ref.startswith("postgres:")]
    if not refs:
        return ""
    running = _running_api_image()
    if not running:
        return ""
    compose_repos = {_repo_of(ref) for ref in refs}
    running_repo = _repo_of(running)
    if running_repo and compose_repos and running_repo not in compose_repos:
        return (
            f"compose 解析出的镜像仓库（{', '.join(sorted(compose_repos))}）与正在运行的容器"
            f"（{running}）不一致。这通常是边车读到过期 docker-compose.yml 造成的"
            "（单文件 bind mount 在宿主 rename 后仍指向旧 inode）。"
            "请执行 `docker compose --profile updater up -d --force-recreate platform-updater` "
            "重启边车后重试。"
        )
    return ""


def _pull_failure_is_fatal(r: subprocess.CompletedProcess, version: str) -> bool:
    """判断一次 `compose pull` 失败是否真的没拿到可用镜像。

    为什么不能只看退出码：`docker compose pull` 同时拉多个共享同一 image 的服务时，
    会输出 `Skipped - Image is already being pulled by <other>`。真正的产品事故是
    拉取真失败后仍去重建；判定标准必须是「目标镜像到底在不在本机」，而不是
    compose 的退出码或某一行 stderr 长什么样。
    """
    refs = _compose_images(version)
    if not refs:
        # 连镜像名都解析不出来，属于 compose 自身不可用，按失败处理。
        return True
    return any(not _image_exists_locally(ref) for ref in refs)


def _present_services() -> list[str]:
    """返回 compose 里实际定义的可升级服务（容忍某个服务缺失，避免整体失败）。"""
    r = _compose("config", "--services")
    if r.returncode != 0:
        return list(_UPGRADABLE_SERVICES)
    defined = set(r.stdout.split())
    return [s for s in _UPGRADABLE_SERVICES if s in defined]


# ---------------- 真实依赖实现（注入给引擎） ----------------

def _do_backup(version: str) -> dict:
    """强制备份：pg_dump + data 卷 + manifest，产出带时间戳的 tar.gz。

    与 scripts/backup.sh 同契约：备份失败即返回 ok=False，引擎据此中止升级。
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    work = os.path.join(_BACKUP_DIR, f".work-{ts}")
    os.makedirs(work, exist_ok=True)
    try:
        # 1) 数据库
        dump_path = os.path.join(work, "db.dump")
        # 注意：-Fc 是二进制格式，必须用二进制管道写文件，不能经 text 解码（否则乱码/报错）。
        with open(dump_path, "wb") as fh:
            dump = subprocess.run(
                [
                    "docker", "compose", "-f", _COMPOSE_FILE, "exec", "-T", _POSTGRES_SERVICE,
                    "pg_dump", "-U", os.environ.get("POSTGRES_USER", "platform"),
                    "-d", os.environ.get("POSTGRES_DB", "platform"), "-Fc",
                ],
                stdout=fh, stderr=subprocess.PIPE, timeout=600, check=False,
            )
        if dump.returncode != 0 or not os.path.getsize(dump_path):
            err = (dump.stderr or b"")[-300:].decode("utf-8", "replace")
            return {"ok": False, "error": f"pg_dump 失败：{err}"}

        # 2) data 目录（含 secret.key）。data 卷以只读方式挂进本容器，直接 tar 即可，
        #    不依赖宿主卷名推导（worktree/改名都不会出错）。
        data_tar = os.path.join(work, "data.tar.gz")
        if not os.path.isdir(_DATA_DIR):
            return {"ok": False, "error": f"data 目录不存在（{_DATA_DIR}），拒绝无备份升级"}
        vol = subprocess.run(
            ["tar", "czf", data_tar, "-C", _DATA_DIR, "."],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if vol.returncode != 0 or not os.path.exists(data_tar) or os.path.getsize(data_tar) == 0:
            return {"ok": False, "error": "data 目录备份失败（secret.key 兜底），已中止"}

        # 3) manifest + 打包
        import json

        with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"version": version, "created_at": ts, "includes": ["db.dump", "data.tar.gz"]}, fh)

        pkg = os.path.join(_BACKUP_DIR, f"backup-{version}-{ts}.tar.gz")
        tar = subprocess.run(
            ["tar", "czf", pkg, "-C", work, "."], capture_output=True, text=True, timeout=300, check=False
        )
        if tar.returncode != 0 or not os.path.exists(pkg):
            return {"ok": False, "error": f"打包备份失败：{tar.stderr[-200:]}"}
        return {"ok": True, "path": pkg}
    finally:
        subprocess.run(["rm", "-rf", work], check=False)


def _do_pull(version: str) -> dict:
    """拉取目标镜像，判定标准是「镜像是否真的可用」，不是 compose 的退出码。

    真实事故（2026-11-16 NAS v1.0.9→v1.0.11）：边车把 compose 文件以**单文件 bind
    mount** 挂进来，宿主改写文件是「写临时文件 + rename」，容器看到的是旧 inode 的
    冻结快照，里面仍写着已退役的 `ghcr.io/lianying1716/*`。于是 pull 稳定报
    `denied`、随后重建也指向不存在的镜像。两件事都得拦：
    1. compose 解析出的镜像与目标版本自相矛盾时，直接给出可诊断错误，别去动容器；
    2. compose 因多服务共享镜像而 `Skipped` 时，只要镜像已在本机就必须算成功。
    """
    env = dict(os.environ, PLATFORM_VERSION=version)
    services = _present_services()
    if not services:
        return {"ok": False, "error": "未在 compose 中找到可升级服务"}
    r = _compose("pull", *services, env=env)
    if _pull_failure_is_fatal(r, version):
        detail = (r.stderr or r.stdout or "")[-300:].strip() or f"退出码 {r.returncode}"
        # 拉取失败时才追加「是不是读到了过期 compose」的诊断。
        # 不能反过来在 pull 之前就拦：用户主动把镜像仓库从 A 迁到 B 是合法操作，
        # 那种情况下配置本来就与运行中的容器不同，提前拦会把正常迁移误判成故障。
        stale = _stale_compose_hint(version)
        if stale:
            return {"ok": False, "error": f"{detail}；{stale}"}
        return {"ok": False, "error": detail}
    return {"ok": True}


def _do_recreate(version: str) -> dict:
    env = dict(os.environ, PLATFORM_VERSION=version)
    services = _present_services()
    if not services:
        return {"ok": False, "error": "未在 compose 中找到可升级服务"}
    # 拉起前再确认一次镜像真的在本机：`up` 在这里会顺手 pull，如果 compose 指向的是
    # 错仓库，它会把「重建」变成「停掉旧容器并报错」，等于把可用服务换成不可用。
    missing = [ref for ref in _compose_images(version) if not _image_exists_locally(ref)]
    if missing:
        return {"ok": False, "error": f"目标镜像不在本机：{', '.join(missing)}，已保持现有容器不动"}
    r = _compose("up", "-d", "--no-deps", *services, env=env)
    return {"ok": r.returncode == 0, "error": r.stderr[-300:]}


def _do_stop(*services: str) -> dict:
    """Stop the requested services before a restore swaps the data volume."""
    targets = [s for s in services if s in _UPGRADABLE_SERVICES]
    if not targets:
        return {"ok": True}
    r = _compose("stop", *targets)
    return {"ok": r.returncode == 0, "error": r.stderr[-200:]}


def _prepare_rollback(version: str) -> dict:
    """Ensure the old image/containers are available without starting them."""
    if not version:
        return {"ok": False, "error": "无可用旧版本号"}
    env = dict(os.environ, PLATFORM_VERSION=version)
    services = _present_services() or list(_UPGRADABLE_SERVICES)
    r = _compose("create", "--pull=never", "--force-recreate", "--no-deps", *services, env=env)
    if r.returncode == 0:
        return {"ok": True}
    pull = _compose("pull", *services, env=env)
    if _pull_failure_is_fatal(pull, version):
        return {"ok": False, "error": f"create 失败({r.stderr[-150:]})，拉取也失败({pull.stderr[-150:]})"}
    r = _compose("create", "--pull=never", "--force-recreate", "--no-deps", *services, env=env)
    return {"ok": r.returncode == 0, "error": r.stderr[-200:]}


def _probe_api_version() -> dict:
    """Read the main API's structured version endpoint once."""
    import urllib.request
    import json

    try:
        with urllib.request.urlopen(_API_HEALTH_URL, timeout=5) as resp:
            if resp.status != 200:
                return {"reachable": False, "status": resp.status}
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return {
                "reachable": True,
                "status": 200,
                "app_version": str(body.get("app_version") or ""),
                "git_commit": str(body.get("git_commit") or ""),
                "db_in_sync": body.get("db_in_sync") is True,
                "db_revision": body.get("db_revision") or "",
                "db_head": body.get("db_head") or "",
            }
    except Exception:  # noqa: BLE001 探测失败即视为不健康，交由引擎回滚
        return {"reachable": False, "error": "version endpoint unreachable"}


def _do_health(version: str) -> dict:
    """Probe the API and attach the target version for the engine's decision.

    A bare HTTP 200 is not enough: the container tag may not have switched, or
    migrations may still be pending.  Return evidence so the engine can decide.
    """
    return {**_probe_api_version(), "target_version": version}


def _do_pin(version: str) -> dict:
    """把目标版本写回 compose 同目录的 .env，作为"当前版本"的持久事实。

    没有这一步，升级后任何一次 `docker compose up -d` 或宿主机重启都会按默认
    tag 重建，静默切回旧版本——网页升级等于白做。
    """
    from app.updater.versionstate import write_pinned_version

    try:
        write_pinned_version(_ENV_FILE, version)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 写盘失败要冒泡成升级失败（引擎会回滚）
        return {"ok": False, "error": str(exc)}


def _do_rollback(version: str, *, api_only: bool = False) -> dict:
    """回滚：把镜像 tag 切回旧版本并重建。version 为旧版本号。

    回滚**不把 pull 当硬前置**：旧镜像此刻通常还在本机（升级只是换了 tag），直接
    `up -d` 就能回来；若恰好不在再尝试拉取。否则网络一抖（实测 ghcr TLS handshake
    timeout）就会让"已经能回滚"变成"回滚也失败"，把可自愈的升级拖成需要人工恢复。
    """
    if not version:
        return {"ok": False, "error": "无可用旧版本号，需手动恢复备份"}
    env = dict(os.environ, PLATFORM_VERSION=version)
    services = _present_services() or list(_UPGRADABLE_SERVICES)
    if api_only:
        # Partial restore: expose the old API for diagnostics, but never start
        # the worker against a data volume that has not been restored.
        services = [service for service in services if service == "platform-api"]
        if not services:
            services = ["platform-api"]
        # Do not stop the old API before its replacement is available.  A
        # missing image would otherwise turn a partial rollback into an outage.
    # 先确认旧镜像真在本机：`up` 在镜像缺失时会顺手去拉，一旦拉取失败（错仓库、
    # 网络抖动），它会先把现有容器停掉再报错，把「本来还能跑的服务」变成全站不可用。
    missing = [ref for ref in _compose_images(version, services) if not _image_exists_locally(ref)]
    if missing:
        pull = _compose("pull", *services, env=env)
        if _pull_failure_is_fatal(pull, version):
            return {
                "ok": False,
                "error": (
                    f"回滚目标镜像不在本机（{', '.join(missing)}），拉取也失败"
                    f"（{pull.stderr[-150:]}）；现有容器未改动"
                ),
            }
    up = _compose("up", "-d", "--no-deps", *services, env=env)
    return {"ok": up.returncode == 0, "error": up.stderr[-200:]}


def _data_volume_name() -> str:
    """Resolve the real Compose volume name through the resolved model.

    Deriving ``<directory>_platform-data`` breaks on renamed checkouts and
    custom COMPOSE_PROJECT_NAME values.  The updater has the compose file, so
    Compose itself is the source of truth.
    """
    r = _compose("config", "--format", "json")
    if r.returncode != 0:
        return ""
    import json

    try:
        config = json.loads(r.stdout or "{}")
    except ValueError:
        return ""
    volumes = config.get("volumes") or {}
    entry = volumes.get("platform-data") or {}
    name = str(entry.get("name") or "")
    if name:
        return name
    # Compose normally materializes names in `config --format json`.  If this
    # Compose version omits it, use the documented project + logical-name rule.
    project = str(config.get("name") or os.environ.get("COMPOSE_PROJECT_NAME") or "")
    return f"{project}_platform-data" if project else ""


def _unsafe_archive_member(member) -> bool:
    """Whether an archive member could write outside its extraction root.

    Data archives are produced by this product, but restore runs with a
    writable volume and must not rely on producer trust.  Reject absolute or
    parent-traversing names and link targets.  A leading ``./`` is normal.
    """
    def unsafe_path(value: str) -> bool:
        if not value:
            return False
        if value.startswith("/"):
            return True
        parts = value.replace("\\", "/").split("/")
        return any(part == ".." for part in parts)

    if unsafe_path(member.name):
        return True
    if (member.issym() or member.islnk()) and unsafe_path(str(member.linkname or "")):
        return True
    return False


def _restore_data_volume(data_tar_path: str) -> dict:
    """Replace /data transactionally from an on-disk backup tarball.

    The archive is streamed through stdin from its file descriptor; it is never
    loaded into the updater process.  Extraction happens inside a staging
    directory *on the target volume*, then the old entries are moved aside and
    the staged tree is promoted, so a corrupt archive fails before live files
    move.
    """
    volume = _data_volume_name()
    if not volume:
        return {"ok": False, "error": "无法从 Compose 解析 platform-data 卷名"}
    script = r"""
set -eu
rm -rf /data/.restore-stage /data/.restore-old
mkdir -p /data/.restore-stage
tar xzf - -C /data/.restore-stage
mkdir -p /data/.restore-old
find /data -mindepth 1 -maxdepth 1 ! -name .restore-stage ! -name .restore-old \
  -exec mv {} /data/.restore-old/ \;
find /data/.restore-stage -mindepth 1 -maxdepth 1 -exec mv {} /data/ \;
rm -rf /data/.restore-stage /data/.restore-old
"""
    try:
        with open(data_tar_path, "rb") as payload:
            r = subprocess.run(
                [
                    "docker", "run", "--rm", "-i",
                    "-v", f"{volume}:/data",
                    "alpine:3.20", "sh", "-c", script,
                ],
                stdin=payload,
                capture_output=True,
                timeout=600,
                check=False,
            )
    except OSError as exc:
        return {"ok": False, "error": f"无法读取 data.tar.gz：{exc}"}
    if r.returncode != 0:
        err = (r.stderr or b"")[-300:].decode("utf-8", "replace")
        return {"ok": False, "error": f"data 卷恢复失败：{err}"}
    return {"ok": True, "volume": volume}


def _do_restore(backup_path: str) -> dict:
    """Restore both the database and data volume from a pre-upgrade backup.

    The manifest is the contract: only packages produced by ``_do_backup`` or
    ``scripts/backup.sh`` are accepted.  A backup that lacks either half is
    rejected before the live DB is touched.  The result reports the two halves
    independently because the engine may have to describe a partial rollback.

    Both payloads are streamed to temporary files, never held whole in memory:
    data volumes can be many gigabytes and the updater sidecar must not be the
    process that runs out of memory during a rollback.
    """
    import json
    import tarfile
    import tempfile

    failure = {"ok": False, "db_restored": False, "data_restored": False}
    if not backup_path or not os.path.isfile(backup_path):
        return {**failure, "error": f"备份不存在：{backup_path}"}

    work = tempfile.mkdtemp(prefix="platform-restore-")
    dump_path = os.path.join(work, "db.dump")
    data_path = os.path.join(work, "data.tar.gz")
    try:
        try:
            with tarfile.open(backup_path, "r:gz") as tar:
                # Both the updater and scripts/backup.sh create the package
                # with ``tar -C <work> .``, so real member names are
                # ``./manifest.json`` etc.  Tests used bare names and missed
                # this.  Normalize only the leading ``./``; nested or absolute
                # paths remain distinct and are rejected as unknown members.
                members: dict[str, tarfile.TarInfo] = {}
                for member in tar.getmembers():
                    name = re.sub(r"^\./+", "", member.name)
                    if name in ("manifest.json", "db.dump", "data.tar.gz"):
                        members[name] = member
                required = ("manifest.json", "db.dump", "data.tar.gz")
                missing = [name for name in required if name not in members]
                if missing:
                    return {**failure, "error": f"备份缺少 {', '.join(missing)}，拒绝恢复"}

                manifest_file = tar.extractfile(members["manifest.json"])
                if manifest_file is None:
                    raise ValueError("manifest.json 不可读")
                manifest = json.load(manifest_file)
                declared = set(manifest.get("includes") or [])
                if not {"db.dump", "data.tar.gz"} <= declared:
                    return {
                        **failure,
                        "error": "manifest 未同时声明 db.dump 与 data.tar.gz，拒绝恢复",
                    }

                for name, target in (("db.dump", dump_path), ("data.tar.gz", data_path)):
                    source = tar.extractfile(members[name])
                    if source is None:
                        return {**failure, "error": f"无法读取 {name}"}
                    with open(target, "wb") as out:
                        while chunk := source.read(1024 * 1024):
                            out.write(chunk)
        except (OSError, tarfile.TarError, ValueError) as exc:
            return {**failure, "error": f"读取备份失败：{exc}"}

        if not os.path.getsize(dump_path):
            return {**failure, "error": "db.dump 为空"}
        if not os.path.getsize(data_path):
            return {**failure, "error": "data.tar.gz 为空，拒绝恢复"}

        # Validate the nested archive without loading it.  A missing or
        # truncated data archive must never leave a restored DB paired with the
        # wrong files/key.
        try:
            with tarfile.open(data_path, "r:gz") as nested:
                first = nested.next()
                if first is None:
                    return {**failure, "error": "data.tar.gz 没有可用文件，拒绝恢复"}
                if _unsafe_archive_member(first):
                    return {**failure, "error": "data.tar.gz 含越界路径，拒绝恢复"}
                for member in nested:
                    if _unsafe_archive_member(member):
                        return {**failure, "error": "data.tar.gz 含越界路径，拒绝恢复"}
        except (OSError, tarfile.TarError) as exc:
            return {**failure, "error": f"data.tar.gz 无法解压：{exc}"}

        with open(dump_path, "rb") as dump_stream:
            restore = subprocess.run(
                [
                    "docker", "compose", "-f", _COMPOSE_FILE, "exec", "-T", _POSTGRES_SERVICE,
                    "pg_restore", "-U", os.environ.get("POSTGRES_USER", "platform"),
                    "-d", os.environ.get("POSTGRES_DB", "platform"), "--clean", "--if-exists",
                    "--no-owner", "--no-privileges",
                ],
                stdin=dump_stream,
                capture_output=True,
                timeout=600,
                check=False,
            )
        if restore.returncode != 0:
            err = (restore.stderr or b"")[-300:].decode("utf-8", "replace")
            return {**failure, "error": f"pg_restore 失败：{err}"}

        data = _restore_data_volume(data_path)
        if not data.get("ok"):
            return {
                "ok": False, "db_restored": True, "data_restored": False,
                "error": f"数据库已恢复，但 {data.get('error') or data}",
            }
        return {"ok": True, "db_restored": True, "data_restored": True}
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


_engine = UpgradeEngine(
    backup=_do_backup, pull=_do_pull, recreate=_do_recreate,
    health=_do_health, rollback=_do_rollback, pin=_do_pin,
    restore=_do_restore, stop=_do_stop, prepare_rollback=_prepare_rollback,
)


def _current_version() -> str:
    """Read the running platform version from the main API.

    The updater sidecar is intentionally not recreated during an upgrade, so its
    own image version can be older than the platform.  Using it as
    ``from_version`` would show a false upgrade path and give rollback the wrong
    target; the main API's structured version endpoint is the source of truth.
    """
    probe = _probe_api_version()
    if probe.get("reachable") is not True:
        return ""
    return str(probe.get("app_version") or "").strip()


class UpgradeIn(BaseModel):
    version: str
    # The updater cannot infer whether a future release has irreversible
    # migrations; the release manifest must state it.  Defaulting to True keeps
    # old clients working, but callers should pass False when unsure.
    reversible: bool = True
    # Expected release commit from GitHub.  When supplied, verification fails
    # if the running image reports a different commit, even when the version
    # string is identical.
    expected_commit: str = ""

    @field_validator("expected_commit")
    @classmethod
    def _commit_shape(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value and not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("expected_commit must be a full 40-character Git SHA")
        return value


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/upgrade/status")
def upgrade_status():
    """轮询进度（只读，无需密钥）——主 API 重启期间前端靠它跟踪。"""
    job = _engine.latest()
    return {"busy": _engine.is_busy(), "job": job.to_dict() if job else None}


@app.post("/upgrade")
def upgrade(body: UpgradeIn, x_updater_token: str = Header(default="")):
    import secrets

    from app.core.version_upgrade import is_valid_version

    if not settings.updater_token:
        raise HTTPException(status_code=409, detail="updater 未启用（未配置 UPDATER_TOKEN）")
    if not secrets.compare_digest(x_updater_token, settings.updater_token):
        raise HTTPException(status_code=401, detail="updater 鉴权失败")
    if not is_valid_version(body.version):
        raise HTTPException(status_code=422, detail=f"非法版本号：{body.version!r}")
    from_version = _current_version()
    try:
        job = _engine.claim(
            body.version,
            from_version=from_version,
            reversible=body.reversible,
            expected_commit=body.expected_commit,
        )
    except UpgradeBusy as exc:
        raise HTTPException(status_code=409, detail="已有升级在进行中，请稍候") from exc
    threading.Thread(
        target=_engine.execute,
        args=(job,),
        daemon=True,
    ).start()
    return {"accepted": True, "version": body.version, "from_version": from_version}
