"""设备配置备份服务 - 通过 SSH (asyncssh) 采集设备运行配置

支持华为 VRP 与华三 Comware 等厂商。关键点：
- 自动识别设备提示符（prompt），据此判断命令是否执行结束；
- 同时尝试华为 / 华三两种关闭分页的命令，避免配置被 `---- More ----` 截断；
- 读取过程中若遇到分页提示 `---- More ----` 自动发送空格续传；
- 不再依赖配置末尾的 `return` 判定结束，配置截断会显式标记失败。
"""
import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta

import asyncssh
from sqlalchemy import select, func, or_, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.config_backup import ConfigBackup, BackupSchedule
from app.services.credential_service import reveal_secret
from app.config import settings

logger = logging.getLogger(__name__)

tz_8 = timezone(timedelta(hours=8))

# SSH legacy algorithm settings（兼容新旧设备，按协商顺序尝试）
# 必须包含老旧算法（diffie-hellman-group1-sha1 / 3des-cbc / ssh-dss / hmac-md5 等），
# 否则老设备（H3C Comware / 华为 VRP）SSH 握手失败（ConnectionLost / KeyExchangeFailed）。
SSH_KEX = (
    "curve25519-sha256,curve25519-sha256@libssh.org,"
    "ecdh-sha2-nistp256,ecdh-sha2-nistp384,ecdh-sha2-nistp521,"
    "diffie-hellman-group14-sha256,diffie-hellman-group16-sha512,"
    "diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha256,"
    "diffie-hellman-group-exchange-sha1,diffie-hellman-group1-sha1"
)
SSH_CIPHERS = (
    "chacha20-poly1305@openssh.com,aes128-gcm@openssh.com,aes256-gcm@openssh.com,"
    "aes128-ctr,aes256-ctr,aes192-ctr,aes128-cbc,aes192-cbc,aes256-cbc,3des-cbc"
)
SSH_HOSTKEYS = "ssh-ed25519,rsa-sha2-512,rsa-sha2-256,ecdsa-sha2-nistp256,ssh-rsa,ssh-dss"
SSH_MACS = "hmac-sha2-256,hmac-sha2-512,hmac-sha1,hmac-sha1-96,hmac-md5"

# 关闭分页的候选命令（华为 VRP / 华三 Comware）
PAGING_DISABLE_CMDS = ("screen-length 0 temporary", "screen-length disable")
# 单设备采集超时（秒）：框式核心设备配置可达数百 KB，需预留充足时间
BACKUP_TIMEOUT = 300

# ANSI 转义序列（vt100 终端设备输出常见，会干扰提示符/配置内容匹配）
ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]|\x1b\(")


def _strip_ansi(text: str) -> str:
    """去掉 vt100 ANSI 转义序列（如 \x1b[?25l、\x1b[2J），还原纯文本。"""
    return ANSI_ESC_RE.sub("", text)

def _decode(chunk) -> str:
    """把会话原始字节智能解码为正确中文（UTF-8 优先，GBK 兜底）。

    设备配置常含中文，H3C 传统编码为 GBK/GB2312，新设备或英文模式为 UTF-8。
    若固定按 utf-8 解码，GBK 字节会抛 UnicodeDecodeError（asyncssh 包装为
    ProtocolError 导致备份失败），因此必须智能识别。
    """
    if isinstance(chunk, bytes):
        for enc in ("utf-8", "gbk"):
            try:
                return chunk.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return chunk.decode("gbk", errors="replace")
    return str(chunk)


async def _drain(reader, timeout: float = 3.0) -> str:
    """读取并丢弃当前可读数据（用于吸收命令回显 / 错误信息）。"""
    out = []
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(8192), timeout=1.5)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        out.append(_decode(chunk))
    return "".join(out)


async def _capture_prompt(writer, reader, timeout: float = 12.0) -> str | None:
    """发送回车并读取，返回设备提示符（最后一行非空内容，如 `<AR1>` / `<H3C-SW>`）。"""
    try:
        writer.write(b"\r\n")
        await writer.drain()
    except Exception:
        pass
    raw = await _drain(reader, timeout)
    # 兼容 Unix / Windows 换行，并去掉 ANSI 转义（vt100 输出可能带 \x1b[... 干扰提示符）
    lines = [ln.strip() for ln in _strip_ansi(raw.replace("\r\n", "\n")).split("\n") if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    # 提示符本身不应是配置命令回显
    if "display" in last or "screen-length" in last:
        # 取更早的一行
        for ln in reversed(lines[:-1]):
            if "display" not in ln and "screen-length" not in ln:
                return ln.strip()
        return None
    return last


# 提示符：用户视图 <xxx>、特权视图 xxx#、系统视图 [xxx]（H3C 可在 system view 下执行 display）
PROMPT_RE = re.compile(r"^(<[^>]*>|\S+#|\[[^\[\]]*\])\s*$")


async def _read_command_output(writer, reader, prompt: str | None, timeout: int) -> str:
    """读取命令输出，直到设备提示符再次出现（命令结束）。

    - 遇到 `---- More ----` 自动发送空格续传，避免配置被分页截断；
    - 通过命令回显中的提示符判定“开始”，通过再次出现独立提示符行判定“结束”；
    - 若无法识别提示符（prompt=None），回退到以配置末尾 `return` 判定结束；
    - 仅在已读到配置结尾 `return`（或命令回显后再次出现提示符）时才判定结束，
      中间若设备输出停顿（如框式核心设备配置量大、CPU 忙）不会误判提前截断。
    """
    loop = asyncio.get_event_loop()
    raw_parts: list[str] = []
    seen_echo = False
    saw_return = False
    deadline = loop.time() + timeout
    last_data = loop.time()

    def joined() -> str:
        return "".join(raw_parts)

    while loop.time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=2)
        except asyncio.TimeoutError:
            # 无数据停顿：仅当已读到配置结尾 return 才判定结束（此时配置已完整输出）；
            # 否则继续等待，避免大配置输出中途停顿被提前截断。
            if saw_return and (loop.time() - last_data) > 4:
                break
            continue
        if not chunk:
            break
        last_data = loop.time()
        text = _decode(chunk)
        # 去掉 ANSI 转义，避免 \x1b[... 干扰提示符/return 匹配
        text_clean = _strip_ansi(text)
        raw_parts.append(text_clean)

        lower = text_clean.lower()
        if "---- more ----" in lower:
            # 分页未完全关闭，发送空格继续；随后继续读取
            try:
                writer.write(b" ")
                await writer.drain()
            except Exception:
                pass
            # 重置超时，给设备输出后续内容
            last_data = loop.time()

        # 识别命令回显（含提示符的那一行，如 <AR1>display current-configuration）
        if not seen_echo and prompt and prompt in joined():
            seen_echo = True

        # 识别配置结尾 return（H3C/华为配置固定以 return 结尾）
        if "return" in text_clean:
            for ln in text_clean.replace("\r\n", "\n").split("\n"):
                if ln.strip() == "return":
                    saw_return = True
                    break

        # 结束判定 1：命令回显之后，再次出现独立提示符行（说明命令输出已完整结束）
        if seen_echo and prompt:
            for ln in text_clean.replace("\r\n", "\n").split("\n"):
                if ln.strip() == prompt:
                    return joined()

        # 结束判定 2（prompt 匹配失败或无提示符时的兜底）：配置以 return 结尾
        if saw_return and (not prompt or not seen_echo):
            # 再尝试读取一点尾部数据
            try:
                extra = await asyncio.wait_for(reader.read(4096), timeout=1.5)
                if extra:
                    raw_parts.append(_decode(extra))
            except asyncio.TimeoutError:
                pass
            return joined()

    return joined()


def _clean_config(raw: str, prompt: str | None) -> str:
    """清理采集到的原始文本：去掉命令回显、分页残片、首尾提示符，保留纯配置。"""
    text = raw.replace("\r\n", "\n")
    lines = text.split("\n")

    # 跳过命令回显行（含 display current-configuration 的那一行及其之前的内容）
    start = 0
    for i, line in enumerate(lines):
        if "display current-configuration" in line:
            start = i + 1
            break
    clean = lines[start:]

    # 去掉分页残片
    clean = [
        ln for ln in clean
        if "---- More ----" not in ln
        and "Unrecognized command" not in ln
        and "Incomplete command" not in ln
        and not ln.strip().startswith("^")
    ]

    # 去掉开头的空行
    while clean and clean[0].strip() == "":
        clean.pop(0)

    # 去掉结尾的空行，再去除提示符行（<AR1> / AR1# / [H3C] 等），
    # 但保留配置中的独立 `#` 章节分隔符（PROMPT_RE 不含纯 `#`）
    while clean and clean[-1].strip() == "":
        clean.pop()
    while clean and PROMPT_RE.match(clean[-1].strip()):
        clean.pop()
    # 提示符剥离后再去掉一次尾部空行（提示符行后可能还有空行）
    while clean and clean[-1].strip() == "":
        clean.pop()

    return "\n".join(clean).strip()


async def fetch_device_config(
    ip: str,
    username: str | None = None,
    password: str | None = None,
    protocol: str = "ssh",
    port: int | None = None,
    timeout: int = BACKUP_TIMEOUT,
) -> str:
    """
    通过 SSH/Telnet 连接设备，完整获取运行配置（display current-configuration）。

    设计要点：
    - 动态捕获设备提示符，命令回显后再次出现提示符即代表输出结束；
    - 同时尝试华为 / 华三关闭分页命令；读取中遇到分页提示自动续传；
    - 返回已清理的纯配置文本；若配置被截断（仍含分页提示 / 缺少结尾 return），
      调用方会在 perform_backup 中据返回文本判定为失败。
    """
    ssh_user = username or settings.DEFAULT_DEVICE_USERNAME
    ssh_pass = password or settings.DEFAULT_DEVICE_PASSWORD
    if not ssh_user or not ssh_pass:
        raise ValueError("设备未配置管理账号或密码")

    # Telnet 走真正的 Telnet 协议（telnetlib3），而非 SSH
    if protocol == "telnet":
        from app.services.telnet_client import fetch_full_config
        return await fetch_full_config(
            ip, ssh_user, ssh_pass,
            port=port or 23, timeout=timeout,
        )

    if settings.SSH_STRICT_HOST_KEY_CHECKING and not settings.SSH_KNOWN_HOSTS:
        raise ValueError("启用了 SSH 主机密钥校验，但未配置 SSH_KNOWN_HOSTS")
    ssh_port = port or 22

    # 网络/设备偶发问题（空闲连接被中间设备断开 ConnectionLost、握手失败、
    # 读取超时等）自动重试一次，提高备份成功率。
    retryable = (
        asyncssh.ConnectionLost,
        asyncssh.ChannelOpenError,
        asyncssh.DisconnectError,
        asyncssh.PermissionDenied,
        TimeoutError,
        OSError,
    )
    for attempt in (1, 2):
        conn = None
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    ip,
                    port=ssh_port,
                    username=ssh_user,
                    password=ssh_pass,
                    known_hosts=settings.SSH_KNOWN_HOSTS or None,
                    kex_algs=SSH_KEX,
                    encryption_algs=SSH_CIPHERS,
                    server_host_key_algs=SSH_HOSTKEYS,
                    mac_algs=SSH_MACS,
                    login_timeout=30,
                    # 定期发 keepalive，防止中间网络设备/防火墙因空闲超时断开 SSH 会话
                    keepalive_interval=30,
                    keepalive_count_max=3,
                ),
                timeout=timeout + 15,
            )

            # encoding=None：以字节模式读取，避免 asyncssh 按 utf-8 解码设备输出的
            # GBK 中文抛 ProtocolError；由 _decode 智能识别 UTF-8/GBK 还原。
            writer, reader, _ = await conn.open_session(
                term_type="vt100", term_size=(200, 50), encoding=None,
            )

            # 捕获设备提示符
            prompt = await _capture_prompt(writer, reader)
            if not prompt:
                # 再尝试一次
                prompt = await _capture_prompt(writer, reader, timeout=6)

            # 关闭分页（两种厂商命令都试一次，生效的那个起作用）
            for cmd in PAGING_DISABLE_CMDS:
                try:
                    writer.write((cmd + "\r\n").encode("utf-8", errors="replace"))
                    await writer.drain()
                    await asyncio.sleep(0.3)
                    await _drain(reader, timeout=2.5)
                except Exception as e:
                    logger.debug(f"paging disable cmd '{cmd}' failed on {ip}: {e}")
            # 重新捕获提示符（分页命令执行后提示符可能稳定，且可清空缓冲）
            try:
                writer.write(b"\r\n")
                await writer.drain()
                np = await _capture_prompt(writer, reader, timeout=6)
                if np:
                    prompt = np
            except Exception:
                pass

            # 发送配置采集命令
            writer.write(b"display current-configuration\r\n")
            await writer.drain()

            raw = await _read_command_output(writer, reader, prompt, timeout)

            try:
                writer.close()
            except Exception:
                pass
            conn.close()

            clean_config = _clean_config(raw, prompt)
            return clean_config

        except retryable as e:
            logger.warning(f"SSH backup attempt {attempt}/2 failed for {ip}: {type(e).__name__}: {e}")
            if attempt == 1:
                await asyncio.sleep(2)
                continue
            logger.error(f"SSH backup failed for {ip}: {type(e).__name__}: {e}")
            raise
        except Exception as e:
            logger.error(f"SSH backup failed for {ip}: {type(e).__name__}: {e}")
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


async def perform_backup(
    db: AsyncSession,
    device_id: int,
    backup_type: str = "manual",
    schedule_id: int | None = None,
) -> ConfigBackup:
    """执行单台设备的配置备份并存入数据库"""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise ValueError(f"Device {device_id} not found")

    backup = ConfigBackup(
        device_id=device_id,
        backup_type=backup_type,
        config_content="",
        status="failed",
        error_message="",
    )

    try:
        config_text = await fetch_device_config(
            device.ip,
            username=device.mgmt_username,
            password=reveal_secret(device.mgmt_password),
            protocol=device.mgmt_protocol or "ssh",
            port=device.mgmt_port,
        )

        if not config_text or len(config_text) < 50:
            backup.error_message = "配置内容为空或过短，可能未成功采集"
            backup.status = "failed"
        elif "---- More ----" in config_text or "---- more ----" in config_text.lower():
            # 配置被分页截断：说明分页未关闭或续传失败，视为不完整
            backup.error_message = "配置被分页截断（---- More ----），未完整获取运行配置"
            backup.status = "failed"
            backup.config_content = config_text
            backup.file_size = len(config_text.encode())
            backup.line_count = len(config_text.split("\n"))
        elif not config_text.rstrip().endswith("return"):
            # 正常华为/华三运行配置以 return 结尾，缺少说明采集被截断
            backup.error_message = "配置不完整（缺少结尾 return，可能被超时或分页截断）"
            backup.status = "failed"
            backup.config_content = config_text
            backup.file_size = len(config_text.encode())
            backup.line_count = len(config_text.split("\n"))
        else:
            backup.config_content = config_text
            backup.config_hash = hashlib.sha256(config_text.encode()).hexdigest()
            backup.file_size = len(config_text.encode())
            backup.line_count = len(config_text.split("\n"))
            backup.status = "success"
            backup.error_message = None

    except Exception as e:
        backup.error_message = f"{type(e).__name__}: {str(e)[:500]}"
        backup.status = "failed"

    db.add(backup)
    await db.flush()

    # Update schedule's last_backup_at if this was scheduled
    if backup_type == "scheduled" and schedule_id:
        sched_result = await db.execute(
            select(BackupSchedule).where(BackupSchedule.id == schedule_id)
        )
        schedule = sched_result.scalar_one_or_none()
        if schedule:
            schedule.last_backup_at = datetime.now(timezone.utc)
            schedule.next_backup_at = calculate_next_backup(schedule)

    await db.commit()
    return backup


def calculate_next_backup(schedule: BackupSchedule) -> datetime:
    """计算下次备份时间"""
    now = datetime.now(timezone.utc).astimezone(tz_8)

    if schedule.frequency == "daily":
        # Next occurrence of hour:minute today or tomorrow
        next_time = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
        if next_time <= now:
            next_time = next_time.replace(day=next_time.day + 1)
        return next_time.astimezone(timezone.utc)

    elif schedule.frequency == "weekly":
        # Find next occurrence of day_of_week at hour:minute
        target_dow = schedule.day_of_week or 0
        days_ahead = (target_dow - now.weekday()) % 7
        if days_ahead == 0:
            next_time = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
            if next_time <= now:
                days_ahead = 7
        next_time = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
        from datetime import timedelta as td
        next_time = next_time + td(days=days_ahead)
        return next_time.astimezone(timezone.utc)

    elif schedule.frequency == "monthly":
        # Day of month (1-28)
        target_day = schedule.day_of_month or 1
        if now.day < target_day:
            next_time = now.replace(day=target_day, hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
        elif now.day == target_day:
            next_time = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
            if next_time <= now:
                if now.month == 12:
                    next_time = next_time.replace(year=now.year + 1, month=1, day=target_day)
                else:
                    next_time = next_time.replace(month=now.month + 1, day=target_day)
        else:
            if now.month == 12:
                next_time = now.replace(year=now.year + 1, month=1, day=target_day,
                                        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
            else:
                next_time = now.replace(month=now.month + 1, day=target_day,
                                        hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
        return next_time.astimezone(timezone.utc)

    return now


# 定时备份全局锁 key（PG advisory lock，防止多进程/多 worker 重复执行备份）
_SCHED_LOCK_KEY = 0x41494F50534B00  # "AIOPSSK\0" 常量
# 配置备份最多并发备份设备数（同 schedule 内串行，跨实例由 advisory lock 兜底）
_BACKUP_CONCURRENCY = 8


async def run_scheduled_backups():
    """检查并执行到期的定时备份任务。

    使用 PostgreSQL advisory lock 保证多进程/多 worker 部署下
    同一时刻只有一个实例执行定时备份，避免重复备份。
    """
    from app.database import async_session

    async with async_session() as db:
        # 尝试获取锁：拿不到说明其他实例正在执行，直接跳过本轮
        locked = (
            await db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _SCHED_LOCK_KEY})
        ).scalar()
        if not locked:
            return
        try:
            now = datetime.now(timezone.utc)
            result = await db.execute(
                select(BackupSchedule).where(
                    BackupSchedule.enabled == True,
                    BackupSchedule.next_backup_at <= now,
                )
            )
            schedules = result.scalars().all()

            if not schedules:
                return

            logger.info(f"Found {len(schedules)} scheduled backups to run")

            for schedule in schedules:
                try:
                    if schedule.is_all_devices:
                        # 全部设备备份：遍历所有设备
                        dev_result = await db.execute(select(Device))
                        all_devices = dev_result.scalars().all()
                        logger.info(f"All-devices schedule: backing up {len(all_devices)} devices")
                        for dev in all_devices:
                            try:
                                backup = await perform_backup(
                                    db, dev.id, backup_type="scheduled",
                                    schedule_id=schedule.id,
                                )
                                status = "success" if backup.status == "success" else "failed"
                                logger.info(f"  Scheduled backup for {dev.name}: {status}")
                            except Exception as e:
                                logger.error(f"  Scheduled backup failed for {dev.name}: {e}")
                    else:
                        # 单设备备份
                        if schedule.device_id:
                            backup = await perform_backup(
                                db, schedule.device_id, backup_type="scheduled",
                                schedule_id=schedule.id,
                            )
                            status = "success" if backup.status == "success" else "failed"
                            logger.info(f"Scheduled backup for device {schedule.device_id}: {status}")
                except Exception as e:
                    logger.error(f"Scheduled backup failed for schedule {schedule.id}: {e}")
        finally:
            await db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _SCHED_LOCK_KEY})


def generate_diff(config1: str, config2: str) -> list[dict]:
    """生成两个配置版本的差异对比"""
    import difflib

    lines1 = config1.splitlines()
    lines2 = config2.splitlines()

    diff = list(difflib.unified_diff(
        lines1, lines2,
        fromfile="version_1",
        tofile="version_2",
        lineterm="",
    ))

    result = []
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            result.append({"type": "hunk", "content": line})
        elif line.startswith("-"):
            result.append({"type": "removed", "content": line[1:]})
        elif line.startswith("+"):
            result.append({"type": "added", "content": line[1:]})
        elif line.startswith(" "):
            result.append({"type": "context", "content": line[1:]})

    return result


async def cleanup_old_backups(days: int = 180, failed_days: int = 30) -> int:
    """清理过期配置备份记录，返回删除条数。

    - 删除 days 天（默认 180）前的所有备份记录；
    - 同时删除 failed_days 天（默认 30）前的备份失败记录（status='failed'），
      避免失败记录长期堆积，成功记录则保留更久。
    """
    from app.database import async_session

    now = datetime.now(timezone.utc)
    conditions = [ConfigBackup.created_at < now - timedelta(days=days)]
    conditions.append(
        (ConfigBackup.created_at < now - timedelta(days=failed_days))
        & (ConfigBackup.status == "failed")
    )
    async with async_session() as db:
        result = await db.execute(select(ConfigBackup).where(or_(*conditions)))
        old = result.scalars().all()
        count = len(old)
        for b in old:
            await db.delete(b)
        await db.commit()
        if count:
            logger.info(
                "Backup cleanup: removed %d records (%dd+ all / %dd+ failed)",
                count, days, failed_days,
            )
        return count
