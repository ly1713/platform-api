"""对外工具清单（单一来源）：MCP 工具 / resource / scope 的权威目录。

为什么需要这个模块：同一份清单曾被写死三次——AI 接入页一个数组、MCP server
一份、接入提示词里一句「14 个工具」。三处漏改任一处的后果不是报错，而是**用户
照着界面上的说明接了个不存在的工具**，或者 agent 自检时数出来的数量与文档不符。

因此：目录在这里，``/api/agent/manifest`` 渲染它，前端读接口，MCP server 的工具
定义由测试与它对齐。任何一处改动都只有这一个地方要改。

注意这里**不是** MCP server 的实现：真正的工具是 ``mcp_server/server.py`` 里的
薄封装（转发 HTTP）。本模块只描述「有哪些工具、要什么权限、干什么用」。
"""

from __future__ import annotations

# 平台能力工具：转发 /api/skills/*。
# scope 与 app/api/skills.py 的 require_scope 一致（改一处必须改另一处，有测试守着）。
SKILL_TOOLS: tuple[dict, ...] = (
    {
        "name": "list_accounts",
        "method": "GET",
        "path": "/api/skills/accounts",
        "scope": "read",
        "summary": "列出所有平台账号及其登录态",
    },
    {
        "name": "check_account",
        "method": "POST",
        "path": "/api/skills/accounts/{account_id}/check",
        "scope": "operate",
        "summary": "对指定账号立即做一次登录态体检",
    },
    {
        "name": "start_qr_login",
        "method": "POST",
        "path": "/api/skills/accounts/qr-login",
        "scope": "operate",
        "summary": "发起扫码登录，返回二维码",
    },
    {
        "name": "poll_qr_login",
        "method": "GET",
        "path": "/api/skills/accounts/qr-login/{session_id}",
        "scope": "operate",
        "summary": "轮询扫码状态，成功后账号自动入库",
    },
    {
        "name": "update_cookie",
        "method": "POST",
        "path": "/api/skills/accounts/cookie",
        "scope": "operate",
        "summary": "手动用 Cookie 录入或更新账号",
    },
    {
        "name": "create_crawl_task",
        "method": "POST",
        "path": "/api/skills/crawl-tasks",
        "scope": "operate",
        "summary": "按关键词创建采集任务",
    },
    {
        "name": "list_crawl_tasks",
        "method": "GET",
        "path": "/api/skills/crawl-tasks",
        "scope": "read",
        "summary": "列出采集任务及状态",
    },
    {
        "name": "trigger_sync",
        "method": "POST",
        "path": "/api/skills/subscriptions/{subscription_id}/sync",
        "scope": "operate",
        "summary": "手动触发一个订阅源的同步",
    },
    {
        "name": "list_followed_creators",
        "method": "GET",
        "path": "/api/skills/followed-creators",
        "scope": "read",
        "summary": "列出已关注博主及同步状态",
    },
    {
        "name": "search_assets",
        "method": "GET",
        "path": "/api/skills/assets",
        "scope": "read",
        "summary": "搜索素材库内容及其加工状态",
    },
    {
        "name": "get_transcript",
        "method": "GET",
        "path": "/api/skills/assets/{asset_id}/transcript",
        "scope": "read",
        "summary": "取某素材的转录文本（分段 + 全文）",
    },
    {
        "name": "get_analytics",
        "method": "GET",
        "path": "/api/skills/analytics",
        "scope": "read",
        "summary": "数据看板摘要（账号、任务、热门内容）",
    },
    {
        "name": "export_assets",
        "method": "POST",
        "path": "/api/skills/assets/export",
        "scope": "read",
        "summary": "把素材的转录与分析导出为 markdown 或 json",
    },
    {
        "name": "import_article",
        "method": "POST",
        "path": "/api/skills/import-article",
        "scope": "operate",
        "summary": "导入单篇微信公众号文章",
    },
)

# 拆解外移工具：转发 /api/agent/*。
# 认领/回传/放弃都要 ``breakdown`` scope：只读 Key 能看料，但改不了报告。
BREAKDOWN_SCOPE = "breakdown"

BREAKDOWN_TOOLS: tuple[dict, ...] = (
    {
        "name": "list_pending_breakdowns",
        "method": "GET",
        "path": "/api/agent/breakdown/pending",
        "scope": "read",
        "summary": "列出等着外部处理的爆款拆解",
    },
    {
        "name": "get_breakdown_packet",
        "method": "GET",
        "path": "/api/agent/breakdown/{asset_id}/packet",
        "scope": "read",
        "summary": "取一条素材的完整拆解料包（含规则，不建租约）",
    },
    {
        "name": "claim_breakdown",
        "method": "POST",
        "path": "/api/agent/breakdown/{asset_id}/claim",
        "scope": BREAKDOWN_SCOPE,
        "summary": "认领素材，返回租约与料包",
    },
    {
        "name": "submit_breakdown_report",
        "method": "POST",
        "path": "/api/agent/breakdown/{asset_id}/report",
        "scope": BREAKDOWN_SCOPE,
        "summary": "回传拆解报告（需 lease_token）",
    },
    {
        "name": "release_breakdown",
        "method": "POST",
        "path": "/api/agent/breakdown/{asset_id}/release",
        "scope": BREAKDOWN_SCOPE,
        "summary": "放弃认领，把素材还回队列",
    },
)

RESOURCES: tuple[dict, ...] = (
    {"uri": "breakdown://contract", "summary": "报告输出契约（字段 / 类型 / 样例 / 硬性规则）"},
    {"uri": "breakdown://rules/hook-criteria", "summary": "当前生效的爆点判据表"},
    {"uri": "breakdown://packet/{asset_id}", "summary": "完整料包全文（料包大时读它）"},
)

SCOPES: dict[str, str] = {
    "read": "查询类（含取拆解料包）",
    BREAKDOWN_SCOPE: "认领拆解任务并回传报告",
    "operate": "建任务 / 登录等写操作",
    "admin": "设置类",
}

# 分组也由后端给。前端按路径前缀猜「哪些是拆解工具」是一种隐性重复：
# 将来若把某个平台工具挪到 /api/agent 下，它会无声地换组；而分组文案一旦
# 在前端写，就又是一份会漂移的副本。
GROUPS: dict[str, dict[str, str]] = {
    "platform": {
        "title": "平台能力",
        "note": "账号登录态、采集、素材与数据看板。",
    },
    "breakdown": {
        "title": "爆款拆解（外部 Agent 执行）",
        "note": "平台发料、你出报告：认领拿租约，按契约回传。只读 Key 能看料，交作业需要 breakdown 权限。",
    },
}


def all_tools() -> list[dict]:
    """全部对外工具（平台能力 + 拆解外移），顺序即界面展示顺序。"""
    items: list[dict] = []
    for group, tools in (("platform", SKILL_TOOLS), ("breakdown", BREAKDOWN_TOOLS)):
        for tool in tools:
            items.append({**tool, "group": group})
    return items


def tool_names() -> list[str]:
    return [t["name"] for t in all_tools()]
