"""PyInstaller entry point for the standalone AIOps server executable.

注意：必须直接 import app.main 并传入应用对象（而非 "app.main:app" 字符串），
否则 PyInstaller 静态分析看不到该引用，打包产物会缺失 app 包。

HTTPS：
  - 部署包自带 backend/certs/server.crt + server.key（自签名，内置信任到移动 APP）；
  - 检测到证书即启用 HTTPS（同端口 8000），未检测到则回退 HTTP；
  - 可用环境变量 AIOPS_SSL_CERTFILE / AIOPS_SSL_KEYFILE 显式覆盖证书路径。

启动日志（2026-10-30）：
  Windows 上后端是**隐藏窗口**拉起的（wscript / Windows 服务 / 计划任务），
  stdout 直接被丢弃。一旦新版本在启动期抛异常，进程"起了十几秒就没了"，
  现场一个字都查不到。所以这里在 import app 之前就把 stdout/stderr 接到
  ``backend\\logs\\startup.log``；import 期异常也会留下 traceback。
  ⚠️ 必须排在所有 app.* import 之**前**，否则 import 失败时日志还没生效。
"""
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def _app_dir() -> Path:
    """部署根目录：frozen 时是 exe 所在目录，源码运行时是仓库根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


class _Tee:
    """把写入同时送到日志文件和原始控制台。

    现场有两种启动方式：隐藏窗口（wscript / 服务 / 计划任务，没有控制台）和
    手工双击 start.bat（有控制台）。隐藏启动时唯一证据是文件；手工启动时运维
    习惯直接看屏幕。两者都要保留，所以这里做 tee，写失败一律吞掉。
    """

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data) if data else 0

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        for s in self._streams:
            try:
                if s.isatty():
                    return True
            except Exception:
                pass
        return False


def _redirect_output_to_log() -> None:
    """把 stdout/stderr 落盘到 backend/logs/startup.log（超过 5MB 轮转一份 .1）。

    落盘失败绝不能阻止平台启动 —— 全部异常吞掉。
    """
    try:
        log_dir = _app_dir() / "backend" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "startup.log"
        try:
            if log_path.exists() and log_path.stat().st_size > 5 * 1024 * 1024:
                log_path.replace(log_dir / "startup.log.1")
        except OSError:
            pass
        stream = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        stream.write(
            "\n===== AIOpsServer start {ts} pid={pid} =====\n".format(
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pid=os.getpid(),
            )
        )
        stream.flush()
        sys.stdout = _Tee(stream, sys.__stdout__)
        sys.stderr = _Tee(stream, sys.__stderr__)
        print("[boot] startup log: {0}".format(log_path))
    except Exception:
        pass


_redirect_output_to_log()

try:
    import uvicorn

    from app.config import BACKEND_DIR
    from app.main import app
except BaseException:
    # import 期失败（缺模块 / 配置解析失败 / 模型登记异常）：留下 traceback 再退出。
    traceback.print_exc()
    print("[fatal] import stage failed, process will exit", file=sys.stderr)
    raise


def _resolve_ssl():
    """返回 (certfile, keyfile)；无证书时返回 (None, None)。"""
    cert = os.getenv("AIOPS_SSL_CERTFILE", "").strip()
    key = os.getenv("AIOPS_SSL_KEYFILE", "").strip()
    if cert and key:
        return cert, key
    certs_dir = BACKEND_DIR / "certs"
    certfile = certs_dir / "server.crt"
    keyfile = certs_dir / "server.key"
    if certfile.is_file() and keyfile.is_file():
        return str(certfile), str(keyfile)
    return None, None


if __name__ == "__main__":
    try:
        from app.version import APP_VERSION

        print("[boot] AIOps {0} | app_dir={1}".format(APP_VERSION, _app_dir()))

        certfile, keyfile = _resolve_ssl()
        kwargs = {}
        if certfile and keyfile:
            kwargs["ssl_certfile"] = certfile
            kwargs["ssl_keyfile"] = keyfile
            print("[boot] HTTPS enabled: {0}".format(certfile))
        else:
            print("[boot] HTTPS disabled (no certs found), falling back to HTTP")

        uvicorn.run(
            app,
            host=os.getenv("AIOPS_HOST", "0.0.0.0"),
            port=int(os.getenv("AIOPS_PORT", "8000")),
            log_level=os.getenv("AIOPS_LOG_LEVEL", "info"),
            **kwargs,
        )
    except BaseException:
        # lifespan 启动失败（迁移报错 / 端口占用 / 证书错误…）：uvicorn 会抛出，
        # 这里再兜一次，确保 traceback 一定落盘。
        traceback.print_exc()
        print("[fatal] server exited with error", file=sys.stderr)
        raise
