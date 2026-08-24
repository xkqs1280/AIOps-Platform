#!/usr/bin/env python3
"""厂商授权激活码生成工具（仅厂商持有，不随平台发布）

用法:
  python generate_license.py --fingerprint <机器码> --version trial [--days 90]
  python generate_license.py --fingerprint <机器码> --version full
  python generate_license.py --version trial --expires 2027-01-01   # 指定到期日（测试用）
  python generate_license.py --show-fingerprint                     # 显示本机指纹
  python generate_license.py --key-dir <目录>                       # 指定密钥目录

首次运行自动生成 RSA-2048 密钥对（private_key.pem / public_key.pem）到 key-dir。
public_key.pem 内容需内嵌到平台 backend/app/services/license_service.py 的 PUBLIC_KEY_PEM。
私钥务必妥善保管，泄露即意味着可伪造授权。
"""
import argparse
import base64
import hashlib
import json
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

KEY_DIR = Path(__file__).resolve().parent / "vendor_keys"


def get_fingerprint() -> str:
    """计算本机机器指纹（与平台 license_service 算法一致）"""
    parts: list[str] = []
    try:
        with open("/etc/machine-id", encoding="utf-8") as f:
            parts.append(f.read().strip())
    except Exception:
        pass
    parts.append(str(uuid.getnode()))
    if sys.platform == "win32":
        import subprocess
        for args in (
            ["wmic", "cpu", "get", "ProcessorId"],
            ["wmic", "diskdrive", "get", "SerialNumber"],
        ):
            try:
                r = subprocess.run(args, capture_output=True, timeout=10)
                for line in r.stdout.decode("gbk", errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.lower().startswith(("processorid", "serialnumber")):
                        parts.append(line)
                        break
            except Exception:
                pass
    return hashlib.sha256("|".join(p for p in parts if p).encode()).hexdigest()[:16].upper()


def load_or_create_keys(key_dir: Path):
    key_dir.mkdir(parents=True, exist_ok=True)
    priv = key_dir / "private_key.pem"
    pub = key_dir / "public_key.pem"
    if priv.exists() and pub.exists():
        return priv.read_bytes(), pub.read_bytes()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub_bytes = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv.write_bytes(priv_bytes)
    pub.write_bytes(pub_bytes)
    print(f"[+] 已生成新密钥对:\n    {priv}\n    {pub}\n")
    return priv_bytes, pub_bytes


def make_code(payload: dict, priv_key) -> str:
    data = json.dumps(payload, separators=(",", ":")).encode()
    sig = priv_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return f"{b64(data)}.{b64(sig)}"


def main():
    ap = argparse.ArgumentParser(description="AIOps 授权激活码生成工具")
    ap.add_argument("--fingerprint", help="目标机器指纹（平台授权页可查）")
    ap.add_argument("--version", choices=["trial", "full"], help="trial=测试版(默认90天) full=全功能版(永久)")
    ap.add_argument("--days", type=int, default=90, help="测试版有效天数（默认90）")
    ap.add_argument("--expires", help="直接指定到期日 YYYY-MM-DD（覆盖 --days，测试锁定用）")
    ap.add_argument("--show-fingerprint", action="store_true", help="显示本机指纹")
    ap.add_argument("--key-dir", default=str(KEY_DIR), help="密钥目录")
    args = ap.parse_args()

    key_dir = Path(args.key_dir)
    priv_bytes, pub_bytes = load_or_create_keys(key_dir)
    priv_key = serialization.load_pem_private_key(priv_bytes, password=None)

    if args.show_fingerprint:
        print(f"本机指纹: {get_fingerprint()}")
        print("提示：生成授权码时用 --fingerprint <该值> 绑定本机。")
        return

    if not args.fingerprint:
        ap.error("缺少 --fingerprint（客户机器指纹）。可用 --show-fingerprint 查看本机指纹。")
    if not args.version:
        ap.error("缺少 --version（trial/full）。")

    if args.expires:
        expires = args.expires
    elif args.version == "full":
        expires = ""
    else:
        expires = (date.today() + timedelta(days=args.days)).isoformat()

    payload = {
        "ver": args.version,
        "ed": expires,
        "fp": args.fingerprint.upper(),
        "sn": int(date.today().strftime("%Y%m%d") + str(uuid.uuid4().int % 10000)),
    }
    code = make_code(payload, priv_key)

    print("=" * 64)
    print(f"  授权类型 : {args.version} ({'永久' if args.version == 'full' else expires + ' 到期'})")
    print(f"  绑定指纹 : {payload['fp']}")
    print(f"  序列号   : {payload['sn']}")
    print("=" * 64)
    print("\n激活码（完整复制，含中间的 .）：\n")
    print(code)
    print()
    print("提示：把上面这串激活码发给客户，在平台「授权管理」页粘贴激活。")
    print("公钥（内嵌平台用）：")
    print(pub_bytes.decode().strip())


if __name__ == "__main__":
    main()
