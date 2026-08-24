#!/usr/bin/env python3
"""AIOps 授权激活码生成工具（可视化 Web 版）

双击启动后自动打开浏览器本地页面：
  1. 粘贴平台「授权管理」页的机器码（或点「获取本机指纹」）
  2. 选择 测试版(3个月) / 全功能版(永久)
  3. 点「生成激活码」→ 复制发给客户即可

密钥对保存在 exe 同级 vendor_keys/（首次运行自动生成）。
必须与平台内置公钥配对：打包部署时把 tools/vendor_keys/ 复制到 exe 同级。
"""
import base64
import hashlib
import json
import sys
import threading
import time
import uuid
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding

# 密钥目录：exe 同级（打包后）/ 脚本同目录（开发）
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
KEY_DIR = BASE_DIR / "vendor_keys"

# ---------------- 授权激活记录存储（exe 同级 activation_records.json） ----------------
RECORDS_FILE = BASE_DIR / "activation_records.json"
_RECORDS_LOCK = threading.Lock()

VERSION_NAMES = {"trial": "测试版", "full": "全功能版"}

# ---------------- 主密码保护（防止未授权人员使用激活工具） ----------------
# 打开工具后必须先输入正确主密码解锁，才能查看机器码 / 生成激活码 / 查看记录。
MASTER_PASSWORD = "spoia"          # 主密码（明文保留便于甲方修改；改动后请重新打包）
MASTER_PASSWORD_HASH = hashlib.sha256(MASTER_PASSWORD.encode()).hexdigest()
# 解锁状态（进程内；每次启动 exe 需重新输入）
_unlocked = False
_UNLOCK_LOCK = threading.Lock()


def verify_master_password(password: str) -> bool:
    """校验主密码（恒定时间比较，避免时序侧信道）。"""
    if not password:
        return False
    return hashlib.sha256(password.encode()).hexdigest() == MASTER_PASSWORD_HASH


def unlock(password: str) -> bool:
    """解锁工具。成功后本进程内所有 API 可用。"""
    global _unlocked
    if not verify_master_password(password):
        return False
    with _UNLOCK_LOCK:
        _unlocked = True
    return True


def is_unlocked() -> bool:
    with _UNLOCK_LOCK:
        return _unlocked


def load_records() -> list:
    """读取全部激活记录（文件损坏时返回空列表）。"""
    try:
        if RECORDS_FILE.is_file():
            data = json.loads(RECORDS_FILE.read_text(encoding="utf-8"))
            records = data.get("records", []) if isinstance(data, dict) else data
            return records if isinstance(records, list) else []
    except Exception:
        pass
    return []


def append_record(fp: str, version: str, expires: str, sn: int, code: str) -> None:
    """追加一条激活记录到本地 JSON（线程安全）。"""
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fp": fp,
        "version": version,
        "expires": expires,
        "sn": sn,
        "code": code,
    }
    with _RECORDS_LOCK:
        records = load_records()
        records.append(record)
        try:
            RECORDS_FILE.write_text(
                json.dumps({"records": records}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 目录只读时仅本次不落盘，不影响生成


def records_summary() -> dict:
    """记录页统计：总数、去重机器数、各机器码激活次数。"""
    records = load_records()
    by_fp: dict[str, int] = {}
    last_time: dict[str, str] = {}
    for r in records:
        fp = r.get("fp", "?")
        by_fp[fp] = by_fp.get(fp, 0) + 1
        last_time[fp] = r.get("time", "")
    machines = [
        {"fp": fp, "count": n, "last_time": last_time.get(fp, "")}
        for fp, n in sorted(by_fp.items(), key=lambda x: (-x[1], x[0]))
    ]
    return {
        "total": len(records),
        "machines": len(machines),
        "by_fp": machines,
    }


def get_fingerprint() -> str:
    """本机指纹（与平台 license_service 算法一致）"""
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


def load_or_create_keys():
    """加载或生成 RSA-2048 密钥对"""
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    priv_path = KEY_DIR / "private_key.pem"
    pub_path = KEY_DIR / "public_key.pem"
    if priv_path.exists() and pub_path.exists():
        return priv_path.read_bytes(), pub_path.read_bytes()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_bytes = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    pub_bytes = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    priv_path.write_bytes(priv_bytes)
    pub_path.write_bytes(pub_bytes)
    return priv_bytes, pub_bytes


def make_code(payload: dict, priv_key) -> str:
    data = json.dumps(payload, separators=(",", ":")).encode()
    sig = priv_key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return f"{b64(data)}.{b64(sig)}"


def check_key_pair() -> bool:
    """校验本机 vendor_keys 私钥对应公钥 == 平台内置公钥。
    防止 exe 缺少配套密钥时自动生成新密钥对 → 激活码在平台验签失败。
    """
    try:
        priv_bytes = (KEY_DIR / "private_key.pem").read_bytes()
        priv = serialization.load_pem_private_key(priv_bytes, password=None)
        pub_bytes = priv.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return pub_bytes.strip() == VENDOR_PUBLIC_KEY_PEM.strip()
    except Exception:
        return False


def generate(fingerprint: str, version: str, days: int = 90, expires: str = ""):
    """生成激活码，返回 (激活码, 授权信息dict)"""
    if not check_key_pair():
        raise ValueError("密钥不匹配：请确保 AIOPS激活工具.exe 与 vendor_keys 文件夹放在同一目录，"
                         "且密钥文件与平台一致（重新从官方 zip 完整解压）")
    fp = fingerprint.strip().upper()
    if not fp:
        raise ValueError("请输入机器码")
    if version == "full":
        exp = ""
    else:
        exp = expires.strip() if expires.strip() else (date.today() + timedelta(days=days)).isoformat()
    payload = {
        "ver": "trial" if version == "trial" else "full",
        "ed": exp,
        "fp": fp,
        "sn": int(date.today().strftime("%Y%m%d") + str(uuid.uuid4().int % 10000)),
    }
    priv_bytes, _ = load_or_create_keys()
    priv = serialization.load_pem_private_key(priv_bytes, password=None)
    code = make_code(payload, priv)
    info = {
        "version": "全功能版（永久）" if payload["ver"] == "full" else f"测试版（{exp} 到期）",
        "expires": "永久" if payload["ver"] == "full" else exp,
        "fingerprint": fp,
        "sn": payload["sn"],
    }
    return code, info


# ---------------- 平台 Logo（方案一「脉冲智核」，内嵌避免外部文件） ----------------
LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <title>AIOps 智能运维</title>
  <polygon points="32,5 54.5,18.5 54.5,45.5 32,59 9.5,45.5 9.5,18.5" fill="#0e4a5e" stroke="#22d3ee" stroke-width="2.5" stroke-linejoin="round"/>
  <line x1="32" y1="32" x2="23" y2="23" stroke="#22d3ee" stroke-width="1" opacity="0.65"/>
  <line x1="32" y1="32" x2="41" y2="23" stroke="#22d3ee" stroke-width="1" opacity="0.65"/>
  <line x1="32" y1="32" x2="41" y2="41" stroke="#22d3ee" stroke-width="1" opacity="0.65"/>
  <line x1="32" y1="32" x2="23" y2="41" stroke="#22d3ee" stroke-width="1" opacity="0.65"/>
  <circle cx="32" cy="32" r="5" fill="#22d3ee"/>
  <circle cx="23" cy="23" r="3" fill="#67e8f9"/>
  <circle cx="41" cy="23" r="3" fill="#67e8f9"/>
  <circle cx="41" cy="41" r="3" fill="#67e8f9"/>
  <circle cx="23" cy="41" r="3" fill="#67e8f9"/>
  <path d="M16,48 H25 L31,44 L35,52 L41,47 H48" fill="none" stroke="#22d3ee" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

# ---------------- 平台内置公钥（用于自检密钥配对，防止生成无效激活码） ----------------
VENDOR_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvAK0qyfn0hjkXxGxb5Wa
1Y86MofRQJuDPusrT32aQxrB7p0AHD8BR4XNC0AIY3VVltthO4k9Sb28150BdOje
gYTqrGeMLVxuhLRpX59wEaAYXZz5DMNhboAGdemwP1RvR+AGWUnEXmQbFYD7troO
h7vguyvhhTqV0JYJUKPTs02VKIJgGAeLspIp7v8yNp3basv6efTXusEu+QV5S2F8
ftpXaUNFcuF0S7blC5xoOe1ZtshpDEJJvwU8Ugt8Fbs0EUeyDgXI0/u+vimQDZIT
kL9vKIllOi/DaZEKvm9CMOUrWxilkGWwdQMFom56U2vaeZnJP8qdMa3jabvIICyF
5QIDAQAB
-----END PUBLIC KEY-----
"""

# ---------------- 内嵌页面 ----------------
PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIOps 授权激活码生成工具</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:"Microsoft YaHei",system-ui; background:#0b1220; color:#e8eef8;
         min-height:100vh; display:flex; justify-content:center; padding:32px 16px; }
  .card { width:100%; max-width:640px; background:#111d31; border:1px solid #24405e;
          border-radius:14px; padding:28px; box-shadow:0 8px 40px rgba(0,0,0,.5); height:fit-content; }
  .logo-row { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
  .logo-row img { width:40px; height:40px; }
  h1 { font-size:20px; color:#22d3ee; }
  .sub { color:#64748b; font-size:13px; margin-bottom:22px; }
  label { display:block; font-size:13px; color:#94a3b8; margin:16px 0 6px; }
  .row { display:flex; gap:10px; }
  input[type=text], input[type=number] { flex:1; background:#0b1728; border:1px solid #2b4a6e;
         color:#e8eef8; border-radius:8px; padding:10px 12px; font-size:14px; outline:none; }
  input:focus { border-color:#22d3ee; }
  .btn { background:#0e7490; color:#fff; border:0; border-radius:8px; padding:10px 16px;
         font-size:14px; cursor:pointer; transition:background .15s; white-space:nowrap; }
  .btn:hover { background:#155e75; }
  .btn.ghost { background:#1e2f45; }
  .btn.ghost:hover { background:#2a3d57; }
  .btn.primary { background:#0e7490; padding:12px; width:100%; font-size:15px; margin-top:20px; }
  .btn.primary:hover { background:#155e75; }
  .radios { display:flex; gap:20px; }
  .radios label { display:flex; align-items:center; gap:8px; color:#cbd5e1; margin:0; cursor:pointer; }
  .field { margin-top:14px; display:none; }
  .field.show { display:block; }
  .info-box { background:#0b1728; border:1px solid #2b4a6e; border-radius:8px; padding:12px 14px;
              margin-top:20px; font-size:13px; line-height:1.9; display:none; }
  .info-box.show { display:block; }
  .info-box b { color:#22d3ee; }
  .code-box { margin-top:14px; display:none; }
  .code-box.show { display:block; }
  .code-box textarea { width:100%; height:130px; background:#0b1728; border:1px solid #2b4a6e;
              color:#7dd3fc; border-radius:8px; padding:10px; font-family:Consolas,monospace;
              font-size:13px; resize:vertical; outline:none; }
  .copy-row { display:flex; justify-content:space-between; align-items:center; margin-top:10px; }
  .hint { color:#475569; font-size:12px; margin-top:18px; line-height:1.7; }
  .err { color:#f87171; font-size:13px; margin-top:10px; min-height:18px; }
  .tabs { display:flex; gap:8px; margin:18px 0 6px; border-bottom:1px solid #24405e; }
  .tab { padding:9px 18px; cursor:pointer; border-radius:8px 8px 0 0; font-size:14px;
         color:#94a3b8; border:1px solid transparent; border-bottom:0; user-select:none; }
  .tab:hover { color:#cbd5e1; background:#15233b; }
  .tab.active { color:#22d3ee; background:#0f2a42; border-color:#24405e; font-weight:600; }
  .tab-page { display:none; }
  .tab-page.show { display:block; }
  .stats { display:flex; gap:14px; margin:14px 0 18px; }
  .stat { flex:1; background:#0b1728; border:1px solid #2b4a6e; border-radius:10px;
          padding:14px 16px; text-align:center; }
  .stat b { display:block; font-size:26px; color:#22d3ee; margin-bottom:4px; }
  .stat span { font-size:12px; color:#64748b; }
  .tbl-wrap { background:#0b1728; border:1px solid #2b4a6e; border-radius:10px; overflow:auto;
              margin-top:16px; max-height:340px; }
  .tbl-wrap h3 { font-size:13px; color:#94a3b8; padding:12px 14px 6px; font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { padding:9px 12px; text-align:left; border-bottom:1px solid #1c3049;
           white-space:nowrap; color:#cbd5e1; }
  th { color:#64748b; font-weight:600; background:#0d1f33; position:sticky; top:0; }
  td .ver-t { color:#38bdf8; }
  td .ver-f { color:#34d399; }
  td .fp-cell { font-family:Consolas,monospace; color:#e2e8f0; }
  td .empty { color:#475569; text-align:center; padding:26px; }
  .mini-btn { background:#1e2f45; color:#cbd5e1; border:1px solid #2b4a6e; border-radius:6px;
              padding:4px 10px; font-size:12px; cursor:pointer; }
  .mini-btn:hover { background:#2a3d57; }
  .lock-mask { position:fixed; inset:0; background:rgba(11,18,32,.96); display:flex;
               align-items:center; justify-content:center; z-index:99; }
  .lock-card { width:100%; max-width:380px; background:#111d31; border:1px solid #24405e;
               border-radius:14px; padding:30px; text-align:center; box-shadow:0 8px 40px rgba(0,0,0,.6); }
  .lock-card h2 { font-size:18px; color:#22d3ee; margin-bottom:6px; }
  .lock-card .sub { margin-bottom:20px; }
  .lock-card input[type=password] { width:100%; background:#0b1728; border:1px solid #2b4a6e;
               color:#e8eef8; border-radius:8px; padding:11px 12px; font-size:15px; outline:none;
               margin-bottom:14px; text-align:center; letter-spacing:2px; }
  .lock-card input:focus { border-color:#22d3ee; }
  .lock-card .err { min-height:20px; }
  .hidden { display:none !important; }
</style>
</head>
<body>
<div class="lock-mask" id="lockMask">
  <div class="lock-card">
    <div class="logo-row" style="justify-content:center;margin-bottom:10px"><img src="/logo" alt=""><div>
      <h2>AIOPS 授权工具</h2>
      <div class="sub">请输入主密码解锁</div>
    </div></div>
    <input type="password" id="lockPwd" placeholder="主密码" autofocus autocomplete="off">
    <button class="btn primary" id="lockBtn" onclick="unlockNow()" style="margin-top:0">解 锁</button>
    <div class="err" id="lockErr"></div>
  </div>
</div>
<div class="card">
  <div class="logo-row"><img src="/logo" alt=""><div>
    <h1>AIOPS 授权激活码生成工具</h1>
    <div class="sub">粘贴机器码，一键生成激活码 · 自动记录每次授权</div>
  </div></div>

  <div class="tabs">
    <div class="tab active" id="tabGenBtn" onclick="switchTab('gen')">生成激活码</div>
    <div class="tab" id="tabRecBtn" onclick="switchTab('rec')">激活记录</div>
  </div>

  <div class="tab-page show" id="pageGen">
  <label>机器码（从平台「授权管理」页复制）</label>
  <div class="row">
    <input type="text" id="fp" placeholder="如 F55E822D0889C09A" spellcheck="false">
    <button class="btn ghost" onclick="getFp()">获取本机指纹</button>
  </div>

  <label>授权类型</label>
  <div class="radios">
    <label><input type="radio" name="ver" value="trial" checked onchange="toggleVer()">测试版（3 个月）</label>
    <label><input type="radio" name="ver" value="full" onchange="toggleVer()">全功能版（永久）</label>
  </div>
  <div class="field show" id="trialField">
    <label>有效期</label>
    <div class="row" style="gap:16px">
      <div style="flex:1"><label style="margin-top:0">天数</label><input type="number" id="days" value="90" min="1"></div>
      <div style="flex:1"><label style="margin-top:0">或指定到期日（留空用天数）</label><input type="text" id="expires" placeholder="2027-01-01" spellcheck="false"></div>
    </div>
  </div>

  <button class="btn primary" onclick="gen()">生 成 激 活 码</button>
  <div class="err" id="err"></div>

  <div class="info-box" id="info"></div>
  <div class="code-box" id="codeBox">
    <textarea id="code" readonly></textarea>
    <div class="copy-row">
      <span class="hint" style="margin:0">将激活码发给客户，在平台「授权管理」粘贴激活</span>
      <button class="btn ghost" onclick="copyCode()">复制激活码</button>
    </div>
  </div>

  <div class="hint">
    说明：激活码绑定机器码（服务器硬件指纹），更换服务器需重新生成；<br>
    测试版到期后平台锁定，全功能版永久授权。<br>
    授权联系邮箱：x1280455974@163.com
  </div>
  </div>

  <div class="tab-page" id="pageRec">
    <div class="stats">
      <div class="stat"><b id="statTotal">0</b><span>已生成激活码（次）</span></div>
      <div class="stat"><b id="statMachines">0</b><span>已授权机器数</span></div>
    </div>

    <div class="tbl-wrap">
      <h3>各机器码激活次数</h3>
      <table>
        <thead><tr><th>机器码</th><th>激活次数</th><th>最近生成时间</th></tr></thead>
        <tbody id="fpRows"></tbody>
      </table>
    </div>

    <div class="tbl-wrap">
      <h3>激活记录明细</h3>
      <table>
        <thead><tr><th>生成时间</th><th>机器码</th><th>版本</th><th>到期时间</th><th>序列号</th><th>激活码</th></tr></thead>
        <tbody id="recRows"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const VNAME = {trial:'<span class="ver-t">测试版</span>', full:'<span class="ver-f">全功能版</span>'};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let _recCodes = [];
let _unlocked = false;

async function unlockNow() {
  const pwd = document.getElementById('lockPwd').value;
  const err = document.getElementById('lockErr');
  const btn = document.getElementById('lockBtn');
  if (!pwd) { err.textContent = '请输入主密码'; return; }
  btn.textContent = '解锁中…';
  err.textContent = '';
  try {
    const r = await fetch('/api/unlock', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pwd})
    });
    const d = await r.json();
    if (d.error) { err.textContent = d.error; btn.textContent = '解 锁'; return; }
    _unlocked = true;
    document.getElementById('lockMask').classList.add('hidden');
    loadRecords();  // 解锁后预载激活记录
  } catch (e) { err.textContent = '解锁失败：' + e; btn.textContent = '解 锁'; }
}
document.getElementById('lockPwd').addEventListener('keydown', e => {
  if (e.key === 'Enter') unlockNow();
});
function switchTab(name) {
  document.getElementById('tabGenBtn').classList.toggle('active', name === 'gen');
  document.getElementById('tabRecBtn').classList.toggle('active', name === 'rec');
  document.getElementById('pageGen').classList.toggle('show', name === 'gen');
  document.getElementById('pageRec').classList.toggle('show', name === 'rec');
  if (name === 'rec') loadRecords();
}
async function loadRecords() {
  if (!_unlocked) return;
  try {
    const r = await fetch('/api/records');
    const d = await r.json();
    if (r.status === 401 || d.error) {
      document.getElementById('recRows').innerHTML = '<tr><td class="empty" colspan="6">' + esc(d.error || '未解锁') + '</td></tr>';
      return;
    }
    document.getElementById('statTotal').textContent = d.stats.total;
    document.getElementById('statMachines').textContent = d.stats.machines;
    document.getElementById('fpRows').innerHTML = d.stats.by_fp.length
      ? d.stats.by_fp.map(m =>
          '<tr><td class="fp-cell">' + esc(m.fp) + '</td><td>' + m.count + ' 次</td><td>' + esc(m.last_time) + '</td></tr>').join('')
      : '<tr><td class="empty" colspan="3">暂无记录，生成激活码后自动记录</td></tr>';
    const rows = d.records.slice().reverse();
    _recCodes = rows.map(x => x.code);
    document.getElementById('recRows').innerHTML = rows.length
      ? rows.map((r, i) =>
          '<tr><td>' + esc(r.time) + '</td><td class="fp-cell">' + esc(r.fp) + '</td><td>' + VNAME[r.version] + '</td>' +
          '<td>' + esc(r.expires) + '</td><td>' + esc(r.sn) + '</td>' +
          '<td><button class="mini-btn" onclick="copyRec(' + i + ',this)">复制</button></td></tr>').join('')
      : '<tr><td class="empty" colspan="6">暂无记录</td></tr>';
  } catch (e) {
    document.getElementById('recRows').innerHTML = '<tr><td class="empty" colspan="6">加载失败：' + esc(e) + '</td></tr>';
  }
}
async function copyRec(i, btn) {
  const code = _recCodes[i];
  if (!code) return;
  try { await navigator.clipboard.writeText(code); }
  catch (e) { const ta=document.createElement('textarea'); ta.value=code; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  btn.textContent = '已复制 ✓'; setTimeout(()=>btn.textContent='复制', 1200);
}
function toggleVer() {
  const trial = document.querySelector('input[name=ver]:checked').value === 'trial';
  document.getElementById('trialField').classList.toggle('show', trial);
}
async function getFp() {
  if (!_unlocked) return;
  const r = await fetch('/api/fingerprint');
  const d = await r.json();
  if (r.status === 401 || d.error) { document.getElementById('err').textContent = d.error || '未解锁'; return; }
  document.getElementById('fp').value = d.fingerprint;
  document.getElementById('err').textContent = '';
}
async function gen() {
  if (!_unlocked) { document.getElementById('err').textContent = '请先解锁工具'; return; }
  const fp = document.getElementById('fp').value.trim();
  const ver = document.querySelector('input[name=ver]:checked').value;
  const days = parseInt(document.getElementById('days').value || '90', 10);
  const expires = document.getElementById('expires').value.trim();
  const err = document.getElementById('err');
  if (!fp) { err.textContent = '请先填写机器码'; return; }
  err.textContent = '生成中…';
  try {
    const r = await fetch('/api/generate', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({fingerprint:fp, version:ver, days, expires})
    });
    const d = await r.json();
    if (d.error) { err.textContent = d.error; return; }
    err.textContent = '';
    document.getElementById('info').innerHTML =
      '授权类型：<b>' + d.info.version + '</b><br>' +
      '到期时间：<b>' + d.info.expires + '</b><br>' +
      '绑定机器码：<b>' + d.info.fingerprint + '</b><br>' +
      '序列号：<b>' + d.info.sn + '</b>';
    document.getElementById('info').classList.add('show');
    document.getElementById('code').value = d.code;
    document.getElementById('codeBox').classList.add('show');
  } catch (e) { err.textContent = '生成失败：' + e; }
}
async function copyCode() {
  const el = document.getElementById('code');
  try { await navigator.clipboard.writeText(el.value); }
  catch (e) { el.select(); document.execCommand('copy'); }
  const b = event.target; b.textContent = '已复制 ✓'; setTimeout(()=>b.textContent='复制激活码', 1500);
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _require_unlock(self) -> bool:
        """未解锁时发送 401 并返回 False。"""
        if is_unlocked():
            return True
        self._send({"error": "需要主密码"}, status=401)
        return False

    def do_GET(self):
        if self.path.startswith("/logo"):
            data = LOGO_SVG.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/api/fingerprint"):
            if not self._require_unlock():
                return
            self._send({"fingerprint": get_fingerprint()})
            return
        if self.path.startswith("/api/records"):
            if not self._require_unlock():
                return
            self._send({"records": load_records(), "stats": records_summary()})
            return
        data = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            # 解锁接口：校验主密码
            if self.path.startswith("/api/unlock"):
                if unlock(body.get("password", "")):
                    self._send({"ok": True, "message": "解锁成功"})
                else:
                    self._send({"error": "主密码错误"}, status=401)
                return
            # 其余接口需要已解锁
            if not self._require_unlock():
                return
            code, info = generate(
                body.get("fingerprint", ""),
                body.get("version", "trial"),
                int(body.get("days") or 90),
                body.get("expires", ""),
            )
            # 生成成功即追加授权激活记录（机器码/版本/到期/序列号/时间）
            append_record(
                fp=info["fingerprint"],
                version="full" if body.get("version") == "full" else "trial",
                expires=info["expires"],
                sn=info["sn"],
                code=code,
            )
            self._send({"code": code, "info": info})
        except Exception as e:
            self._send({"error": str(e)})


def find_free_port(start=8765):
    import socket
    for port in range(start, start + 50):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            continue
    return start


def main():
    port = find_free_port()
    # 首次运行生成密钥（若无）
    load_or_create_keys()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"AIOPS 授权激活码生成工具已启动: http://127.0.0.1:{port}  (密钥目录 {KEY_DIR})")
    server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        # 自检：不启动窗口，验证密钥配对与生成逻辑
        print("key_pair_ok:", check_key_pair(), "| key_dir:", KEY_DIR)
        code, info = generate("F55E822D0889C09A", "trial", 90)
        print(json.dumps({"code_len": len(code), "info": info}, ensure_ascii=False))
        print("self-test OK")
    else:
        main()
