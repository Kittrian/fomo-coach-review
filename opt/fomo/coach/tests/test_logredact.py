import logging

import httpx

from app import logredact


def test_redact_patterns():
    assert logredact.redact("POST https://mainnet.helius-rpc.com/?api-key=abc-123 \"HTTP/1.1 200 OK\"") == \
        "POST https://mainnet.helius-rpc.com/?api-key=<redacted> \"HTTP/1.1 200 OK\""
    assert logredact.redact("x?api_key=Q&b=1") == "x?api_key=<redacted>&b=1"
    assert logredact.redact("wss://h/?apikey=Z") == "wss://h/?apikey=<redacted>"
    assert logredact.redact("https://api.mainnet-beta.solana.com") == "https://api.mainnet-beta.solana.com"


def test_httpx_request_log_is_redacted(caplog):
    logredact.install()
    logging.getLogger("httpx").setLevel(logging.INFO)
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"result": 1}))
    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx.Client(transport=t).post("https://mainnet.helius-rpc.com/?api-key=REDACTED", json={})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "HTTP Request" in text and "FAKE-KEY-123" not in text and "api-key=<redacted>" in text


def test_install_idempotent_and_main_installs():
    import app.main  # noqa: F401  importing main runs install()
    logredact.install()
    logredact.install()
    assert logging.getLogger("httpx").filters.count(logredact._FILTER) == 1
