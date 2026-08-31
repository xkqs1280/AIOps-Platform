"""平台版本信息（集中管理，升级模块与前端共用）。

升级包 manifest.json 中的 version 必须与此一致（或更高）才能执行升级。
构建 Windows 生产包时由 deploy/build_windows_exe.ps1 同步注入 BUILD_TIME。
"""

APP_NAME = "AIOps 智能运维托管平台"
APP_VERSION = "4.3.0"
APP_BUILD_TIME = "2026-08-31"

# 版本号三段比较工具：用于升级包版本校验（如 "4.0.0" > "3.6.0"）
def parse_version(v: str) -> tuple:
    parts = []
    for seg in str(v).strip().split("."):
        num = ""
        for ch in seg:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def version_ge(current: str, target: str) -> bool:
    """current >= target"""
    return parse_version(current) >= parse_version(target)


def version_gt(current: str, target: str) -> bool:
    """current > target"""
    return parse_version(current) > parse_version(target)
