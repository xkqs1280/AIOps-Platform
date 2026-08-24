"""Native Windows Service host for the AIOps FastAPI application."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
LOG_DIR = PROJECT_DIR / "deploy"


class AIOpsPlatformService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AIOpsPlatform"
    _svc_display_name_ = "AIOps Platform"
    _svc_description_ = "AIOps FastAPI monitoring platform"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.process: subprocess.Popen | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.chdir(BACKEND_DIR)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            # 兼容两种布局：AIOpsServer.exe 与 AIOpsService.exe 同级（onefile），
            # 或 AIOpsService.exe 位于 AIOpsServer/ 子目录（旧 onedir 布局）。
            candidates = [exe_dir / "AIOpsServer.exe", exe_dir / "AIOpsServer" / "AIOpsServer.exe"]
            server = next((p for p in candidates if p.is_file()), candidates[0])
            command = [str(server)]
        else:
            command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
        with (LOG_DIR / "backend-service.log").open("a", encoding="utf-8") as log:
            self.process = subprocess.Popen(
                command,
                cwd=BACKEND_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(AIOpsPlatformService)
