"""拆解外移的 MCP 表面：工具/资源与 HTTP 路由必须一一对应。

为什么单独成文件而不是塞进 ``test_skills_surface``：那一份守的是 ``/api/skills``，
这一份守的是 ``/api/agent``。两者是不同通道（前者是平台能力，后者是拆解任务
的取料/回传），混在一份里会让「新增一个工具」变成改一份越来越长的清单。

守三条：
1. 每个新工具的名字都在 MCP server 里真实注册；
2. server.py 里写死的 ``/api/agent`` 路径都能在 app 里找到（改一边忘改另一边 → 红）；
3. 契约与判据表由**后端渲染**，server.py 不含字段表的第二份副本。
"""

import re
from pathlib import Path

import pytest

SERVER_PY = Path(__file__).resolve().parents[1] / "mcp_server" / "server.py"


def _route_map():
    from app.main import app

    out = {}
    for r in app.routes:
        if hasattr(r, "methods") and hasattr(r, "path") and r.path.startswith("/api/agent"):
            out.setdefault(r.path, set()).update(m for m in r.methods if m != "HEAD")
    return out


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{p}", path)


def test_mcp_server_references_only_real_agent_routes():
    src = SERVER_PY.read_text(encoding="utf-8")
    paths = set(re.findall(r'"(/api/agent[^"]*)"', src))
    assert paths, "MCP server 里没有引用任何 /api/agent 路由（工具没接上？）"
    known = {_norm(p) for p in _route_map()}
    missing = [p for p in paths if _norm(p) not in known]
    assert not missing, f"MCP server 引用了不存在的 /api/agent 路由: {missing}"


def test_mcp_registers_the_five_breakdown_tools():
    pytest.importorskip("mcp")
    import os

    os.environ.setdefault("PLATFORM_API_KEY", "dummy-for-import")
    from mcp_server import server

    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    required = {
        "list_pending_breakdowns",
        "get_breakdown_packet",
        "claim_breakdown",
        "submit_breakdown_report",
        "release_breakdown",
    }
    assert required <= names, f"缺少 MCP 工具: {required - names}"


def test_mcp_registers_breakdown_resources():
    """规则走 resource 而不是工具定义：工具描述会进每一轮上下文，规则不会。"""
    pytest.importorskip("mcp")
    import os

    os.environ.setdefault("PLATFORM_API_KEY", "dummy-for-import")
    from mcp_server import server

    uris = set()
    manager = getattr(server.mcp, "_resource_manager", None)
    if manager is not None:
        for resource in manager.list_resources():
            uris.add(str(resource.uri))
    assert "breakdown://contract" in {u.rstrip("/") for u in uris} or any(
        "breakdown://contract" in u for u in uris
    ), f"缺少 breakdown://contract resource，实际: {uris}"


def test_contract_text_is_not_duplicated_in_mcp_server():
    """server.py 里不能有字段表的第二份副本。

    只要它开始自己拼字段清单，就说明「契约真相」分了家：源码改了、server 没改时，
    agent 会照过期契约交作业，而错误信息看起来像它自己不守格式。
    """
    src = SERVER_PY.read_text(encoding="utf-8")
    for key in ("replication_tips", "psych_triggers", "blueprint"):
        assert key not in src, f"MCP server 复制了契约字段 {key}（应转发 /api/agent/contract）"


def test_contract_endpoint_renders_from_contract_module(client):
    from app.core import breakdown_contract as contract

    raw = client.post("/api/ai/keys", json={"name": "c", "scopes": ["read"]}).json()["api_key"]
    r = client.get("/api/agent/contract", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    assert r.text == contract.render_agent_schema()


def test_hook_criteria_endpoint_reports_current_table(client):
    raw = client.post("/api/ai/keys", json={"name": "c", "scopes": ["read"]}).json()["api_key"]
    body = client.get(
        "/api/agent/hook-criteria", headers={"Authorization": f"Bearer {raw}"}
    ).json()
    assert body["criteria"]
    assert body["max_matches"] == 12
    for item in body["criteria"]:
        assert f"[{item['id']}]" in body["text"]
