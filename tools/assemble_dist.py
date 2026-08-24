# 组装生产部署目录
import shutil, os, io, re
from cryptography.fernet import Fernet

dist = r"D:\WorkBuddy\codex\AIOps\dist\AIOps-Windows"
deploy_src = r"D:\WorkBuddy\codex\AIOps\deploy"

for f in ["one-click-install.ps1", "one-click-install.bat", "reset_admin.ps1", "reset_admin.bat",
          "fix_after_upgrade.ps1", "fix_after_upgrade.bat",
          "start_hidden.vbs", "fix_after_reboot.bat"]:
    s = os.path.join(deploy_src, f)
    if os.path.exists(s):
        shutil.copy(s, os.path.join(dist, "deploy", f))
        print(f"deploy/{f} OK")

s = os.path.join(deploy_src, "一键部署.bat")
if os.path.exists(s):
    shutil.copy(s, os.path.join(dist, "一键部署.bat"))
    print("一键部署.bat OK")

inst_dir = os.path.join(dist, "tools", "installers")
os.makedirs(inst_dir, exist_ok=True)
for s in [r"D:\WorkBuddy\codex\postgresql-18.4-2-windows-x64.exe"]:
    if os.path.exists(s):
        shutil.copy(s, inst_dir)
        print(os.path.basename(s), "OK")

shutil.copy(os.path.join(deploy_src, "tools_README.txt"), os.path.join(dist, "tools", "README.txt"))
shutil.copy(os.path.join(deploy_src, "README_prod.md"), os.path.join(dist, "README.md"))
print("READMEs OK")

env_path = os.path.join(dist, "backend", ".env")
with io.open(env_path, encoding="utf-8") as f:
    env = f.read()
m = re.search(r"CREDENTIAL_ENCRYPTION_KEY=(.+)", env)
key = m.group(1).strip() if m else ""
try:
    Fernet(key.encode())
    print(f"env key 有效 ({len(key)} chars) OK")
except Exception as e:
    print(f"env key 无效: {e}")
for line in env.splitlines():
    if line.startswith(("DATABASE_URL", "BOOTSTRAP_ADMIN_PASSWORD")):
        print(" ", line.split("=")[0], "=", line.split("=")[1][:35])
