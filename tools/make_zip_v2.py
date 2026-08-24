# 打包 aiops-v4.0.zip
import zipfile, os

src = r"D:\WorkBuddy\codex\AIOps\dist\AIOps-Windows"
out = r"D:\WorkBuddy\codex\AIOps\dist\aiops-v4.0.zip"

total_files = 0
def add_dir(z, base, dirpath):
    global total_files
    for name in os.listdir(dirpath):
        full = os.path.join(dirpath, name)
        arc = os.path.join(base, name)
        if os.path.isdir(full):
            add_dir(z, arc, full)
        else:
            z.write(full, arc)
            total_files += 1

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    add_dir(z, "AIOps-Windows", src)

size = os.path.getsize(out)
print(f"打包完成: {total_files} 个文件, {size/1024/1024:.0f} MB")

# 验证
z = zipfile.ZipFile(out)
raw = z.read("AIOps-Windows/deploy/one-click-install.ps1").decode("utf-8-sig", errors="replace")
print("ps1 含 拉起安装向导:", "拉起安装向导" in raw)
raw2 = z.read("AIOps-Windows/backend/.env").decode("utf-8")
print("DATABASE_URL:", [l for l in raw2.splitlines() if l.startswith("DATABASE_URL")][0][:60])
raw3 = z.read("AIOps-Windows/一键部署.bat")
try:
    raw3.decode("gbk")
    print("bat GBK OK")
except UnicodeDecodeError:
    print("bat NOT GBK!")
print("testzip:", "OK" if z.testzip() is None else "FAIL")
