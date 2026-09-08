"""后台循环监督器（P1-8）。

背景：平台各类后台循环（健康检测/指标采集/威胁情报/业务监控/备份调度/每日清理/
巡检后台）一旦因未捕获异常或意外 return 退出，任务会永久消失且无人重启
（asyncio.create_task 崩溃后仅留一条 traceback，不会自愈）。

本模块提供：
- `supervise()`：把任意循环协程工厂包成"永不退出"的监督循环——循环体抛异常或
  意外结束都会记录日志并延迟重启（restart_delay 秒）；
- 心跳存活标记：监督期间按 heartbeat_interval 周期刷新 `_heartbeats[name]`，
  供观测/健康检查确认各循环仍活着（而非静默卡死/退出）。

用法：
    task = asyncio.create_task(supervise("health-check", health_check_loop))
"""
import asyncio
import logging
from time import monotonic

logger = logging.getLogger(__name__)

# name -> 最近一次心跳的 monotonic 时间戳（存活标记）
_heartbeats: dict[str, float] = {}
# name -> asyncio.Task（供外部查询运行状态）
_supervisors: dict[str, asyncio.Task] = {}


def heartbeats() -> dict[str, float]:
    """返回各循环最近心跳（monotonic 秒）。"""
    return dict(_heartbeats)


def supervisor_status() -> dict[str, dict]:
    """返回各监督循环运行状态快照（供观测）。"""
    out: dict[str, dict] = {}
    now = monotonic()
    for name, task in _supervisors.items():
        out[name] = {
            "running": not task.done(),
            "done": task.done(),
            "cancelled": task.cancelled(),
            "last_heartbeat_age": round(now - _heartbeats.get(name, 0.0), 1)
            if name in _heartbeats else None,
        }
    return out


async def supervise(
    name: str,
    factory,
    restart_delay: float = 15.0,
    heartbeat_interval: float = 5.0,
) -> None:
    """监督一个无限循环协程工厂，异常/退出后延迟重启。

    Args:
        name: 循环名（心跳键，须唯一）。
        factory: async callable，每次调用返回一个协程；正常应无限循环，
                 返回或抛异常均视为异常终止 → 记录日志并延迟重启。
        restart_delay: 异常退出后的重启延迟（秒）。
        heartbeat_interval: 心跳刷新间隔（秒）；确认循环仍活着。
    """
    while True:
        loop_task = asyncio.create_task(factory())
        try:
            # 自我注册（直接 create_task(supervise(...)) 时也可被观测到）
            _supervisors[name] = asyncio.current_task()
            # 循环体运行期间周期刷新心跳；若子任务结束则跳出处理
            while not loop_task.done():
                _heartbeats[name] = monotonic()
                done, _ = await asyncio.wait(
                    {loop_task}, timeout=heartbeat_interval
                )
                if done:
                    break
            # 子任务异常结束：取回异常以便记录（不会吞掉原始 traceback）
            if not loop_task.cancelled():
                exc = loop_task.exception()
                if exc is not None:
                    logger.error(
                        "后台循环 %s 崩溃：%r，%.0fs 后重启",
                        name, exc, restart_delay,
                        exc_info=(type(exc), exc, exc.__traceback__),
                    )
                else:
                    logger.error(
                        "后台循环 %s 意外退出（return），%.0fs 后重启",
                        name, restart_delay,
                    )
        except asyncio.CancelledError:
            # 进程停机：取消循环体并把取消向上传播
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):
                pass
            _heartbeats.pop(name, None)
            _supervisors.pop(name, None)
            raise
        # 延迟重启
        try:
            await asyncio.sleep(restart_delay)
        except asyncio.CancelledError:
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):
                pass
            _heartbeats.pop(name, None)
            _supervisors.pop(name, None)
            raise


def start_supervised(name: str, factory, restart_delay: float = 15.0) -> asyncio.Task:
    """启动一个受监督的后台循环并保存强引用，防止 GC 回收。"""
    task = asyncio.create_task(supervise(name, factory, restart_delay=restart_delay))
    _supervisors[name] = task
    task.add_done_callback(lambda _t, _n=name: _supervisors.pop(_n, None))
    return task


def loop_beat(name: str) -> None:
    """供循环体内部周期性调用，刷新存活心跳。"""
    _heartbeats[name] = monotonic()
