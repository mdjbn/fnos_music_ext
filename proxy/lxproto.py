"""lx-music-sync-server 协议客户端：握手 + WebSocket + message2call RPC（零第三方依赖）。

为什么自带实现：proxy 的 venv 只有 fastapi/uvicorn/httpx/… 这类基础依赖，
而 lx-music-sync-server 的握手要 AES-128-ECB 与 RSA-OAEP 解密、通道要 WebSocket——
`cryptography`/`websockets` 都不在依赖里。为了一个可选功能把两个带 native 扩展的包
塞进飞牛的安装链（离线、换源、wheel 架构）风险太大，所以这里用标准库把这三块自己实现：

* AES-128-ECB —— 仅用于握手（服务端 `src/utils/tools.ts:44-52`，Node `aes-128-ecb` 无 IV）；
* RSA —— 客户端**自己生成密钥对**，把公钥（SPKI PEM 主体，单行 base64）塞进握手明文，
  服务端用 `crypto.publicEncrypt(..., RSA_PKCS1_OAEP_PADDING)`（默认 SHA-1）加密
  `{clientId,key,serverName}` 回给我们（`src/server/auth.ts:56-67`）；
* WebSocket —— 最小客户端（握手 + 帧 + 掩码 + 分片 + ping/pong），只跑 ws/wss，不跑浏览器那套；
* message2call —— 见 `proxy/lxproto.py:LxRpc` 的报文说明。

协议事实（源码级，HEAD d47aca4 / v2.1.2）：
* `/ah` 首次认证：头 `m` = base64(AES(`'lx-music auth::' + '\\n' + 公钥主体 + '\\n' + 设备名 [+ '\\n' + 'lx_music_mobile']`,
  key=base64(md5(密码)[:16])))，200 的 body = base64(RSA-OAEP(JSON))；401 = 密码错，
  403 = 同 IP 失败 ≥10 次被封（`src/server/auth.ts:14-17,73-103`）。
* `/ah` 再次认证：带头 `i=<clientId>`、`m` = base64(AES('lx-music auth::'+设备名, 会话 key))，
  200 的 body = base64(AES('Hello~::^-^::~v4~', 会话 key))（`auth.ts:19-41`）。
* WebSocket：`ws://host:9527/?i=<clientId>&t=<base64(AES('lx-music connect', 会话 key))>`，
  校验失败服务端直接回裸 401（`src/server/server.ts:260-276`）。
* 报文就是 JSON 文本，**不加密**；>1024 字符时 gzip+base64 并加前缀 `cg_`（`tools.ts:81-103`）。
  请求 `{"name":"<path. join('.')>__<随机>","path":[...],"data":[参数...]}`，
  应答 `{"name":<同名>,"error":null|"错误串","data":结果}`。
* 连上后**由服务端驱动**：它先调客户端 `getEnabledFeatures('server', {list:1,dislike:1})`，
  再调 `modules.list.sync(socket)`，最后调客户端 `finished()`（`src/server/sync/sync.ts`）。
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import json
import os
import secrets
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import httpx

# ---------------------------------------------------------------------------
# AES-128-ECB（FIPS-197）
# ---------------------------------------------------------------------------

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r & 0xFF


def _expand_key(key: bytes) -> list[bytes]:
    if len(key) != 16:
        raise ValueError("AES-128 需要 16 字节密钥")
    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(words[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ t[j] for j in range(4)])
    return [bytes(sum(words[i * 4:i * 4 + 4], [])) for i in range(11)]


def _add_round_key(state: bytes, rk: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(state, rk))


def _encrypt_block(block: bytes, rks: list[bytes]) -> bytes:
    s = _add_round_key(block, rks[0])
    for rnd in range(1, 11):
        # SubBytes + ShiftRows（状态按列优先：s[c*4+r]）
        t = bytearray(16)
        for c in range(4):
            for r in range(4):
                t[c * 4 + r] = _SBOX[s[((c + r) % 4) * 4 + r]]
        s = bytes(t)
        if rnd != 10:
            m = bytearray(16)
            for c in range(4):
                col = s[c * 4:c * 4 + 4]
                m[c * 4 + 0] = _mul(col[0], 2) ^ _mul(col[1], 3) ^ col[2] ^ col[3]
                m[c * 4 + 1] = col[0] ^ _mul(col[1], 2) ^ _mul(col[2], 3) ^ col[3]
                m[c * 4 + 2] = col[0] ^ col[1] ^ _mul(col[2], 2) ^ _mul(col[3], 3)
                m[c * 4 + 3] = _mul(col[0], 3) ^ col[1] ^ col[2] ^ _mul(col[3], 2)
            s = bytes(m)
        s = _add_round_key(s, rks[rnd])
    return s


def _decrypt_block(block: bytes, rks: list[bytes]) -> bytes:
    s = _add_round_key(block, rks[10])
    for rnd in range(9, -1, -1):
        # InvShiftRows + InvSubBytes
        t = bytearray(16)
        for c in range(4):
            for r in range(4):
                t[((c + r) % 4) * 4 + r] = _INV_SBOX[s[c * 4 + r]]
        s = bytes(t)
        s = _add_round_key(s, rks[rnd])
        if rnd != 0:
            m = bytearray(16)
            for c in range(4):
                col = s[c * 4:c * 4 + 4]
                m[c * 4 + 0] = _mul(col[0], 14) ^ _mul(col[1], 11) ^ _mul(col[2], 13) ^ _mul(col[3], 9)
                m[c * 4 + 1] = _mul(col[0], 9) ^ _mul(col[1], 14) ^ _mul(col[2], 11) ^ _mul(col[3], 13)
                m[c * 4 + 2] = _mul(col[0], 13) ^ _mul(col[1], 9) ^ _mul(col[2], 14) ^ _mul(col[3], 11)
                m[c * 4 + 3] = _mul(col[0], 11) ^ _mul(col[1], 13) ^ _mul(col[2], 9) ^ _mul(col[3], 14)
            s = bytes(m)
    return s


def aes_ecb_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB + **PKCS#7** 填充。

    ⚠️ 必须是 PKCS#7 而不是零填充：服务端用 `createCipheriv('aes-128-ecb', key, '')`
    且没关 autoPadding，Node 默认按 PKCS#7 填充、解密时也会校验并剥掉（`cipher.final()`
    校验失败直接抛错 ⇒ 握手 401）。已用 node 实测对齐（长度 16 的明文会多出一个整块）。
    """
    rks = _expand_key(key)
    pad = 16 - (len(data) % 16)
    data = data + bytes([pad]) * pad
    return b"".join(_encrypt_block(data[i:i + 16], rks) for i in range(0, len(data), 16))


def aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("密文长度必须是 16 的倍数")
    rks = _expand_key(key)
    out = b"".join(_decrypt_block(data[i:i + 16], rks) for i in range(0, len(data), 16))
    pad = out[-1] if out else 0
    if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad):
        return out[:-pad]
    return out.rstrip(b"\x00")  # 容错：理论上不会走到（服务端只会发 PKCS#7）


# ---------------------------------------------------------------------------
# RSA（自生成密钥对 + OAEP-SHA1 解密 + SPKI 公钥导出）
# ---------------------------------------------------------------------------

_SMALL_PRIMES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71,
                 73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149, 151, 157,
                 163, 167, 173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233, 239, 241,
                 251, 257, 263, 269, 271, 277, 281, 283, 293, 307, 311, 313, 317, 331, 337, 347,
                 349, 353, 359, 367, 373, 379, 383, 389, 397, 401, 409, 419, 421, 431, 433, 439,
                 443, 449, 457, 461, 463, 467, 479, 487, 491, 499, 503, 509, 521, 523, 541]


def _is_probable_prime(n: int, rounds: int = 12) -> bool:
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _rand_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        # 先小素数筛掉绝大多数候选，再做 Miller-Rabin
        if any(cand % p == 0 for p in _SMALL_PRIMES):
            continue
        if _is_probable_prime(cand):
            return cand


@dataclass
class RsaKey:
    """客户端 RSA 密钥对（只用于解服务端那一次 OAEP 加密的会话信息）。"""

    n: int
    e: int
    d: int
    p: int
    q: int

    @property
    def bits(self) -> int:
        return self.n.bit_length()

    def public_pem_body(self) -> str:
        """SPKI DER 的单行 base64（服务端会自己补 BEGIN/END PUBLIC KEY 头尾）。"""
        der_int = lambda v: _der(0x02, _int_bytes(v))  # noqa: E731
        rsa_pub = _der(0x30, der_int(self.n) + der_int(self.e))
        alg = _der(0x30, bytes.fromhex("06092a864886f70d010101") + b"\x05\x00")
        spki = _der(0x30, alg + _der(0x03, b"\x00" + rsa_pub))
        return base64.b64encode(spki).decode("ascii")

    def public_pem(self) -> str:
        body = self.public_pem_body()
        return f"-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----"

    def oaep_sha1_decrypt(self, ciphertext: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        if len(ciphertext) != k:
            raise ValueError(f"RSA 密文长度应为 {k}，实际 {len(ciphertext)}")
        m = pow(int.from_bytes(ciphertext, "big"), self.d, self.n)
        em = m.to_bytes(k, "big")
        if em[0] != 0:
            raise ValueError("OAEP 解码失败：首字节非 0")
        h_len = 20
        masked_seed, masked_db = em[1:1 + h_len], em[1 + h_len:]
        seed = _mgf1_xor(masked_seed, masked_db, h_len)
        db = _mgf1_xor(masked_db, seed, masked_db.__len__())
        l_hash = hashlib.sha1(b"").digest()
        if db[:h_len] != l_hash:
            raise ValueError("OAEP 解码失败：lHash 不匹配")
        idx = h_len
        while idx < len(db) and db[idx] == 0:
            idx += 1
        if idx >= len(db) or db[idx] != 1:
            raise ValueError("OAEP 解码失败：缺少分隔符")
        return db[idx + 1:]


def _mgf1_xor(masked: bytes, seed: bytes, out_len: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < out_len:
        out += hashlib.sha1(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(masked, out[:out_len]))


def _int_bytes(v: int) -> bytes:
    length = max(1, (v.bit_length() + 7) // 8)
    return v.to_bytes(length, "big")


def _der(tag: int, payload: bytes) -> bytes:
    if len(payload) < 0x80:
        return bytes([tag, len(payload)]) + payload
    raw = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(raw)]) + raw + payload


def generate_rsa_keypair(bits: int = 2048) -> RsaKey:
    """生成 RSA 密钥对（纯 Python，2048 位通常 <1s；只在首次配对时用一次）。"""
    e = 65537
    half = bits // 2
    while True:
        p = _rand_prime(half)
        q = _rand_prime(bits - half)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        return RsaKey(n=n, e=e, d=pow(e, -1, phi), p=p, q=q)


# ---------------------------------------------------------------------------
# 握手
# ---------------------------------------------------------------------------

AUTH_MSG = "lx-music auth::"
HELLO_MSG = "Hello~::^-^::~v4~"
CONNECT_MSG = "lx-music connect"


class LxSyncError(Exception):
    """握手/通道层的可读错误（带上「下一步怎么办」的提示）。"""


def aes_key_from_password(password: str) -> bytes:
    """服务端口径：md5(密码) 的十六进制前 16 个**字符**当 AES 密钥（`auth.ts:45-47`）。"""
    return hashlib.md5(str(password).encode("utf-8")).hexdigest()[:16].encode("ascii")


def aes_key_from_session(key_b64: str) -> bytes:
    """再次认证/建连用的会话密钥：服务端下发的 base64 字符串解码后的 16 字节。"""
    return base64.b64decode(str(key_b64))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def normalize_base_url(url: str) -> str:
    """把用户填的地址归一成 http(s)://host[:port][/路径]；同时接受 ws(s):// 写法。

    **路径必须保留**：lxserver 这类增强版支持「一个用户一个连接路径」，
    客户端地址形如 `https://host:9528/<用户名>`，根路径会被服务端 403 掉
    （`Access denied: Root path access is disabled`）。
    """
    raw = str(url or "").strip()
    if not raw:
        raise LxSyncError("未配置同步服务地址")
    if not raw.startswith(("http://", "https://", "ws://", "wss://")):
        raw = "http://" + raw
    if raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    elif raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    return raw.rstrip("/")


def tls_insecure() -> bool:
    """自签证书的局域网服务端：置 FNMUSIC_LX_SYNC_INSECURE_TLS=1 跳过证书校验。"""
    return str(os.environ.get("FNMUSIC_LX_SYNC_INSECURE_TLS", "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def ws_url_of(base_url: str, client_id: str, key_b64: str) -> str:
    """构造建连 URL：ws(s)://host[:port][/路径]/?i=<clientId>&t=<AES('lx-music connect')>。"""
    parts = urlsplit(normalize_base_url(base_url))
    token = _b64(aes_ecb_encrypt(CONNECT_MSG.encode("utf-8"), aes_key_from_session(key_b64)))
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return f"{scheme}://{parts.netloc}{path}?i={quote(client_id, safe='')}&t={quote(token, safe='')}"


async def _retry(coro_factory, attempts: int = 3, delay: float = 0.6):
    """公网/反代后面的同步服务偶发 TLS 被重置（实测用户那台就会），必须重试。

    只重试「连不上」这类瞬时错误；401/403 由调用方按状态码直接处理，不会进到这里。
    """
    last: "Exception | None" = None
    for i in range(max(1, attempts)):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i + 1 < max(1, attempts):
                await asyncio.sleep(delay * (i + 1))
    assert last is not None
    raise last


async def lx_auth(
    base_url: str,
    password: str,
    device_name: str = "fnmusic-ext",
    client_id: str = "",
    key_b64: str = "",
    timeout: float = 15.0,
) -> dict:
    """完成 `/ah` 认证。返回 `{"client_id","key","server_name","paired"}`。

    有 `client_id`+`key_b64`（上次配对留下的）时走再次认证，否则现场生成 RSA 密钥对走首次配对，
    并在返回值里带上 `private_key`（调用方需要自己保存吗？不需要——`{clientId,key}` 就够后续使用）。
    """
    base = normalize_base_url(base_url)
    headers: dict[str, str] = {}
    rsa: RsaKey | None = None
    if client_id and key_b64:
        session_key = aes_key_from_session(key_b64)
        plain = (AUTH_MSG + device_name).encode("utf-8")
        headers["m"] = _b64(aes_ecb_encrypt(plain, session_key))
        headers["i"] = client_id
    else:
        rsa = generate_rsa_keypair()
        plain = "\n".join([AUTH_MSG, rsa.public_pem_body(), device_name]).encode("utf-8")
        headers["m"] = _b64(aes_ecb_encrypt(plain, aes_key_from_password(password)))
    async def _request():
        # ⚠️ 必须拼成绝对 URL：httpx 的 base_url 合并会吃掉最后一段路径，
        #    而这里的路径就是「连接码/用户名」（`.../Xsj…/ah`），丢了就 403/401。
        async with httpx.AsyncClient(timeout=timeout, verify=not tls_insecure()) as client:
            return await client.get(f"{base}/ah", headers=headers)

    try:
        resp = await _retry(_request)
    except Exception as exc:  # noqa: BLE001
        raise LxSyncError(f"连接同步服务失败：{type(exc).__name__}: {exc}") from exc
    if resp.status_code == 401:
        raise LxSyncError("同步服务认证失败：密码不对（服务端 config.js 的 users[].password / LX_USER_*），"
                          "或本地保存的配对信息已失效（服务端清过 devices.json 时需重新配对）")
    if resp.status_code == 403:
        raise LxSyncError("同步服务拒绝连接：本机 IP 因多次认证失败被暂时封禁（约 2 天），请换 IP 或重启服务端")
    if resp.status_code != 200:
        raise LxSyncError(f"同步服务返回异常状态 {resp.status_code}：{resp.text[:120]}")
    body = resp.text.strip()
    if rsa is not None:
        try:
            payload = json.loads(rsa.oaep_sha1_decrypt(base64.b64decode(body)).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise LxSyncError(f"解析同步服务下发的会话信息失败：{type(exc).__name__}: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("clientId") or not payload.get("key"):
            raise LxSyncError(f"同步服务下发的会话信息不完整：{str(payload)[:120]}")
        return {"client_id": str(payload["clientId"]), "key": str(payload["key"]),
                "server_name": str(payload.get("serverName") or ""), "paired": True}
    # 再次认证：只校验服务端回的 hello 明文
    try:
        hello = aes_ecb_decrypt(base64.b64decode(body), aes_key_from_session(key_b64))
    except Exception as exc:  # noqa: BLE001
        raise LxSyncError(f"再次认证响应无法解密（本地保存的 clientId/key 可能已失效）：{exc}") from exc
    if not hello.rstrip(b"\x00").decode("utf-8", "ignore").startswith("Hello~"):
        raise LxSyncError("再次认证失败：本地保存的配对信息已失效，请在管理页清空后重试")
    return {"client_id": client_id, "key": key_b64, "server_name": "", "paired": False}


# ---------------------------------------------------------------------------
# 最小 WebSocket 客户端
# ---------------------------------------------------------------------------

_OP_CONT, _OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class LxWebSocket:
    """asyncio 版最小 WebSocket 客户端（文本帧 + 分片 + ping/pong + close）。

    只实现 lx-music-sync-server 需要的那部分：服务端不压缩扩展、不用子协议。
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float = 30.0):
        self._reader = reader
        self._writer = writer
        self._timeout = timeout
        self._buffer = b""
        self.closed = False

    @classmethod
    async def connect(cls, url: str, timeout: float = 15.0) -> "LxWebSocket":
        parts = urlsplit(url)
        use_tls = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if use_tls else 80)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        ctx = None
        if use_tls:
            ctx = ssl._create_unverified_context() if tls_insecure() else ssl.create_default_context()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ctx), timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            raise LxSyncError(f"无法连接同步服务 WebSocket {host}:{port}：{type(exc).__name__}: {exc}") from exc
        key = _b64(secrets.token_bytes(16))
        host_header = f"{host}:{port}" if port not in (80, 443) else host
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(req.encode("ascii"))
        await writer.drain()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            writer.close()
            raise LxSyncError(f"WebSocket 握手无响应：{type(exc).__name__}: {exc}") from exc
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status_line:
            writer.close()
            if "401" in status_line:
                raise LxSyncError("WebSocket 鉴权失败（clientId/会话 key 不匹配，请重新配对）")
            raise LxSyncError(f"WebSocket 握手被拒绝：{status_line[:120]}")
        return cls(reader, writer, timeout=timeout)

    async def send_text(self, text: str) -> None:
        await self._send_frame(_OP_TEXT, text.encode("utf-8"))

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise LxSyncError("WebSocket 已关闭")
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        mask = secrets.token_bytes(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._writer.write(bytes(header) + masked)
        await self._writer.drain()

    async def _read_exact(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = await self._reader.read(65536)
            if not chunk:
                raise LxSyncError("WebSocket 连接被对端关闭")
            self._buffer += chunk
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    async def recv_text(self) -> str | None:
        """读一条完整文本消息；收到 close 返回 None。自动回 pong、拼接分片。"""
        data = bytearray()
        started = False
        while True:
            head = await asyncio.wait_for(self._read_exact(2), timeout=self._timeout)
            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            masked = bool(head[1] & 0x80)
            length = head[1] & 0x7F
            if length == 126:
                length = int.from_bytes(await self._read_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(await self._read_exact(8), "big")
            mask = await self._read_exact(4) if masked else b""
            payload = await self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == _OP_PING:
                await self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                self.closed = True
                try:
                    self._writer.close()
                except Exception:  # noqa: BLE001
                    pass
                return None
            if opcode in (_OP_TEXT, _OP_BIN):
                data += payload
                started = True
                if fin:
                    return data.decode("utf-8", "replace")
                continue
            if opcode == _OP_CONT and started:
                data += payload
                if fin:
                    return data.decode("utf-8", "replace")

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self._send_frame(_OP_CLOSE, (1000).to_bytes(2, "big"))
        except Exception:  # noqa: BLE001
            pass
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# message2call RPC
# ---------------------------------------------------------------------------

def encode_message(payload: dict) -> str:
    """>1024 字符走 gzip+base64 并加 `cg_` 前缀（与 `tools.ts:81-87` 一致）。"""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(text) > 1024:
        return "cg_" + base64.b64encode(gzip.compress(text.encode("utf-8"))).decode("ascii")
    return text


def decode_message(raw: str) -> dict:
    text = raw
    if text.startswith("cg_"):
        text = gzip.decompress(base64.b64decode(text[3:])).decode("utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise LxSyncError(f"报文不是对象：{str(data)[:80]}")
    return data


class LxRpc:
    """message2call 的 Python 侧：既能调对端，也能被对端调。

    报文：请求 `{"name":"<path. join('.')>__<随机>","path":[...],"data":[参数...]}`；
    应答 `{"name":<同名>,"error":null|"错误串","data":结果}`（见 message2call dist `onMessage`）。
    """

    def __init__(self, ws: LxWebSocket, handlers: "dict[str, Callable[..., Any]]" | None = None):
        self._ws = ws
        self._handlers = handlers or {}
        self._pending: dict[str, asyncio.Future] = {}
        self.closed = False

    async def call(self, path: "list[str]", *args: Any, timeout: float = 30.0) -> Any:
        name = f"{'.'.join(path)}__{secrets.randbelow(10**16)}"
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[name] = fut
        await self._ws.send_text(encode_message({"name": name, "path": list(path), "data": list(args)}))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(name, None)

    async def handle_one(self) -> bool:
        """处理一条报文；返回 False 表示连接已关闭。"""
        raw = await self._ws.recv_text()
        if raw is None:
            self.closed = True
            return False
        msg = decode_message(raw)
        name = str(msg.get("name") or "")
        path = msg.get("path")
        if path:
            # 对端请求我们执行某个方法
            args = msg.get("data") or []
            handler = self._handlers.get(str(path[-1])) or self._handlers.get(".".join(map(str, path)))
            if handler is None:
                await self._reply(name, error=f"{path[-1]} is not defined")
                return True
            try:
                result = handler(*args)
                if asyncio.iscoroutine(result):
                    result = await result
                await self._reply(name, data=result)
            except Exception as exc:  # noqa: BLE001
                await self._reply(name, error=str(exc)[:200])
            return True
        # 对端应答我们的调用
        fut = self._pending.get(name)
        if fut is not None and not fut.done():
            if msg.get("error"):
                fut.set_exception(LxSyncError(str(msg.get("error"))))
            else:
                fut.set_result(msg.get("data"))
        return True

    async def _reply(self, name: str, data: Any = None, error: "str | None" = None) -> None:
        await self._ws.send_text(encode_message({"name": name, "error": error, "data": data}))


# ---------------------------------------------------------------------------
# 一次完整同步会话
# ---------------------------------------------------------------------------

EMPTY_LIST_DATA: dict = {"defaultList": [], "loveList": [], "userList": []}


def _empty(*_args: Any) -> dict:
    return dict(EMPTY_LIST_DATA)


class LxConnection:
    """一条可读可写的同步连接：后台泵 + 可调用服务端 + 收集服务端歌单。

    只读用法（本项目主路径）：`getEnabledFeatures` 回 `skipSnapshot: True`、
    `list_sync_get_list_data` 回空表 ⇒ 服务端判定「它有数据、对端没有」后主动调
    `list_sync_set_list_data(服务端歌单)`（`src/modules/list/sync/sync.ts:238` + `:67-71`）。
    写用法（预留给后续「把飞牛歌单同步回服务端」）：`await conn.call(["onListSyncAction"], {...})`。
    """

    def __init__(self, base_url: str, password: str, device_name: str = "fnmusic-ext",
                 client_id: str = "", key_b64: str = "", timeout: float = 25.0):
        self.base_url = base_url
        self.password = password
        self.device_name = device_name
        self.client_id = client_id
        self.key_b64 = key_b64
        self.timeout = timeout
        self.list_data: "dict | None" = None
        self.finished = False
        self.paired = False
        self._ws: "LxWebSocket | None" = None
        self._rpc: "LxRpc | None" = None
        self._pump: "asyncio.Task | None" = None
        self._error: "Exception | None" = None

    # -- handlers -----------------------------------------------------------
    def _handlers(self) -> dict:
        def on_set(list_data: Any = None, *_a: Any) -> None:
            if isinstance(list_data, dict):
                self.list_data = list_data
            return None

        def on_finished(*_a: Any) -> None:
            self.finished = True
            return None

        return {
            "getEnabledFeatures": lambda *_a: {"list": {"skipSnapshot": True}, "dislike": False},
            "finished": on_finished,
            "list_sync_get_list_data": _empty,
            "list_sync_get_md5": lambda *_a: "",
            "list_sync_get_sync_mode": lambda *_a: "overwrite_remote_local_full",
            "list_sync_set_list_data": on_set,
            "list_sync_finished": on_finished,
            "onListSyncAction": lambda *_a: "",
        }

    # -- lifecycle ----------------------------------------------------------
    async def open(self) -> "LxConnection":
        auth = await lx_auth(self.base_url, self.password, device_name=self.device_name,
                             client_id=self.client_id, key_b64=self.key_b64, timeout=self.timeout)
        self.client_id, self.key_b64, self.paired = auth["client_id"], auth["key"], auth["paired"]
        self._ws = await _retry(lambda: LxWebSocket.connect(
            ws_url_of(self.base_url, self.client_id, self.key_b64), timeout=self.timeout))
        self._rpc = LxRpc(self._ws, self._handlers())
        return self

    def start_pump(self) -> None:
        """后台持续处理报文（调用 `call()` 前必须起，否则应答没人读）。"""
        if self._pump is None:
            self._pump = asyncio.get_event_loop().create_task(self._pump_loop())

    async def _pump_loop(self) -> None:
        assert self._rpc is not None
        while True:
            try:
                if not await self._rpc.handle_one():
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 通道异常只记录，调用方按超时/空数据处理
                self._error = exc
                return

    async def wait_for(self, predicate: "Callable[[], bool]", timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while time.monotonic() < deadline:
            if predicate():
                return True
            if self._error is not None:
                raise self._error if isinstance(self._error, LxSyncError) else LxSyncError(str(self._error))
            await asyncio.sleep(0.02)
        return predicate()

    async def call(self, path: "list[str]", *args: Any, timeout: float = 30.0) -> Any:
        if self._rpc is None:
            raise LxSyncError("连接尚未建立")
        self.start_pump()
        return await self._rpc.call(path, *args, timeout=timeout)

    async def collect(self, timeout: float | None = None) -> dict:
        """等 `list_sync_set_list_data` + `finished`，返回服务端歌单（拿不到就是空表）。"""
        self.start_pump()
        try:
            await self.wait_for(lambda: self.list_data is not None and self.finished, timeout)
        except LxSyncError:
            raise
        if self.list_data is None:
            self.list_data = dict(EMPTY_LIST_DATA)
        return self.list_data

    async def close(self) -> None:
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
            try:
                await self._pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._pump = None
        if self._ws is not None:
            await self._ws.close()


async def sync_session(
    base_url: str,
    password: str,
    device_name: str = "fnmusic-ext",
    client_id: str = "",
    key_b64: str = "",
    timeout: float = 25.0,
) -> dict:
    """连上服务端并把**服务端保存的歌单**整包拉下来（只读，不改动服务端歌单）。

    返回 `{"list_data","client_id","key","paired","finished"}`；`client_id`/`key` 请调用方
    落盘复用，否则每次都会在服务端新增一台设备。
    """
    conn = LxConnection(base_url, password, device_name=device_name,
                        client_id=client_id, key_b64=key_b64, timeout=timeout)
    await conn.open()
    try:
        list_data = await conn.collect(timeout)
    finally:
        await conn.close()
    return {"list_data": list_data, "client_id": conn.client_id, "key": conn.key_b64,
            "paired": conn.paired, "finished": conn.finished}


# ---------------------------------------------------------------------------
# 配对信息持久化（clientId/key 存 cache 目录，避免每次连接都在服务端新增一台设备）
# ---------------------------------------------------------------------------

def load_identity(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("clientId") and data.get("key"):
            return {"client_id": str(data["clientId"]), "key": str(data["key"])}
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_identity(path: str, client_id: str, key_b64: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"clientId": client_id, "key": key_b64}, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def clear_identity(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
