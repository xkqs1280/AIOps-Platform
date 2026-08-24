# -*- mode: python ; coding: utf-8 -*-
"""AIOpsService.spec — Windows 服务宿主（onefile，无控制台窗口）。

服务宿主负责以 Windows 服务方式拉起/停止 AIOpsServer.exe，
自身不包含 FastAPI 应用逻辑，仅依赖 pywin32。

用法（由 build_windows_exe.ps1 调用）：
    python -m PyInstaller deploy/AIOpsService.spec --distpath dist/AIOps-Windows --workpath build/pyinstaller
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))
_BACKEND_DIR = os.path.join(_ROOT, "backend")
# 平台 logo 图标（tools/make_icon.py 生成）
_ICON = os.path.join(_ROOT, "build", "logo.ico")

hiddenimports = [
    "win32timezone",
]

a = Analysis(
    [os.path.join(_BACKEND_DIR, "aiops_windows_service.py")],
    pathex=[_BACKEND_DIR],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AIOpsService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)
