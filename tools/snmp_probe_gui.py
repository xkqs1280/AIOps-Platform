# -*- coding: utf-8 -*-
"""
SNMP 探测工具（GUI 版）

基于 snmp_probe.py 的探测核心，提供图形界面，适合在无命令行经验的环境使用。
支持批量设备、多 community、SNMPv1/v2c、内置 OID 组与自定义 OID，结果实时展示并保存 CSV。

打包：pyinstaller --onefile --windowed --icon ../../build/logo.ico --name SNMP探测工具 snmp_probe_gui.py
"""
import asyncio
import csv
import datetime
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 引入探测核心（同目录 snmp_probe.py）
try:
    from snmp_probe import (
        snmp_get, snmp_walk, GET_GROUPS, WALK_GROUPS, parse_hosts, value_to_str,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from snmp_probe import (
        snmp_get, snmp_walk, GET_GROUPS, WALK_GROUPS, parse_hosts, value_to_str,
    )

ALL_GROUPS = {**GET_GROUPS, **WALK_GROUPS}
GROUP_LABELS = {
    "system": "系统信息",
    "serial": "序列号专项",
    "entity": "实体MIB(序列号/型号)",
    "manu": "制造信息(H3C私有)",
    "lldp": "基础LLDP",
    "lldp-med": "LLDP-MED",
    "lldp-neighbor": "LLDP邻居",
    "lldp-full": "LLDP全部",
}


class ProbeApp:
    def __init__(self, root):
        self.root = root
        root.title("AIOps SNMP 探测工具")
        root.geometry("920x720")
        root.minsize(820, 620)

        self.msg_queue = queue.Queue()
        self.worker = None
        self.loop = None
        self.task = None
        self.running = False

        self._build_ui()
        self.root.after(100, self._poll_queue)

    # ---------------- UI ----------------
    def _build_ui(self):
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # 顶部参数区
        top = ttk.LabelFrame(main, text="探测参数", padding=8)
        top.pack(fill=tk.X)

        # 设备列表
        ttk.Label(top, text="设备列表（每行一个 IP 或 IP:端口）").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.hosts_text = tk.Text(top, width=42, height=4)
        self.hosts_text.grid(row=1, column=0, rowspan=4, padx=(0, 10), sticky=tk.N)

        # 右侧参数
        right = ttk.Frame(top)
        right.grid(row=0, column=1, rowspan=5, sticky=tk.NW)
        ttk.Label(right, text="SNMP community（逗号分隔，依次尝试）").grid(row=0, column=0, sticky=tk.W)
        self.community_var = tk.StringVar(value="aiops")
        ttk.Entry(right, textvariable=self.community_var, width=28).grid(row=1, column=0, sticky=tk.W, pady=2)

        frm = ttk.Frame(right)
        frm.grid(row=2, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Label(frm, text="版本").pack(side=tk.LEFT)
        self.version_var = tk.StringVar(value="v2c")
        ttk.Combobox(frm, textvariable=self.version_var, values=["v2c", "v1"], width=4, state="readonly").pack(side=tk.LEFT, padx=6)
        ttk.Label(frm, text="超时(秒)").pack(side=tk.LEFT)
        self.timeout_var = tk.StringVar(value="6")
        ttk.Spinbox(frm, from_=1, to=30, textvariable=self.timeout_var, width=4).pack(side=tk.LEFT, padx=6)
        ttk.Label(frm, text="重试").pack(side=tk.LEFT)
        self.retries_var = tk.StringVar(value="1")
        ttk.Spinbox(frm, from_=0, to=5, textvariable=self.retries_var, width=4).pack(side=tk.LEFT, padx=6)

        # 测试组
        ttk.Label(right, text="测试内容").grid(row=3, column=0, sticky=tk.W, pady=(8, 0))
        self.group_vars = {}
        grp = ttk.Frame(right)
        grp.grid(row=4, column=0, sticky=tk.W)
        for i, g in enumerate(ALL_GROUPS):
            var = tk.BooleanVar(value=(g in ("system", "serial", "entity")))
            self.group_vars[g] = var
            cb = ttk.Checkbutton(grp, text=GROUP_LABELS.get(g, g), variable=var)
            cb.grid(row=i // 2, column=i % 2, sticky=tk.W, padx=(0, 12), pady=1)

        # 自定义 OID + 输出文件
        ttk.Label(right, text="自定义 OID（逗号分隔，可选）").grid(row=5, column=0, sticky=tk.W, pady=(8, 0))
        self.oids_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.oids_var, width=48).grid(row=6, column=0, sticky=tk.W)

        ttk.Label(right, text="结果保存为 CSV").grid(row=7, column=0, sticky=tk.W, pady=(8, 0))
        out = ttk.Frame(right)
        out.grid(row=8, column=0, sticky=tk.W)
        self.out_var = tk.StringVar(value=self._default_out_path())
        ttk.Entry(out, textvariable=self.out_var, width=36).pack(side=tk.LEFT)
        ttk.Button(out, text="浏览", command=self._browse_out).pack(side=tk.LEFT, padx=4)

        # 控制按钮
        btns = ttk.Frame(main)
        btns.pack(fill=tk.X, pady=8)
        self.start_btn = ttk.Button(btns, text="▶ 开始探测", command=self.start_probe)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btns, text="■ 停止", command=self.stop_probe, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="清空日志", command=self._clear_log).pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main, textvariable=self.status_var).pack(fill=tk.X, pady=(0, 4))

        # 结果区
        res = ttk.LabelFrame(main, text="探测结果", padding=4)
        res.pack(fill=tk.BOTH, expand=True)
        self.log = tk.Text(res, wrap=tk.NONE, font=("Consolas", 9), bg="#111418", fg="#d5dde5")
        self.log.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sbar = ttk.Scrollbar(res, command=self.log.yview)
        sbar.pack(fill=tk.Y, side=tk.RIGHT)
        self.log.config(yscrollcommand=sbar.set)
        self.log.tag_configure("ok", foreground="#5fd45f")
        self.log.tag_configure("fail", foreground="#ff7b72")
        self.log.tag_configure("head", foreground="#58a6ff", font=("Consolas", 9, "bold"))

    def _browse_out(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=os.path.basename(self.out_var.get()))
        if path:
            self.out_var.set(path)

    def _default_out_path(self):
        """默认输出到桌面，带时间戳，避免工作目录不可写或文件被覆盖。"""
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        desk = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desk):
            desk = os.getcwd()
        return os.path.join(desk, f"SNMP探测结果_{ts}.csv")

    # ---------------- 日志 ----------------
    def _log(self, text, tag=None):
        self.msg_queue.put(("log", text, tag))

    def _poll_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                if item[0] == "log":
                    _, text, tag = item
                    self.log.insert(tk.END, text + "\n", tag or ())
                    self.log.see(tk.END)
                elif item[0] == "status":
                    self.status_var.set(item[1])
                elif item[0] == "saved":
                    path = item[1]
                    if messagebox.askyesno("保存成功", f"结果已保存到：\n{path}\n\n是否打开所在文件夹？"):
                        self._open_folder(path)
                elif item[0] == "done":
                    _, ok = item
                    self._finish(ok)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _clear_log(self):
        self.log.delete("1.0", tk.END)

    def _save_csv(self, results, out_path):
        """保存结果到 CSV，返回 (成功?, 提示文字)。"""
        try:
            with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["host", "community", "group", "oid_name", "oid", "status", "value"])
                w.writerows(results)
            return True, f"已保存 {len(results)} 条记录"
        except Exception as e:
            return False, f"保存失败：{e}"

    def _open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(os.path.dirname(os.path.abspath(path)))  # noqa: S606
            else:
                import subprocess
                subprocess.Popen(["xdg-open", os.path.dirname(os.path.abspath(path))])
        except Exception as e:
            self._log(f"打开文件夹失败：{e}", "fail")

    def _set_running(self, running):
        self.running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)

    def _finish(self, ok):
        self._set_running(False)
        self.status_var.set("完成" if ok else "已停止")

    # ---------------- 探测 ----------------
    def start_probe(self):
        if self.running:
            return
        # 解析输入
        hosts_raw = self.hosts_text.get("1.0", tk.END).strip()
        hosts_file = None
        community = self.community_var.get().strip() or "aiops"
        version = self.version_var.get()
        try:
            timeout = int(self.timeout_var.get())
            retries = int(self.retries_var.get())
        except ValueError:
            messagebox.showerror("参数错误", "超时/重试必须是数字")
            return
        groups = [g for g, v in self.group_vars.items() if v.get()]
        oids = self.oids_var.get().strip()
        out_path = self.out_var.get().strip() or "snmp_probe_result.csv"

        # 设备列表：文本框优先，支持整段粘贴；否则文件
        if hosts_raw:
            hosts = parse_hosts(hosts_raw, None)
        else:
            hosts = parse_hosts(None, hosts_file)
        if not hosts:
            messagebox.showerror("参数错误", "请填写至少一个设备 IP")
            return
        if not groups and not oids:
            messagebox.showerror("参数错误", "请至少勾选一个测试组或填写自定义 OID")
            return

        self._clear_log()
        self._set_running(True)
        self.worker = threading.Thread(
            target=self._worker, args=(hosts, community, version, timeout, retries, groups, oids, out_path),
            daemon=True,
        )
        self.worker.start()

    def stop_probe(self):
        if self.running and self.loop is not None and self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
            self.status_var.set("正在停止...")

    def _worker(self, hosts, community, version, timeout, retries, groups, oids, out_path):
        """后台线程：独立事件循环跑探测；停止时通过 task.cancel() 强制中断。"""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(
            self._run_probe(hosts, community, version, timeout, retries, groups, oids, out_path)
        )
        try:
            self.loop.run_until_complete(self.task)
        except asyncio.CancelledError:
            pass  # 停止：由 _run_probe 的 finally 兜底保存
        finally:
            self.loop.close()
            self.loop = None
            self.task = None

    async def _run_probe(self, hosts, community, version, timeout, retries, groups, oids, out_path):
        communities = [c.strip() for c in community.split(",") if c.strip()]
        tests = []
        if oids:
            for i, oid in enumerate(oids.split(",")):
                oid = oid.strip()
                if oid:
                    tests.append(("custom", f"custom_{i}", oid, False))
        for g in groups:
            if g in GET_GROUPS:
                for name, oid in GET_GROUPS[g].items():
                    tests.append((g, name, oid, False))
            elif g in WALK_GROUPS:
                for name, oid in WALK_GROUPS[g].items():
                    tests.append((g, name, oid, True))

        results = []
        aborted = False
        self._log(f"设备 {len(hosts)} 台 | community {communities} | 测试项 {len(tests)} | 输出 {out_path}", "head")
        self._log("=" * 70, "head")

        try:
            for idx, (ip, port) in enumerate(hosts, 1):
                self.msg_queue.put(("status", f"探测中 {idx}/{len(hosts)}：{ip}"))
                self._log(f"\n>>> 设备 {ip}:{port}", "head")
                # 探测 community
                active = None
                for c in communities:
                    ok, val = await snmp_get(ip, "1.3.6.1.2.1.1.1.0", c, port, version, timeout, retries)
                    if ok:
                        active = c
                        self._log(f"  community='{c}' 可用 -> {val[:80]}", "ok")
                        break
                    self._log(f"  community='{c}' 不可用 -> {val}", "fail")
                if active is None:
                    results.append((ip, ";".join(communities), "-", "sysDescr", "1.3.6.1.2.1.1.1.0", "FAIL", "所有 community 均不可达"))
                    continue

                for group, name, oid, is_walk in tests:
                    if is_walk:
                        rows = await snmp_walk(ip, oid, active, port, version, timeout, retries, max_nodes=200)
                        if not rows:
                            self._log(f"  [WALK] {name:<20} -> 无节点(设备未实现该MIB)", "fail")
                            results.append((ip, active, group, name, oid, "EMPTY", "无节点"))
                        elif rows[0][0] == "error":
                            self._log(f"  [WALK] {name:<20} -> {rows[0][1]}", "fail")
                            results.append((ip, active, group, name, oid, "FAIL", rows[0][1]))
                        else:
                            self._log(f"  [WALK] {name:<20} -> 返回 {len(rows)} 个节点", "ok")
                            for o, v in rows:
                                results.append((ip, active, group, name, o, "OK", v))
                    else:
                        ok, val = await snmp_get(ip, oid, active, port, version, timeout, retries)
                        tag = "ok" if ok else "fail"
                        self._log(f"  [GET ] {name:<20} {oid:<38} -> {'OK' if ok else 'FAIL'}: {val[:90]}", tag)
                        results.append((ip, active, group, name, oid, "OK" if ok else "FAIL", val))
        except asyncio.CancelledError:
            aborted = True  # 用户停止：由 finally 保存已采集结果
        except Exception as e:
            aborted = True
            self._log(f"\n探测异常：{type(e).__name__}: {e}", "fail")
        finally:
            # 无论完成 / 停止 / 异常，都保存已采集结果
            ok, msg = self._save_csv(results, out_path)
            if ok:
                label = "已停止" if aborted else "完成"
                self._log(f"\n{label}：{msg} -> {out_path}", "head")
                self.msg_queue.put(("status", f"{label}：{msg}"))
                self.msg_queue.put(("saved", out_path))
            else:
                self._log(f"\n{msg}", "fail")
                self.msg_queue.put(("status", "保存 CSV 失败"))
            self.msg_queue.put(("done", True))


def main():
    root = tk.Tk()
    ProbeApp(root)
    root.mainloop()


if __name__ == "__main__":
    # --selftest：仅验证 tkinter 可用并退出
    if "--selftest" in sys.argv:
        root = tk.Tk()
        root.destroy()
        print("GUI selftest OK")
        sys.exit(0)
    main()
