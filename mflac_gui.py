#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mflac2mp3 图形界面 — QQ 音乐加密音频批量转 MP3

用法:
  python mflac_gui.py          打开图形界面
  python mflac_gui.py --smoke  无界面自检 (写入 smoke_result.txt 后退出)

打包为单文件 exe (在项目目录执行):
  pip install pyinstaller
  python -m PyInstaller --noconfirm --clean --onefile --windowed --name mflac2mp3 mflac_gui.py
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import mflac2mp3 as M

APP_TITLE = "mflac2mp3 — OGG / FLAC / QQ 音乐转 MP3"
CONFIG_NAME = "gui_config.json"
SUPPORTED_DESC = "音频文件 (ogg/flac/mflac/mgg/qmc 系列)"
BITRATE_CHOICES = ("320", "256", "224", "192", "128", "VBR (最高音质)")


def load_config() -> dict:
    try:
        p = M.app_dir() / CONFIG_NAME
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config(cfg: dict) -> None:
    try:
        (M.app_dir() / CONFIG_NAME).write_text(
            json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


class GuiApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("900x600")
        root.minsize(760, 500)

        self.cfg = load_config()
        self.files: list[Path] = []
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self._stages: dict[int, str] = {}

        self._build_style()
        self._build_ui()
        self._load_cfg_to_ui()
        self._refresh_ffmpeg_status()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll)

    # ---------------------------------------------------- UI 构建

    def _build_style(self):
        style = ttk.Style(self.root)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break

    def _build_ui(self):
        root = self.root
        pad = {"padx": 8, "pady": 4}

        # 顶部按钮
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        self.btn_add_files = ttk.Button(top, text="添加文件", command=self.add_files)
        self.btn_add_files.pack(side="left", padx=(0, 6))
        self.btn_add_dir = ttk.Button(top, text="添加文件夹", command=self.add_folder)
        self.btn_add_dir.pack(side="left", padx=(0, 6))
        self.btn_remove = ttk.Button(top, text="移除所选", command=self.remove_selected)
        self.btn_remove.pack(side="left", padx=(0, 6))
        self.btn_clear = ttk.Button(top, text="清空列表", command=self.clear_files)
        self.btn_clear.pack(side="left")

        # 选项区
        opt = ttk.LabelFrame(root, text="转换选项")
        opt.pack(fill="x", **pad)

        row1 = ttk.Frame(opt)
        row1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row1, text="码率:").pack(side="left")
        self.var_bitrate = tk.StringVar(value="320")
        self.cmb_bitrate = ttk.Combobox(
            row1, textvariable=self.var_bitrate, state="readonly", width=10,
            values=list(BITRATE_CHOICES))
        self.cmb_bitrate.pack(side="left", padx=(4, 16))

        self.var_same_dir = tk.BooleanVar(value=True)
        self.chk_same_dir = ttk.Checkbutton(
            row1, text="输出到原目录", variable=self.var_same_dir,
            command=self._toggle_outdir)
        self.chk_same_dir.pack(side="left", padx=(0, 8))
        ttk.Label(row1, text="输出目录:").pack(side="left")
        self.var_outdir = tk.StringVar(value="")
        self.ent_outdir = ttk.Entry(row1, textvariable=self.var_outdir)
        self.ent_outdir.pack(side="left", fill="x", expand=True, padx=4)
        self.btn_outdir = ttk.Button(row1, text="浏览...", command=self.browse_outdir, width=8)
        self.btn_outdir.pack(side="left")

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=8, pady=(2, 6))
        self.var_keep_flac = tk.BooleanVar(value=False)
        self.chk_keep_flac = ttk.Checkbutton(row2, text="保留解密的原格式音频",
                                             variable=self.var_keep_flac)
        self.chk_keep_flac.pack(side="left", padx=(0, 16))
        self.var_overwrite = tk.BooleanVar(value=False)
        self.chk_overwrite = ttk.Checkbutton(row2, text="覆盖已存在文件",
                                             variable=self.var_overwrite)
        self.chk_overwrite.pack(side="left", padx=(0, 16))
        self.var_flac_only = tk.BooleanVar(value=False)
        self.chk_flac_only = ttk.Checkbutton(row2, text="只解密不转 MP3",
                                             variable=self.var_flac_only)
        self.chk_flac_only.pack(side="left")

        row3 = ttk.Frame(opt)
        row3.pack(fill="x", padx=8, pady=(0, 6))
        self.lbl_ffmpeg = ttk.Label(row3, text="ffmpeg: 检测中...")
        self.lbl_ffmpeg.pack(side="left")
        self.btn_ffmpeg = ttk.Button(row3, text="指定 ffmpeg...", command=self.browse_ffmpeg)
        self.btn_ffmpeg.pack(side="left", padx=8)
        ttk.Label(row3, foreground="#888",
                  text="提示: 新版 musicex 文件需要本机 QQ 音乐客户端正在运行且已登录"
                  ).pack(side="right")

        # 文件列表
        lst = ttk.LabelFrame(root, text="文件列表 (双击打开所在目录)")
        lst.pack(fill="both", expand=True, **pad)
        columns = ("name", "fmt", "size", "status")
        self.tree = ttk.Treeview(lst, columns=columns, show="headings",
                                 selectmode="extended")
        self.tree.heading("name", text="文件名")
        self.tree.heading("fmt", text="格式")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.column("name", width=380, anchor="w")
        self.tree.column("fmt", width=70, anchor="center")
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("status", width=260, anchor="w")
        vsb = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        vsb.pack(side="left", fill="y", pady=8)
        self.tree.bind("<Double-1>", self._open_selected_dir)

        # 底部进度与操作
        bottom = ttk.Frame(root)
        bottom.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.lbl_stage = ttk.Label(bottom, text="就绪", width=44, anchor="w")
        self.lbl_stage.pack(side="left")

        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=8, pady=(0, 10))
        self.lbl_summary = ttk.Label(actions, text="未添加文件", anchor="w")
        self.lbl_summary.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="取消", command=self.cancel_run,
                                     state="disabled")
        self.btn_cancel.pack(side="right", padx=(8, 0))
        self.btn_start = ttk.Button(actions, text="开始转换", command=self.start_run)
        self.btn_start.pack(side="right")

    # ---------------------------------------------------- 选项联动

    def _toggle_outdir(self):
        same = self.var_same_dir.get()
        state = "disabled" if same else "normal"
        self.ent_outdir.configure(state=state)
        self.btn_outdir.configure(state=state)

    def _load_cfg_to_ui(self):
        cfg = self.cfg
        if str(cfg.get("bitrate")) in BITRATE_CHOICES:
            self.var_bitrate.set(str(cfg["bitrate"]))
        if cfg.get("output_dir"):
            self.var_same_dir.set(False)
            self.var_outdir.set(cfg["output_dir"])
        self._toggle_outdir()
        self.var_keep_flac.set(bool(cfg.get("keep_flac")))
        self.var_overwrite.set(bool(cfg.get("overwrite")))
        self.var_flac_only.set(bool(cfg.get("flac_only")))
        self._ffmpeg_manual = cfg.get("ffmpeg") or None

    def _save_cfg_from_ui(self):
        self.cfg.update({
            "bitrate": self.var_bitrate.get(),
            "output_dir": "" if self.var_same_dir.get() else self.var_outdir.get().strip(),
            "keep_flac": self.var_keep_flac.get(),
            "overwrite": self.var_overwrite.get(),
            "flac_only": self.var_flac_only.get(),
            "ffmpeg": getattr(self, "_ffmpeg_manual", None) or "",
        })
        save_config(self.cfg)

    def _refresh_ffmpeg_status(self):
        exe = M.find_ffmpeg(getattr(self, "_ffmpeg_manual", None))
        if exe:
            self.lbl_ffmpeg.configure(
                foreground="#0a7d32",
                text=f"ffmpeg: 已找到 ({Path(exe).parent})")
        else:
            self.lbl_ffmpeg.configure(
                foreground="#c0392b",
                text="ffmpeg: 未找到 (只能解密, 无法转 MP3; 请安装 ffmpeg 或手动指定)")

    # ---------------------------------------------------- 文件列表

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[(SUPPORTED_DESC, " ".join("*" + ext for ext in sorted(M.SUPPORTED_EXTS))),
                       ("所有文件", "*.*")])
        self._append_files([Path(p) for p in paths])

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择要批量转换的文件夹")
        if folder:
            found = M.collect_inputs([folder])
            self._append_files(found)

    def _append_files(self, paths: list[Path]):
        existing = {str(p.resolve()).lower() for p in self.files}
        added = 0
        for p in paths:
            key = str(p.resolve()).lower()
            if key not in existing:
                self.files.append(p)
                existing.add(key)
                added += 1
        self._rebuild_tree()
        if paths and added == 0:
            self.lbl_summary.configure(text="所选文件均已在列表中")

    def remove_selected(self):
        sel = set(self.tree.selection())
        keep = [p for i, p in enumerate(self.files) if str(i) not in sel]
        self.files = keep
        self._rebuild_tree()

    def clear_files(self):
        self.files = []
        self._rebuild_tree()

    def _rebuild_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.files):
            size_mb = p.stat().st_size / 1024 / 1024 if p.exists() else 0
            self.tree.insert("", "end", iid=str(i),
                             values=(p.stem, p.suffix.lstrip("."), f"{size_mb:.1f} MB", "等待中"))
        n = len(self.files)
        self.lbl_summary.configure(text=f"共 {n} 个文件" if n else "未添加文件")

    def _open_selected_dir(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < len(self.files):
            folder = str(self.files[idx].parent)
            try:
                os.startfile(folder)  # noqa: S606 (仅打开目录)
            except OSError:
                pass

    # ---------------------------------------------------- 转换控制

    def browse_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outdir.set(d)

    def browse_ffmpeg(self):
        f = filedialog.askopenfilename(
            title="选择 ffmpeg.exe", filetypes=[("ffmpeg", "ffmpeg.exe"), ("所有文件", "*.*")])
        if f:
            self._ffmpeg_manual = f
            self._refresh_ffmpeg_status()

    def _build_options(self) -> M.ConvertOptions:
        bitrate_raw = self.var_bitrate.get()
        vbr = bitrate_raw.startswith("VBR")
        bitrate = 320 if vbr else int(bitrate_raw.split()[0])
        return M.ConvertOptions(
            output_dir=None if self.var_same_dir.get() else self.var_outdir.get().strip(),
            bitrate=bitrate,
            vbr=vbr,
            keep_flac=self.var_keep_flac.get(),
            flac_only=self.var_flac_only.get(),
            overwrite=self.var_overwrite.get(),
            ffmpeg=getattr(self, "_ffmpeg_manual", None),
        )

    def start_run(self):
        if self.running:
            return
        if not self.files:
            messagebox.showinfo(APP_TITLE, "请先添加要转换的文件。")
            return
        opts = self._build_options()
        ctx = M.ConvertContext(opts)
        if not ctx.flac_only and not ctx.ffmpeg:
            if not messagebox.askyesno(
                    APP_TITLE,
                    "未找到 ffmpeg，无法转成 MP3（将只解密保留原格式）。\n是否继续？\n\n"
                    "建议安装 ffmpeg (ffmpeg.org) 后将其加入 PATH，或放在本工具同目录。"):
                return
        self._save_cfg_from_ui()
        self._set_running(True)
        self.cancel_event.clear()
        ctx.cancel_event = self.cancel_event
        files = list(self.files)
        for i in range(len(files)):
            self._set_row(i, status="等待中")
        self.worker = threading.Thread(target=self._worker, args=(ctx, files), daemon=True)
        self.worker.start()

    def cancel_run(self):
        if self.running:
            self.cancel_event.set()
            self.lbl_stage.configure(text="正在取消 (等待当前任务结束)...")

    def _worker(self, ctx: M.ConvertContext, files: list[Path]):
        ok_count = fail_count = 0
        cancelled = False
        output_dirs: set[Path] = set()
        try:
            for i, path in enumerate(files):
                if self.cancel_event.is_set():
                    self.q.put(("result", i, False, "已取消"))
                    cancelled = True
                    continue
                self.q.put(("begin", i, path.name))

                def progress(p, _i=i):
                    self.q.put(("progress", _i, p))

                def stage(s, _i=i):
                    self.q.put(("stage", _i, s))

                try:
                    ok, msg = M.convert_one(path, ctx, progress=progress, stage=stage)
                    self.q.put(("result", i, ok, msg))
                    ok_count += 1 if ok else 0
                    fail_count += 0 if ok else 1
                    if ok:
                        output_dirs.add((ctx.out_dir or path.parent).resolve())
                except M.OperationCancelled:
                    self.q.put(("result", i, False, "已取消"))
                    fail_count += 1
                    cancelled = True
                except Exception as e:
                    traceback.print_exc()
                    self.q.put(("result", i, False, f"异常: {e}"))
                    fail_count += 1
        finally:
            self.q.put(("done", ok_count, fail_count, cancelled, sorted(output_dirs)))

    # ---------------------------------------------------- 队列轮询

    def _poll(self):
        try:
            while True:
                event = self.q.get_nowait()
                kind = event[0]
                if kind == "begin":
                    _, i, name = event
                    self._set_row(i, status="转换中")
                    self.progress.configure(value=0)
                    self.lbl_stage.configure(text=name)
                elif kind == "stage":
                    _, i, text = event
                    self._stages[i] = text
                    name = self.files[i].name if i < len(self.files) else ""
                    self._set_row(i, status=text)
                    self.lbl_stage.configure(text=f"{name}: {text}")
                elif kind == "progress":
                    _, i, p = event
                    self.progress.configure(value=int(p * 100))
                    stage_text = self._stages.get(i, "转换中")
                    self._set_row(i, status=f"{stage_text} {int(p * 100)}%")
                elif kind == "result":
                    _, i, ok, msg = event
                    self._set_row(i, status=("✓ " if ok else "✗ ") + msg)
                elif kind == "done":
                    _, ok_count, fail_count, cancelled, output_dirs = event
                    cancelled = cancelled or self.cancel_event.is_set()
                    self.progress.configure(value=100 if not cancelled else self.progress["value"])
                    summary = (f"{'已取消 ' if cancelled else ''}完成: 成功 {ok_count}, 失败 {fail_count}")
                    self.lbl_summary.configure(text=summary)
                    self.lbl_stage.configure(text="就绪")
                    self._set_running(False)
                    self._refresh_ffmpeg_status()
                    if not cancelled:
                        for folder in output_dirs:
                            try:
                                os.startfile(str(folder))
                            except OSError:
                                self.lbl_stage.configure(text=f"无法自动打开输出目录: {folder}")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(80, self._poll)

    def _set_row(self, i: int, status: str | None = None):
        iid = str(i)
        if self.tree.exists(iid):
            if status is not None:
                self.tree.set(iid, "status", status)

    def _set_running(self, running: bool):
        self.running = running
        state = "disabled" if running else "normal"
        for w in (self.btn_add_files, self.btn_add_dir, self.btn_remove,
                  self.btn_clear, self.btn_start):
            w.configure(state=state)
        self.btn_cancel.configure(state="normal" if running else "disabled")

    def _on_close(self):
        if self.running:
            if not messagebox.askyesno(APP_TITLE, "转换正在进行，确定要退出吗？"):
                return
            self.cancel_event.set()
            self.root.after(800, self.root.destroy)
            return
        self._save_cfg_from_ui()
        self.root.destroy()


def smoke_convert(target: str) -> int:
    """无界面转换单个文件, 结果写入 smoke_result.txt (用于打包后验证)。"""
    path = Path(target)
    try:
        if not path.is_file():
            raise RuntimeError(f"文件不存在: {target}")
        ctx = M.ConvertContext(M.ConvertOptions(overwrite=True))
        ok, msg = M.convert_one(path, ctx)
        result = ("OK" if ok else "FAIL") + " | " + msg
        code = 0 if ok else 1
    except Exception as e:
        result = f"FAIL | {e.__class__.__name__}: {e}"
        code = 1
    try:
        (M.app_dir() / "smoke_result.txt").write_text(result, encoding="utf-8")
    except OSError:
        pass
    return code


def main() -> int:
    argv = sys.argv[1:]

    if "--smoke" in argv:
        ok = M.self_test()
        ffmpeg = M.find_ffmpeg() or "未找到"
        try:
            (M.app_dir() / "smoke_result.txt").write_text(
                ("OK" if ok else "FAIL") + " | ffmpeg: " + str(ffmpeg), encoding="utf-8")
        except OSError:
            pass
        return 0 if ok else 1

    for i, a in enumerate(argv):
        if a == "--smoke-convert" and i + 1 < len(argv):
            return smoke_convert(argv[i + 1])

    if sys.platform == "win32":
        try:  # 高 DPI 下界面更清晰
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    app = GuiApp(root)
    # 拖拽/命令行传入的文件直接加入列表
    init_paths = [a for a in argv if not a.startswith("-") and Path(a).exists()]
    if init_paths:
        app._append_files(M.collect_inputs(init_paths))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
