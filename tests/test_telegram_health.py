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


def _channel_row(missed_posts, hook="automator-telegram", chat_id=-1001435532197):
    return {"hook_id": hook, "channel": "mlzoomcamp", "chat_id": chat_id,
            "received_max_message_id": 268, "public_max_message_id": 270,
            "missed": [post["message_id"] for post in missed_posts],
            "missed_posts": missed_posts, "status": "failing",
            "problems": [{"code": "missed_posts", "detail": "x"}]}


def test_auto_relay_relays_text_posts_on_a_healthy_webhook(monkeypatch):
    _patch_items(monkeypatch, [ITEM])
    posts = [{"message_id": 269, "date": 1794000000, "text": "Hello classmates"},
             {"message_id": 270, "date": 1794000100, "text": ""}]  # media post
    posts[1]["text"] = "  "
    sent = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        sent.append((method, url, headers, body))
        return 200, b'{"accepted": true, "event_id": "e1"}'

    result = telegram_health.auto_relay([_channel_row(posts)],
                                        [{"hook_id": "automator-telegram", "status": "ok",
                                          "pending_update_count": 0}],
                                        transport=transport)
    [hook] = result
    assert result[hook]["relayed"] == [269]
    assert result[hook]["errors"] == [(270, "no text in the public preview (media post)")]
    [call] = sent
    method, url, headers, body = call
    assert (method, url) == ("POST", URL)
    assert headers["x-telegram-bot-api-secret-token"] == "1:tok"
    update = json.loads(body)
    assert update["update_id"] == "relay--1001435532197-269"
    assert update["channel_post"]["message_id"] == 269
    assert update["channel_post"]["chat"] == {"id": -1001435532197, "type": "channel",
                                              "username": "mlzoomcamp"}
    assert update["channel_post"]["text"] == "Hello classmates"


def test_auto_relay_skips_failing_or_pending_webhooks(monkeypatch):
    posts = [{"message_id": 269, "date": 1794000000, "text": "Hello"}]
    sent = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        sent.append(url)
        return 200, b"{}"

    failing = [{"hook_id": "automator-telegram", "status": "failing",
                "pending_update_count": 0}]
    pending = [{"hook_id": "automator-telegram", "status": "ok",
                "pending_update_count": 3}]
    assert telegram_health.auto_relay([_channel_row(posts)], failing,
                                      transport=transport) == {}
    assert telegram_health.auto_relay([_channel_row(posts)], pending,
                                      transport=transport) == {}
    assert sent == []  # Telegram holds those posts and will deliver them itself


def test_auto_relay_reports_a_failed_post_without_raising(monkeypatch):
    _patch_items(monkeypatch, [ITEM])
    posts = [{"message_id": 269, "date": 1794000000, "text": "Hello"}]

    def transport(method, url, *, headers=None, body=None, timeout=15):
        return 502, b"bad gateway"

    result = telegram_health.auto_relay([_channel_row(posts)],
                                        [{"hook_id": "automator-telegram", "status": "ok",
                                          "pending_update_count": 0}],
                                        transport=transport)
    assert result["automator-telegram"]["relayed"] == []
    assert "HTTP 502" in result["automator-telegram"]["errors"][0][1]


def test_handler_relays_missed_posts(monkeypatch):
    healthy = telegram_health._report(ITEM, dict(HEALTHY), [])
    posts = [{"message_id": 269, "date": 1794000000, "text": "Hello"}]
    monkeypatch.setattr(telegram_health, "check", lambda: [healthy])
    monkeypatch.setattr(telegram_health, "channel_check",
                        lambda: [_channel_row(posts)])
    _patch_items(monkeypatch, [ITEM])
    relayed_calls = []
    monkeypatch.setattr(telegram_health, "auto_relay",
                        lambda channels, rows: relayed_calls.append((channels, rows)) or
                        {"automator-telegram": {"relayed": [269], "errors": []}})
    monkeypatch.setattr("src.dapier.triggers.seen.claim", lambda *a, **k: True)
    monkeypatch.setattr("src.dapier.engine.notify.ses_client", lambda: FakeSes())
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "dev@example.test")
    cloudwatch = FakeCloudWatch()
    real_put_metric = telegram_health._put_metric
    monkeypatch.setattr(telegram_health, "_put_metric",
                        lambda count: real_put_metric(count, cloudwatch))
    result = telegram_health.handler()
    assert result["relayed"] == {"automator-telegram": {"relayed": [269], "errors": []}}
    [row] = [r for r in result["triggers"] if "channel" in r]
    assert row["relayed"] == [269]


def test_handler_publishes_metric_and_alerts_hourly(monkeypatch):
    failing = telegram_health._report(ITEM, {"url": ""}, [{"code": "url_mismatch", "detail": "gone"}])
    monkeypatch.setattr(telegram_health, "check", lambda: [failing])
    monkeypatch.setattr(telegram_health, "channel_check", lambda: [])
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
    monkeypatch.setattr(telegram_health, "channel_check", lambda: [])
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
        return {"triggers": [failing], "channels": [
            {"hook_id": "automator-telegram", "channel": "mlzoomcamp", "status": "failing",
             "received_max_message_id": 268, "public_max_message_id": 269,
             "problems": [{"code": "missed_posts", "detail": "t.me/mlzoomcamp/269 never reached"}]}]}

    monkeypatch.setattr("dapier_cli.commands.hooks.api.call", fake_call)
    assert main.main(["webhooks", "telegram-health"]) == 1
    assert main.main(["webhooks", "telegram-health", "automator-telegram", "--repair"]) == 0
    assert calls == [("GET", "/api/agent/hook-triggers/telegram-health", None),
                     ("POST", "/api/agent/hook-triggers/telegram-health",
                      {"name": "automator-telegram"})]
    out = capsys.readouterr().out
    assert "FAILING" in out and "PROBLEM url_mismatch" in out
    assert "channel @mlzoomcamp" in out and "PROBLEM missed_posts" in out
    assert "Re-registered: automator-telegram" in out


# --- channel check: posts Telegram never sent (the bot's own posts) -----------

def _post_event(hook, username, chat_id, message_id):
    return {"connector": "telegram", "source": hook,
            "data": {"hook": hook, "is_channel_post": True, "message_id": message_id,
                     "chat": {"id": chat_id, "type": "channel", "username": username}}}


def _preview(*posts):
    """``(message_id, datetime[, text])`` tuples -> one t.me/s preview page."""
    blocks = ""
    for post in posts:
        post_id, when = post[0], post[1]
        text = post[2] if len(post) > 2 else None
        body = (f'<div class="tgme_widget_message_text js-message_text" dir="auto">'
                f'{text}</div>' if text is not None else "")
        blocks += (
            f'<div class="tgme_widget_message_wrap"><div data-post="mlzoomcamp/{post_id}">'
            f'<time datetime="{when}" class="time">x</time>{body}</div></div>')
    return f"<html>{blocks}</html>"


def test_public_posts_parses_the_preview():
    page = _preview((268, "2026-11-15T08:05:08+00:00"),
                    (269, "2026-11-18T10:51:24+00:00",
                     'Deadline is near.<br/><br/>Submit at '
                     '<a href="https://courses.example.test/eval" target="_blank">the form</a>'),
                    (270, "2026-11-18T12:27:40+00:00", ""))
    posts = telegram_health.public_posts(page)
    assert [post[0] for post in posts] == [268, 269, 270]
    assert posts[0][2] == ""
    assert posts[1][2] == "Deadline is near.\n\nSubmit at https://courses.example.test/eval"
    assert posts[2][2] == ""


def test_channel_check_flags_posts_that_never_arrived(monkeypatch):
    _patch_items(monkeypatch, [ITEM])
    events = [_post_event("automator-telegram", "mlzoomcamp", -1001435532197, 268),
              {"connector": "telegram", "data": {"hook": "automator-telegram", "chat": {"id": 1}}}]
    page = _preview((268, "2026-11-15T08:05:08+00:00"),   # received
                    (269, "2026-11-18T10:51:24+00:00"),   # missed, 2h old
                    (270, "2026-11-18T12:45:00+00:00"))   # inside the grace window
    [row] = telegram_health.channel_check(events=events, fetch=lambda username: page, now=NOW)
    assert row["status"] == "failing" and row["missed"] == [269]
    assert row["problems"][0]["code"] == "missed_posts"
    assert "dapier webhooks test automator-telegram" in row["problems"][0]["detail"]


def test_channel_check_ok_when_caught_up_and_ignores_old_gaps(monkeypatch):
    _patch_items(monkeypatch, [ITEM])
    events = [_post_event("automator-telegram", "mlzoomcamp", -1, 192)]
    old_gap = _preview((193, "2026-11-03T08:12:40+00:00"))  # older than the max age
    [row] = telegram_health.channel_check(events=events, fetch=lambda u: old_gap, now=NOW)
    assert row["status"] == "ok"
    caught_up = _preview((192, "2026-11-18T10:00:00+00:00"))
    [row] = telegram_health.channel_check(events=events, fetch=lambda u: caught_up, now=NOW)
    assert row["status"] == "ok"


def test_channel_check_preview_failure_is_unknown_not_failing(monkeypatch):
    _patch_items(monkeypatch, [ITEM])
    events = [_post_event("automator-telegram", "mlzoomcamp", -1, 1)]

    def boom(username):
        raise OSError("blocked")

    [row] = telegram_health.channel_check(events=events, fetch=boom, now=NOW)
    assert row["status"] == "unknown" and "blocked" in row["preview_error"]


def test_channel_check_skips_disabled_triggers(monkeypatch):
    _patch_items(monkeypatch, [])
    events = [_post_event("automator-telegram", "mlzoomcamp", -1, 1)]
    assert telegram_health.channel_check(events=events, fetch=lambda u: "", now=NOW) == []
