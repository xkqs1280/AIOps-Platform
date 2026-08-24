# 打包 aiops.zip 并验证
import zipfile, os, shutil

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

# 验证关键内容
z = zipfile.ZipFile(out)
raw = z.read("AIOps-Windows/deploy/one-click-install.ps1").decode("utf-8-sig", errors="replace")
print("ps1 含 aiops2026:", "aiops2026" in raw)
print("ps1 含 拉起安装向导:", "拉起安装向导" in raw)
raw2 = z.read("AIOps-Windows/backend/.env").decode("utf-8")
dl = [l for l in raw2.splitlines() if l.startswith("DATABASE_URL")]
print(".env DATABASE_URL:", dl[0][:70] if dl else "MISSING")
raw3 = z.read("AIOps-Windows/一键部署.bat")
try:
    raw3.decode("gbk")
    print("bat GBK OK: True")
except UnicodeDecodeError:
    print("bat GBK OK: False")
print("testzip:", "OK" if z.testzip() is None else "FAIL")
# 验证升级模块关键内容
names = z.namelist()
print("upgrade_apply.ps1 已打包:", any("deploy/upgrade_apply.ps1" in n for n in names))
print("SystemUpgrade 前端已打包:", any("SystemUpgrade" in n for n in names))

# 同步到项目根
shutil.copy(out, r"D:\WorkBuddy\codex\AIOps\AIOps.zip")
print("copied to project root")
