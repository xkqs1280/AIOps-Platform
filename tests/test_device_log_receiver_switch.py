# -*- coding: utf-8 -*-
"""设备日志接收开关（页面可控）回归测试。

背景：接收器原先只有 ``.env`` 的 ``SYSLOG_UDP_ENABLED`` 总闸，改它必须重启进程。
运维需要「不重启就停收 / 开收」（典型场景：临时把 514 端口让给第三方程序），
因此加了持久化到 ``device_log_settings`` 的运行时开关。

这里锁定几条契约，防止以后被改回去：
  1. 开关状态以 ``receiver_enabled()`` 为准，且与 ``.env`` 总闸取「与」；
  2. 唤醒事件的 set 状态**必须恒等于**开关状态 —— 否则关闭态下 ``await ev.wait()``
     会立即返回，接收循环变成不绑端口也不休眠的**空转活锁**（CPU 打满且日志不进来）；
  3. 关闭时必须清掉上次的绑定失败信息，否则页面会「已关闭」与「绑定失败」同时显示；
  4. 关闭要真的**释放端口**，重新开启要能**不重启就绑回来**。
"""
import asyncio

from app.config import settings
from app.services import syslog_receiver


def test_default_enabled_follows_env():
    """未做任何切换时，开关跟随 .env 总闸（升级上来的老部署行为不变）。"""
    syslog_receiver.reset_state()
    assert syslog_receiver.receiver_enabled() is settings.SYSLOG_UDP_ENABLED
    assert syslog_receiver.receiver_status()["enabled"] is settings.SYSLOG_UDP_ENABLED


def test_toggle_updates_status_and_event():
    """切换开关要同时改状态与唤醒事件，且二者始终一致。"""
    syslog_receiver.reset_state()
    ev = syslog_receiver._event()
    assert ev.is_set() is True

    assert syslog_receiver.set_receiver_enabled(False) is False
    assert syslog_receiver.receiver_enabled() is False
    assert syslog_receiver.receiver_status()["enabled"] is False
    assert ev.is_set() is False, "关闭后事件必须 clear，否则等待循环会空转"

    assert syslog_receiver.set_receiver_enabled(True) is True
    assert syslog_receiver.receiver_status()["enabled"] is True
    assert ev.is_set() is True


def test_disabling_clears_stale_bind_error():
    """关闭后不残留上次的绑定失败：界面上「已关闭」和「绑定失败」不能同时出现。"""
    syslog_receiver.reset_state()
    syslog_receiver._stats["bind_error"] = "OSError: [WinError 10048] 端口被占用"
    syslog_receiver._stats["bind_hints"] = ["旧提示"]
    syslog_receiver._stats["bind_occupier"] = ["PID 4321 (kiwi.exe)"]

    syslog_receiver.set_receiver_enabled(False)

    st = syslog_receiver.receiver_status()
    assert st["bind_error"] is None
    assert st["bind_hints"] is None
    assert st["bind_occupier"] is None


def test_master_switch_dominates(monkeypatch):
    """总闸（.env）关掉时，页面开关无法把它打开 —— 取「与」而非「或」。"""
    monkeypatch.setattr(settings, "SYSLOG_UDP_ENABLED", False)
    syslog_receiver.reset_state()
    assert syslog_receiver.receiver_enabled() is False

    # 强行开启也不生效（页面开关处于禁用态，后端同样要挡住）
    assert syslog_receiver.set_receiver_enabled(True) is False
    assert syslog_receiver.receiver_status()["enabled"] is False
    assert syslog_receiver.receiver_status()["configured"] is False
    assert syslog_receiver._event().is_set() is False


async def test_loop_releases_port_on_disable_and_rebinds_on_enable(monkeypatch):
    """真跑接收循环：关闭真的解绑端口，重新开启不需重启就能绑回来。

    端口用 0（内核随机分配空闲端口）而不是 514：测试机通常不允许普通用户
    绑定特权端口，用 0 才能稳定验证「绑上 / 解绑」本身。
    """
    monkeypatch.setattr(settings, "SYSLOG_UDP_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "SYSLOG_UDP_PORT", 0)
    syslog_receiver.reset_state()

    task = asyncio.create_task(syslog_receiver.syslog_udp_loop())
    try:
        await asyncio.sleep(0.2)
        assert syslog_receiver.receiver_status()["bound"] is True, "开启时应已绑定端口"

        # 关闭 → 应在 _flush_loop 的一轮（0.25s）内解绑
        syslog_receiver.set_receiver_enabled(False)
        await asyncio.sleep(0.6)
        assert syslog_receiver.receiver_status()["bound"] is False, "关闭后必须释放端口"
        assert not task.done(), "关闭是挂起等待，不是结束循环（否则监督器会反复重启）"

        # 重新开启 → 事件唤醒并重新绑定
        syslog_receiver.set_receiver_enabled(True)
        await asyncio.sleep(0.3)
        assert syslog_receiver.receiver_status()["bound"] is True, "开启后应重新绑定"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
