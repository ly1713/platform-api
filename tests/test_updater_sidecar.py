"""updater 边车：共享密钥鉴权 + 版本白名单 + 后台编排 + 只重建 api/worker。

边车持有 docker socket，是升级系统里权限最高的组件，因此安全测试优先。
用注入的假引擎/假依赖测试，不真的动 docker。
"""

import time

import pytest
from fastapi.testclient import TestClient


class _FakeEngine:
    """假引擎：记录 start 调用，模拟后台任务。"""

    def __init__(self):
        self.started = []
        self.job = None
        self._busy = False

    def is_busy(self):
        return self._busy

    def start(self, version, from_version="", reversible=True, expected_commit=""):
        return self.execute(self.claim(
            version, from_version=from_version, reversible=reversible,
            expected_commit=expected_commit,
        ))

    def claim(self, version, from_version="", reversible=True, expected_commit=""):
        from app.updater.engine import UpgradeJob
        from app.updater.engine import UpgradeBusy

        if self.is_busy():
            raise UpgradeBusy("busy")

        self.job = UpgradeJob(version=version, from_version=from_version, status="success")
        return self.job

    def execute(self, job):
        self.started.append((job.version, job.from_version, job.reversible, job.expected_commit))
        return job

    def latest(self):
        return self.job


def _client(monkeypatch, *, token="s3cr3t", engine=None):
    import app.updater.service as updater_app
    from app.config import settings

    monkeypatch.setattr(settings, "updater_token", token, raising=False)
    monkeypatch.setattr(settings, "updater_url", "http://x", raising=False)
    eng = engine or _FakeEngine()
    monkeypatch.setattr(updater_app, "_engine", eng, raising=False)
    return TestClient(updater_app.app), eng


def test_requires_token(monkeypatch):
    """无/错密钥一律 401，不执行业务。"""
    client, eng = _client(monkeypatch)
    assert client.post("/upgrade", json={"version": "v1.0.0"}).status_code == 401
    assert client.post("/upgrade", json={"version": "v1.0.0"}, headers={"X-Updater-Token": "wrong"}).status_code == 401
    assert eng.started == []


def test_rejects_invalid_version(monkeypatch):
    """非法版本号 422，且绝不执行。"""
    client, eng = _client(monkeypatch)
    r = client.post("/upgrade", json={"version": "v1; rm -rf /"}, headers={"X-Updater-Token": "s3cr3t"})
    assert r.status_code == 422
    assert eng.started == []


def test_accepts_valid_version_returns_accepted(monkeypatch):
    """合法版本 + 正确密钥 → 200 且 accepted=true，后台启动一次。"""
    client, eng = _client(monkeypatch)
    r = client.post("/upgrade", json={"version": "v1.2.3"}, headers={"X-Updater-Token": "s3cr3t"})
    assert r.status_code == 200, r.text
    assert r.json().get("accepted") is True
    # 后台线程启动，稍等
    for _ in range(50):
        if eng.started:
            break
        time.sleep(0.01)
    assert eng.started and eng.started[0][0] == "v1.2.3"


def test_upgrade_uses_main_api_version_not_sidecar_version(monkeypatch):
    """边车不会随升级重建，from_version 必须来自主 API。"""
    import json
    import urllib.request

    client, eng = _client(monkeypatch)

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"app_version": "v1.0.9", "db_in_sync": True}).encode()

    seen = []

    def _urlopen(url, timeout):
        seen.append((url, timeout))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    r = client.post("/upgrade", json={"version": "v1.0.10"}, headers={"X-Updater-Token": "s3cr3t"})

    assert r.status_code == 200, r.text
    assert r.json()["from_version"] == "v1.0.9"
    for _ in range(50):
        if eng.started:
            break
        time.sleep(0.01)
    assert eng.started == [("v1.0.10", "v1.0.9", True, "")]
    assert seen


def test_upgrade_reports_unknown_from_version_when_main_api_is_unreachable(monkeypatch):
    import urllib.request

    client, eng = _client(monkeypatch)

    def _urlopen(*_args, **_kwargs):
        raise OSError("not reachable")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    r = client.post("/upgrade", json={"version": "v1.0.10"}, headers={"X-Updater-Token": "s3cr3t"})

    assert r.status_code == 200, r.text
    assert r.json()["from_version"] == ""
    for _ in range(50):
        if eng.started:
            break
        time.sleep(0.01)
    assert eng.started == [("v1.0.10", "", True, "")]


def test_busy_returns_409(monkeypatch):
    """已有升级进行中 → 409。"""
    eng = _FakeEngine()
    eng._busy = True
    client, _ = _client(monkeypatch, engine=eng)
    r = client.post("/upgrade", json={"version": "v1.2.3"}, headers={"X-Updater-Token": "s3cr3t"})
    assert r.status_code == 409


def test_health_no_auth(monkeypatch):
    client, _ = _client(monkeypatch)
    assert client.get("/health").status_code == 200


def test_disabled_when_no_token(monkeypatch):
    """未配置密钥 → 服务视为未启用，拒绝所有升级。"""
    client, _ = _client(monkeypatch, token="")
    r = client.post("/upgrade", json={"version": "v1.2.3"}, headers={"X-Updater-Token": "x"})
    assert r.status_code in (401, 409)


def test_status_reports_job(monkeypatch):
    """进度查询返回 busy + job（无需密钥）。"""
    client, eng = _client(monkeypatch)
    eng.start("v9.9.9")
    s = client.get("/upgrade/status")
    assert s.status_code == 200
    body = s.json()
    assert body["job"]["version"] == "v9.9.9"
    assert body["job"]["status"] == "success"


def test_status_empty_before_any_upgrade(monkeypatch):
    client, _ = _client(monkeypatch)
    s = client.get("/upgrade/status")
    assert s.status_code == 200
    assert s.json()["job"] is None


# ---------------- 真实升级事故回归（2026-11-16 NAS v1.0.9→v1.0.11） ----------------
# 边车把 docker-compose.yml 以**单文件** bind mount 挂进来。宿主用「写临时文件 +
# rename」改写后，容器仍读旧 inode 的冻结快照，里面指向已退役的 ghcr.io/lianying1716。
# 于是 pull 稳定 denied、重建与回滚都指向不存在的镜像，服务被停掉且回不来。


def _completed(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_pull_treats_skipped_as_success_when_image_present(monkeypatch):
    """多服务共享同一镜像时 compose 会 `Skipped`，只要镜像在本机就必须算成功。

    事故前 `_do_pull` 直接用 returncode，`denied` 固然该失败，但把 `Skipped`
    这类良性输出也一并当信号，会让判定变得取决于 stderr 长什么样。
    """
    import app.updater.service as svc

    monkeypatch.setattr(svc, "_present_services", lambda: ["platform-api", "platform-worker"])
    monkeypatch.setattr(svc, "_compose_images", lambda *_a, **_k: ["ghcr.io/x/y:v1.0.11"])
    monkeypatch.setattr(svc, "_image_exists_locally", lambda _ref: True)
    monkeypatch.setattr(svc, "_stale_compose_hint", lambda _v: "")
    monkeypatch.setattr(
        svc, "_compose",
        lambda *_a, **_k: _completed(0, "platform-api Skipped - already being pulled\n", ""),
    )
    assert svc._do_pull("v1.0.11")["ok"] is True


def test_pull_fails_when_target_image_absent(monkeypatch):
    """compose 退出 0 但目标镜像并不在本机，不能当成拉取成功。"""
    import app.updater.service as svc

    monkeypatch.setattr(svc, "_present_services", lambda: ["platform-api"])
    monkeypatch.setattr(svc, "_compose_images", lambda *_a, **_k: ["ghcr.io/x/y:v1.0.11"])
    monkeypatch.setattr(svc, "_image_exists_locally", lambda _ref: False)
    monkeypatch.setattr(svc, "_stale_compose_hint", lambda _v: "")
    monkeypatch.setattr(svc, "_compose", lambda *_a, **_k: _completed(0))
    out = svc._do_pull("v1.0.11")
    assert out["ok"] is False


def test_pull_failure_attaches_stale_compose_diagnosis(monkeypatch):
    """拉取失败且 compose 与运行中容器不同仓库时，把「过期 compose」写进错误。

    诊断必须在 pull 之后追加，不能提前拦截：用户主动把镜像仓库从 A 迁到 B 时，
    配置与运行中的容器本来就不同，提前拦会把正常迁移误判成故障。
    """
    import app.updater.service as svc

    monkeypatch.setattr(svc, "_present_services", lambda: ["platform-api", "platform-worker"])
    monkeypatch.setattr(svc, "_compose_images", lambda *_a, **_k: ["ghcr.io/x/y:v1.0.11"])
    monkeypatch.setattr(svc, "_image_exists_locally", lambda _ref: False)
    monkeypatch.setattr(svc, "_stale_compose_hint", lambda _v: "过期 compose：请重启边车")
    monkeypatch.setattr(
        svc, "_compose", lambda *_a, **_k: _completed(1, "", "Error response from daemon: denied")
    )
    out = svc._do_pull("v1.0.11")
    assert out["ok"] is False
    assert "denied" in out["error"] and "过期 compose" in out["error"]


def test_pull_success_ignores_repo_migration(monkeypatch):
    """镜像拉取成功时不得因为「仓库名变了」而报错——那是用户主动迁移。"""
    import app.updater.service as svc

    monkeypatch.setattr(svc, "_present_services", lambda: ["platform-api"])
    monkeypatch.setattr(svc, "_compose_images", lambda *_a, **_k: ["ghcr.io/new/repo:v1.0.11"])
    monkeypatch.setattr(svc, "_image_exists_locally", lambda _ref: True)
    monkeypatch.setattr(svc, "_stale_compose_hint", lambda _v: "不应被调用")
    monkeypatch.setattr(svc, "_compose", lambda *_a, **_k: _completed(0))
    assert svc._do_pull("v1.0.11")["ok"] is True


def test_recreate_refuses_when_image_absent(monkeypatch):
    """重建前镜像不在本机 → 保持现有容器不动，绝不先停再失败。"""
    import app.updater.service as svc

    monkeypatch.setattr(svc, "_present_services", lambda: ["platform-api"])
    monkeypatch.setattr(svc, "_compose_images", lambda *_a, **_k: ["ghcr.io/x/y:v1.0.11"])
    monkeypatch.setattr(svc, "_image_exists_locally", lambda _ref: False)
    called = []
    monkeypatch.setattr(svc, "_compose", lambda *a, **k: called.append(a) or _completed(0))
    out = svc._do_recreate("v1.0.11")
    assert out["ok"] is False and "不在本机" in out["error"]
    assert called == [], "镜像缺失时不能执行 compose up（那会先停掉现有容器）"


def test_rollback_keeps_service_when_target_image_unavailable(monkeypatch):
    """回滚目标镜像拉不到时，明确报告「现有容器未改动」，而不是停服后失败。"""
    import app.updater.service as svc

    monkeypatch.setattr(svc, "_present_services", lambda: ["platform-api"])
    monkeypatch.setattr(svc, "_compose_images", lambda *_a, **_k: ["ghcr.io/x/y:v1.0.9"])
    monkeypatch.setattr(svc, "_image_exists_locally", lambda _ref: False)
    calls = []

    def _compose(*args, **_kwargs):
        calls.append(args)
        return _completed(1, "", "Error response from daemon: denied")

    monkeypatch.setattr(svc, "_compose", _compose)
    out = svc._do_rollback("v1.0.9")
    assert out["ok"] is False
    assert "现有容器未改动" in out["error"]
    assert all(args[0] != "up" for args in calls), "目标镜像不可用时不得执行 compose up"
