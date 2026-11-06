# -*- coding: utf-8 -*-
"""PDF 逐页盖章工具：从 PDF/图片取章，批量叠加到 PDF 每页。"""

import io
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageTk

APP_NAME = "PDF 逐页盖章"
SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PDFStampTool")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

BG = "#f2f4f7"
CARD = "#ffffff"
BORDER = "#e3e8ef"
TEXT = "#1f2733"
MUTED = "#7a8699"
ACCENT = "#b02a30"
ACCENT_DARK = "#8f1f24"
ACCENT_SOFT = "#fbeceb"
OK = "#1f8a53"
ERR = "#c0392b"

FONT = "Microsoft YaHei UI"


def mm2pt(v):
    return v / 25.4 * 72.0


POSITIONS = ["右下", "中下", "左下", "右上", "左上", "自定义"]


def place_rect(pw, ph, w, h, corner, margin, cx=0.0, cy=0.0):
    """返回印章左上角坐标，单位与传入的页面尺寸一致。"""
    if corner == "自定义":
        x, y = cx, cy
    elif corner == "右下":
        x, y = pw - margin - w, ph - margin - h
    elif corner == "中下":
        x, y = (pw - w) / 2.0, ph - margin - h
    elif corner == "左下":
        x, y = margin, ph - margin - h
    elif corner == "右上":
        x, y = pw - margin - w, margin
    else:
        x, y = margin, margin
    x = min(max(0.0, x), max(0.0, pw - w))
    y = min(max(0.0, y), max(0.0, ph - h))
    return x, y


def red_ratio(im):
    small = im.convert("RGB").resize((140, 140))
    a = np.asarray(small).astype(np.int16)
    red = (a[:, :, 0] > 110) & (a[:, :, 0] - a[:, :, 1] > 40) & (a[:, :, 0] - a[:, :, 2] > 40)
    return float(red.mean())


def to_rgba(im, strength=0.98, opacity=1.0):
    """白底转透明；保留红色印迹，按白色混合反算颜色，避免白边。"""
    if im.mode in ("RGBA", "LA"):
        rgba = np.asarray(im.convert("RGBA")).astype(np.float32)
        rgba[:, :, 3] *= opacity
        return Image.fromarray(np.clip(rgba, 0, 255).astype(np.uint8), "RGBA")

    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    mn = rgb.min(axis=2)
    alpha = np.clip((255.0 - mn) * strength, 0, 255)
    alpha[alpha < 8] = 0
    al = alpha / 255.0
    out = np.zeros_like(rgb)
    nz = al > 0
    for c in range(3):
        out[:, :, c] = np.where(nz, (rgb[:, :, c] - (255.0 - alpha)) / np.maximum(al, 1e-6), 0)
    out = np.clip(out, 0, 255)
    arr = np.dstack([out, alpha * opacity]).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def crop_alpha(im):
    a = np.asarray(im.getchannel("A"))
    ys, xs = np.where(a > 8)
    if len(xs) == 0:
        return im
    pad = 2
    box = (max(0, xs.min() - pad), max(0, ys.min() - pad),
           min(im.width, xs.max() + 1 + pad), min(im.height, ys.max() + 1 + pad))
    return im.crop(box)


def _overlap(a, b):
    r = fitz.Rect(a)
    r.intersect(b)
    return 0.0 if r.is_empty else r.width * r.height


def _stamp_region(page, box):
    """把和印章重叠的图片、文字一起框进来，避免丢掉中间的签名或字。"""
    region = fitz.Rect(box)
    limit = region.get_area() * 4
    boxes = [fitz.Rect(i["bbox"]) for i in page.get_image_info()]
    for blk in page.get_text("dict").get("blocks", []):
        boxes.append(fitz.Rect(blk["bbox"]))
    for b in boxes:
        if b.is_empty or b.get_area() > limit:
            continue
        if _overlap(region, b) >= 0.25 * b.get_area():
            region.include_rect(b)
    pad = max(4.0, 0.03 * max(region.width, region.height))
    region = fitz.Rect(region.x0 - pad, region.y0 - pad,
                       region.x1 + pad, region.y1 + pad)
    region.intersect(page.rect)
    return region


def find_stamp_in_pdf(path, dpi=600):
    """定位页面里最像印章的图片，再按该区域整块渲染取章（保留重叠的签名、文字）。"""
    doc = fitz.open(path)
    try:
        best, best_score = None, -1.0
        for pno in range(min(len(doc), 40)):
            page = doc[pno]
            for info in page.get_image_info(xrefs=True):
                xref = info.get("xref") or 0
                if xref <= 0:
                    continue
                box = fitz.Rect(info["bbox"])
                if box.width < 60 or box.height < 60:
                    continue
                try:
                    raw = doc.extract_image(xref)
                    im = Image.open(io.BytesIO(raw["image"]))
                except Exception:
                    continue
                if im.width < 100 or im.height < 100:
                    continue
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGB")
                if not (0.55 < im.width / im.height < 1.8):
                    continue
                frac = red_ratio(im)
                if frac < 0.05:
                    continue
                score = frac * math.sqrt(im.width * im.height)
                if score > best_score:
                    best, best_score = (pno, box), score

        if best is None:
            return None
        pno, box = best
        page = doc[pno]
        region = _stamp_region(page, box)
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=region, alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return crop_alpha(to_rgba(im))
    finally:
        doc.close()


def parse_pages(text, total):
    text = (text or "").strip()
    if text in ("", "全部", "ALL", "all"):
        return list(range(1, total + 1))
    pages = set()
    for part in re.split(r"[,，;；\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            pages.update(range(a, b + 1))
        elif part.isdigit():
            pages.add(int(part))
        else:
            raise ValueError("页码格式不对：%s" % part)
    pages = {p for p in pages if 1 <= p <= total}
    if not pages:
        raise ValueError("页码超出范围")
    return sorted(pages)


class App:
    def __init__(self, root):
        self.root = root
        self.files = []          # (path, pages, status)
        self.stamp_base = None   # PIL RGBA, 满墨
        self.stamp_path = ""
        self.stamp_thumb = None
        self.preview_photo = None
        self.queue = queue.Queue()
        self.worker = None
        self.last_out_dir = ""

        root.title(APP_NAME)
        root.configure(bg=BG)
        root.geometry("1200x820")
        root.minsize(1100, 780)
        self._center()
        self._style()
        self._build()
        self._load_settings()
        self.root.after(100, self._poll)

    # ---------- 基础 ----------
    def _center(self):
        self.root.update_idletasks()
        w, h = 1200, 820
        x = (self.root.winfo_screenwidth() - w) // 2
        y = max(0, (self.root.winfo_screenheight() - h) // 2 - 20)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _style(self):
        st = ttk.Style()
        st.theme_use("clam")
        st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                     rowheight=30, borderwidth=0, font=(FONT, 9))
        st.configure("Treeview.Heading", background="#f7f9fc", foreground=MUTED,
                     font=(FONT, 9), relief="flat", borderwidth=0)
        st.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", TEXT)])
        st.configure("TCombobox", fieldbackground=CARD, background=CARD, arrowcolor=MUTED,
                     bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD, padding=4)
        st.configure("TSpinbox", fieldbackground=CARD, bordercolor=BORDER, lightcolor=CARD,
                     darkcolor=CARD, arrowcolor=MUTED, padding=4)
        st.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#eceff3",
                     bordercolor="#eceff3", lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
        st.configure("TScale", background=CARD, troughcolor="#eceff3")

    def card(self, parent, title):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", pady=(0, 14))
        tk.Label(wrap, text=title, bg=BG, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 7))
        body = tk.Frame(wrap, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        body.pack(fill="x")
        inner = tk.Frame(body, bg=CARD)
        inner.pack(fill="x", padx=16, pady=14)
        return inner

    def button(self, parent, text, cmd, kind="ghost", width=None):
        if kind == "primary":
            b = tk.Button(parent, text=text, command=cmd, bg=ACCENT, fg="#ffffff",
                          activebackground=ACCENT_DARK, activeforeground="#ffffff",
                          relief="flat", bd=0, font=(FONT, 10, "bold"),
                          padx=18, pady=8, cursor="hand2", disabledforeground="#e9c9c9")
        else:
            b = tk.Button(parent, text=text, command=cmd, bg="#f7f9fc", fg=TEXT,
                          activebackground="#eaeff6", activeforeground=TEXT,
                          relief="flat", bd=0, highlightbackground=BORDER, highlightthickness=1,
                          font=(FONT, 9), padx=12, pady=6, cursor="hand2")
        if width:
            b.configure(width=width)
        return b

    # ---------- 布局 ----------
    def _build(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=28, pady=(20, 12))
        tk.Label(header, text=APP_NAME, bg=BG, fg=TEXT, font=(FONT, 17, "bold")).pack(side="left")
        tk.Label(header, text="批量、逐页、保留原文字层", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side="left", padx=(12, 0), pady=(5, 0))

        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=28, pady=(0, 20))
        main.grid_columnconfigure(0, weight=0, minsize=368)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        left = tk.Frame(main, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        right = tk.Frame(main, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")

        self._build_stamp_card(left)
        self._build_param_card(left)
        self._build_files_card(left)
        self._build_right(right)

    def _build_stamp_card(self, parent):
        body = self.card(parent, "印章")
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x")
        self.btn_pdf = self.button(row, "从 PDF 取章", self.pick_stamp_pdf, "ghost")
        self.btn_pdf.pack(side="left")
        self.btn_img = self.button(row, "选图片", self.pick_stamp_image, "ghost")
        self.btn_img.pack(side="left", padx=(8, 0))

        info = tk.Frame(body, bg=CARD)
        info.pack(fill="x", pady=(12, 0))
        thumb_box = tk.Frame(info, width=90, height=90, bg="#fafbfc",
                             highlightbackground=BORDER, highlightthickness=1)
        thumb_box.pack(side="left")
        thumb_box.pack_propagate(False)
        self.thumb = tk.Label(thumb_box, bg="#fafbfc")
        self.thumb.pack(fill="both", expand=True)
        right = tk.Frame(info, bg=CARD)
        right.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.stamp_name = tk.Label(right, text="尚未选择印章", bg=CARD, fg=TEXT,
                                   font=(FONT, 9, "bold"), anchor="w", justify="left", wraplength=200)
        self.stamp_name.pack(anchor="w")
        self.stamp_hint = tk.Label(right, text="支持含电子章的 PDF，或 PNG/JPG 图片",
                                   bg=CARD, fg=MUTED, font=(FONT, 8), anchor="w",
                                   justify="left", wraplength=200)
        self.stamp_hint.pack(anchor="w", pady=(4, 0))

        op = tk.Frame(body, bg=CARD)
        op.pack(fill="x", pady=(12, 0))
        tk.Label(op, text="墨色浓度", bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")
        self.opacity = tk.DoubleVar(value=0.95)
        self.opacity_lbl = tk.Label(op, text="95%", bg=CARD, fg=TEXT, font=(FONT, 9), width=5, anchor="e")
        self.opacity_lbl.pack(side="right")
        scale = ttk.Scale(op, from_=0.35, to=1.0, variable=self.opacity,
                          command=lambda v: self._on_opacity())
        scale.pack(side="left", fill="x", expand=True, padx=(10, 8))

    def _build_param_card(self, parent):
        body = self.card(parent, "盖章参数")
        grid = tk.Frame(body, bg=CARD)
        grid.pack(fill="x")
        grid.grid_columnconfigure(1, weight=1)

        def label(text, r):
            tk.Label(grid, text=text, bg=CARD, fg=MUTED, font=(FONT, 9)).grid(
                row=r, column=0, sticky="w", pady=5)

        label("章径 (mm)", 0)
        self.size_var = tk.StringVar(value="40")
        ttk.Spinbox(grid, from_=15, to=80, textvariable=self.size_var, width=8).grid(
            row=0, column=1, sticky="w", padx=(14, 0), pady=5)

        label("边距 (mm)", 1)
        self.margin_var = tk.StringVar(value="8")
        ttk.Spinbox(grid, from_=0, to=40, textvariable=self.margin_var, width=8).grid(
            row=1, column=1, sticky="w", padx=(14, 0), pady=5)

        label("位置", 2)
        self.corner_var = tk.StringVar(value="右下")
        ttk.Combobox(grid, textvariable=self.corner_var, values=POSITIONS,
                     state="readonly", width=8).grid(row=2, column=1, sticky="w", padx=(14, 0), pady=5)

        label("X / Y (mm)", 3)
        xy = tk.Frame(grid, bg=CARD)
        xy.grid(row=3, column=1, sticky="w", padx=(14, 0), pady=5)
        self.cx_var = tk.StringVar(value="0")
        self.cy_var = tk.StringVar(value="0")
        self.cx_spin = ttk.Spinbox(xy, from_=0, to=500, increment=1,
                                   textvariable=self.cx_var, width=6)
        self.cx_spin.pack(side="left")
        tk.Label(xy, text="/", bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=6)
        self.cy_spin = ttk.Spinbox(xy, from_=0, to=500, increment=1,
                                   textvariable=self.cy_var, width=6)
        self.cy_spin.pack(side="left")
        self.xy_hint = tk.Label(xy, text="距页面左上角", bg=CARD, fg=MUTED, font=(FONT, 8))
        self.xy_hint.pack(side="left", padx=(8, 0))

        label("输出后缀", 4)
        self.suffix_var = tk.StringVar(value="-已盖章")
        tk.Entry(grid, textvariable=self.suffix_var, bg=CARD, fg=TEXT, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, font=(FONT, 9)).grid(
            row=4, column=1, sticky="ew", padx=(14, 0), pady=5, ipady=4)

        label("页码", 5)
        self.pages_var = tk.StringVar(value="全部")
        tk.Entry(grid, textvariable=self.pages_var, bg=CARD, fg=TEXT, relief="flat",
                 highlightbackground=BORDER, highlightthickness=1, font=(FONT, 9)).grid(
            row=5, column=1, sticky="ew", padx=(14, 0), pady=5, ipady=4)

        for var in (self.size_var, self.margin_var, self.corner_var,
                    self.cx_var, self.cy_var):
            var.trace_add("write", lambda *a: self.refresh_preview())
        self.corner_var.trace_add("write", lambda *a: self._on_corner())
        self._on_corner()

    def _on_corner(self):
        custom = self.corner_var.get() == "自定义"
        st = "normal" if custom else "disabled"
        self.cx_spin.configure(state=st)
        self.cy_spin.configure(state=st)
        self.xy_hint.configure(text="距页面左上角" if custom else "选「自定义」后可调")

    def _custom_mm(self):
        try:
            cx, cy = float(self.cx_var.get()), float(self.cy_var.get())
        except ValueError:
            raise ValueError("X / Y 坐标要填数字（毫米）。")
        if not (0 <= cx <= 2000 and 0 <= cy <= 2000):
            raise ValueError("X / Y 坐标超出范围（0-2000 mm）。")
        return cx, cy

    def _build_files_card(self, parent):
        body = self.card(parent, "待盖章文件")
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x")
        self.button(row, "+ 添加 PDF", self.add_files, "ghost").pack(side="left")
        self.button(row, "+ 添加文件夹", self.add_folder, "ghost").pack(side="left", padx=(8, 0))
        self.button(row, "清空", self.clear_files, "ghost").pack(side="right")
        self.button(row, "移除选中", self.remove_selected, "ghost").pack(side="right", padx=(0, 6))
        self.count_lbl = tk.Label(body, text="0 个文件", bg=CARD, fg=MUTED, font=(FONT, 8))
        self.count_lbl.pack(anchor="w", pady=(10, 0))

    def _build_right(self, parent):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(side="bottom", fill="x", pady=(12, 0))
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 8))
        line = tk.Frame(bar, bg=BG)
        line.pack(fill="x")
        self.status = tk.Label(line, text="就绪", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.status.pack(side="left")
        self.btn_open = self.button(line, "打开输出目录", self.open_out_dir, "ghost")
        self.btn_open.pack(side="right", padx=(8, 0))
        self.btn_run = self.button(line, "开始盖章", self.start, "primary")
        self.btn_run.pack(side="right")

        list_wrap = tk.Frame(parent, bg=BG)
        list_wrap.pack(side="bottom", fill="x", pady=(12, 0))
        tk.Label(list_wrap, text="文件列表", bg=BG, fg=MUTED,
                 font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 7))
        tbl = tk.Frame(list_wrap, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        tbl.pack(fill="x")
        self.tree = ttk.Treeview(tbl, columns=("name", "pages", "status"), show="headings",
                                 height=3, selectmode="browse")
        self.tree.heading("name", text="文件")
        self.tree.heading("pages", text="页数")
        self.tree.heading("status", text="状态")
        self.tree.column("name", anchor="w", width=520)
        self.tree.column("pages", anchor="center", width=70, stretch=False)
        self.tree.column("status", anchor="center", width=120, stretch=False)
        sb = ttk.Scrollbar(tbl, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.refresh_preview())

        prev_wrap = tk.Frame(parent, bg=BG)
        prev_wrap.pack(side="top", fill="both", expand=True)
        head = tk.Frame(prev_wrap, bg=BG)
        head.pack(fill="x", pady=(0, 7))
        tk.Label(head, text="预览", bg=BG, fg=MUTED, font=(FONT, 9, "bold")).pack(side="left")
        self.preview_hint = tk.Label(head, text="", bg=BG, fg=MUTED, font=(FONT, 8))
        self.preview_hint.pack(side="right")

        box = tk.Frame(prev_wrap, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="both", expand=True)
        self.preview = tk.Label(box, bg="#eef2f7", text="选择文件后在这里预览盖章效果",
                                fg=MUTED, font=(FONT, 10))
        self.preview.pack(fill="both", expand=True, padx=12, pady=12)

    # ---------- 印章 ----------
    def pick_stamp_pdf(self):
        path = filedialog.askopenfilename(title="选择含印章的 PDF", filetypes=[("PDF", "*.pdf")])
        if not path:
            return
        try:
            im = find_stamp_in_pdf(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, "读取失败：\n%s" % e)
            return
        if im is None:
            messagebox.showwarning(APP_NAME, "这个 PDF 里没找到像印章的图片，请改用图片文件（PNG 背景透明最佳）。")
            return
        self._set_stamp(im, path, "自动提取")

    def pick_stamp_image(self):
        path = filedialog.askopenfilename(
            title="选择印章图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            im = Image.open(path)
            im.load()
        except Exception as e:
            messagebox.showerror(APP_NAME, "图片打不开：\n%s" % e)
            return
        self._set_stamp(im, path, "图片")

    def _set_stamp(self, im, path, how):
        base = to_rgba(im, opacity=1.0)
        base = crop_alpha(base)
        if base.width < 40 or base.height < 40:
            messagebox.showwarning(APP_NAME, "印章图太小，换一张清晰点的。")
            return
        self.stamp_base = base
        self.stamp_path = path
        self.stamp_name.configure(text=os.path.basename(path))
        self.stamp_hint.configure(text="%s · %d×%d px" % (how, base.width, base.height))
        thumb = base.copy()
        thumb.thumbnail((78, 78), Image.LANCZOS)
        canvas = Image.new("RGBA", (84, 84), (250, 251, 252, 255))
        canvas.alpha_composite(thumb, ((84 - thumb.width) // 2, (84 - thumb.height) // 2))
        self.stamp_thumb = ImageTk.PhotoImage(canvas)
        self.thumb.configure(image=self.stamp_thumb, text="")
        self.refresh_preview()

    def _on_opacity(self):
        self.opacity_lbl.configure(text="%d%%" % round(self.opacity.get() * 100))
        self.refresh_preview()

    # ---------- 文件 ----------
    def add_files(self):
        paths = filedialog.askopenfilenames(title="选择 PDF（可多选）", filetypes=[("PDF", "*.pdf")])
        self._add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择文件夹（含 PDF）")
        if not folder:
            return
        paths = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                 if f.lower().endswith(".pdf")]
        self._add_paths(paths)

    def _add_paths(self, paths):
        have = {p for p, _, _ in self.files}
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p in have or self.stamp_path == p:
                continue
            try:
                doc = fitz.open(p)
                n = doc.page_count
                doc.close()
            except Exception:
                continue
            self.files.append((p, n, "待处理"))
            have.add(p)
            added += 1
        self._render_tree()
        if added:
            self.refresh_preview()

    def clear_files(self):
        self.files = []
        self._render_tree()
        self.refresh_preview()

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(self.tree.item(sel[0], "tags")[0])
        self.files.pop(idx)
        self._render_tree()
        self.refresh_preview()

    def _render_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, (p, n, st) in enumerate(self.files):
            tag = OK if st.startswith("完成") else (ERR if st.startswith("失败") else TEXT)
            self.tree.insert("", "end", values=(os.path.basename(p), n, st), tags=(str(i), tag))
        self.tree.tag_configure(OK, foreground=OK)
        self.tree.tag_configure(ERR, foreground=ERR)
        self.count_lbl.configure(text="%d 个文件" % len(self.files))

    # ---------- 预览 ----------
    def _current_file(self):
        sel = self.tree.selection()
        if sel:
            return self.files[int(self.tree.item(sel[0], "tags")[0])][0]
        return self.files[0][0] if self.files else None

    def refresh_preview(self):
        path = self._current_file()
        if not path:
            self.preview.configure(image="", text="选择文件后在这里预览盖章效果")
            self.preview_hint.configure(text="")
            return
        if self.stamp_base is None:
            self.preview.configure(image="", text="先选择印章（从 PDF 提取或选图片）")
            self.preview_hint.configure(text="")
            return
        try:
            doc = fitz.open(path)
            total = doc.page_count
            pages = parse_pages(self.pages_var.get(), total)
            pno = pages[0] - 1
            page = doc[pno]
            box_w = 470.0
            zoom = min(1.6, box_w / page.rect.width)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            bg_img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            doc.close()
        except Exception as e:
            self.preview.configure(image="", text="预览失败：%s" % e)
            return

        try:
            size = mm2pt(float(self.size_var.get()))
            margin = mm2pt(float(self.margin_var.get()))
            cx_mm, cy_mm = self._custom_mm()
        except ValueError:
            return
        corner = self.corner_var.get()
        w = int(round(size * zoom))
        h = int(round(size * zoom * self.stamp_base.height / self.stamp_base.width))
        x, y = place_rect(bg_img.width, bg_img.height, w, h, corner,
                          int(round(margin * zoom)),
                          mm2pt(cx_mm) * zoom, mm2pt(cy_mm) * zoom)
        x, y = int(round(x)), int(round(y))

        ink = self.stamp_base.copy()
        alpha = ink.getchannel("A").point(lambda v: int(v * self.opacity.get()))
        ink.putalpha(alpha)
        ink = ink.resize((max(1, w), max(1, h)), Image.LANCZOS)
        canvas = bg_img.convert("RGBA")
        canvas.alpha_composite(ink, (x, y))

        canvas.thumbnail((470, 430), Image.LANCZOS)
        self.preview_photo = ImageTk.PhotoImage(canvas.convert("RGB"))
        self.preview.configure(image=self.preview_photo, text="")
        self.preview_hint.configure(text="第 %d 页 / 共 %d 页" % (pno + 1, total))

    # ---------- 执行 ----------
    def _validate(self):
        if not self.files:
            messagebox.showwarning(APP_NAME, "先添加要盖章的 PDF。")
            return False
        if self.stamp_base is None:
            messagebox.showwarning(APP_NAME, "先选择印章。")
            return False
        try:
            size = float(self.size_var.get())
            margin = float(self.margin_var.get())
            if not (10 <= size <= 100) or not (0 <= margin <= 60):
                raise ValueError
            cx_mm, cy_mm = self._custom_mm()
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e) if str(e) else "章径或边距数值不对。")
            return False
        if not self.suffix_var.get().strip():
            messagebox.showwarning(APP_NAME, "输出后缀不能为空，否则会覆盖原文件。")
            return False
        return True

    def start(self):
        if not self._validate():
            return
        self._save_settings()
        jobs = []
        try:
            for p, n, st in self.files:
                jobs.append((p, parse_pages(self.pages_var.get(), n)))
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return

        self.btn_run.configure(state="disabled", text="正在盖章…")
        self.progress.configure(value=0)
        self.status.configure(text="准备中…", fg=MUTED)
        for i, (p, _, _) in enumerate(self.files):
            self._set_status(i, "处理中…")
        self._render_tree()

        ink = self.stamp_base
        alpha = ink.getchannel("A").point(lambda v: int(v * self.opacity.get()))
        ink = ink.copy()
        ink.putalpha(alpha)
        buf = io.BytesIO()
        ink.save(buf, "PNG")
        data = buf.getvalue()

        params = dict(size=mm2pt(float(self.size_var.get())),
                      margin=mm2pt(float(self.margin_var.get())),
                      corner=self.corner_var.get(),
                      cx=mm2pt(float(self.cx_var.get())),
                      cy=mm2pt(float(self.cy_var.get())),
                      suffix=self.suffix_var.get().strip(),
                      data=data)

        self.total_pages = sum(len(pg) for _, pg in jobs)
        self.done_pages = 0
        t = threading.Thread(target=self._work, args=(jobs, params), daemon=True)
        t.start()

    def _set_status(self, idx, text):
        p, n, _ = self.files[idx]
        self.files[idx] = (p, n, text)

    def _work(self, jobs, params):
        for i, (src, pages) in enumerate(jobs):
            try:
                out = self._output_path(src, params["suffix"])
                doc = fitz.open(src)
                size, margin = params["size"], params["margin"]
                corner = params["corner"]
                for pno in pages:
                    page = doc[pno - 1]
                    r = page.rect
                    w = size
                    h = size * self.stamp_base.height / self.stamp_base.width
                    x, y = place_rect(r.width, r.height, w, h, corner, margin,
                                      params["cx"], params["cy"])
                    rect = fitz.Rect(x, y, x + w, y + h)
                    page.insert_image(rect, stream=params["data"], overlay=True)
                    self.queue.put(("tick", 1))
                tmp = out + ".part"
                doc.save(tmp, garbage=3, deflate=True)
                doc.close()
                os.replace(tmp, out)
                self.queue.put(("ok", i, out))
            except Exception as e:
                self.queue.put(("err", i, str(e)))
        self.queue.put(("done",))

    def _output_path(self, src, suffix):
        folder = os.path.dirname(src)
        stem = os.path.splitext(os.path.basename(src))[0]
        cand = os.path.join(folder, stem + suffix + ".pdf")
        k = 2
        while os.path.exists(cand) and os.path.abspath(cand) != os.path.abspath(src):
            cand = os.path.join(folder, "%s%s (%d).pdf" % (stem, suffix, k))
            k += 1
        return cand

    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "tick":
                    self.done_pages += msg[1]
                    pct = self.done_pages / max(1, self.total_pages) * 100
                    self.progress.configure(value=pct)
                    self.status.configure(text="正在盖章  %d / %d 页" % (self.done_pages, self.total_pages))
                elif kind == "ok":
                    self._set_status(msg[1], "完成")
                    self.last_out_dir = os.path.dirname(msg[2])
                    self._render_tree()
                elif kind == "err":
                    self._set_status(msg[1], "失败")
                    self.status.configure(text="有文件处理失败：%s" % msg[2], fg=ERR)
                    self._render_tree()
                elif kind == "done":
                    self.btn_run.configure(state="normal", text="开始盖章")
                    if str(self.status.cget("fg")) != ERR:
                        self.status.configure(text="完成，文件已保存到原目录", fg=OK)
                    self.progress.configure(value=100)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def open_out_dir(self):
        folder = self.last_out_dir
        if not folder and self.files:
            folder = os.path.dirname(self.files[0][0])
        if folder and os.path.isdir(folder):
            os.startfile(folder)

    # ---------- 配置 ----------
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.size_var.set(str(s.get("size", 40)))
            self.margin_var.set(str(s.get("margin", 8)))
            self.corner_var.set(s.get("corner", "右下"))
            self.cx_var.set(str(s.get("cx", 0)))
            self.cy_var.set(str(s.get("cy", 0)))
            self.suffix_var.set(s.get("suffix", "-已盖章"))
            self.opacity.set(float(s.get("opacity", 0.95)))
            self.opacity_lbl.configure(text="%d%%" % round(self.opacity.get() * 100))
        except Exception:
            pass

    def _save_settings(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(dict(size=self.size_var.get(), margin=self.margin_var.get(),
                               corner=self.corner_var.get(), cx=self.cx_var.get(),
                               cy=self.cy_var.get(), suffix=self.suffix_var.get(),
                               opacity=round(self.opacity.get(), 2)), f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
