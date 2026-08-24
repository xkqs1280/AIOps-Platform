#!/usr/bin/env python3
"""AIOps 一键升级包制作工具（厂商侧，复用授权私钥签名）

用法：
  python build_upgrade_package.py --version 4.1.0 --changelog "新增一键升级模块"
  python build_upgrade_package.py --version 4.1.0 --source "D:\\WorkBuddy\\codex\\AIOps\\dist\\AIOps-Windows" --output aiops-upgrade-v4.1.0.zip

产物：zip，含
  manifest.json      版本/构建时间/变更说明/最低兼容版本/signature（RSA-SHA256）
  AIOpsServer.exe    新后端（可选，缺失时仅升级前端）
  AIOpsService.exe   服务宿主（可选）
  frontend/dist/*    新前端

校验逻辑与平台 upgrade_service.verify_signature 完全一致：
  签名 = RSA-SHA256(规范化 JSON)  base64url（剔除 signature 字段、sort_keys、紧凑）
"""
import argparse
import base64
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KEY_DIR = Path(__file__).resolve().parent / "vendor_keys"
DEFAULT_SOURCE = Path(__file__).resolve().parent.parent / "dist" / "AIOps-Windows"


def load_private_key(key_dir: Path):
    priv = key_dir / "private_key.pem"
    if not priv.exists():
        sys.exit(f"[X] 缺少私钥: {priv}\n    先运行 tools/generate_license.py 生成密钥对（--show-fingerprint 即可触发）。")
    return serialization.load_pem_private_key(priv.read_bytes(), password=None)


def normalize_manifest(manifest: dict) -> bytes:
    """与平台端一致的规范化。"""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_signature(manifest: dict, priv_key) -> str:
    sig = priv_key.sign(normalize_manifest(manifest), padding.PKCS1v15(), hashes.SHA256())
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def main():
    ap = argparse.ArgumentParser(description="AIOps 一键升级包制作工具")
    ap.add_argument("--version", required=True, help="升级包版本（如 4.1.0，必须高于当前运行版本）")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="部署目录（含 AIOpsServer.exe / frontend/dist）")
    ap.add_argument("--output", default="", help="输出 zip 路径（默认 aiops-upgrade-<version>.zip）")
    ap.add_argument("--build-time", default="", help="构建时间（默认当前时间）")
    ap.add_argument("--changelog", default="", help="变更说明")
    ap.add_argument("--min-supported", default="3.6.0", help="最低兼容的旧版本（低于此版本的旧平台禁止直接升级）")
    ap.add_argument("--backend-src", default="", help="后端源码目录（Linux 源码部署升级用，如 D:\\WorkBuddy\\codex\\AIOps\\backend\\app）；提供后打进升级包 backend/app")
    ap.add_argument("--key-dir", default=str(KEY_DIR), help="密钥目录")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        sys.exit(f"[X] 部署目录不存在: {source}")

    exe = source / "AIOpsServer.exe"
    service = source / "AIOpsService.exe"
    frontend = source / "frontend" / "dist"
    if not exe.exists() and not frontend.is_dir():
        sys.exit("[X] 部署目录中既没有 AIOpsServer.exe 也没有 frontend/dist，无法制作升级包")

    build_time = args.build_time or datetime.now().strftime("%Y-%m-%d %H:%M")
    manifest = {
        "version": args.version,
        "build_time": build_time,
        "changelog": args.changelog,
        "min_supported_version": args.min_supported,
    }

    priv_key = load_private_key(Path(args.key_dir))
    manifest["signature"] = make_signature(manifest, priv_key)
    print(f"[+] manifest 签名完成: version={args.version}")

    output = Path(args.output) if args.output else Path.cwd() / f"aiops-upgrade-v{args.version}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)

    def _add_dir(zf: zipfile.ZipFile, base: Path, arc_prefix: str):
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.name.lower() != "desktop.ini":
                zf.write(p, f"{arc_prefix}/{p.relative_to(base).as_posix()}")

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        if exe.exists():
            zf.write(exe, "AIOpsServer.exe")
            print(f"[+] 包含 AIOpsServer.exe ({exe.stat().st_size // 1024} KB)")
        if service.exists():
            zf.write(service, "AIOpsService.exe")
            print(f"[+] 包含 AIOpsService.exe ({service.stat().st_size // 1024} KB)")
        if frontend.is_dir():
            _add_dir(zf, frontend, "frontend/dist")
            print(f"[+] 包含 frontend/dist")
        # 升级执行脚本（Windows ps1 / Linux sh），随版本演进，升级时优先使用 staging 内脚本
        deploy_dir = source / "deploy"
        for deploy_name in ("upgrade_apply.ps1", "upgrade_apply.sh"):
            deploy_file = deploy_dir / deploy_name
            if deploy_file.is_file():
                zf.write(deploy_file, f"deploy/{deploy_name}")
                print(f"[+] 包含 deploy/{deploy_name}")
        # Linux 源码部署升级：backend/app 源码（可选）
        if args.backend_src:
            backend_src = Path(args.backend_src)
            if backend_src.is_dir():
                _add_dir(zf, backend_src, "backend/app")
                print(f"[+] 包含 backend/app 源码（Linux 源码部署用）")
            else:
                print(f"[!] --backend-src 目录不存在，跳过: {backend_src}")

    print(f"[OK] 升级包已生成: {output} ({output.stat().st_size // 1024} KB)")
    print("      上传到平台「系统设置 → 系统升级」即可一键升级。")


if __name__ == "__main__":
    main()
