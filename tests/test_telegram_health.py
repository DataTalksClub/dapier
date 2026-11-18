"""Telegram webhook health: Telegram's own view of each telegram trigger."""

import json
from datetime import datetime, timezone

import pytest

from src.dapier.connections.providers import telegram_api
from src.dapier.triggers import hook_triggers, telegram_health

NOW = datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)
URL = "https://dapier.test/hooks/telegram/automator-telegram"
ITEM = {"hook_id": "automator-telegram", "kind": "telegram", "connection_id": "bot",
        "url": URL, "token": "s3cret", "enabled": True}
HEALTHY = {"url": URL, "pending_update_count": 0, "has_custom_certificate": False,
           "allowed_updates": ["message", "edited_message", "channel_post",
                               "edited_channel_post", "callback_query"]}


def codes(problems):
    return [problem["code"] for problem in problems]


def test_healthy_registration_has_no_problems():
    assert telegram_health.assess(ITEM, HEALTHY, now=NOW) == []


def test_old_error_with_drained_queue_is_healthy():
    info = {**HEALTHY, "last_error_date": int(NOW.timestamp()) - 60,
            "last_error_message": "Wrong response from the webhook: 500 Internal Server Error"}
    # A recent error but nothing pending: Telegram already redelivered.
    assert telegram_health.assess(ITEM, info, now=NOW) == []


def test_stuck_queue_behind_a_delivery_error_is_failing():
    info = {**HEALTHY, "pending_update_count": 3,
            "last_error_date": int(NOW.timestamp()) - 120,
            "last_error_message": "Wrong response from the webhook: 500 Internal Server Error"}
    problems = telegram_health.assess(ITEM, info, now=NOW)
    assert codes(problems) == ["delivery_error"]
    assert "500 Internal Server Error" in problems[0]["detail"]


def test_repointed_or_deleted_webhook_is_failing():
    assert codes(telegram_health.assess(ITEM, {**HEALTHY, "url": "https://elsewhere.test/x"},
                                        now=NOW)) == ["url_mismatch"]
    problems = telegram_health.assess(ITEM, {"url": "", "pending_update_count": 4}, now=NOW)
    assert codes(problems) == ["url_mismatch"]
    assert "no webhook registered" in problems[0]["detail"]


def test_backlog_and_dropped_update_types_are_failing():
    info = {**HEALTHY, "pending_update_count": 25, "allowed_updates": ["message"]}
    assert codes(telegram_health.assess(ITEM, info, now=NOW)) == ["backlog", "missing_updates"]


def _transport(result, calls):
    def transport(method, url, headers=None, body=None, timeout=10):
        calls.append(url.rsplit("/", 1)[-1])
        return 200, json.dumps({"ok": True, "result": result}).encode()
    return transport


def _patch_items(monkeypatch, items):
    monkeypatch.setattr(hook_triggers, "load_items", lambda table_ref=None: items)
    monkeypatch.setattr(telegram_health, "_token", lambda item, connections_table=None: "1:tok")


def test_check_reports_each_enabled_telegram_trigger(monkeypatch):
    other = {"hook_id": "orders", "kind": "webhook", "url": "u"}
    off = {**ITEM, "hook_id": "off", "enabled": False}
    _patch_items(monkeypatch, [ITEM, other, off])
    calls = []
    rows = telegram_health.check(transport=_transport({**HEALTHY, "pending_update_count": 2}, calls))
    assert calls == ["getWebhookInfo"]
    assert [(row["hook_id"], row["status"], row["pending_update_count"]) for row in rows] == [
        ("automator-telegram", "ok", 2)]


def test_check_counts_an_unreachable_bot_as_failing(monkeypatch):
    _patch_items(monkeypatch, [ITEM])

    def boom(token, transport=None):
        raise telegram_api.TelegramApiError("Telegram rejected getWebhookInfo: Unauthorized")

    monkeypatch.setattr(telegram_api, "get_webhook_info", boom)
    [row] = telegram_health.check()
    assert row["status"] == "failing"
    assert codes(row["problems"]) == ["probe_failed"]


def test_repair_reregisters_with_the_stored_secret(monkeypatch):
    _patch_items(monkeypatch, [ITEM])
    registered = []
    monkeypatch.setattr(hook_triggers, "_register_telegram",
                        lambda item, **kwargs: registered.append((item["url"], item["token"])))
    assert telegram_health.repair() == ["automator-telegram"]
    assert registered == [(URL, "s3cret")]
    with pytest.raises(hook_triggers.TriggerError):
        telegram_health.repair("missing")


class FakeCloudWatch:
    def __init__(self):
        self.data = []

    def put_metric_data(self, **kwargs):
        self.data.append(kwargs)


class FakeSes:
    def __init__(self):
        self.sent = []

    def send_email(self, **kwargs):
        self.sent.append(kwargs)


def test_handler_publishes_metric_and_alerts_hourly(monkeypatch):
    failing = telegram_health._report(ITEM, {"url": ""}, [{"code": "url_mismatch", "detail": "gone"}])
    monkeypatch.setattr(telegram_health, "check", lambda: [failing])
    cloudwatch, ses = FakeCloudWatch(), FakeSes()
    real_put_metric = telegram_health._put_metric
    monkeypatch.setattr(telegram_health, "_put_metric",
                        lambda count: real_put_metric(count, cloudwatch))
    claimed = set()

    def claim(scope, key, **kwargs):
        if key in claimed:
            return False
        claimed.add(key)
        return True

    monkeypatch.setattr("src.dapier.triggers.seen.claim", claim)
    monkeypatch.setattr("src.dapier.engine.notify.ses_client", lambda: ses)
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "dev@example.test")
    monkeypatch.setenv("DAPIER_ALERT_SENDER", "alerts@example.test")
    first = telegram_health.handler()
    second = telegram_health.handler()
    assert (first["failing"], first["alerted"]) == (1, True)
    assert second["alerted"] is False  # same hour: throttled
    assert [d["MetricData"][0]["Value"] for d in cloudwatch.data] == [1.0, 1.0]
    [mail] = ses.sent
    assert mail["Destination"]["ToAddresses"] == ["dev@example.test"]
    assert "automator-telegram" in mail["Message"]["Subject"]["Data"]
    assert "--repair" in mail["Message"]["Body"]["Text"]["Data"]


def test_put_metric_shape():
    cloudwatch = FakeCloudWatch()
    telegram_health._put_metric(0, cloudwatch)
    [call] = cloudwatch.data
    assert call["Namespace"] == "Dapier/Triggers"
    assert call["MetricData"][0]["MetricName"] == "TelegramWebhookUnhealthy"


def test_agent_route_get_and_repair(monkeypatch):
    from src.dapier.api import agent as agent_api

    monkeypatch.setattr("src.dapier.api.agent.authenticate", lambda event: ("sub-1", None))
    monkeypatch.setattr("src.dapier.api.agent.authz.is_operator", lambda payload: True)
    monkeypatch.setattr("src.dapier.api.agent.audit.emit", lambda *a, **k: None)
    row = telegram_health._report(ITEM, HEALTHY, [])
    monkeypatch.setattr(telegram_health, "check", lambda: [row])
    monkeypatch.setattr(telegram_health, "repair", lambda name=None: ["automator-telegram"])
    path = "/api/agent/hook-triggers/telegram-health"
    response = agent_api.route({"headers": {}}, "GET", path)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["triggers"][0]["status"] == "ok"
    response = agent_api.route({"headers": {}, "body": json.dumps({"name": "automator-telegram"})},
                               "POST", path)
    assert json.loads(response["body"])["repaired"] == ["automator-telegram"]


def test_agent_route_requires_operator(monkeypatch):
    from src.dapier.api import agent as agent_api

    monkeypatch.setattr("src.dapier.api.agent.require_operator",
                        lambda event, action: (None, {"statusCode": 403}))
    response = agent_api.route({"headers": {}}, "GET", "/api/agent/hook-triggers/telegram-health")
    assert response["statusCode"] == 403


def test_cli_telegram_health(monkeypatch, capsys):
    from dapier_cli import main

    calls = []
    failing = telegram_health._report(ITEM, {"url": "", "pending_update_count": 7},
                                      [{"code": "url_mismatch", "detail": "no webhook"}])

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "POST":
            return {"repaired": ["automator-telegram"],
                    "triggers": [telegram_health._report(ITEM, HEALTHY, [])]}
        return {"triggers": [failing]}

    monkeypatch.setattr("dapier_cli.commands.hooks.api.call", fake_call)
    assert main.main(["webhooks", "telegram-health"]) == 1
    assert main.main(["webhooks", "telegram-health", "automator-telegram", "--repair"]) == 0
    assert calls == [("GET", "/api/agent/hook-triggers/telegram-health", None),
                     ("POST", "/api/agent/hook-triggers/telegram-health",
                      {"name": "automator-telegram"})]
    out = capsys.readouterr().out
    assert "FAILING" in out and "PROBLEM url_mismatch" in out
    assert "Re-registered: automator-telegram" in out
