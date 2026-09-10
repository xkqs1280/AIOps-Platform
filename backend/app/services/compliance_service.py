"""等保2.0合规自动化检测引擎 — 基于 GB/T 22239-2019 对网络设备进行合规基线检查。

支持的检查项覆盖等保2.0三级要求中的 8.1.3（安全通信网络）、8.1.4（安全区域边界）、
8.1.5（安全计算环境）等控制点。

同时内置「等保二级交换机配置核查」规则集（基于真实 SSH 采集 display 命令评估）：
身份鉴别 / 访问控制 / 安全审计 / 入侵防范 / 数据保密性 五大类，可对全部或部分设备批量评估。
"""

from app.models.p3_security import ComplianceCheck, SecurityEvent
from app.models.device import Device
from app.database import get_session  # noqa: F401 — re-exported for callers
from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from datetime import datetime, timedelta, timezone, date  # noqa: F401 — re-exported for callers

import asyncio
import logging
import re
import uuid

import asyncssh

from app.config import settings
from app.services import config_baseline_service
from app.services.credential_service import reveal_secret

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 等保2.0 二级交换机配置核查规则集（基于 SSH 真实配置采集）
# ---------------------------------------------------------------------------
# 规则定义：每个核查项对应一组 display 命令 + 关键词/正则匹配逻辑，
# 采集设备真实配置输出后评估合规状态（compliant / partial / non_compliant / not_applicable）。

SECONDARY_CATEGORIES = {
    "identity_auth": "身份鉴别",
    "access_control": "访问控制",
    "security_audit": "安全审计",
    "intrusion_prevention": "入侵防范",
    "data_confidentiality": "数据保密性",
}

# 核查命令集（H3C/华为兼容：命令不存在时设备会回显错误，哨兵机制保证不中断）
SECONDARY_COMMANDS = [
    "display password-control",
    "display local-user",
    "display user-interface vty 0 4",
    "display current-configuration | include authentication-mode",
    "display current-configuration | begin user-interface vty",
    "display interface brief",
    "display info-center",
    "display current-configuration | include loghost",
    "display logbuffer reverse | include LOGIN",
    "display telnet server status",
    "display ssh server status",
    "display current-configuration | include dhcp snooping",
    "display current-configuration | include arp anti-attack",
    "display current-configuration | include ip source guard",
    "display snmp-agent community",
]

SECONDARY_RULES = [
    # ── 身份鉴别 ──
    {
        "control_id": "SEC-1.1",
        "category": "identity_auth",
        "desc": "密码复杂度与有效期策略已启用",
        "keywords": ["password-control enable", "password complexity", "password length"],
    },
    {
        "control_id": "SEC-1.2",
        "category": "identity_auth",
        "desc": "登录失败次数限制与锁定策略已配置",
        "keywords": ["login-attempt", "lock-time", "login failed", "password-attempt"],
    },
    {
        "control_id": "SEC-1.3",
        "category": "identity_auth",
        "desc": "远程管理空闲超时已设置（Idle-Timeout 非 0）",
        "keywords": ["idle-timeout"],
    },
    {
        "control_id": "SEC-1.4",
        "category": "identity_auth",
        "desc": "VTY 线路使用 AAA/RADIUS 认证（authentication-mode scheme）",
        "keywords": ["authentication-mode scheme", "authentication-mode aaa"],
    },
    # ── 访问控制 ──
    {
        "control_id": "SEC-2.1",
        "category": "access_control",
        "desc": "管理源地址限制（VTY 应用 ACL）",
        "keywords": ["acl", "inbound"],
    },
    {
        "control_id": "SEC-2.2",
        "category": "access_control",
        "desc": "账户权限分离（存在多账户或审计/安全角色）",
        "keywords": ["network-operator", "security-audit", "monitor-operator", "level-15", "level 15"],
    },
    {
        "control_id": "SEC-2.3",
        "category": "access_control",
        "desc": "未使用端口已 shutdown（存在 DOWN 接口）",
        "keywords": ["DOWN", "down", "shutdown"],
    },
    # ── 安全审计 ──
    {
        "control_id": "SEC-3.1",
        "category": "security_audit",
        "desc": "信息中心/日志功能已启用",
        "keywords": ["Information Center: enable", "信息中心：使能", "info-center enable"],
    },
    {
        "control_id": "SEC-3.2",
        "category": "security_audit",
        "desc": "已配置远程日志服务器（loghost）",
        "keywords": ["loghost"],
    },
    {
        "control_id": "SEC-3.3",
        "category": "security_audit",
        "desc": "审计日志包含登录/配置变更记录",
        "keywords": ["LOGIN", "CFG"],
    },
    # ── 入侵防范 ──
    {
        "control_id": "SEC-4.1",
        "category": "intrusion_prevention",
        "desc": "Telnet 已关闭，仅保留 SSH 管理",
        "keywords": ["telnet server", "Telnet server", "ssh server"],
    },
    {
        "control_id": "SEC-4.2",
        "category": "intrusion_prevention",
        "desc": "已启用 DHCP Snooping / ARP 防攻击 / IP Source Guard",
        "keywords": ["dhcp snooping", "arp anti-attack", "arp detection", "ip source guard"],
    },
    # ── 数据保密性 ──
    {
        "control_id": "SEC-5.1",
        "category": "data_confidentiality",
        "desc": "SNMP 版本安全（v3 优先，v2c 需高复杂度团体名）",
        "keywords": ["snmp-agent sys-info version v3", "SNMPv3", "snmp-agent community"],
    },
]


def _assess_secondary_rule(rule: dict, output: str) -> tuple[str, str]:
    """基于采集到的命令输出评估单条等保二级规则。

    Returns:
        (status, evidence)
    """
    cid = rule["control_id"]
    desc = rule["desc"]
    out_lower = output.lower()

    # SEC-4.1 Telnet 关闭判断特殊：需确认 Telnet 服务关闭 / SSH 服务开启
    if cid == "SEC-4.1":
        telnet_off = re.search(r"telnet server[^\n]*(disable|disabled|关闭|关闭服务)", output, re.I)
        telnet_disable = "telnet server enable" not in out_lower
        ssh_on = re.search(r"ssh server[^\n]*(enable|enabled|开启)", output, re.I)
        if telnet_off or (telnet_disable and ssh_on):
            return ("compliant", f"{desc}：已关闭 Telnet 且启用 SSH 管理")
        if ssh_on:
            return ("partial", f"{desc}：已启用 SSH，但未确认 Telnet 已关闭")
        if telnet_disable:
            return ("partial", f"{desc}：未检测到 Telnet 启用，但 SSH 状态未知")
        return ("non_compliant", f"{desc}：检测到 Telnet 仍处于启用状态")

    # SEC-1.3 空闲超时特殊：Idle-Timeout 必须为非 0
    if cid == "SEC-1.3":
        m = re.search(r"idle[- ]timeout\s+(\d+)", output, re.I)
        if m and int(m.group(1)) > 0:
            return ("compliant", f"{desc}：检测到 Idle-Timeout = {m.group(1)}s")
        if "idle-timeout" in out_lower or "idle timeout" in out_lower:
            return ("partial", f"{desc}：存在 Idle-Timeout 配置但值为 0 或无法解析")
        return ("non_compliant", f"{desc}：未检测到空闲超时配置（VTY 默认不超时）")

    # SEC-2.1 管理源地址限制：需 ACL 已应用在 VTY 且存在 inbound
    if cid == "SEC-2.1":
        has_acl = re.search(r"\bacl\b", output, re.I)
        has_inbound = "inbound" in out_lower
        if has_acl and has_inbound:
            return ("compliant", f"{desc}：VTY 下已应用 ACL 限制管理源地址")
        if has_acl:
            return ("partial", f"{desc}：存在 ACL 配置，但未确认应用到 VTY inbound")
        return ("non_compliant", f"{desc}：未配置管理源地址限制（无 ACL）")

    # SEC-2.2 账户权限分离：存在多个用户或审计/安全角色
    if cid == "SEC-2.2":
        user_lines = [ln.strip() for ln in output.splitlines() if re.match(r"^\s*\S+\s+(active|inactive)", ln)]
        if any(k in output for k in ("network-operator", "security-audit", "monitor-operator")):
            return ("compliant", f"{desc}：检测到独立审计/安全角色账户")
        if len(user_lines) >= 2 or output.lower().count("local-user") >= 2:
            return ("partial", f"{desc}：检测到多个账户，但未确认三权分立角色分配")
        return ("non_compliant", f"{desc}：仅存在单一 admin 账户，无权限分离")

    # SEC-2.3 未使用端口 shutdown：display interface brief 中存在 down 接口即视为已隔离
    if cid == "SEC-2.3":
        down_cnt = len(re.findall(r"\b(DOWN|down)\b", output))
        if down_cnt > 0:
            return ("compliant", f"{desc}：检测到 {down_cnt} 个 DOWN/关闭状态的接口")
        return ("partial", f"{desc}：未检测到明显 DOWN 接口，无法确认未使用端口是否已 shutdown")

    # SEC-5.1 SNMP 版本：v3 优先，v2c 需复杂团体名，禁止 public
    if cid == "SEC-5.1":
        v3 = "v3" in out_lower
        public_used = re.search(r"community[^\n]*\bpublic\b", output, re.I)
        has_community = re.search(r"community[^\n]*\S", output, re.I)
        if v3:
            return ("compliant", f"{desc}：检测到 SNMPv3 配置，具备加密认证")
        if has_community and not public_used:
            return ("partial", f"{desc}：使用 SNMPv2c 且团体名非默认，建议升级 SNMPv3 或加 ACL 限制")
        if public_used:
            return ("non_compliant", f"{desc}：检测到默认团体名 public，安全性弱")
        return ("partial", f"{desc}：未获取到 SNMP 配置详情，无法评估")

    # 通用：关键词命中即视为已配置
    hit = [k for k in rule["keywords"] if k.lower() in out_lower]
    if hit:
        return ("compliant", f"{desc}：检测到配置特征 {', '.join(hit[:3])}")
    return ("non_compliant", f"{desc}：未检测到相关配置")


async def _collect_secondary_output(device: Device) -> str:
    """通过 SSH 采集等保二级核查命令输出（H3C/华为兼容，哨兵截断）。

    Raises:
        ValueError: 设备无 SSH 凭据或连接失败。
    """
    username = device.mgmt_username
    password = reveal_secret(device.mgmt_password) if device.mgmt_password else None
    if not username or not password:
        raise ValueError("设备未配置 SSH 管理凭据")
    protocol = device.mgmt_protocol or "ssh"
    port = device.mgmt_port or (23 if protocol == "telnet" else 22)

    # Telnet 走真正的 Telnet 协议（telnetlib3），而非 SSH
    if protocol == "telnet":
        from app.services.telnet_client import run_command
        outputs = []
        # 等保核查命令集合（H3C/华为通用）
        for cmd in SECONDARY_COMMANDS:
            try:
                out = await run_command(
                    device.ip, username, password, cmd,
                    port=port, timeout=60,
                )
                outputs.append(f"########## {cmd} ##########\n{out}")
            except Exception as e:
                outputs.append(f"########## {cmd} ##########\n<error> {e}")
        return "\n\n".join(outputs)

    conn = None
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                device.ip,
                port=port,
                username=username,
                password=password,
                known_hosts=settings.SSH_KNOWN_HOSTS or None,
                login_timeout=30,
                # 兼容老旧华为 VRP / H3C Comware 设备的弱算法
                kex_algs=(
                    "curve25519-sha256,curve25519-sha256@libssh.org,"
                    "ecdh-sha2-nistp256,ecdh-sha2-nistp384,ecdh-sha2-nistp521,"
                    "diffie-hellman-group14-sha256,diffie-hellman-group16-sha512,"
                    "diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha256,"
                    "diffie-hellman-group-exchange-sha1,diffie-hellman-group1-sha1"
                ),
                encryption_algs=(
                    "chacha20-poly1305@openssh.com,aes128-gcm@openssh.com,"
                    "aes256-gcm@openssh.com,aes128-ctr,aes256-ctr,aes192-ctr,"
                    "aes128-cbc,aes192-cbc,aes256-cbc,3des-cbc"
                ),
                server_host_key_algs=(
                    "ssh-ed25519,rsa-sha2-512,rsa-sha2-256,"
                    "ecdsa-sha2-nistp256,ssh-rsa,ssh-dss"
                ),
                mac_algs=(
                    "hmac-sha2-256,hmac-sha2-512,hmac-sha1,hmac-sha1-96,hmac-md5"
                ),
            ),
            timeout=45,
        )
        writer, reader, _ = await conn.open_session(term_type="vt100", term_size=(200, 50), encoding=None)
        await asyncio.sleep(1)
        try:
            await asyncio.wait_for(reader.read(8192), timeout=2)
        except asyncio.TimeoutError:
            pass
        writer.write(b"screen-length disable\r\n")
        await writer.drain()
        await asyncio.sleep(0.5)
        try:
            await asyncio.wait_for(reader.read(8192), timeout=2)
        except asyncio.TimeoutError:
            pass

        outputs = []
        for cmd in SECONDARY_COMMANDS:
            marker = "__AIOPS_DONE_" + uuid.uuid4().hex + "__"
            writer.write(f"{cmd}\r\n".encode("utf-8", errors="replace"))
            writer.write(f"{marker}\r\n".encode("utf-8", errors="replace"))
            await writer.drain()
            out = ""
            end = asyncio.get_event_loop().time() + 40
            while asyncio.get_event_loop().time() < end:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=10)
                except asyncio.TimeoutError:
                    break
                if not chunk:
                    break
                s = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
                out += s
                if marker in out:
                    out = out.split(marker, 1)[0]
                    break
            outputs.append(f"\n===== {cmd} =====\n{out}")
            # 命令可能关闭通道，检测到异常时重开会话
            if "channel not open" in out.lower() or "broken pipe" in out.lower():
                try:
                    writer.close()
                except Exception:
                    pass
                writer, reader, _ = await conn.open_session(term_type="vt100", term_size=(200, 50), encoding=None)
                await asyncio.sleep(0.5)

        try:
            writer.close()
        except Exception:
            pass
        conn.close()
        text = "\n".join(outputs)
        if len(text) < 200:
            raise ValueError(f"采集输出过短（{len(text)} 字符），可能未建立有效会话")
        return text
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


async def run_secondary_compliance_check(session, device_id: int) -> dict:
    """对单台设备执行等保二级配置核查（SSH 采集真实配置评估）。

    Args:
        session: 异步 SQLAlchemy 会话。
        device_id: 目标设备 ID。

    Returns:
        dict: 包含 device_id/device_name/method/score/passed/total/details/categories，
        其中 method = "ssh_config"（真实配置核查）或 "snmp_fallback"（SSH 不可用时回退 SNMP 推断）。
    """
    device = (await session.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
    if device is None:
        return {"device_id": device_id, "error": "设备不存在"}

    try:
        output = await _collect_secondary_output(device)
        method = "ssh_config"
    except Exception as e:
        logger.warning(f"等保二级 SSH 核查失败 {device.name}({device.ip}): {e}")
        # 不再回退到「指标推断」（CPU<70% 就算处理能力冗余那套）：那会凭空造出一个
        # 看上去真实的分数，客户拿去当等保测评依据会出问题。如实报告不可用，
        # 运行态无结论；配置基线部分仍会由 run_device_compliance_check 合并进来。
        return {
            "device_id": device_id,
            "id": device_id,
            "device_name": device.name,
            "ip": device.ip,
            "method": "unavailable",
            "score": 0.0,
            "passed": 0,
            "total": 0,
            "details": [],
            "categories": {
                cat_key: {"label": cat_label, "score": 0.0, "passed": 0, "total": 0}
                for cat_key, cat_label in SECONDARY_CATEGORIES.items()
            },
            "checked_at": datetime.now(timezone.utc),
            "note": f"SSH 采集不可用（{e}），运行态核查未完成",
        }

    # ── SSH 配置核查评估 ──
    results = []
    for rule in SECONDARY_RULES:
        status, evidence = _assess_secondary_rule(rule, output)
        cid = rule["control_id"]
        stmt = pg_insert(ComplianceCheck).values(
            device_id=device_id,
            control_id=cid,
            control_desc=rule["desc"],
            status=status,
            evidence=evidence,
            checked_at=datetime.now(timezone.utc),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["device_id", "control_id"],
            set_={
                "control_desc": stmt.excluded.control_desc,
                "status": stmt.excluded.status,
                "evidence": stmt.excluded.evidence,
                "checked_at": stmt.excluded.checked_at,
            },
        )
        await session.execute(stmt)
        results.append({
            "control_id": cid,
            "category": rule["category"],
            "desc": rule["desc"],
            "status": status,
            "evidence": evidence,
        })
    await session.commit()

    applicable = [r for r in results if r["status"] != "not_applicable"]
    passed = sum(1 for r in applicable if r["status"] == "compliant")
    score = round(passed / len(applicable) * 100, 1) if applicable else 100.0

    categories = {}
    for cat_key, cat_label in SECONDARY_CATEGORIES.items():
        cat_results = [r for r in results if r["category"] == cat_key]
        categories[cat_key] = {
            "label": cat_label,
            "score": round(sum(1 for r in cat_results if r["status"] == "compliant") / len(cat_results) * 100, 1) if cat_results else 0.0,
        }

    return {
        "device_id": device_id,
        "id": device_id,
        "device_name": device.name,
        "ip": device.ip,
        "method": "ssh_config",
        "score": score,
        "passed": passed,
        "total": len(applicable),
        "details": results,
        "categories": categories,
        "checked_at": datetime.now(timezone.utc),
    }


async def run_secondary_compliance_check_batch(session, device_ids: list[int] | None = None) -> dict:
    """对全部或部分设备执行完整等保核查（运行态 + 配置基线，并发，限 5 路，每台独立事务提交）。

    Args:
        session: 异步 SQLAlchemy 会话（仅用于读取设备列表）。
        device_ids: 指定设备 ID 列表；None 表示全部设备。

    Returns:
        dict: {"devices": [...], "total_devices": int, "overall_avg": float}
    """
    from app.database import async_session

    if device_ids is None or not device_ids:
        ids_result = await session.execute(select(Device.id))
        device_ids = [row[0] for row in ids_result.all()]

    sem = asyncio.Semaphore(5)

    async def limited(did):
        async with sem:
            # 每台设备使用独立会话，避免并发共享同一 session 的冲突，且各自显式 commit
            async with async_session() as dev_session:
                return await run_device_compliance_check(dev_session, did)

    devices = await asyncio.gather(*[limited(did) for did in device_ids], return_exceptions=True)

    results = []
    for r in devices:
        if isinstance(r, Exception):
            logger.error(f"等保二级评估异常: {r}")
            continue
        results.append(r)

    avg = round(sum(d["score"] for d in results if "score" in d) / len(results), 1) if results else 0.0
    return {
        "devices": results,
        "total_devices": len(results),
        "overall_avg": avg,
    }


# ---------------------------------------------------------------------------
# 运行态核查 + 配置基线核查 的合并
# ---------------------------------------------------------------------------

def _merge_device_results(runtime: dict, collected: dict | None) -> dict:
    """把两类核查结果合成一台设备的完整结论。

    - 运行态核查（SSH 采集 display 命令）：看服务是否真的在跑、日志是否落地；
    - 配置基线核查（配置备份全文）：看配置里写了什么。

    两者控制项不同、id 不重叠（SEC-* 与 CFG-*），因此直接拼接后统一重算评分；
    ``details[].source`` 区分来源（``runtime`` / ``config``），供前端标注。
    配置未采集时其 14 项为 not_applicable，会被排除出评分，不影响原有分数。
    """
    if runtime.get("error"):
        return runtime

    runtime_details = runtime.get("details") or []
    for d in runtime_details:
        d.setdefault("source", "runtime")

    cfg_details = (collected or {}).get("details") or []
    merged = runtime_details + cfg_details

    applicable = [d for d in merged if d.get("status") != "not_applicable"]
    passed = sum(1 for d in applicable if d.get("status") == "compliant")
    score = round(passed / len(applicable) * 100, 1) if applicable else 0.0

    categories = {}
    for key, label in SECONDARY_CATEGORIES.items():
        rows = [d for d in merged if d.get("category") == key]
        ok = sum(1 for d in rows if d.get("status") == "compliant")
        categories[key] = {
            "label": label,
            "score": round(ok / len(rows) * 100, 1) if rows else 0.0,
            "passed": ok,
            "total": len(rows),
        }

    # 只有真正跑出结论的来源才算一种数据源；unavailable / none 不列入
    methods = [m for m in [runtime.get("method")] if m and m not in ("none", "unavailable")]
    if (collected or {}).get("config_available") and "config_backup" not in methods:
        methods.append("config_backup")

    out = dict(runtime)
    out.update({
        "details": merged,
        "score": score,
        "passed": passed,
        "total": len(applicable),
        "categories": categories,
        "methods": methods,
        "config_available": bool((collected or {}).get("config_available")),
        "config_backup_at": (collected or {}).get("config_backup_at"),
        "config_note": (collected or {}).get("note"),
    })
    return out


async def run_device_compliance_check(session, device_id: int) -> dict:
    """单台设备的完整等保核查（运行态 + 配置基线），供批量接口与页面使用。

    两侧结论都会落库（``SEC-*`` 由运行态核查写、``CFG-*`` 在这里写），
    这样 ``GET /compliance/status`` 才能读到完整结论。只合并不落库的话，
    页面上会出现「刚评估完、一刷新配置项就没了」的错觉。
    """
    runtime = await run_secondary_compliance_check(session, device_id)
    try:
        collected = await config_baseline_service.collect_findings(session, device_id)
        if collected.get("details"):
            await config_baseline_service.persist_details(
                session, device_id, collected["details"]
            )
    except Exception as e:
        # 配置基线核查失败不应拖垮运行态结论（例如规则集缺失）
        logger.error(f"配置基线核查失败 device_id={device_id}: {e}")
        collected = None
    return _merge_device_results(runtime, collected)


# ---------------------------------------------------------------------------
# 等保2.0 三级合规检查规则集
# ---------------------------------------------------------------------------

COMPLIANCE_RULES = [
    {
        "control_id": "8.1.3.2-a",
        "desc": "网络设备处理能力具备冗余空间",
        "applicable_types": ["switch", "router", "firewall"],
        "requires_snmp": True,
    },
    {
        "control_id": "8.1.3.3-a",
        "desc": "重要网络设备配置热冗余",
        "applicable_types": ["core_switch", "firewall"],
        "requires_snmp": True,
    },
    {
        "control_id": "8.1.3.5-a",
        "desc": "设备链路冗余",
        "applicable_types": ["core_switch", "router"],
        "requires_snmp": True,
    },
    {
        "control_id": "8.1.4.2-b",
        "desc": "限制非法内联",
        "applicable_types": ["firewall"],
        "requires_syslog": True,
    },
    {
        "control_id": "8.1.4.3-a",
        "desc": "入侵检测/防御",
        "applicable_types": ["firewall"],
        "requires_syslog": True,
    },
    {
        "control_id": "8.1.4.4-a",
        "desc": "恶意代码防范",
        "applicable_types": ["firewall"],
        "requires_snmp": True,
    },
    {
        "control_id": "8.1.4.5-a",
        "desc": "安全审计覆盖所有用户操作",
        "applicable_types": ["switch", "router", "firewall"],
        "requires_syslog": True,
    },
    {
        "control_id": "8.1.5.1-a",
        "desc": "设备身份标识与鉴别",
        "applicable_types": ["switch", "router", "firewall"],
        "requires_config": True,
    },
    {
        "control_id": "8.1.5.3-d",
        "desc": "设备管理超时退出",
        "applicable_types": ["switch", "router", "firewall"],
        "requires_config": True,
    },
]

# 合规状态映射：从 (符合条件, 部分符合条件) 到 status
_CONFIDENCE_THRESHOLD_HIGH = 0.85
_CONFIDENCE_THRESHOLD_LOW = 0.50


# ---------------------------------------------------------------------------
# 1. 规则匹配
# ---------------------------------------------------------------------------

def is_rule_applicable(rule, device_type):
    """判断规则是否适用于指定设备类型。

    Args:
        rule: COMPLIANCE_RULES 中的规则字典。
        device_type: 设备的 device_type 字符串。

    Returns:
        bool: True 表示规则适用于该设备类型。
    """
    return device_type in rule["applicable_types"]


# ---------------------------------------------------------------------------
# 2. 单设备合规检查
# ---------------------------------------------------------------------------

async def run_compliance_check(session, device_id):
    """对指定设备执行全部等保2.0合规检查，结果 upsert 到 ComplianceCheck 表。

    .. deprecated:: 4.6.0
        **已停止使用**，不再被任何 API 或页面调用。它按 CPU/内存/SNMP 版本
        「推断」合规结论（如 CPU<70% 就判"处理能力具备冗余空间"），并非真实核查，
        不能作为等保测评依据。保留仅为兼容历史调用方。

        替代方案（均为真实核查）：
          - ``config_baseline_service.run_config_baseline_check``：配置态核查
          - ``compliance_service.run_secondary_compliance_check``：运行态核查
          - ``compliance_service.run_device_compliance_check``：两者合并

    由于无法获取真实 SNMP/syslog/config 数据，检查逻辑基于可用指标模拟：
      - SNMP 类规则：根据 CPU/内存使用率推断冗余/处理能力
      - syslog 类规则：根据 SecurityEvent 表中是否存在对应事件记录推断
      - config 类规则：根据设备 SNMP 版本间接推断配置合规

    Args:
        session: 异步 SQLAlchemy 会话。
        device_id: 目标设备 ID。

    Returns:
        list[dict]: 所有合规检查结果，包含 control_id、desc、status、evidence 字段。
        设备不存在时返回空列表。
    """
    result = await session.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        return []

    device_type = device.device_type or ""
    now = datetime.now(timezone.utc)
    results = []

    for rule in COMPLIANCE_RULES:
        if not is_rule_applicable(rule, device_type):
            results.append({
                "control_id": rule["control_id"],
                "desc": rule["desc"],
                "status": "not_applicable",
                "evidence": f"规则不适用于设备类型 {device_type}",
            })
            continue

        status, evidence = await _evaluate_rule(session, rule, device)

        # upsert 到数据库
        stmt = pg_insert(ComplianceCheck).values(
            device_id=device_id,
            control_id=rule["control_id"],
            control_desc=rule["desc"],
            status=status,
            evidence=evidence,
            checked_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["device_id", "control_id"],
            set_={
                "control_desc": stmt.excluded.control_desc,
                "status": stmt.excluded.status,
                "evidence": stmt.excluded.evidence,
                "checked_at": stmt.excluded.checked_at,
            },
        )
        await session.execute(stmt)

        results.append({
            "control_id": rule["control_id"],
            "desc": rule["desc"],
            "status": status,
            "evidence": evidence,
        })

    await session.flush()
    return results


async def _evaluate_rule(session, rule, device):
    """针对单条规则评估设备的合规状态（内部辅助函数）。

    Returns:
        tuple[str, str]: (status, evidence_text)
    """
    device_id = device.id

    # ── SNMP 类规则：基于 CPU/内存使用率推断处理能力冗余 ──
    if rule.get("requires_snmp"):
        cpu = device.cpu_usage
        mem = device.memory_usage

        if cpu is None and mem is None:
            return ("partial", "SNMP 数据不可用，无法获取设备性能指标进行冗余评估")

        cpu_ok = cpu is not None and cpu < 70
        mem_ok = mem is not None and mem < 70
        cpu_txt = f"{cpu:.1f}" if cpu is not None else "N/A"
        mem_txt = f"{mem:.1f}" if mem is not None else "N/A"

        if cpu_ok and mem_ok:
            return (
                "compliant",
                f"设备处理能力充裕：CPU={cpu_txt}%, 内存={mem_txt}%，具备冗余空间",
            )
        elif cpu_ok or mem_ok:
            return (
                "partial",
                f"设备部分资源充裕：CPU={cpu_txt}%{'（正常）' if cpu_ok else '（偏高）'}, "
                f"内存={mem_txt}%{'（正常）' if mem_ok else '（偏高）'}",
            )
        else:
            return (
                "non_compliant",
                f"设备处理能力不足：CPU={cpu_txt}%, 内存={mem_txt}%，缺乏冗余空间",
            )

    # ── syslog 类规则：基于 SecurityEvent 记录推断 ──
    if rule.get("requires_syslog"):
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)

        # 检查该设备在近 7 天内是否有对应类别的事件
        if rule["control_id"] == "8.1.4.2-b":
            # 限制非法内联：检查是否有被 blocked 的策略违规事件
            blocked_result = await session.execute(
                select(func.count(SecurityEvent.id)).where(
                    and_(
                        SecurityEvent.device_id == device_id,
                        SecurityEvent.action == "blocked",
                        SecurityEvent.timestamp >= seven_days_ago,
                    )
                )
            )
            blocked_count = blocked_result.scalar() or 0

            if blocked_count > 0:
                return (
                    "compliant",
                    f"近 7 天检测到并阻止 {blocked_count} 次非法连接尝试，内联限制机制正常工作",
                )
            else:
                # 进一步检查是否有 policy/detected 事件
                policy_result = await session.execute(
                    select(func.count(SecurityEvent.id)).where(
                        and_(
                            SecurityEvent.device_id == device_id,
                            SecurityEvent.event_category.in_(["policy", "intrusion"]),
                            SecurityEvent.timestamp >= seven_days_ago,
                        )
                    )
                )
                policy_count = policy_result.scalar() or 0
                if policy_count > 0:
                    return (
                        "partial",
                        f"近 7 天检测到 {policy_count} 次策略/入侵事件，但未全部阻止",
                    )
                return ("partial", "近 7 天未检测到安全事件，无法确认内联限制机制状态")

        elif rule["control_id"] == "8.1.4.3-a":
            # 入侵检测/防御：检查入侵类事件数量和处置率
            intrusion_result = await session.execute(
                select(func.count(SecurityEvent.id)).where(
                    and_(
                        SecurityEvent.device_id == device_id,
                        SecurityEvent.event_category == "intrusion",
                        SecurityEvent.timestamp >= seven_days_ago,
                    )
                )
            )
            intrusion_count = intrusion_result.scalar() or 0

            blocked_intrusion_result = await session.execute(
                select(func.count(SecurityEvent.id)).where(
                    and_(
                        SecurityEvent.device_id == device_id,
                        SecurityEvent.event_category == "intrusion",
                        SecurityEvent.action.in_(["blocked", "dropped"]),
                        SecurityEvent.timestamp >= seven_days_ago,
                    )
                )
            )
            blocked_intrusion_count = blocked_intrusion_result.scalar() or 0

            if intrusion_count == 0:
                return ("partial", "近 7 天未检测到入侵事件，无法评估入侵检测/防御有效性")
            block_rate = blocked_intrusion_count / intrusion_count
            if block_rate >= _CONFIDENCE_THRESHOLD_HIGH:
                return (
                    "compliant",
                    f"入侵防御有效率 {block_rate:.0%}（阻止 {blocked_intrusion_count}/{intrusion_count}）",
                )
            elif block_rate >= _CONFIDENCE_THRESHOLD_LOW:
                return (
                    "partial",
                    f"入侵防御部分有效：阻止率 {block_rate:.0%}（阻止 {blocked_intrusion_count}/{intrusion_count}）",
                )
            else:
                return (
                    "non_compliant",
                    f"入侵防御不足：阻止率仅 {block_rate:.0%}（阻止 {blocked_intrusion_count}/{intrusion_count}）",
                )

        elif rule["control_id"] == "8.1.4.5-a":
            # 安全审计覆盖：各类事件是否都有记录
            audit_result = await session.execute(
                select(
                    SecurityEvent.event_category,
                    func.count(SecurityEvent.id).label("cnt"),
                )
                .where(
                    and_(
                        SecurityEvent.device_id == device_id,
                        SecurityEvent.timestamp >= seven_days_ago,
                    )
                )
                .group_by(SecurityEvent.event_category)
            )
            categories = {row[0]: row[1] for row in audit_result.all()}

            if not categories:
                return ("non_compliant", "近 7 天无任何安全审计日志记录")

            expected_cats = {"intrusion", "policy", "audit", "anomaly", "malware"}
            covered = expected_cats & set(categories.keys())
            coverage = len(covered) / len(expected_cats)

            if coverage >= _CONFIDENCE_THRESHOLD_HIGH:
                return (
                    "compliant",
                    f"安全审计覆盖 {len(covered)}/{len(expected_cats)} 类事件：{', '.join(sorted(covered))}",
                )
            elif coverage >= _CONFIDENCE_THRESHOLD_LOW:
                return (
                    "partial",
                    f"安全审计部分覆盖 {len(covered)}/{len(expected_cats)} 类事件，"
                    f"缺失��{', '.join(sorted(expected_cats - covered))}",
                )
            else:
                return (
                    "non_compliant",
                    f"安全审计严重不足：仅覆盖 {len(covered)}/{len(expected_cats)} 类事件",
                )

        # 兜底
        return ("partial", "syslog 数据不完整，无法准确评估该控制项")

    # ── config 类规则：基于设备配置参数推断 ──
    if rule.get("requires_config"):
        if rule["control_id"] == "8.1.5.1-a":
            # 设备身份标识与鉴别：SNMPv3 提供加密认证，视为合规
            snmp_v = (device.snmp_version or "").lower()
            if snmp_v == "v3":
                return ("compliant", "设备使用 SNMPv3，具备身份标识与加密鉴别机制")
            elif snmp_v in ("v2", "v2c"):
                return ("partial", f"设备使用 SNMP{snmp_v}，具备 community 字符串认证但缺少加密")
            elif snmp_v == "v1":
                return ("non_compliant", "设备使用 SNMPv1，缺乏有效的身份鉴别机制")
            else:
                return ("partial", f"SNMP 版本为 '{device.snmp_version}'，无法确认身份鉴别机制")

        elif rule["control_id"] == "8.1.5.3-d":
            # 设备管理超时退出：如果有 SNMPv3 或 syslog 审计记录，推断有配置
            snmp_v = (device.snmp_version or "").lower()
            if snmp_v == "v3":
                return ("compliant", "设备使用 SNMPv3，推断已配置管理会话超时退出机制")

            # 检查是否有审计日志中记录的会话超时事件
            timeout_result = await session.execute(
                select(func.count(SecurityEvent.id)).where(
                    and_(
                        SecurityEvent.device_id == device_id,
                        SecurityEvent.event_category == "audit",
                        SecurityEvent.description.ilike("%timeout%"),
                    )
                )
            )
            if (timeout_result.scalar() or 0) > 0:
                return ("compliant", "审计日志中检测到会话超时记录，推断超时退出机制已配置")
            return ("partial", "缺少配置数据，无法确认管理超时退出机制是否已启用")

    # 未知规则类型
    return ("not_applicable", "无法评估：缺少必要的数据源")


# ---------------------------------------------------------------------------
# 3. 合规评分计算
# ---------------------------------------------------------------------------

async def calculate_compliance_score(session, device_id):
    """计算指定设备的等保合规评分（百分制）。

    评分规则：对所有 applicable 规则，compliant=通过，其余皆不通过。
    仅当设备存在且至少有一项 applicable 规则时才返回有意义的分数。

    Args:
        session: 异步 SQLAlchemy 会话。
        device_id: 目标设备 ID。

    Returns:
        dict | None:
            {
                "device_id": int,
                "score": float,           # 0-100 合规分数
                "passed": int,            # 通过项数
                "total": int,             # applicable 规则总数
                "details": [
                    {"control_id": str, "desc": str, "status": str, "evidence": str},
                    ...
                ]
            }
        设备不存在时返回 None。
    """
    device_result = await session.execute(select(Device).where(Device.id == device_id))
    if device_result.scalar_one_or_none() is None:
        return None

    results = await run_compliance_check(session, device_id)

    # 仅统计 applicable 规则
    applicable = [r for r in results if r["status"] != "not_applicable"]
    if not applicable:
        return {
            "device_id": device_id,
            "score": 100.0,
            "passed": 0,
            "total": 0,
            "details": results,
        }

    passed = sum(1 for r in applicable if r["status"] == "compliant")
    score = round(passed / len(applicable) * 100, 1)

    return {
        "device_id": device_id,
        "score": score,
        "passed": passed,
        "total": len(applicable),
        "details": results,
    }


# ---------------------------------------------------------------------------
# 4. 合规状态查询（读取已落库的核查结论）
# ---------------------------------------------------------------------------
#
# 口径说明：本模块早期用「CPU<70% 就算处理能力冗余」这类指标推断来模拟合规结论
# （见 run_compliance_check 的 docstring），可信度低，**不再作为页面评分依据**。
# 现在只统计两类真实核查的落库结果：
#   SEC-*  运行态核查（SSH 采集 display 命令，见 SECONDARY_RULES）
#   CFG-*  配置基线核查（配置备份全文解析，见 config_baseline_service）
# 早期写入的 8.1.* 模拟行保留在表中但不再参与评分，避免污染真实结论。

def _control_category_map() -> dict:
    """control_id → 五维分类。"""
    mapping = {}
    try:
        from app.services.config_audit_engine import load_rules
        for r in load_rules()["rules"]:
            mapping[r["id"]] = r.get("category") or ""
    except Exception:
        logger.warning("配置基线规则集加载失败，CFG-* 项将归入未分类", exc_info=True)
    for r in SECONDARY_RULES:
        mapping[r["control_id"]] = r.get("category") or ""
    return mapping


def _is_real_check(control_id: str) -> bool:
    """只认真实核查项；排除早期指标模拟写入的 8.1.* 行。"""
    return control_id.startswith("CFG-") or control_id.startswith("SEC-")


def _summarize_device(device_row, check_rows, category_map) -> dict:
    """把一台设备的落库核查行汇总成前端需要的结构。"""
    details = []
    for r in check_rows:
        cid = r.control_id or ""
        if not _is_real_check(cid):
            continue
        details.append({
            "control_id": cid,
            "category": category_map.get(cid, ""),
            "desc": r.control_desc or "",
            "status": r.status or "not_applicable",
            "evidence": r.evidence or "",
            "source": "config" if cid.startswith("CFG-") else "runtime",
        })
    # 同来源内按 control_id 排序，展示稳定
    details.sort(key=lambda d: (0 if d["source"] == "runtime" else 1, d["control_id"]))

    applicable = [d for d in details if d["status"] != "not_applicable"]
    passed = sum(1 for d in applicable if d["status"] == "compliant")
    score = round(passed / len(applicable) * 100, 1) if applicable else 0.0

    categories = {}
    for key, label in SECONDARY_CATEGORIES.items():
        rows = [d for d in details if d["category"] == key]
        ok = sum(1 for d in rows if d["status"] == "compliant")
        categories[key] = {
            "label": label,
            "score": round(ok / len(rows) * 100, 1) if rows else 0.0,
            "passed": ok,
            "total": len(rows),
        }

    methods = []
    if any(d["source"] == "runtime" for d in details):
        methods.append("ssh_config")
    # 配置基线：只有在真正拿到配置全文（不是整片 not_applicable）时才算一种数据源
    config_available = any(
        d["source"] == "config" and d["status"] != "not_applicable" for d in details
    )
    if config_available:
        methods.append("config_backup")

    checked_at = max((r.checked_at for r in check_rows if r.checked_at), default=None)

    return {
        "device_id": device_row.id,
        "id": device_row.id,
        "device_name": device_row.name,
        "ip": device_row.ip,
        "device_type": device_row.device_type,
        "score": score,
        "compliance_score": score,
        "passed": passed,
        "total": len(applicable),
        "checked": bool(applicable),
        "checked_at": checked_at,
        "method": methods[0] if methods else "none",
        "methods": methods,
        "config_available": config_available,
        "categories": categories,
        "details": details,
    }


def _empty_summary(device_row) -> dict:
    """尚未核查过的设备：明确标记为未核查，而不是给一个假的 0 分。"""
    return {
        "device_id": device_row.id,
        "id": device_row.id,
        "device_name": device_row.name,
        "ip": device_row.ip,
        "device_type": device_row.device_type,
        "score": None,
        "compliance_score": None,
        "passed": 0,
        "total": 0,
        "checked": False,
        "checked_at": None,
        "method": "none",
        "methods": [],
        "config_available": False,
        "categories": {
            key: {"label": label, "score": 0.0, "passed": 0, "total": 0}
            for key, label in SECONDARY_CATEGORIES.items()
        },
        "details": [],
    }


async def get_compliance_status(session, device_id=None, page=None, page_size=None):
    """查询合规状态：单设备详情或全局概要。

    Args:
        session: 异步 SQLAlchemy 会话。
        device_id: 指定设备 ID 时返回该设备明细；None 时返回全平台概要。
        page: 全局模式分页页码（配合 page_size 使用）；None 表示不分页返回全部。
        page_size: 全局模式每页条数。

    Returns:
        dict:
            单设备模式 — 该设备汇总（score/passed/total/details/categories/methods），
                         另含 category_scores 扁平形式便于前端雷达使用。
            全局模式 —   {
                "devices": [当前页设备],
                "total_devices": int,
                "overall_avg": float, "overall_score": float,   # 同值，兼容两种字段名
                "compliant_devices": int,                       # 评分 ≥ 90 的设备数
                "categories": {五维: 平均分},                    # 扁平数值，供雷达图
                "non_compliant_items": [...],
            }
        设备不存在时单设备模式返回 None。
    """
    category_map = _control_category_map()

    # ── 单设备模式 ──
    if device_id is not None:
        device = (
            await session.execute(select(Device).where(Device.id == device_id))
        ).scalar_one_or_none()
        if device is None:
            return None
        rows = (await session.execute(
            select(ComplianceCheck).where(ComplianceCheck.device_id == device_id)
        )).scalars().all()
        summary = _summarize_device(device, rows, category_map) if rows else _empty_summary(device)
        summary["category_scores"] = {k: v["score"] for k, v in summary["categories"].items()}
        return summary

    # ── 全局模式：一次取全部设备与全部核查行，避免逐设备重复查询 ──
    devices = (await session.execute(select(Device).order_by(Device.id))).scalars().all()
    check_rows = (await session.execute(select(ComplianceCheck))).scalars().all()

    grouped: dict[int, list] = {}
    for r in check_rows:
        grouped.setdefault(r.device_id, []).append(r)

    summaries = []
    for dev in devices:
        rows = grouped.get(dev.id) or []
        summaries.append(_summarize_device(dev, rows, category_map) if rows else _empty_summary(dev))

    # 未核查的设备（total=0）不参与平均分，避免拉低真实水平
    scored = [s for s in summaries if s["total"] > 0]
    overall = round(sum(s["score"] for s in scored) / len(scored), 1) if scored else 0.0
    compliant = sum(1 for s in scored if s["score"] >= 90)

    category_scores = {}
    for key in SECONDARY_CATEGORIES:
        vals = [s["categories"][key]["score"] for s in summaries if s["categories"][key]["total"] > 0]
        category_scores[key] = round(sum(vals) / len(vals)) if vals else 0

    non_compliant_items = []
    for s in summaries:
        for d in s["details"]:
            if d["status"] == "non_compliant":
                non_compliant_items.append({
                    "device_id": s["device_id"],
                    "device_name": s["device_name"],
                    "control_id": d["control_id"],
                    "desc": d["desc"],
                    "evidence": d["evidence"],
                    "source": d["source"],
                })

    total_devices = len(summaries)
    if page is not None and page_size is not None and page_size > 0:
        start = (page - 1) * page_size
        page_devices = summaries[start:start + page_size]
    else:
        page_devices = summaries

    return {
        "devices": page_devices,
        "total_devices": total_devices,
        "overall_avg": overall,
        "overall_score": overall,
        "compliant_devices": compliant,
        "categories": category_scores,
        "non_compliant_items": non_compliant_items,
    }
