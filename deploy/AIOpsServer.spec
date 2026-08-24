# -*- mode: python ; coding: utf-8 -*-
"""AIOpsServer.spec — FastAPI 后端可执行文件（默认 onefile，前端外置）。

前端 dist 默认不打包进 exe（由 build_windows_exe.ps1 复制到
AIOps-Windows/frontend/dist），便于前端独立升级、无需重新打 exe。
如需单文件内嵌前端，构建时设置环境变量 AIOPS_EMBED_FRONTEND=1。

用法（由 build_windows_exe.ps1 调用，勿直接命令行运行）：
    python -m PyInstaller deploy/AIOpsServer.spec --distpath dist/AIOps-Windows --workpath build/pyinstaller
"""
import os

from PyInstaller.utils.hooks import collect_submodules

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))
_BACKEND_DIR = os.path.join(_ROOT, "backend")
# 平台 logo 图标（tools/make_icon.py 生成）
_ICON = os.path.join(_ROOT, "build", "logo.ico")

# 让 collect_submodules 能找到 app 包（app 位于 backend/ 下）
import sys as _sys
_sys.path.insert(0, _BACKEND_DIR)

# 收集 app 包全部子模块（FastAPI 应用本身），确保 PyInstaller 一并打包
hiddenimports = collect_submodules("app")
# uvicorn 动态加载子模块，必须显式收集，否则运行时报 module not found
hiddenimports += collect_submodules("uvicorn")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    # SQLAlchemy async + PostgreSQL
    "sqlalchemy.dialects.postgresql.psycopg_async",
    "psycopg",
    "psycopg_binary",
    # passlib 动态注册的 bcrypt handler
    "passlib.handlers.bcrypt",
    # python-jose 的 cryptography 后端
    "jose.backends.cryptography_backend",
    # pywin32（Windows 下被间接引用；缺失会报 No module named 'win32timezone'）
    "win32timezone",
    "win32api",
    "win32file",
    "win32event",
    "win32service",
    "win32serviceutil",
    # Telnet 客户端（telnetlib3，其插件按 entry point 动态加载，需显式收集）
    "telnetlib3",
    "telnetlib3.client",
    "telnetlib3.server",
]

# 前端产物：默认外置；设置 AIOPS_EMBED_FRONTEND=1 时内嵌
datas = []
if os.environ.get("AIOPS_EMBED_FRONTEND") == "1":
    datas.append((os.path.join(_ROOT, "frontend", "dist"), "frontend/dist"))

a = Analysis(
    [os.path.join(_BACKEND_DIR, "aiops_entry.py")],
    pathex=[_BACKEND_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# 构建模式由环境变量控制：AIOPS_BUILD_MODE=onedir 时生成带 _internal 的目录版
if os.environ.get("AIOPS_BUILD_MODE") == "onedir":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="AIOpsServer",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="AIOpsServer",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="AIOpsServer",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=_ICON,
    )
