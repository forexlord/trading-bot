from src.telegram import TelegramAlerts


class FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text


def test_send_does_nothing_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr("src.telegram.requests.post", lambda *a, **k: calls.append((a, k)) or FakeResponse())

    alerts = TelegramAlerts(bot_token="", chat_id="")
    alerts.send("hello")

    assert calls == []


def test_send_posts_to_telegram_api_when_enabled(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse()

    monkeypatch.setattr("src.telegram.requests.post", fake_post)

    alerts = TelegramAlerts(bot_token="TOKEN123", chat_id="CHAT456")
    alerts.send("hello world")

    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert payload == {"chat_id": "CHAT456", "text": "hello world"}


def test_trade_opened_formats_message(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "src.telegram.requests.post",
        lambda url, json, timeout: captured.update(json) or FakeResponse(),
    )
    alerts = TelegramAlerts(bot_token="T", chat_id="C")
    alerts.trade_opened("EURUSD", "LONG", 0.02, 1.10500, 1.10300, 1.10900)

    assert "EURUSD" in captured["text"]
    assert "LONG" in captured["text"]


def test_send_logs_error_on_non_200_without_raising(monkeypatch):
    monkeypatch.setattr(
        "src.telegram.requests.post", lambda *a, **k: FakeResponse(status_code=400, text="bad request")
    )
    alerts = TelegramAlerts(bot_token="T", chat_id="C")
    alerts.send("hello")  # must not raise
