"""对外工具清单：目录、MCP server、界面三处必须说的是同一件事。

历史事实：工具清单被写死过三次（AI 接入页数组、MCP server 定义、接入提示词里的
「14 个工具」）。漏改一处不会报错，只会让用户照着界面接一个不存在的工具，或者
让 agent 自检出来的工具数与文档不符。这份测试就是防漂移的那一把尺子。
"""

import re
from pathlib import Path

import pytest

from app.core import agent_manifest as catalog

SERVER_PY = Path(__file__).resolve().parents[1] / "mcp_server" / "server.py"
AI_ACCESS = Path(__file__).resolve().parents[1] / "web" / "src" / "pages" / "AiAccess.vue"


def _manifest(client):
    return client.get("/api/agent/manifest").json()


def test_manifest_lists_every_catalog_tool(client):
    body = _manifest(client)
    assert [t["name"] for t in body["tools"]] == catalog.tool_names()
    assert body["tool_count"] == len(catalog.tool_names())


def test_manifest_tool_count_matches_mcp_server_registration(client):
    """MCP server 真实注册的工具数必须与目录一致（两处都改才算改完）。"""
    pytest.importorskip("mcp")
    import os

    os.environ.setdefault("PLATFORM_API_KEY", "dummy-for-import")
    from mcp_server import server

    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    catalogued = set(catalog.tool_names())
    assert catalogued <= registered, f"目录里有但 MCP 没注册: {catalogued - registered}"
    # 反向也要成立：MCP 新增工具忘了登记目录时，界面与提示词就会漏掉它。
    assert registered <= catalogued, f"MCP 注册了但目录没登记: {registered - catalogued}"


def test_catalog_paths_all_exist_and_scope_is_enforced(client):
    """每个工具描述的方法+路径都要真实存在，且缺 scope 时确实是 403。"""
    from app.main import app

    real = set()
    for r in app.routes:
        if hasattr(r, "methods") and hasattr(r, "path"):
            for m in r.methods:
                if m != "HEAD":
                    real.add((m, r.path))
    missing = [(t["method"], t["path"]) for t in catalog.all_tools() if (t["method"], t["path"]) not in real]
    assert not missing, f"目录里登记了不存在的路由: {missing}"

    raw = client.post("/api/ai/keys", json={"name": "ro", "scopes": ["read"]}).json()["api_key"]
    headers = {"Authorization": f"Bearer {raw}"}
    # read 能看料，不能交作业（这就是 breakdown scope 的意义）。
    assert client.get("/api/agent/breakdown/pending", headers=headers).status_code == 200
    r = client.post(
        "/api/agent/breakdown/1/report", json={"lease_token": "x", "report": {}}, headers=headers
    )
    assert r.status_code == 403


def test_frontend_does_not_hardcode_tool_list():
    """AI 接入页不许再写死工具数组或工具数量。

    页面本身就是「用户看到的事实」。它一旦自己列工具，就会在下次加工具时安静地
    变成过期文档；读 ``/api/agent/manifest`` 才是唯一不会漂移的做法。
    """
    src = AI_ACCESS.read_text(encoding="utf-8")
    for name in ("list_accounts", "get_breakdown_packet", "claim_breakdown"):
        assert name not in src, f"AiAccess.vue 手写了工具名 {name}（应读 /api/agent/manifest）"
    # 写死数量（曾有一句「14 个工具」）比写死名字更隐蔽：加工具时它不会报错，
    # 只会让 agent 自检出来的数与页面说明对不上。
    assert not re.search(r"\d+\s*个工具", src), "AiAccess.vue 写死了工具数量（应取 manifest 的 tool_count）"
    assert "manifest_api_agent_manifest_get" in src, "AiAccess.vue 没有从 manifest 接口取清单"


def test_scopes_documented_for_every_tool_scope(client):
    body = _manifest(client)
    used = {t["scope"] for t in body["tools"]}
    assert used <= set(body["scopes"]), f"工具有未登记的 scope: {used - set(body['scopes'])}"
