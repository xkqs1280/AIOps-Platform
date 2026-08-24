"""H3C 设备巡检服务 - 通过 SSH 自动采集并生成巡检报告"""
import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone, timedelta

import asyncssh
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.device import Device
from app.models.inspection import InspectionTask, InspectionDeviceResult
from app.services.h3c_inspection_parser import parse_raw_file, generate_reports
from app.services.credential_service import reveal_secret

logger = logging.getLogger(__name__)

tz_8 = timezone(timedelta(hours=8))

# SSH legacy algorithm settings for old H3C Comware / Huawei VRP devices
# 必须包含老旧算法（diffie-hellman-group1-sha1 / 3des-cbc / ssh-dss / hmac-md5 等），
# 否则老设备 SSH 握手失败（ConnectionLost）。与 compliance_service 保持一致的宽松集。
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

INSPECTION_COMMANDS = [
    "screen-length disable",
    "display version",
    "display device",
    "display device manuinfo",
    "display cpu",
    "display memory",
    "display environment",
    "display fan",
    "display power",
    "display power verbose",
    "display logbuffer reverse",
    "display bfd session",
    "display mac-address mac-move",
    "display link-aggregation summary",
    "display link-aggregation verbose",
    "display transceiver interface",
    "display transceiver diagnosis interface",
    "display counters rate inbound interface",
    "display counters rate outbound interface",
    "display counters inbound interface",
    "display counters outbound interface",
    "display interface",
    "display ospf peer",
    "display bgp peer ipv4",
    "display clock",
]

# Commands that may take longer to output
LONG_OUTPUT_COMMANDS = {    "display logbuffer reverse",
    "display interface",
    "display transceiver diagnosis interface",
    "display counters inbound interface",
    "display counters outbound interface",
    "display current-configuration",
}

# ── 并行采集优化 ──
# 单台设备同时打开的 SSH 会话数（命令分片并发执行）。H3C Comware 默认 vty
# 一般为 5，取 4 既保证并发又留有余量；若设备 vty 不足，采集时会自适应降级。
PARALLEL_SESSIONS = 4
# 单个巡检任务内并发采集的设备数。实测 5 台并发（20 个连接）会挤占防火墙等
# vty 数量少的设备，导致命令输出丢失；取 2 兼顾速度与可靠性。
MAX_PARALLEL_DEVICES = 2
# 输出体积极大的命令：单独分片，避免其长输出阻塞其他命令，显著缩短单设备总耗时
HEAVY_COMMANDS = {
    "display interface",
    "display counters inbound interface",
    "display counters outbound interface",
    "display transceiver diagnosis interface",
    "display logbuffer reverse",
}


def _get_report_dir() -> str:
    """获取巡检报告存储目录"""
    base_dir = getattr(settings, "REPORTS_DIR", None) or os.path.join(os.getcwd(), "reports", "inspections")
    os.makedirs(base_dir, exist_ok=True)
    return base_dir


async def _execute_command(
    writer,
    reader,
    command: str,
    timeout: int = 30,
    command_timeout: int = 60,
    max_bytes: int = 8 * 1024 * 1024,
) -> str:
    """在已建立的 SSH 会话中执行单条命令并读取输出。

    终止策略：写完命令后立即发送一个随机哨兵串，读到该哨兵串即判定命令输出结束
    （设备会把哨兵当作未知命令回显并报错，必然出现在真实输出之后）。该方式不依赖
    输出内容，即使命令返回二进制（如光模块诊断）或末行不带提示符也能秒级返回。

    会话以字节模式运行，二进制回显用 errors='replace' 安全解码。
    """
    logger.debug(f"Sending command: {command}")
    marker = "__AIOPS_DONE_" + uuid.uuid4().hex + "__"
    writer.write(f"{command}\r\n".encode("utf-8", errors="replace"))
    writer.write(f"{marker}\r\n".encode("utf-8", errors="replace"))
    await writer.drain()

    output = ""
    end_time = asyncio.get_event_loop().time() + command_timeout

    while asyncio.get_event_loop().time() < end_time:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        except asyncio.TimeoutError:
            if output:
                break
            continue
        if not chunk:
            # 通道已被对端关闭
            break
        chunk_str = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        output += chunk_str
        if len(output) > max_bytes:
            break
        if marker in output:
            # 仅保留哨兵之前的内容（真实命令输出），丢弃哨兵回显与报错行
            output = output.split(marker, 1)[0]
            break

    return output


async def _open_session(conn) -> tuple:
    """在已建立的 SSH 连接上开一个交互会话，并关闭分页输出。

    返回 (writer, reader)。若某条命令导致通道被设备关闭，可重新调用本函数
    在原有连接上开新会话，避免整轮巡检失败。
    """
    writer, reader, _ = await conn.open_session(term_type="vt100", term_size=(200, 50), encoding=None)
    # 消费登录 banner，直到看到提示符 <hostname> 再继续——
    # 若 banner 未读完就发命令，设备会把命令吞掉（AC/防火墙等设备常见）。
    prompt_re = re.compile(rb'<[^>\r\n]+>[ \r\n]*$', re.MULTILINE)
    buf = b""
    deadline = asyncio.get_event_loop().time() + 10
    while asyncio.get_event_loop().time() < deadline:
        try:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=2)
        except asyncio.TimeoutError:
            break  # 无更多数据，视为就绪
        if not chunk:
            break
        buf += chunk
        if prompt_re.search(buf):
            break
    # 关闭分页，防止长输出等待空格
    writer.write(b"screen-length disable\r\n")
    await writer.drain()
    await asyncio.sleep(0.3)
    try:
        await asyncio.wait_for(reader.read(65536), timeout=1.5)
    except asyncio.TimeoutError:
        pass
    return writer, reader


def _split_commands_for_parallel(commands: list[str], n: int) -> list[list[str]]:
    """将巡检命令均衡分片到 n 个会话上并发执行。

    重命令（输出体积极大，如 display interface）优先打散到不同会话，
    轻命令随后轮询填充，使各会话负载尽量均衡 —— 单设备总耗时约等于
    「最慢分片耗时」，而非「所有命令串行之和」。
    """
    if n <= 1:
        return [commands]
    groups = [[] for _ in range(n)]
    heavy = [c for c in commands if c in HEAVY_COMMANDS]
    light = [c for c in commands if c not in HEAVY_COMMANDS]
    for i, c in enumerate(heavy):
        groups[i % n].append(c)
    for i, c in enumerate(light):
        groups[(len(heavy) + i) % n].append(c)
    return [g for g in groups if g]


async def collect_device_output(
    ip: str,
    username: str | None = None,
    password: str | None = None,
    protocol: str = "ssh",
    port: int | None = None,
    timeout: int = 30,
) -> str:
    """通过 SSH/Telnet 连接 H3C 设备，并发执行巡检命令并返回原始采集文本。

    优化：在单条 SSH 连接上打开多个会话（通道），将巡检命令均衡分片后
    并发执行，单设备采集耗时从「命令串行之和」降为「最慢分片耗时」。对接口
    数极多的设备（display interface / counters 输出巨大）提速尤其明显。

    会话以字节模式运行，并对「通道中途被关闭」做自愈：单个命令失败（如光模块
    诊断返回二进制导致通道异常）后，会在原连接上重开会话继续后续命令。
    """
    ssh_user = username or settings.DEFAULT_DEVICE_USERNAME
    ssh_pass = password or settings.DEFAULT_DEVICE_PASSWORD
    if not ssh_user or not ssh_pass:
        raise ValueError("设备未配置管理账号或密码")

    # Telnet 走真正的 Telnet 协议（telnetlib3），单会话串行执行巡检命令
    if protocol == "telnet":
        from app.services.telnet_client import run_command
        commands = [c for c in INSPECTION_COMMANDS if c != "screen-length disable"]
        outputs = []
        for cmd in commands:
            try:
                cmd_timeout = 120 if cmd in LONG_OUTPUT_COMMANDS else 60
                out = await run_command(
                    ip, ssh_user, ssh_pass, cmd,
                    port=port or 23, timeout=cmd_timeout,
                )
                outputs.append(out)
            except Exception as e:
                logger.warning(f"Telnet 命令 '{cmd}' 在 {ip} 失败: {e}")
                outputs.append(f"\n<error> command failed: {cmd}: {e}\n")
        return "\n".join(outputs)

    if settings.SSH_STRICT_HOST_KEY_CHECKING and not settings.SSH_KNOWN_HOSTS:
        raise ValueError("启用了 SSH 主机密钥校验，但未配置 SSH_KNOWN_HOSTS")
    ssh_port = port or 22

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
            ),
            timeout=timeout + 10,
        )

        commands = [c for c in INSPECTION_COMMANDS if c != "screen-length disable"]

        # 自适应打开并行会话：设备 vty 数有限，打开失败时降级为更少会话
        sessions = []
        for _ in range(PARALLEL_SESSIONS):
            try:
                sessions.append(await _open_session(conn))
            except Exception as e:
                logger.warning(f"设备 {ip} 并行会话打开失败，降级并发数: {e}")
                break
        if not sessions:
            raise RuntimeError(f"无法在 {ip} 上建立任何 SSH 会话")

        groups = _split_commands_for_parallel(commands, len(sessions))

        async def run_group(session, cmds):
            writer, reader = session
            outputs = []
            for cmd in cmds:
                # Some commands produce large output, give them more time
                cmd_timeout = 120 if cmd in LONG_OUTPUT_COMMANDS else 60
                try:
                    out = await _execute_command(writer, reader, cmd, timeout=30, command_timeout=cmd_timeout)
                    outputs.append(out)
                except Exception as e:
                    logger.warning(f"命令 '{cmd}' 在 {ip} 失败: {e}")
                    outputs.append(f"\n<error> command failed: {cmd}: {e}\n")
                    # 通道被关闭时，尝试在原有连接上重开会话以继续后续命令
                    err = str(e).lower()
                    if "channel not open" in err or "not open" in err or "broken pipe" in err:
                        try:
                            writer, reader = await _open_session(conn)
                        except Exception:
                            break
            return outputs

        results = await asyncio.gather(*[run_group(s, g) for s, g in zip(sessions, groups)])
        all_output = []
        for group_out in results:
            all_output.extend(group_out)

        for writer, _ in sessions:
            try:
                writer.close()
            except Exception:
                pass
        conn.close()

        return "\n".join(all_output)

    except Exception as e:
        logger.error(f"SSH 巡检采集失败 {ip}: {type(e).__name__}: {e}")
        raise
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _build_raw_file_content(device: Device, raw_output: str) -> str:
    """把 SSH 采集输出包装成 inspection.py 期望的原始文件格式。

    设备回显中通常已经包含 `<hostname>command` 形式的提示符行，
    直接返回原始输出即可供 split_sections_offsets 按提示符分割段落。
    若首行没有提示符，则在开头补一个，确保解析器能识别主机名。
    """
    stripped = raw_output.strip()
    if not stripped:
        return ""

    # 若第一行不是提示符，补一个通用提示符
    first_line = stripped.splitlines()[0].strip()
    if not (first_line.startswith("<") and (first_line.endswith(">") or first_line.endswith("#"))):
        hostname = device.name or "H3C-Device"
        stripped = f"<{hostname}>\n" + stripped

    return stripped


async def _run_single_device_inspection(
    db: AsyncSession,
    task: InspectionTask,
    device: Device,
    result_dir: str,
    dev_result: InspectionDeviceResult | None = None,
) -> InspectionDeviceResult:
    """执行单台设备的巡检并保存结果。

    传 dev_result 时更新该已有结果行（用于手动"重新执行"，避免产生重复行）；
    不传则新建一行（主流程首次巡检）。
    """
    if dev_result is None:
        dev_result = InspectionDeviceResult(
            task_id=task.id,
            device_id=device.id,
            device_name=device.name,
            device_ip=device.ip,
            status="running",
        )
        db.add(dev_result)
        await db.commit()
        await db.refresh(dev_result)
    else:
        dev_result.status = "running"
        dev_result.completed_at = None
        dev_result.error_message = None
        dev_result.raw_output = None
        dev_result.parsed_data = None
        await db.commit()

    try:
        raw_output = await collect_device_output(
            device.ip,
            username=device.mgmt_username,
            password=reveal_secret(device.mgmt_password),
            protocol=device.mgmt_protocol or "ssh",
            port=device.mgmt_port,
        )

        if not raw_output or len(raw_output) < 100:
            raise ValueError("采集输出过短，可能 SSH 会话未正常建立")

        # Build simulated raw file content and parse
        file_content = _build_raw_file_content(device, raw_output)
        device_data = parse_raw_file(
            filepath=f"{device.ip}.txt",
            content=file_content,
        )
        # Ensure identifier fields align with platform device
        device_data["Sys_ip"] = device.ip
        device_data["Sys_name"] = device.name

        dev_result.raw_output = raw_output
        dev_result.parsed_data = device_data
        dev_result.status = "success"
        dev_result.completed_at = datetime.now(timezone.utc)

    except Exception as e:
        logger.exception(f"Device inspection failed for {device.name} ({device.ip})")
        dev_result.status = "failed"
        dev_result.error_message = f"{type(e).__name__}: {str(e)[:500]}"
        dev_result.completed_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(dev_result)
    return dev_result


async def run_inspection_task(db: AsyncSession, task_id: int):
    """执行巡检任务：采集所有设备、解析并生成报告。"""
    result = await db.execute(select(InspectionTask).where(InspectionTask.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        logger.error(f"Inspection task {task_id} not found")
        return

    task.status = "running"
    task.started_at = datetime.now(timezone.utc)
    task.total_devices = len(task.device_ids)
    task.success_count = 0
    task.failed_count = 0
    await db.commit()

    result_dir = os.path.join(_get_report_dir(), f"task_{task_id}")
    os.makedirs(result_dir, exist_ok=True)

    # Load devices
    devices_result = await db.execute(select(Device).where(Device.id.in_(task.device_ids)))
    devices = devices_result.scalars().all()

    # 并发采集所有设备：每台设备使用独立 DB 会话（避免共享会话并发冲突），
    # 并以信号量限制同时采集的设备数，既提速又避免压垮设备或耗尽连接。
    semaphore = asyncio.Semaphore(MAX_PARALLEL_DEVICES)

    async def limited_inspect(device: Device):
        async with semaphore:
            from app.database import async_session
            async with async_session() as dev_db:
                return await _run_single_device_inspection(dev_db, task, device, result_dir)

    # 总超时：30 分钟。个别设备卡死（SSH 挂起/无响应）时不能让整个任务无限等待，
    # 超时后已完成部分仍按成功比例决定是否出报告。
    TASK_TIMEOUT = 30 * 60  # 30 分钟（秒）

    collector_tasks = [asyncio.create_task(limited_inspect(d)) for d in devices]
    done, pending = await asyncio.wait(
        collector_tasks, timeout=TASK_TIMEOUT, return_when=asyncio.ALL_COMPLETED,
    )

    # 超时未完成（卡死）的任务：取消并释放资源
    stuck_count = len(pending)
    if pending:
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        logger.warning(
            f"Inspection task {task_id}: {stuck_count} 台设备卡死超时，任务已截断"
        )

    all_device_data = []
    for t in done:
        try:
            res = t.result()
        except Exception:
            logger.exception("单设备巡检异常")
            task.failed_count += 1
            continue
        if res.status == "success" and res.parsed_data:
            all_device_data.append(res.parsed_data)
            task.success_count += 1
        else:
            task.failed_count += 1

    if stuck_count:
        task.failed_count += stuck_count

    success_ratio = task.success_count / task.total_devices if task.total_devices else 0

    # 报告生成判定：
    # - 无卡死：只要有成功设备即出报告（原逻辑）；
    # - 有卡死：成功设备占比需 ≥ 60% 才出报告，避免小部分设备卡死拖垮整个任务；
    #   不足 60% 则视为失败并给出明确原因。
    if all_device_data and (stuck_count == 0 or success_ratio >= 0.6):
        try:
            # openpyxl/python-docx 为同步 CPU 密集操作，放线程池避免阻塞事件循环
            xlsx_path, docx_path = await asyncio.to_thread(
                generate_reports,
                all_device_data,
                result_dir,
                prefix=f"h3c_inspection_task{task_id}",
            )
            task.excel_path = xlsx_path
            task.word_path = docx_path
            if stuck_count:
                task.error_message = (
                    f"{stuck_count} 台设备卡死超时未纳入报告；成功 {task.success_count}/{task.total_devices}（{success_ratio:.0%}）"
                )
        except Exception as e:
            logger.exception("Failed to generate inspection reports")
            task.error_message = f"报告生成失败: {type(e).__name__}: {str(e)[:500]}"
    else:
        if stuck_count:
            task.error_message = (
                f"{stuck_count} 台设备巡检卡死超时，成功比例 {success_ratio:.0%} 不足 60%，未生成报告"
            )
        elif not all_device_data:
            task.error_message = "没有设备巡检成功，无法生成报告"

    task.status = "completed" if task.success_count > 0 else "failed"
    task.completed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info(f"Inspection task {task_id} finished: {task.success_count}/{task.total_devices} success")


async def create_inspection_task(db: AsyncSession, name: str, device_ids: list[int]) -> InspectionTask:
    """创建巡检任务并在后台执行。"""
    task = InspectionTask(
        name=name,
        device_ids=device_ids,
        total_devices=len(device_ids),
        status="pending",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Fire and forget background execution
    asyncio.create_task(_run_task_with_db(task.id))
    return task


async def _run_task_with_db(task_id: int):
    """在独立的数据库会话中执行巡检任务。"""
    from app.database import async_session
    async with async_session() as db:
        try:
            await run_inspection_task(db, task_id)
        except Exception as e:
            logger.exception(f"Background inspection task {task_id} failed")
            try:
                result = await db.execute(select(InspectionTask).where(InspectionTask.id == task_id))
                task = result.scalar_one_or_none()
                if task:
                    task.status = "failed"
                    task.error_message = f"{type(e).__name__}: {str(e)[:500]}"
                    task.completed_at = datetime.now(timezone.utc)
                    await db.commit()
            except Exception:
                pass
