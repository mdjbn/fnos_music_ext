"""PushPlus 推送模块测试：开关门控、节流、错误码处理、凭据不泄露。"""
import json
import logging

import httpx
import pytest

from proxy import pushplus


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TOKEN", raising=False)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_ENABLED", raising=False)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TOPIC", raising=False)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_TEMPLATE", raising=False)
    monkeypatch.delenv("FNMUSIC_PUSHPLUS_URL", raising=False)
    monkeypatch.setattr(pushplus, "_MIN_INTERVAL_S", 0.0)
    pushplus.reset_throttle()
    yield
    pushplus.reset_throttle()


def _enable(monkeypatch, token="t" * 32):
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "true")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOKEN", token)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ 开关 ----

def test_disabled_without_token(monkeypatch):
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "true")
    assert pushplus.enabled() is False, "没有 token 就不该发任何推送"


def test_disabled_by_flag_even_with_token(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "false")
    assert pushplus.enabled() is False


@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes", "on"])
def test_enabled_flag_values(monkeypatch, flag):
    _enable(monkeypatch)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", flag)
    assert pushplus.enabled() is True


@pytest.mark.parametrize("flag", ["false", "0", "no", "off", "garbage"])
def test_disabled_flag_values(monkeypatch, flag):
    _enable(monkeypatch)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", flag)
    assert pushplus.enabled() is False


def test_empty_flag_falls_back_to_default_enabled(monkeypatch):
    """空值等同未设置 → 取默认（启用）。与 CONF 里其它开关的解析口径一致。"""
    _enable(monkeypatch)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_ENABLED", "")
    assert pushplus.enabled() is True


def test_defaults(monkeypatch):
    assert pushplus.push_url() == "https://www.pushplus.plus/send"
    assert pushplus.template() == "markdown"
    assert pushplus.topic() == ""
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_URL", "https://example.invalid/send/")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TEMPLATE", "TXT")
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOPIC", "group1")
    assert pushplus.push_url() == "https://example.invalid/send/"
    assert pushplus.template() == "txt", "模板名应归一化为小写"
    assert pushplus.topic() == "group1"


def test_code_hints_cover_documented_codes():
    assert pushplus.code_hint(pushplus.CODE_BAD_TOKEN) == "用户 token 无效"
    assert "实名" in pushplus.code_hint(pushplus.CODE_NOT_REALNAME)
    assert "限流" in pushplus.code_hint(pushplus.CODE_RATE_LIMITED)
    assert "积分" in pushplus.code_hint(pushplus.CODE_NO_CREDIT)
    assert pushplus.code_hint(12345) == "未知错误"


# ------------------------------------------------------------------ 成功 ----

@pytest.mark.anyio
async def test_send_success(monkeypatch):
    _enable(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200, "msg": "请求成功", "data": "flowid123"})

    ok = await pushplus.send(_client(handler), "标题", "内容")
    assert ok is True
    assert seen["url"] == "https://www.pushplus.plus/send"
    assert seen["body"]["token"] == "t" * 32
    assert seen["body"]["title"] == "标题"
    assert seen["body"]["content"] == "内容"
    assert seen["body"]["template"] == "markdown"
    assert "topic" not in seen["body"], "未配置群组时不该带 topic 键"


@pytest.mark.anyio
async def test_send_includes_topic_when_configured(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("FNMUSIC_PUSHPLUS_TOPIC", "mygroup")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200})

    assert await pushplus.send(_client(handler), "t", "c") is True
    assert seen["body"]["topic"] == "mygroup"


@pytest.mark.anyio
async def test_send_truncates_long_title(monkeypatch):
    """PushPlus 免费档标题上限 100 字。"""
    _enable(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200})

    await pushplus.send(_client(handler), "长" * 500, "c")
    assert len(seen["body"]["title"]) == 100


@pytest.mark.anyio
async def test_send_custom_template(monkeypatch):
    _enable(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200})

    await pushplus.send(_client(handler), "t", "c", tpl="html")
    assert seen["body"]["template"] == "html"


@pytest.mark.anyio
async def test_send_creates_and_closes_own_client(monkeypatch):
    """未传 client 时自建并在结束后关闭，不泄漏连接。"""
    _enable(monkeypatch)
    created = []
    orig_init = httpx.AsyncClient.__init__

    def spy_init(self, *a, **kw):
        created.append(self)
        kw.setdefault("transport", httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 200})))
        orig_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy_init)
    assert await pushplus.send(None, "t", "c") is True
    assert created and created[0].is_closed


# ------------------------------------------------------------------ 节流 ----

@pytest.mark.anyio
async def test_duplicate_content_deduped_within_ttl(monkeypatch):
    _enable(monkeypatch)
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 200})

    client = _client(handler)
    assert await pushplus.send(client, "同标题", "同内容") is True
    assert await pushplus.send(client, "同标题", "同内容") is False
    assert n["c"] == 1, "PushPlus 免费档同内容每小时限 3 条，必须本地去重"

    # 不同内容照常发送
    assert await pushplus.send(client, "同标题", "别的内容") is True
    assert n["c"] == 2


@pytest.mark.anyio
async def test_force_bypasses_dedupe(monkeypatch):
    _enable(monkeypatch)
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 200})

    client = _client(handler)
    assert await pushplus.send(client, "t", "c") is True
    assert await pushplus.send(client, "t", "c", force=True) is True
    assert n["c"] == 2


@pytest.mark.anyio
async def test_min_interval_throttles(monkeypatch):
    """全局最小间隔生效（免费档每分钟 5 次）。"""
    _enable(monkeypatch)
    monkeypatch.setattr(pushplus, "_MIN_INTERVAL_S", 0.05)
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 200})

    client = _client(handler)
    await pushplus.send(client, "a", "1")
    await pushplus.send(client, "b", "2")
    assert n["c"] == 2


@pytest.mark.anyio
async def test_reset_throttle_clears_window(monkeypatch):
    _enable(monkeypatch)
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 200})

    client = _client(handler)
    await pushplus.send(client, "t", "c")
    pushplus.reset_throttle()
    assert await pushplus.send(client, "t", "c") is True
    assert n["c"] == 2


# ---------------------------------------------------------------- 错误处理 ----

@pytest.mark.anyio
@pytest.mark.parametrize("code", [903, 900, 888, 905, 500, 302, 401])
async def test_send_returns_false_on_rejection_codes(monkeypatch, code):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": code, "msg": "x", "data": None})

    ok = await pushplus.send(_client(handler), f"标题{code}", "内容")
    assert ok is False


@pytest.mark.anyio
async def test_permanent_config_errors_clear_dedupe(monkeypatch, caplog):
    """token 无效属永久配置错误：清掉去重指纹并打 error 日志，而不是静默重试。"""
    _enable(monkeypatch)
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 903, "msg": "用户令牌不正确", "data": "无效的用户token"})

    with caplog.at_level(logging.ERROR, logger="fnmusic_proxy.pushplus"):
        assert await pushplus.send(_client(handler), "t", "c") is False
        # 指纹已被清掉，所以第二次仍会真正发出去（而非被去重吞掉）
        assert await pushplus.send(_client(handler), "t", "c") is False
    assert n["c"] == 2
    assert any("PushPlus 配置有误" in r.message for r in caplog.records)


@pytest.mark.anyio
async def test_send_swallows_network_errors(monkeypatch):
    """推送失败绝不能把异常抛回播放主链路。"""
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS 解析失败")

    assert await pushplus.send(_client(handler), "t", "c") is False


@pytest.mark.anyio
async def test_send_handles_http_500(monkeypatch):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nginx error")

    assert await pushplus.send(_client(handler), "t", "c") is False


@pytest.mark.anyio
async def test_send_handles_non_dict_body(monkeypatch):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected", "array"])

    assert await pushplus.send(_client(handler), "t", "c") is False


@pytest.mark.anyio
async def test_send_noop_when_disabled(monkeypatch):
    """未启用时连请求都不该发出。"""
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 200})

    assert await pushplus.send(_client(handler), "t", "c") is False
    assert n["c"] == 0


@pytest.mark.anyio
async def test_send_noop_on_empty_payload(monkeypatch):
    _enable(monkeypatch)
    n = {"c": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(200, json={"code": 200})

    assert await pushplus.send(_client(handler), "", "") is False
    assert n["c"] == 0


# ------------------------------------------------------------------ 安全 ----

@pytest.mark.anyio
async def test_token_never_logged(monkeypatch, caplog):
    secret = "s3cr3t-pushplus-token-value-1234567890"
    _enable(monkeypatch, token=secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200})

    with caplog.at_level(logging.DEBUG):
        await pushplus.send(_client(handler), "标题", "内容")
    assert secret not in caplog.text
    assert secret not in json.dumps([r.message for r in caplog.records])


@pytest.mark.anyio
async def test_token_never_logged_on_failure(monkeypatch, caplog):
    secret = "s3cr3t-pushplus-token-value-1234567890"
    _enable(monkeypatch, token=secret)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 903, "msg": secret})

    with caplog.at_level(logging.DEBUG):
        await pushplus.send(_client(handler), "标题", "内容")
    assert secret not in caplog.text
