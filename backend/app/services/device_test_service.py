"""设备连通性测试服务：「编辑设备 → 测试连接」的后端探测逻辑。

职责：
- 验证**远程管理凭据**是否可用（SSH / Telnet 用给定账号口令登录）
- 验证 **SNMP** 是否可通（用给定 community GET sysDescr）

特点：
- 纯探测：不落库、不修改设备状态、不影响告警；
- 两条链路**并行**执行，整体耗时取决于较慢的一条；
- 错误按类型细分（认证失败 / 端口不通 / 超时 / 协议不匹配 / 被拒绝），
  直接给出可操作的中文提示，而不是把底层异常原样抛给用户。
"""
import asyncio
import logging
import re
import socket
import time

import asyncssh
import telnetlib3

from app.config import settings
from app.services.backup_service import SSH_KEX, SSH_CIPHERS, SSH_HOSTKEYS, SSH_MACS
from app.services.discovery_service import snmp_get

logger = logging.getLogger(__name__)

# sysDescr.0：任何 SNMP 设备都应答的基础 OID，用来判断 community 是否正确
SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"

# 设备提示符：<H3C-SW> / AR1# / SW1(config)# 等
_PROMPT_RE = re.compile(r"^(<[^>]*>|[\w.\-]+[#>](\([^)]*\))?)\s*$")
# 明确的认证失败特征（设备回显）
_AUTH_FAIL_RE = re.compile(
    r"(authentication fail|login incorrect|invalid password|password error|"
    r"access denied|permission denied|login failed|incorrect password|"
    r"认证失败|密码错误|用户名或密码错误)",
    re.I,
)
# 登录提示（再次出现即说明又回到了认证环节 → 凭据不对）
_USER_PROMPT_RE = re.compile(r"(username:|login:|user name|用户名)")
_PASS_PROMPT_RE = re.compile(r"(password:|口令|密码)")


def _err_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


def _classify_network_error(exc: BaseException, target: str, timeout: float) -> str:
    """把连接异常翻译成用户能看懂的原因。"""
    if isinstance(exc, asyncio.TimeoutError) or isinstance(exc, TimeoutError):
        return f"{target} 连接超时（{timeout:.0f}s 内无响应），请检查 IP/端口与网络可达性"
    if isinstance(exc, socket.gaierror):
        return f"{target} 无法解析主机地址：{exc}"
    if isinstance(exc, ConnectionRefusedError):
        return f"{target} 端口被拒绝（服务未监听或端口填错）"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return f"{target} 连接被设备重置（对端可能不支持该协议/端口）"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (113, 101, 110, 111, 10060, 10061):
        return f"{target} 网络不可达或被拒绝（{exc}）"
    return f"{target} 连接失败：{_err_text(exc)}"


# === SSH ===

async def _test_ssh(ip: str, port: int, username: str, password: str, timeout: float) -> dict:
    """SSH 登录测试：握手 + 口令认证全部通过才算成功。"""
    conn = None
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                ip,
                port=port or 22,
                username=username,
                password=password,
                known_hosts=settings.SSH_KNOWN_HOSTS or None,
                kex_algs=SSH_KEX,
                encryption_algs=SSH_CIPHERS,
                server_host_key_algs=SSH_HOSTKEYS,
                mac_algs=SSH_MACS,
                login_timeout=timeout,
                # 只做口令认证：不读 ssh-agent / 本地私钥，避免"其实用密钥登进去了"
                # 而误判用户填的口令是正确的
                client_keys=None,
                agent_path=None,
                preferred_auth="password",
            ),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "message": f"SSH 登录超时（{timeout:.0f}s），请检查 IP/端口与网络"}
    except asyncssh.PermissionDenied:
        return {"ok": False, "message": "SSH 认证失败：用户名或密码不正确（或该账号无登录权限）"}
    except asyncssh.HostKeyNotVerifiable:
        return {"ok": False, "message": "SSH 主机密钥校验失败，请检查平台 known_hosts 配置"}
    except asyncssh.KeyExchangeFailed as e:
        return {"ok": False, "message": f"SSH 算法协商失败（设备过旧或被限制）：{_err_text(e)}"}
    except asyncssh.DisconnectError as e:
        return {"ok": False, "message": f"SSH 连接被设备断开：{_err_text(e)}"}
    except asyncssh.Error as e:
        name = type(e).__name__
        if "auth" in name.lower() or "permission" in name.lower():
            return {"ok": False, "message": "SSH 认证失败：用户名或密码不正确"}
        return {"ok": False, "message": f"SSH 失败：{_err_text(e)}"}
    except Exception as e:  # noqa: BLE001 —— 网络层异常统一归类
        return {"ok": False, "message": _classify_network_error(e, "SSH", timeout)}

    # 走到这里说明握手 + 口令认证都通过；先取版本信息再关闭会话
    version = ""
    try:
        version = conn.get_extra_info("server_version") or ""
    except Exception:  # noqa: BLE001
        version = ""
    try:
        conn.close()
        await conn.wait_closed()
    except Exception:  # noqa: BLE001
        pass

    detail = f"已通过口令认证（账号 {username}）"
    if version:
        detail += f"，服务端 {version}"
    return {"ok": True, "message": "SSH 登录成功", "detail": detail}


# === Telnet ===

async def _test_telnet(ip: str, port: int, username: str, password: str, timeout: float) -> dict:
    """Telnet 登录测试：等待提示符出现才算成功。

    设备回显「Authentication failed / Login incorrect」或再次索要账号口令，
    均判为凭据错误。
    """
    reader = writer = None
    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(
                ip,
                port=port or 23,
                encoding=None,
                force_binary=True,
                connect_minwait=0.2,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "message": f"Telnet 连接超时（{timeout:.0f}s），请检查 IP/端口与网络"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": _classify_network_error(e, "Telnet", timeout)}

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    buf = b""
    user_sent = pass_sent = False
    result: dict | None = None
    try:
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
            except asyncio.TimeoutError:
                chunk = b""
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "message": _classify_network_error(e, "Telnet", timeout)}
                break
            if chunk:
                buf += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8", "replace")
            text = buf.decode("utf-8", "replace")
            low = text.lower()

            if user_sent and pass_sent:
                # 顺序很重要：先看明确报错，再看提示符（进入命令行=成功），
                # 最后才判"又回到登录提示"（凭据不对），避免成功回显被误判。
                if _AUTH_FAIL_RE.search(text):
                    result = {"ok": False,
                              "message": "Telnet 认证失败：用户名或密码不正确"}
                    break
                lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]
                if lines and _PROMPT_RE.match(lines[-1]):
                    result = {"ok": True, "message": "Telnet 登录成功",
                              "detail": f"已进入命令行（账号 {username}，提示符 {lines[-1][:40]}）"}
                    break
                if _USER_PROMPT_RE.search(low) or _PASS_PROMPT_RE.search(low):
                    result = {"ok": False,
                              "message": "Telnet 认证失败：用户名或密码不正确（设备重新要求登录）"}
                    break
            elif not user_sent:
                if _USER_PROMPT_RE.search(low):
                    writer.write((username + "\r\n").encode("utf-8", "replace"))
                    await writer.drain()
                    user_sent = True
                    buf = b""
                elif _PASS_PROMPT_RE.search(low):
                    # 少数设备只问口令、不问用户名
                    writer.write((password + "\r\n").encode("utf-8", "replace"))
                    await writer.drain()
                    user_sent = pass_sent = True
                    buf = b""
            elif not pass_sent and _PASS_PROMPT_RE.search(low):
                writer.write((password + "\r\n").encode("utf-8", "replace"))
                await writer.drain()
                pass_sent = True
                buf = b""
            if not chunk:
                await asyncio.sleep(0.2)

        if result is None:
            if not user_sent:
                msg = "Telnet 未出现登录提示（端口可能不是 Telnet，或设备未开启）"
            elif not pass_sent:
                msg = "Telnet 未出现口令提示（设备登录流程异常）"
            else:
                msg = f"Telnet 登录超时（{timeout:.0f}s），未能进入命令行"
            result = {"ok": False, "message": msg}
    finally:
        for closer in (writer, reader):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
    return result


# === SNMP ===

async def _test_snmp(ip: str, community: str, version: str, timeout: float) -> dict:
    """SNMP 可达性测试：用 community 取 sysDescr.0。"""
    if (version or "").lower().startswith("v3"):
        return {
            "ok": False,
            "message": "平台当前以 SNMP v2c（community）采集，v3 请以设备侧配置为准",
            "skipped": True,
        }
    if not community:
        return {"ok": False, "message": "未填写 SNMP Community，无法测试"}

    t0 = time.perf_counter()
    try:
        value = await snmp_get(ip, SYS_DESCR_OID, community, timeout=max(2, int(timeout)))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"SNMP 查询异常：{_err_text(e)}"}
    if not value:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
            "message": "SNMP 无响应：community 不正确，或设备未开启 SNMP / 与本机不通",
        }
    descr = value.strip().replace("\n", " ")[:120]
    return {"ok": True, "message": "SNMP 可通", "detail": f"设备自述：{descr}"}


# === 对外入口 ===

async def test_device_connection(
    *,
    ip: str,
    protocol: str = "ssh",
    port: int | None = None,
    username: str = "",
    password: str = "",
    snmp_version: str = "v2c",
    community: str = "",
    timeout: float = 8.0,
    password_from_db: bool = False,
    community_from_db: bool = False,
) -> dict:
    """并行测试远程管理凭据与 SNMP，返回 {ok, summary, items[]}。"""
    protocol = (protocol or "ssh").lower()
    if protocol not in ("ssh", "telnet"):
        return {
            "ok": False,
            "summary": f"不支持的管理协议：{protocol}",
            "items": [{"target": protocol, "label": protocol.upper(), "ok": False,
                       "message": f"不支持的管理协议：{protocol}"}],
        }

    mgmt_label = "SSH 登录" if protocol == "ssh" else "Telnet 登录"
    if not username:
        mgmt_task = asyncio.sleep(0, result={"ok": False, "message": "未填写用户名，无法测试登录"})
    elif not password:
        mgmt_task = asyncio.sleep(0, result={"ok": False, "message": "未填写密码，无法测试登录"})
    elif protocol == "ssh":
        mgmt_task = _test_ssh(ip, port or 22, username, password, timeout)
    else:
        mgmt_task = _test_telnet(ip, port or 23, username, password, timeout)

    snmp_task = _test_snmp(ip, community, snmp_version, timeout)

    async def _timed(coro) -> dict:
        t0 = time.perf_counter()
        res = await coro
        res.setdefault("latency_ms", int((time.perf_counter() - t0) * 1000))
        return res

    mgmt_res, snmp_res = await asyncio.gather(_timed(mgmt_task), _timed(snmp_task))

    items = [
        {
            "target": protocol,
            "label": mgmt_label,
            "ok": bool(mgmt_res.get("ok")),
            "message": mgmt_res.get("message") or "",
            "detail": mgmt_res.get("detail"),
            "latency_ms": mgmt_res.get("latency_ms"),
            "used_saved_credential": bool(password_from_db),
        },
        {
            "target": "snmp",
            "label": "SNMP",
            "ok": bool(snmp_res.get("ok")),
            "message": snmp_res.get("message") or "",
            "detail": snmp_res.get("detail"),
            "latency_ms": snmp_res.get("latency_ms"),
            "used_saved_credential": bool(community_from_db),
            "skipped": bool(snmp_res.get("skipped")),
        },
    ]

    mgmt_ok = items[0]["ok"]
    snmp_ok = items[1]["ok"]
    if mgmt_ok and snmp_ok:
        summary = f"{mgmt_label}与 SNMP 均正常"
    elif mgmt_ok:
        summary = f"{mgmt_label}成功，SNMP 不通"
    elif snmp_ok:
        summary = f"SNMP 可通，{mgmt_label}失败"
    else:
        summary = f"{mgmt_label}与 SNMP 均失败"
    return {"ok": mgmt_ok and snmp_ok, "summary": summary, "items": items}
