from src.web.api.notifications import _normalize_link, _run_status, _to_out
from src.core.notify_center import _finalize_channel_records, _push_result_status, notify_task_done
from src.web.models import AgentRun, Notification


def test_legacy_stock_notification_link_is_migrated_to_portfolio():
    assert (
        _normalize_link("/stocks?symbol=002436&market=CN")
        == "/portfolio?symbol=002436&market=CN"
    )


def test_notification_link_preserves_supported_destination():
    assert _normalize_link("/reports?date=2026-08-10") == "/reports?date=2026-08-10"
    assert _normalize_link(None) == ""


def test_skipped_agent_run_corrects_legacy_completed_notification():
    notification = Notification(
        id=9,
        category="agent_run",
        level="success",
        title="✅ 兴森科技(002436) intraday_monitor 已完成（耗时 0.1s）",
        body="分析已完成，可在个股详情查看。",
        link="/portfolio?symbol=002436&market=CN",
    )
    run = AgentRun(
        agent_name="intraday_monitor",
        status="success",
        result="当前A股非交易时段，已跳过执行",
    )

    assert _run_status(run) == "skipped"
    output = _to_out(notification, run)
    assert output.level == "warning"
    assert "已跳过" in output.title
    assert output.body == run.result


def test_notify_task_done_supports_skipped_status(monkeypatch):
    captured = {}

    def fake_push(title, body, **kwargs):
        captured.update(title=title, body=body, **kwargs)
        return 21

    monkeypatch.setattr("src.core.notify_center.push_notification", fake_push)

    notification_id = notify_task_done(
        "盘中监测",
        ok=True,
        status="skipped",
        detail="当前非交易时段",
        duration_ms=100,
    )

    assert notification_id == 21
    assert captured["title"] == "⏭️ 盘中监测 已跳过（耗时 0.1s）"
    assert captured["level"] == "warning"
    assert captured["body"] == "当前非交易时段"


def test_channel_receipts_keep_safe_names_and_per_channel_status():
    records = [
        {"id": 1, "name": "手机 PushPlus", "type": "pushplus", "status": "pending", "error": ""},
        {"id": 2, "name": "交易 Telegram", "type": "telegram", "status": "pending", "error": ""},
    ]
    result = {
        "success": False,
        "error": "Telegram 发送失败",
        "channels": [
            {"type": "pushplus", "success": True, "receipt": {"message_id": "secret-receipt"}},
            {"type": "apprise", "success": False, "error": "Telegram 发送失败"},
        ],
    }

    finalized = _finalize_channel_records(records, result)

    # 名称与每渠道状态映射不变(前端靠这两项筛"哪个渠道失败")。
    assert [(i["id"], i["name"], i["type"], i["status"]) for i in finalized] == [
        (1, "手机 PushPlus", "pushplus", "sent"),
        (2, "交易 Telegram", "telegram", "failed"),
    ]
    assert finalized[0]["error"] == ""
    # 失败文案是**落库并对外**的(push_channels → API → 通知中心), 只允许
    # 「固定类别 + 关联号」; 上游回执里的任意文本(可能带 token/URL)不转发。
    assert "失败（内部错误）" in finalized[1]["error"]
    assert "参考号: ERR-" in finalized[1]["error"]
    assert "Telegram 发送失败" not in finalized[1]["error"]
    assert "secret-receipt" not in str(finalized)

    status, err = _push_result_status(result, finalized)
    assert status == "failed"
    assert "参考号: ERR-" in err and "Telegram 发送失败" not in err

    # 本仓库自己生成的文案原样保留(不重复包一层)。
    safe = "通知发送失败（网络连接失败）；参考号: ERR-ABCD1234"
    assert _push_result_status({"success": False, "error": safe}, []) == ("failed", safe)
