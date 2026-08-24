# 备份本地源码 -> dist/AIOps-v2.5-bak.zip（排除依赖/构建产物/缓存）
import os
import zipfile
import time

SRC = r"D:\WorkBuddy\codex\AIOps"
OUT = r"D:\WorkBuddy\codex\AIOps\dist\AIOps-v2.5-bak.zip"

EXCLUDE_DIRS = {
    "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".git", "reports", "uvicorn.log", "backups",
    "dist_tmp",
}
EXCLUDE_EXTS = {".pyc", ".log", ".zip"}

count = 0
total_size = 0


def add_tree(z, base_dir, arc_root):
    global count, total_size
    for name in os.listdir(base_dir):
        full = os.path.join(base_dir, name)
        rel = os.path.join(arc_root, name)
        if os.path.isdir(full):
            if name in EXCLUDE_DIRS or name.startswith("dist_old"):
                continue
            add_tree(z, full, rel)
        else:
            if name.endswith(tuple(EXCLUDE_EXTS)):
                continue
            z.write(full, rel)
            count += 1
            total_size += os.path.getsize(full)


os.makedirs(os.path.dirname(OUT), exist_ok=True)
t0 = time.time()
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root in ("backend", "frontend", "deploy", "tools", ".workbuddy"):
        add_tree(z, os.path.join(SRC, root), root)
    for f in os.listdir(SRC):
        full = os.path.join(SRC, f)
        if os.path.isfile(full) and not f.endswith(tuple(EXCLUDE_EXTS)):
            if f.startswith(("dist", "build")):
                continue
            z.write(full, f)
            count += 1
            total_size += os.path.getsize(full)

print(f"备份完成: {count} 文件, {total_size/1024/1024:.1f} MB, 用时 {time.time()-t0:.1f}s")
print("输出:", OUT)
