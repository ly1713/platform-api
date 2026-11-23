"""拆解外移 API（``/api/agent/*``）的契约测试。

守四件事，任何一条失守都意味着「外部 agent 拿到的料 / 交回的作业」不可信：

1. **鉴权分层**：只读 Key 能取料但写不了报告；没有 ``breakdown`` scope 一律 403。
2. **租约语义**：同一 agent 幂等拿同一 token；别人抢 → 409；过期可接管；无 token
   或 token 不匹配的回传 → 409。
3. **料包完整性**：数字由后端算、转录与内部链路同源截断、规则一次给全。
4. **回传校验**：不合格报告 422 且**逐条**给原因；报告里的数字不能覆盖后端算的。
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.core import breakdown_contract as contract


# ============================================================
# 夹具
# ============================================================


def _seed(session, *, external_id="ext1", author_id="A", play=5000, like=400, with_l3=True):
    """一条可拆解的素材：内容 + 资产 + 转录 + L3 + 快照。"""
    from app.models import (
        ContentItem,
        ContentMetricSnapshot,
        MediaAsset,
        Transcript,
        VideoUnderstanding,
    )

    c = ContentItem(
        account_id=1,
        platform="douyin",
        external_id=external_id,
        content_type="video",
        title="标题",
        author_name="小红",
        author_id=author_id,
        url="https://x/v",
        duration_sec=30,
        auto_category="knowledge",
        published_at=datetime(2026, 10, 1, 9, 0, 0),
    )
    session.add(c)
    session.flush()
    a = MediaAsset(
        content_id=c.id, relative_path=f"douyin/{c.id}", video_file="v.mp4", download_status="done"
    )
    session.add(a)
    session.flush()
    session.add(
        Transcript(
            asset_id=a.id,
            language="zh",
            model="base",
            segments=[{"start": 0.0, "end": 3.2, "text": "你有没有想过"}],
            full_text="你有没有想过",
        )
    )
    if with_l3:
        session.add(
            VideoUnderstanding(
                asset_id=a.id,
                category="knowledge",
                summary="讲了三个方法",
                dimensions={"visual": "白板"},
                timeline=[{"t": 1, "event": "开场"}],
                status="done",
                model="base",
                evidence_class="words",
            )
        )
    session.add(
        ContentMetricSnapshot(
            content_id=c.id,
            play=play,
            like=like,
            comment=20,
            favorite=10,
            share=5,
            captured_at=datetime.utcnow(),
        )
    )
    session.commit()
    return c, a


def _key(client, scopes=("read", "breakdown")):
    r = client.post("/api/ai/keys", json={"name": "agent-a", "scopes": list(scopes)})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


def _report(**over):
    base = {
        "verdict": "钩子够狠",
        "score": 88,
        "hook": {"hook_type": "反常识", "why": "开头就反问"},
        "structure": [{"segment": "0-3s", "role": "钩子", "summary": "提问"}],
        "comment_insights": {"top_themes": ["求方法"]},
        "hook_matches": [{"criterion": "hook", "evidence": "开场就反问"}],
    }
    base.update(over)
    return base


def _drain_queue(session, asset_id):
    """外部模式下列表端点只认「报告未完成」，先把行建出来。"""
    from app.models import ViralBreakdown

    session.add(ViralBreakdown(asset_id=asset_id, status="pending"))
    session.commit()


# ============================================================
# 1. 鉴权分层
# ============================================================


def test_agent_endpoints_require_auth(anon_client, session):
    _, asset = _seed(session)
    assert anon_client.get("/api/agent/breakdown/pending").status_code == 401
    assert anon_client.get(f"/api/agent/breakdown/{asset.id}/packet").status_code == 401
    assert anon_client.post(f"/api/agent/breakdown/{asset.id}/claim").status_code == 401
    assert anon_client.get("/api/agent/manifest").status_code == 401


def test_read_only_key_cannot_submit(client, session):
    """只有 read 的 Key 能看料，但写不了报告——「看看」和「交作业」分开授权。"""
    _, asset = _seed(session)
    raw = _key(client, scopes=("read",))
    H = {"Authorization": f"Bearer {raw}"}

    assert client.get(f"/api/agent/breakdown/{asset.id}/packet", headers=H).status_code == 200
    r = client.post(f"/api/agent/breakdown/{asset.id}/claim", headers=H)
    assert r.status_code == 403
    assert "breakdown" in r.json()["detail"]


def test_breakdown_scope_key_can_claim(client, session):
    _, asset = _seed(session)
    raw = _key(client, scopes=("breakdown",))
    H = {"Authorization": f"Bearer {raw}"}
    assert client.post(f"/api/agent/breakdown/{asset.id}/claim", headers=H).status_code == 200


# ============================================================
# 2. 租约语义
# ============================================================


def test_claim_returns_lease_and_packet(client, session):
    _, asset = _seed(session)
    raw = _key(client)
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/claim?agent_name=my-agent",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lease_token"]
    assert body["agent_name"] == "my-agent"
    assert body["contract_version"] == contract.BREAKDOWN_CONTRACT_VERSION
    assert body["packet"]["asset_id"] == asset.id
    assert body["packet"]["contract_version"] == contract.BREAKDOWN_CONTRACT_VERSION
    # 料包必须自带规则：外部不该为了知道「什么算爆点」再调一次接口。
    assert body["packet"]["rules"]["hook_criteria"]
    assert body["packet"]["rules"]["output_schema"]["fields"]
    assert body["packet"]["rules"]["hard_rules"]


def test_repeat_claim_by_same_agent_is_idempotent(client, session):
    """同一 agent 在租约内重复认领必须拿到**同一个** token。

    换 token 会让它上一次调用拿到的凭据立刻失效——这是纯粹的坑，而不是保护。
    """
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    first = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    second = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    assert first["lease_token"] == second["lease_token"]
    assert second["attempts"] == 1  # 幂等重复认领不累加次数


def test_other_agent_cannot_steal_live_lease(client, session):
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    assert client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).status_code == 200
    r = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a2", headers=H)
    assert r.status_code == 409
    assert "已被 a1 认领" in r.json()["detail"]


def test_expired_lease_can_be_taken_over(client, session):
    """过期租约可被接管（attempts 累加，token 换新）。"""
    from app.models import BreakdownClaim

    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    first = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()

    claim = session.query(BreakdownClaim).filter_by(asset_id=asset.id).one()
    claim.lease_until = datetime.utcnow() - timedelta(minutes=1)
    session.commit()

    second = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a2", headers=H).json()
    assert second["lease_token"] != first["lease_token"]
    assert second["attempts"] == 2


def test_claim_exhausted_after_max_attempts(client, session):
    """认领次数用尽后不再外放，返回 409 说明原因（不允许静默卡住）。"""
    from app.models import BreakdownClaim

    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H)

    claim = session.query(BreakdownClaim).filter_by(asset_id=asset.id).one()
    claim.attempts = 3  # 与默认 breakdown_max_attempts 一致
    claim.lease_until = datetime.utcnow() - timedelta(minutes=1)
    session.commit()

    r = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a2", headers=H)
    assert r.status_code == 409
    assert "不再外放" in r.json()["detail"]


def test_submit_without_claim_is_conflict(client, session):
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={"lease_token": "whatever", "report": _report()},
        headers=H,
    )
    assert r.status_code == 409
    assert "无人认领" in r.json()["detail"]


def test_submit_with_wrong_token_is_conflict(client, session):
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H)
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={"lease_token": "stale-token", "report": _report()},
        headers=H,
    )
    assert r.status_code == 409
    assert "凭据不匹配" in r.json()["detail"]


def test_release_returns_asset_to_queue(client, session):
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/release",
        json={"lease_token": claimed["lease_token"], "reason": "材料不够"},
        headers=H,
    )
    assert r.status_code == 200

    from app.models import ViralBreakdown

    row = session.query(ViralBreakdown).filter_by(asset_id=asset.id).one()
    session.refresh(row)
    assert row.status == "pending"
    assert row.error == "材料不够"
    # 释放后别人可以接
    again = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a2", headers=H)
    assert again.status_code == 200


# ============================================================
# 3. 料包
# ============================================================


def test_packet_numbers_come_from_backend(client, session):
    _, asset = _seed(session, play=5000, like=400)
    raw = _key(client)
    body = client.get(
        f"/api/agent/breakdown/{asset.id}/packet", headers={"Authorization": f"Bearer {raw}"}
    ).json()
    metrics = body["metrics"]
    assert metrics["play"] == 5000
    assert metrics["like"] == 400
    assert metrics["median_play"] == 5000.0
    # 分母必须暴露：样本 2 条和 200 条的「6 倍」不是一个意思。
    assert metrics["peer_sample_size"] == 1


def test_packet_transcript_truncation_is_structured(client, session):
    """截断是结构化事实，不是让外部从文本里猜。"""
    _, asset = _seed(session)
    raw = _key(client)
    body = client.get(
        f"/api/agent/breakdown/{asset.id}/packet?max_chars=4",
        headers={"Authorization": f"Bearer {raw}"},
    ).json()
    tr = body["transcript"]
    rendered = "[0:00-0:03] 你有没有想过"  # 内部提示词看到的是带时间戳文本
    assert tr["truncated"] is True
    assert tr["original_chars"] == len(rendered)
    assert tr["provided_chars"] == 4
    # 内部提示词看到的那份文本也要带同样的提示语，两条路径材料一致。
    assert "转录在此处被截断" in body["rules"]["transcript_provided"]


def test_packet_rejects_non_positive_max_chars(client, session):
    _, asset = _seed(session)
    raw = _key(client)
    r = client.get(
        f"/api/agent/breakdown/{asset.id}/packet?max_chars=0",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 422


def test_packet_requires_video_understanding(client, session):
    """视频理解不从平台外移：缺 L3 就没有可分析的材料，两条路径同一组前置校验。"""
    _, asset = _seed(session, with_l3=False)
    raw = _key(client)
    r = client.get(
        f"/api/agent/breakdown/{asset.id}/packet", headers={"Authorization": f"Bearer {raw}"}
    )
    assert r.status_code == 409
    assert "视频理解" in r.json()["detail"]


def test_packet_404_for_missing_asset(client):
    raw = _key(client)
    r = client.get("/api/agent/breakdown/9999/packet", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 404


def test_pending_lists_unclaimed_only(client, session):
    _, a1 = _seed(session, external_id="p1")
    _, a2 = _seed(session, external_id="p2")
    _drain_queue(session, a1.id)
    _drain_queue(session, a2.id)
    H = {"Authorization": f"Bearer {_key(client)}"}
    client.post(f"/api/agent/breakdown/{a1.id}/claim?agent_name=a1", headers=H)

    body = client.get("/api/agent/breakdown/pending", headers=H).json()
    ids = [item["asset_id"] for item in body["items"]]
    assert a2.id in ids
    assert a1.id not in ids  # 正在被别人做


def test_pending_skips_exhausted_claims(client, session):
    """次数用尽的素材不再出现在队列里——避免 agent 反复看到一条永远接不下的活。"""
    from app.models import BreakdownClaim

    _, asset = _seed(session)
    _drain_queue(session, asset.id)
    H = {"Authorization": f"Bearer {_key(client)}"}
    client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H)

    claim = session.query(BreakdownClaim).filter_by(asset_id=asset.id).one()
    claim.attempts = 3
    claim.lease_until = datetime.utcnow() - timedelta(minutes=1)
    session.commit()

    body = client.get("/api/agent/breakdown/pending", headers=H).json()
    assert asset.id not in [item["asset_id"] for item in body["items"]]


def test_manifest_lists_tools_and_scopes(client):
    raw = _key(client)
    body = client.get("/api/agent/manifest", headers={"Authorization": f"Bearer {raw}"}).json()
    names = {t["name"] for t in body["tools"]}
    assert {"list_pending_breakdowns", "get_breakdown_packet", "claim_breakdown",
            "submit_breakdown_report", "release_breakdown"} <= names
    assert "breakdown" in body["scopes"]
    assert body["contract_version"] == contract.BREAKDOWN_CONTRACT_VERSION


# ============================================================
# 4. 回传
# ============================================================


def test_submit_report_lands_and_marks_producer(client, session):
    from app.models import ViralBreakdown

    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=my-agent", headers=H).json()
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={"lease_token": claimed["lease_token"], "report": _report()},
        headers=H,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "done"
    assert body["score"] == 88
    assert body["produced_by"] == "external:my-agent"
    assert body["contract_version"] == contract.BREAKDOWN_CONTRACT_VERSION
    assert body["dropped_entries"] == []

    row = session.query(ViralBreakdown).filter_by(asset_id=asset.id).one()
    session.refresh(row)
    assert row.verdict == "钩子够狠"
    assert row.sections["hook"] == {"hook_type": "反常识", "why": "开头就反问"}
    assert row.sections["hook_matches"] == [{"criterion": "hook", "evidence": "开场就反问"}]


def test_submit_ignores_external_metrics(client, session):
    """回传里的数字一律不采信：data_context 只能由后端算。"""
    from app.models import ViralBreakdown

    _, asset = _seed(session, play=5000, like=400)
    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={
            "lease_token": claimed["lease_token"],
            "report": _report(data_context={"play": 9999999}, metrics={"play": 1}),
        },
        headers=H,
    )
    row = session.query(ViralBreakdown).filter_by(asset_id=asset.id).one()
    session.refresh(row)
    assert row.data_context["play"] == 5000


def test_submit_rejects_non_object_with_error_list(client, session):
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={"lease_token": claimed["lease_token"], "report": ["不是对象"]},
        headers=H,
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["errors"] and "不是 JSON 对象" in detail["errors"][0]


def test_submit_drops_fabricated_criterion_and_reports_it(client, session):
    """编造判据 id 的报告仍入库，但那条命中被丢弃**并回传给外部**。

    静默丢弃会让外部一直以为自己写得对；逐条回报才能自纠。
    """
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={
            "lease_token": claimed["lease_token"],
            "report": _report(
                hook_matches=[
                    {"criterion": "made_up", "evidence": "看起来很燃"},
                    {"criterion": "hook", "evidence": "开场提问"},
                ]
            ),
        },
        headers=H,
    )
    assert r.status_code == 200
    assert len(r.json()["dropped_entries"]) == 1
    assert "made_up" in r.json()["dropped_entries"][0]

    from app.models import ViralBreakdown

    row = session.query(ViralBreakdown).filter_by(asset_id=asset.id).one()
    session.refresh(row)
    assert row.sections["hook_matches"] == [{"criterion": "hook", "evidence": "开场提问"}]


def test_rejected_report_does_not_destroy_existing_done_report(client, session):
    """坏报告不能把已有 done 报告打回 pending——否则反复提交坏数据就能冲掉结果。"""
    from app.models import ViralBreakdown

    _, asset = _seed(session)
    session.add(
        ViralBreakdown(asset_id=asset.id, status="done", score=77, verdict="旧结论")
    )
    session.commit()

    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={"lease_token": claimed["lease_token"], "report": "坏数据"},
        headers=H,
    )
    assert r.status_code == 422

    row = session.query(ViralBreakdown).filter_by(asset_id=asset.id).one()
    session.refresh(row)
    assert row.status == "done"
    assert row.verdict == "旧结论"


def test_submit_after_release_is_conflict(client, session):
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    client.post(
        f"/api/agent/breakdown/{asset.id}/release",
        json={"lease_token": claimed["lease_token"]},
        headers=H,
    )
    r = client.post(
        f"/api/agent/breakdown/{asset.id}/report",
        json={"lease_token": claimed["lease_token"], "report": _report()},
        headers=H,
    )
    assert r.status_code == 409


def test_claim_then_packet_matches_standalone_packet(client, session):
    """认领返回的料包材料必须与单独取料一致（否则「先看料再决定」会误导）。"""
    _, asset = _seed(session)
    H = {"Authorization": f"Bearer {_key(client)}"}
    # 先取料（此时无人认领），再认领：顺序决定 claim 段是否存在。
    standalone = client.get(f"/api/agent/breakdown/{asset.id}/packet", headers=H).json()
    claimed = client.post(f"/api/agent/breakdown/{asset.id}/claim?agent_name=a1", headers=H).json()
    # 认领前后的差异只允许出现在「认领元信息」上：claim（谁在做）与
    # existing_report（认领会建出 pending 行）。材料本身必须逐字段一致，
    # 否则「先看料再决定」给出的是一份和实际作业不同的预览。
    assert standalone["claim"] is None
    assert claimed["packet"]["claim"]["agent_name"] == "a1"
    material = ("content", "metrics", "transcript", "understanding", "signals", "comments", "rules")
    for key in material:
        assert claimed["packet"][key] == standalone[key], f"料包 {key} 段在认领前后不一致"
