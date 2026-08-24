# -*- coding: utf-8 -*-
"""备份 AIOps 完整源码到 D:\\WorkBuddy\\codex\\AIOps-BAK（排除依赖/构建产物/缓存）。"""
import os
import shutil
import time

SRC = r"D:\WorkBuddy\codex\AIOps"
DST = r"D:\WorkBuddy\codex\AIOps-BAK"

EXCLUDE_DIRS = {
    "node_modules", "dist", "build", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".git", "dist_act3", "dist_act4", "dist_act6",
    "_pybuild", "_pybuild2", ".workbuddy", "reports", "backups",
}
EXCLUDE_EXTS = {".pyc", ".log"}
EXCLUDE_FILES = {"AIOps.zip"}


def _ignore(src_dir, names):
    skipped = []
    for n in names:
        full = os.path.join(src_dir, n)
        if os.path.isdir(full):
            if n in EXCLUDE_DIRS or n.startswith("_backup_"):
                skipped.append(n)
        else:
            if os.path.splitext(n)[1] in EXCLUDE_EXTS or n in EXCLUDE_FILES:
                skipped.append(n)
    return skipped


def main():
    assert os.path.isdir(SRC), f"源目录不存在: {SRC}"
    os.makedirs(DST, exist_ok=True)
    t0 = time.time()
    # 逐项复制：先清空 DST 下同名的旧目录内容由 copytree 覆盖
    copied = shutil.copytree(SRC, DST, ignore=_ignore, dirs_exist_ok=True,
                             copy_function=shutil.copy2)

    n_files = n_dirs = 0
    for root, dirs, files in os.walk(DST):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        n_dirs += len(dirs)
        n_files += len(files)
    size = sum(
        os.path.getsize(os.path.join(r, f))
        for r, _, fs in os.walk(DST) for f in fs
    )
    print(f"备份完成: {n_files} 文件 / {n_dirs} 子目录, {size/1024/1024:.1f} MB, 用时 {time.time()-t0:.1f}s")
    print(f"目标: {DST}")


if __name__ == "__main__":
    main()
