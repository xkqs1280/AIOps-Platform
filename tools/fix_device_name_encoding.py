# -*- coding: utf-8 -*-
"""修复生产库 devices.name 中误存为 Hex-STRING 的中文设备名称。

背景：SNMP 采集 H3C/华为设备的 sysName 时，若 sysName 为 GBK 编码中文
（如"分水集团7602"），旧版 `_value_to_str` 只识别可打印 ASCII，导致中文
被当作二进制转成 `B7 D6 ...` 形式的十六进制字符串存入数据库。

本脚本扫描 devices 表，把 name 满足"纯 Hex"格式且能解码为中文的值还原
为可读中文。解码判定与 `discovery_service._try_decode_cn` 完全一致。

用法：
  python fix_device_name_encoding.py            # 使用 .env 的 DATABASE_URL
  python fix_device_name_encoding.py --url postgresql://user:pwd@host:5432/db
  python fix_device_name_encoding.py --dry-run  # 只打印不改库

连接库：psycopg（同步版）。若生产 venv 无 psycopg 可先 pip install psycopg。
"""
import argparse
import os
import re
import sys

# --- 解码判定（与 discovery_service._try_decode_cn 保持一致，避免引入 app 依赖） ---
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_CTRL_CHARS = "".join(chr(c) for c in range(32) if c not in (9, 10, 13))
_HEX_STRING_RE = re.compile(r"^(?:[0-9A-Fa-f]{2})(?:[ ][0-9A-Fa-f]{2})*$")


def try_decode_cn(raw: bytes):
    """尝试把字节流解码为中文文本；失败返回 None。"""
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if not _CJK_RE.search(text):
            continue
        if any(c in _CTRL_CHARS for c in text):
            continue
        return text
    return None


def parse_hex_string(s: str) -> bytes | None:
    """把 `B7 D6 CB AE` 还原为字节；格式不符返回 None。"""
    s = s.strip()
    if not _HEX_STRING_RE.match(s):
        return None
    try:
        return bytes(int(x, 16) for x in s.split())
    except ValueError:
        return None


def load_env_url():
    """从 backend/.env 读取 DATABASE_URL（兼容 KEY=VALUE 行）。"""
    for p in (os.path.join(os.path.dirname(__file__), "..", "backend", ".env"),
              os.path.join(os.path.dirname(__file__), ".env")):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip()
    return None


def main():
    ap = argparse.ArgumentParser(description="修复设备名称 Hex-STRING 乱码")
    ap.add_argument("--url", help="数据库连接串（默认读 backend/.env 的 DATABASE_URL）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不改库")
    args = ap.parse_args()

    url = args.url or load_env_url()
    if not url:
        print("未找到数据库连接串：请用 --url 传入或配置 backend/.env 的 DATABASE_URL")
        sys.exit(1)
    # psycopg 3 兼容：把 psycopg_async/psycopg2 驱动名换成 psycopg
    url = re.sub(r"postgresql\+psycopg_async", "postgresql+psycopg", url)
    url = re.sub(r"postgresql\+psycopg2", "postgresql+psycopg", url)

    try:
        import psycopg
    except ImportError:
        print("缺少 psycopg：请先 `pip install psycopg`")
        sys.exit(1)

    conn = psycopg.connect(url, connect_timeout=10)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM devices ORDER BY id")
    rows = cur.fetchall()

    fixed, skipped = [], 0
    for did, name in rows:
        if not name or not isinstance(name, str):
            continue
        raw = parse_hex_string(name)
        if raw is None:
            continue
        decoded = try_decode_cn(raw)
        if decoded is None:
            skipped += 1
            continue
        fixed.append((did, name, decoded))

    print(f"扫描 {len(rows)} 台设备，可修复 {len(fixed)} 台，跳过(无法解码) {skipped} 台\n")
    for did, old, new in fixed:
        print(f"  [id={did}] {old!r}  =>  {new!r}")

    if not args.dry_run and fixed:
        cur.executemany("UPDATE devices SET name = %s WHERE id = %s",
                        [(new, did) for did, _, new in fixed])
        print(f"\n已更新 {len(fixed)} 台设备名称。")
    elif args.dry_run:
        print(f"\n(--dry-run 未写库) 可更新 {len(fixed)} 台。")
    conn.close()


if __name__ == "__main__":
    main()
