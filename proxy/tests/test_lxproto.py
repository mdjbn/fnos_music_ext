"""lx-music-sync-server 协议层测试：AES / RSA-OAEP / WebSocket / message2call。

这些测试不联网：node 相关的互操作测试在本机没有 node 时自动跳过（飞牛上大概率没有 node，
但开发机与 CI 有，能挡住「只有我们自洽、和真实客户端不兼容」这类问题）。
"""
import asyncio
import base64
import hashlib
import json
import shutil

import pytest

from proxy import lxproto as P

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="需要 node 做互操作校验")


def _run(coro):
    return asyncio.run(coro)


def _node(script: str, *args: str) -> str:
    import subprocess

    r = subprocess.run([NODE, "-e", script, "--", *args], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node 失败: {r.stderr[:400]}"
    return r.stdout.strip()


# ------------------------------------------------------------------ AES ----

def test_aes_fips197_vector_and_pkcs7():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    ct = P.aes_ecb_encrypt(pt, key)
    # PKCS#7：16 字节明文会被补齐成 32 字节（多一个整块），首个分组仍是 FIPS-197 的值
    assert len(ct) == 32
    assert ct[:16].hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert P.aes_ecb_decrypt(ct, key) == pt


@pytest.mark.parametrize("text", ["lx-music connect", "lx-music auth::", "A" * 16,
                                 "Hello~::^-^::~v4~", "中文歌单名", ""])
def test_aes_pkcs7_roundtrip(text):
    key = hashlib.md5(b"pw").hexdigest()[:16].encode()
    ct = P.aes_ecb_encrypt(text.encode("utf-8"), key)
    assert len(ct) % 16 == 0 and len(ct) >= 16
    assert P.aes_ecb_decrypt(ct, key).decode("utf-8") == text


@requires_node
def test_aes_matches_node_byte_for_byte():
    """Node `createCipheriv('aes-128-ecb', key, '')` 默认 PKCS#7 + autoPadding，
    我们必须逐字节一致，否则握手密文服务端解不开（`cipher.final()` 直接抛错）。"""
    node = ("const c=require('crypto');"
            "const k=Buffer.from(process.argv[1],'hex');"
            "const ci=c.createCipheriv('aes-128-ecb',k,'');"
            "console.log(Buffer.concat([ci.update(Buffer.from(process.argv[2],'utf8')),ci.final()]).toString('base64'));")
    key_hex = "000102030405060708090a0b0c0d0e0f"
    key = bytes.fromhex(key_hex)
    for text in ("lx-music connect", "lx-music auth::", "A" * 16, "中文歌单名"):
        needle = base64.b64decode(_node(node, key_hex, text))
        assert P.aes_ecb_encrypt(text.encode(), key) == needle


def test_aes_key_derivations():
    # 服务端口径：md5(密码) 十六进制的前 16 个字符当 AES 密钥
    assert P.aes_key_from_password("testpass123") == hashlib.md5(b"testpass123").hexdigest()[:16].encode()
    raw = bytes(range(16))
    assert P.aes_key_from_session(base64.b64encode(raw).decode()) == raw


# ------------------------------------------------------------------ RSA ----

@requires_node
def test_rsa_spki_is_accepted_by_node_and_oaep_decrypts():
    key = P.generate_rsa_keypair(bits=2048)
    assert key.bits == 2048
    node = ("const c=require('crypto');"
            "const k=c.createPublicKey(process.argv[1]);"
            "console.log(k.asymmetricKeyType, k.asymmetricKeyDetails.modulusLength);"
            "console.log(c.publicEncrypt({key:process.argv[1],padding:c.constants.RSA_PKCS1_OAEP_PADDING},"
            "Buffer.from(process.argv[2],'utf8')).toString('base64'));")
    msg = '{"clientId":"a/b+c==","key":"k1k2","serverName":"fnOS"}'
    out = _node(node, key.public_pem(), msg).split("\n")
    assert out[0].split() == ["rsa", "2048"]
    assert key.oaep_sha1_decrypt(base64.b64decode(out[1])).decode() == msg


def test_rsa_keygen_is_reasonably_fast_and_unique():
    import time

    t = time.monotonic()
    a, b = P.generate_rsa_keypair(bits=1024), P.generate_rsa_keypair(bits=1024)
    assert time.monotonic() - t < 30
    assert a.n != b.n and a.p * a.q == a.n
    assert pow(pow(42, a.e, a.n), a.d, a.n) == 42     # RSA 往返


def test_oaep_rejects_wrong_length():
    key = P.generate_rsa_keypair(bits=1024)
    with pytest.raises(ValueError):
        key.oaep_sha1_decrypt(b"short")


# --------------------------------------------------------------- URL/帧 ----

def test_normalize_base_url_and_ws_url():
    assert P.normalize_base_url("192.168.1.5:9527") == "http://192.168.1.5:9527"
    assert P.normalize_base_url("ws://h:1/") == "http://h:1"
    assert P.normalize_base_url("wss://h/") == "https://h"
    # 增强版（lxserver 等）用「一个用户一个连接路径」：路径必须原样保留
    assert P.normalize_base_url("https://h:9528/Xsj1020999515/") == "https://h:9528/Xsj1020999515"
    token = base64.b64encode(bytes(range(16))).decode()   # 会话 key 恒为 16 字节的 base64
    url = P.ws_url_of("http://h:9527", "cid/x=", token)
    assert url.startswith("ws://h:9527/?i=")
    assert P.ws_url_of("https://h:9528/Code1", "cid", token).startswith("wss://h:9528/Code1/?i=")
    q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
    from urllib.parse import unquote
    assert unquote(q["i"]) == "cid/x="
    # t 必须能解回明文 'lx-music connect'（服务端就是拿它做 AES 校验的）
    plain = P.aes_ecb_decrypt(base64.b64decode(unquote(q["t"])), P.aes_key_from_session(token))
    assert plain == b"lx-music connect"


def test_message_encoding_uses_cg_gzip_over_1024():
    small = P.encode_message({"name": "x", "path": ["a"], "data": [1]})
    assert not small.startswith("cg_") and json.loads(small)["name"] == "x"
    big = P.encode_message({"name": "y", "path": ["b"], "data": ["中文" * 800]})
    assert big.startswith("cg_") and len(big) < len("中文" * 800)
    assert P.decode_message(big)["path"] == ["b"]
    with pytest.raises(P.LxSyncError):
        P.decode_message(json.dumps([1, 2, 3]))


# ------------------------------------------------- 假服务端：WS + RPC 全链路 ----

_ACCEPT_SALT = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


async def _read_frame(reader):
    head = await reader.readexactly(2)
    fin, opcode = bool(head[0] & 0x80), head[0] & 0x0F
    masked, length = bool(head[1] & 0x80), head[1] & 0x7F
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


async def _write_frame(writer, opcode, payload, fin=True):
    header = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += n.to_bytes(2, "big")
    else:
        header.append(127)
        header += n.to_bytes(8, "big")
    writer.write(bytes(header) + payload)
    await writer.drain()


class _FakeSyncServer:
    """极简 WS 服务端：只实现这个测试需要的那点协议。"""

    def __init__(self, handler):
        self.handler = handler
        self.server = None
        self.port = 0
        self.request_line = ""

    async def __aenter__(self):
        async def on_client(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                self.request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
                key = ""
                for line in head.decode("latin-1").split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept = base64.b64encode(hashlib.sha1((key + _ACCEPT_SALT).encode()).digest()).decode()
                writer.write(
                    ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode()
                )
                await writer.drain()
                await self.handler(reader, writer)
            except (asyncio.IncompleteReadError, ConnectionResetError):
                pass
            finally:
                writer.close()

        self.server = await asyncio.start_server(on_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


def test_websocket_and_rpc_roundtrip():
    """服务端先调我们（getEnabledFeatures）→ 我们应答；再发 ping → 自动回 pong；
    最后我们调服务端并拿到结果。整个过程覆盖掩码、帧长、ping、请求/应答配对。"""
    seen = {}

    async def handler(reader, writer):
        await _write_frame(writer, 0x1, P.encode_message({
            "name": "getEnabledFeatures__1", "path": ["getEnabledFeatures"],
            "data": ["server", {"list": 1, "dislike": 1}]}).encode())
        await _write_frame(writer, 0x9, b"ping!")          # WS 级 ping
        # 客户端可能在处理 ping 之前就把请求写出去了 ⇒ 不能按顺序假设，
        # 一律按内容分类，并且收齐「应答 + pong + 请求」三样才结束。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5
        while loop.time() < deadline and not {"reply", "pong", "request"} <= set(seen):
            try:
                _, opcode, payload = await asyncio.wait_for(_read_frame(reader), timeout=1)
            except asyncio.TimeoutError:
                continue
            if opcode == 0xA:
                seen["pong"] = opcode
                continue
            msg = P.decode_message(payload.decode())
            if msg.get("path") == ["onListSyncAction"]:
                seen["request"] = msg
                await _write_frame(writer, 0x1, P.encode_message({
                    "name": msg["name"], "error": None, "data": {"snapshot": "abc"}}).encode())
            else:
                seen["reply"] = msg
        await asyncio.sleep(0.1)

    async def main():
        async with _FakeSyncServer(handler) as srv:
            ws = await P.LxWebSocket.connect(f"ws://127.0.0.1:{srv.port}/", timeout=5)
            called = {}

            def on_features(module, features):
                called["features"] = (module, features)
                return {"list": {"skipSnapshot": True}, "dislike": False}

            rpc = P.LxRpc(ws, {"getEnabledFeatures": on_features})
            pump = asyncio.get_event_loop().create_task(_pump(rpc))
            result = await rpc.call(["onListSyncAction"], {"action": "x"}, timeout=5)
            pump.cancel()
            await ws.close()
            return called, result, seen

    called, result, seen = _run(main())
    assert called["features"] == ("server", {"list": 1, "dislike": 1})
    assert seen["reply"] == {"name": "getEnabledFeatures__1", "error": None,
                            "data": {"list": {"skipSnapshot": True}, "dislike": False}}
    assert seen["pong"] == 0xA
    assert seen["request"]["path"] == ["onListSyncAction"]
    assert seen["request"]["data"] == [{"action": "x"}]
    assert result == {"snapshot": "abc"}


def test_websocket_reads_fragmented_text_message():
    """服务端把一条大文本拆成「FIN=0 文本帧 + 续帧」下发时必须能拼回来。"""
    payload = json.dumps({"name": "big", "data": "中文" * 300}, ensure_ascii=False)

    async def handler(reader, writer):  # noqa: ARG001
        raw = payload.encode()
        half = len(raw) // 2
        await _write_frame(writer, 0x1, raw[:half], fin=False)
        await _write_frame(writer, 0x0, raw[half:], fin=True)
        await asyncio.sleep(0.2)

    async def main():
        async with _FakeSyncServer(handler) as srv:
            ws = await P.LxWebSocket.connect(f"ws://127.0.0.1:{srv.port}/prefix/Code1/", timeout=5)
            got = await ws.recv_text()
            await ws.close()
            return got, srv.request_line

    got, line = _run(main())
    assert json.loads(got)["data"] == "中文" * 300
    # 连接路径（= 增强版的「连接码/用户名」）必须原样带上去，否则服务端 401/403
    assert line.startswith("GET /prefix/Code1/")


def test_retry_helper_retries_transient_errors_then_succeeds():
    """公网反代偶发 TLS 重置（用户那台实测就会）：必须重试，不能一失败就放弃。"""
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("TLS/SSL connection has been closed (EOF)")
        return "ok"

    assert _run(P._retry(flaky, attempts=3, delay=0)) == "ok"
    assert len(calls) == 3


def test_retry_helper_reraises_after_attempts():
    calls = []

    async def always_fail():
        calls.append(1)
        raise ConnectionError("boom")

    with pytest.raises(ConnectionError):
        _run(P._retry(always_fail, attempts=2, delay=0))
    assert len(calls) == 2


def test_tls_insecure_flag_reads_env(monkeypatch):
    monkeypatch.delenv("FNMUSIC_LX_SYNC_INSECURE_TLS", raising=False)
    assert P.tls_insecure() is False
    monkeypatch.setenv("FNMUSIC_LX_SYNC_INSECURE_TLS", "1")
    assert P.tls_insecure() is True


async def _pump(rpc):
    while True:
        if not await rpc.handle_one():
            return


def test_ws_connect_reports_401_readably():
    async def handler(reader, writer):  # noqa: ARG001
        pass

    async def main():
        # 直接连一个非 WS 的 TCP 端口：服务端回了 HTTP 401
        async def on_client(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 401 Unauthorized\r\n\r\n")
            await writer.drain()
            writer.close()

        srv = await asyncio.start_server(on_client, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            with pytest.raises(P.LxSyncError) as err:
                await P.LxWebSocket.connect(f"ws://127.0.0.1:{port}/", timeout=3)
            assert "鉴权失败" in str(err.value)
        finally:
            srv.close()
            await srv.wait_closed()

    _run(main())


def test_identity_roundtrip(tmp_path):
    path = str(tmp_path / "sub" / "identity.json")
    assert P.load_identity(path) == {}
    P.save_identity(path, "cid-1", "key-1")
    assert P.load_identity(path) == {"client_id": "cid-1", "key": "key-1"}
    P.clear_identity(path)
    assert P.load_identity(path) == {}
    (tmp_path / "bad").write_text("not json", encoding="utf-8")
    assert P.load_identity(str(tmp_path / "bad")) == {}


# ------------------------------------------------- 真机 E2E（默认跳过）----

@pytest.mark.skipif(not __import__("os").environ.get("FNMUSIC_LXSYNC_E2E_URL"),
                    reason="需要真实 lx-music-sync-server：设 FNMUSIC_LXSYNC_E2E_URL/PASSWORD")
def test_e2e_against_real_sync_server(tmp_path, monkeypatch):
    """对着真实服务端跑：配对 → 读取（这一步不改服务端歌单）。"""
    import os

    url = os.environ["FNMUSIC_LXSYNC_E2E_URL"]
    pwd = os.environ.get("FNMUSIC_LXSYNC_E2E_PASSWORD", "")
    monkeypatch.setenv("FNMUSIC_LX_SYNC_DIR", str(tmp_path))

    async def main():
        first = await P.sync_session(url, pwd, device_name="fnmusic-ext-pytest", timeout=20)
        assert first["client_id"] and first["key"] and first["paired"] is True
        P.save_identity(P.os.path.join(str(tmp_path), "identity.json"), first["client_id"], first["key"])
        second = await P.sync_session(url, pwd, device_name="fnmusic-ext-pytest",
                                      client_id=first["client_id"], key_b64=first["key"], timeout=20)
        assert second["paired"] is False and second["client_id"] == first["client_id"]
        return second["list_data"]

    data = _run(main())
    assert set(data) >= {"defaultList", "loveList", "userList"}
