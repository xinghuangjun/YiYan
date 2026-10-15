# -*- coding: utf-8 -*-
"""
忆言 —— 一个零依赖（只用 Python 标准库 + tkinter）的图形界面聊天客户端。

支持任何“OpenAI 兼容”接口的服务，例如：
  OpenAI、DeepSeek、Kimi(月之暗面)、通义千问、智谱 GLM、硅基流动、
  Ollama、LM Studio、vLLM、one-api / new-api 等中转站。

用法：
    python llm_chat.py              打开图形界面
    python llm_chat.py --selftest   不打开界面，自测网络模块（本地模拟服务）

配置保存在： 用户主目录/.llm_gui_chat.json
"""

from __future__ import annotations

import base64
import json
import queue
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import ctypes
import struct
import tkinter as tk
import zlib
from ctypes import wintypes
from tkinter import filedialog, font as tkfont, messagebox, ttk

# Win32 句柄（本程序面向 Windows：屏幕识别、文件拖放都要用）
u32 = ctypes.windll.user32
g32 = ctypes.windll.gdi32

APP_TITLE = "忆言"
APP_VERSION = "1.1"
PORTABLE_CONFIG_NAME = "llm_chat_config.json"   # 便携版的配置文件名
NORMAL_CONFIG_NAME = ".llm_gui_chat.json"       # 源码运行时的默认配置文件


def _base_dir() -> Path:
    """程序所在目录（打包后 = exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except Exception:
            pass
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def portable_mode() -> bool:
    """是否便携模式（配置放程序目录、不碰系统用户目录）。

    * 打包后的 exe：**始终**便携，配置就放在 exe 旁边，换台电脑插上就能用，
      也不会读到那台电脑上已有的设置。
    * 源码运行：程序目录里放一个 `portable.txt` 就同样走便携模式；
      没放则沿用用户主目录（老行为）。
    """
    base = _base_dir()
    return bool(getattr(sys, "frozen", False) or (base / "portable.txt").exists())


def default_config_path() -> Path:
    """按运行方式决定配置文件位置。"""
    base = _base_dir()
    if portable_mode():
        return base / PORTABLE_CONFIG_NAME
    return Path.home() / NORMAL_CONFIG_NAME


CONFIG_PATH = default_config_path()

# 中文字体：按优先级挑一个系统里存在的（Windows / macOS / Linux 都能有中文字形）
FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "WenQuanYi Micro Hei",
    "SimHei", "Arial Unicode MS",
)
FONT = "Microsoft YaHei UI"


def pick_font(root):
    try:
        from tkinter import font as tkfont
        have = set(tkfont.families(root))
    except Exception:
        return FONT
    for name in FONT_CANDIDATES:
        if name in have:
            return name
    return "TkDefaultFont"

# 常见服务商：名称 / Base URL / 模型示例
# 视觉模型（VLM）预设：只保留有视觉能力的服务与模型。
# 注意：DeepSeek 目前没有视觉模型，因此**不在**这个列表里（语言模型列表里保留）。
VLM_PRESETS = [
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("Kimi 视觉", "https://api.moonshot.cn/v1", "moonshot-v1-8k-vision-preview"),
    ("通义千问 VL", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-vl-plus"),
    ("智谱 GLM-4V", "https://open.bigmodel.cn/api/paas/v4", "glm-4v-flash"),
    ("硅基流动 VL", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-VL-7B-Instruct"),
    ("Ollama 本地", "http://localhost:11434/v1", "llava:7b"),
    ("LM Studio 本地", "http://localhost:1234/v1", "local-model"),
]

# 支持的图片后缀（对话框与拖拽用）
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
MAX_IMAGE_MB = 15.0     # 单张图片上限，超过就提示压缩

# 视觉模型要回答的问题
VLM_PROMPT = (
    "请用中文详细描述这张图片的全部可见内容，包括：画面主体与场景、人物或物体、"
    "其中的文字/数字/表格/代码、界面元素与布局、颜色与风格。"
    "只输出描述本身，不要问好、不要提问、不要加前后缀。"
)


def image_mime(path) -> str:
    """按文件头判断图片真实格式（不少文件后缀与内容并不一致）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return "image/jpeg"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def image_data_url(path) -> str:
    """把本地图片读成 data URL（MIME 按真实格式给，避免服务端解不出来）。"""
    data = Path(path).read_bytes()
    return "data:%s;base64,%s" % (image_mime(path), base64.b64encode(data).decode("ascii"))

# Windows 文件拖放：窗口过程里只做纯 ctypes 解析并入队，
# 由 Tk 侧定时轮询取出（避免在窗口过程里回调 Tcl，防止重入崩溃）。
_DROP_PROCS = []          # 回调对象必须一直被引用，否则会被回收
_PENDING_DROPS = []       # 已接收、等待交给界面的文件列表


def enable_file_drop(widget, callback):
    """把 tk 控件变成 Windows 文件拖放目标（纯 ctypes，不需要第三方库）。

    返回 True 表示启用成功；失败只是没有拖放功能，不影响其它功能。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
        hwnd = int(widget.winfo_id())
        WM_DROPFILES = 0x0233
        GWLP_WNDPROC = -4
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                     ctypes.c_size_t, ctypes.c_ssize_t)
        try:
            get_long, set_long = user32.GetWindowLongPtrW, user32.SetWindowLongPtrW
        except AttributeError:
            get_long, set_long = user32.GetWindowLongW, user32.SetWindowLongW
        get_long.restype = ctypes.c_void_p
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        set_long.restype = ctypes.c_void_p
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = LRESULT
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                           ctypes.c_uint, ctypes.c_size_t,
                                           ctypes.c_ssize_t]
        shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                           ctypes.c_wchar_p, ctypes.c_uint]
        shell32.DragFinish.argtypes = [ctypes.c_void_p]
        shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

        old_proc = get_long(hwnd, GWLP_WNDPROC)
        if not old_proc:
            return False

        def proc(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    hdrop = ctypes.c_void_p(wparam)
                    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
                    names = []
                    for i in range(min(int(count), 64)):
                        buf = ctypes.create_unicode_buffer(2048)
                        if shell32.DragQueryFileW(hdrop, i, buf, 2048):
                            names.append(buf.value)
                    if names:
                        _PENDING_DROPS.append(names)      # 纯 Python，不碰 Tcl
                except Exception:
                    pass
                try:
                    shell32.DragFinish(ctypes.c_void_p(wparam))
                except Exception:
                    pass
                return 0
            try:
                return user32.CallWindowProcW(ctypes.c_void_p(old_proc), h, msg,
                                              wparam, lparam)
            except Exception:
                return 0

        new_proc = WNDPROC(proc)
        set_long(hwnd, GWLP_WNDPROC, ctypes.cast(new_proc, ctypes.c_void_p))
        shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)   # 注意：在 shell32 里
        _DROP_PROCS.append(new_proc)

        def poll():
            try:
                while _PENDING_DROPS:
                    callback(_PENDING_DROPS.pop(0))
            except Exception:
                pass
            try:
                widget.after(300, poll)
            except Exception:
                pass

        widget.after(300, poll)
        return True
    except Exception:
        return False


PRESETS = [
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("Kimi 月之暗面", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    ("通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("硅基流动", "https://api.siliconflow.cn/v1", "Qwen/Qwen2.5-7B-Instruct"),
    ("Ollama 本地", "http://localhost:11434/v1", "qwen2.5:7b"),
    ("LM Studio 本地", "http://localhost:1234/v1", "local-model"),
]

# 记忆优化（精简记忆）的默认值
MEM_X_DEFAULT = 3                   # 单次精简几条记忆（设置里的 x）
MEM_Y_DEFAULT = 6                   # 最多保留几条带 D 的记忆（设置里的 y）

DEFAULT_CONFIG = {
    "base_url": "",
    "api_key": "",
    "model": "",
    "system_prompt": "你是一个乐于助人的中文 AI 助手，回答简洁准确。",
    "temperature": "0.7",
    "stream": True,
    "hide_reasoning": False,
    "timeout": "120",
    # 「设置 → 对话总结」里的三项，用户可自己填；这里是默认值
    "vlm_base_url": "",          # 视觉模型接口（可选，留空则不支持发图）
    "vlm_api_key": "",
    "vlm_model": "",
    "mem_base_url": "",          # 记忆优化模型（精简记忆用）
    "mem_api_key": "",
    "mem_model": "",
    "mem_k": "0.5",              # W 的时间权重 k（0~1）
    "mem_x": MEM_X_DEFAULT,      # 单次精简的记忆条数 x
    "mem_y": MEM_Y_DEFAULT,      # 最多存在的记忆条数 y
    "screen_watch": False,       # 屏幕识别：是否启用
    "screen_interval": 30,       # 屏幕识别：每隔多少秒截一次
    "screen_target": {"kind": "screen"},   # 截取目标：整个屏幕 或 某个窗口
    "screen_half": True,         # 截图宽高减半（省 token）
    "review_rounds": 10,         # 每多少次聊天让模型复习一次历史总结
    "summary_rounds": 10,        # 每多少轮对话生成一次总结文件
    "summary_max_files": 10,     # 最多同时存在多少个总结文件
    "summary_max_chars": 2000,   # 单个总结文件的字数上限
}

INPUT_PLACEHOLDER = ("在这里输入你的问题，按 Enter 发送，Shift+Enter 换行；"
                     "图片可直接拖进这里")

# --------------------------------------------------------------------------- #
#  思考内容识别：兼容「reasoning_content 字段」与「正文里的 <think>…</think> 标签」
# --------------------------------------------------------------------------- #
THINK_TAG_NAMES = ("think", "thinking", "thought", "reasoning", "reason", "cot",
                   "analysis", "reflection")
_THINK_ALT = "|".join(THINK_TAG_NAMES)
_OPEN_TAG_RE = re.compile(r"<\s*\\?\s*(?:%s)\s*\\?\s*>" % _THINK_ALT, re.IGNORECASE)
_CLOSE_TAG_RE = re.compile(r"<\s*/\s*\\?\s*(?:%s)\s*\\?\s*>" % _THINK_ALT, re.IGNORECASE)
_PARTIAL_TAG_RE = re.compile(r"^<\s*/?\s*\\?[A-Za-z_]{0,20}\s*$")


class ThinkTagSplitter:
    """把模型输出切成「思考」与「正文」两部分。

    用法：不断 feed() 增量文本，收到 (kind, text)；流结束时再 flush()。
    kind 为 'reasoning'（思考）或 'content'（正文）。
    标签被流式分片切断（例如先来 '<thi' 再来 'nk>'）也能正确识别。
    """

    def __init__(self):
        self.buf = ""
        self.in_think = False

    def _hold_len(self):
        """结尾若是「还没写完的标签」，返回需要暂存的字符数（否则返回 0）。"""
        seg = self.buf[-40:]
        i = seg.rfind("<")
        if i == -1:
            return 0
        seg = seg[i:]
        if ">" in seg:
            return 0
        return len(seg) if _PARTIAL_TAG_RE.match(seg) else 0

    def feed(self, chunk):
        """喂入增量文本，返回 [(kind, text), ...]。"""
        self.buf += chunk
        out = []
        while self.buf:
            regex = _CLOSE_TAG_RE if self.in_think else _OPEN_TAG_RE
            m = regex.search(self.buf)
            if m:
                if m.start() > 0:
                    out.append(("reasoning" if self.in_think else "content", self.buf[:m.start()]))
                self.buf = self.buf[m.end():]
                self.in_think = not self.in_think
                continue
            hold = self._hold_len()
            if hold and hold < len(self.buf):
                out.append(("reasoning" if self.in_think else "content", self.buf[:-hold]))
                self.buf = self.buf[-hold:]
            elif not hold:
                out.append(("reasoning" if self.in_think else "content", self.buf))
                self.buf = ""
            break
        return [(k, t) for k, t in out if t]

    def flush(self):
        """流结束时调用：残留内容按当前状态归类（没闭合的思考仍算思考）。"""
        kind = "reasoning" if self.in_think else "content"
        rest, self.buf = self.buf, ""
        self.in_think = False
        return [(kind, rest)] if rest else []


def split_think_text(text):
    """一次性切分整段文本，返回 (正文, 思考内容)。"""
    sp = ThinkTagSplitter()
    pieces = sp.feed(text or "") + sp.flush()
    body = "".join(t for k, t in pieces if k == "content")
    mind = "".join(t for k, t in pieces if k == "reasoning")
    return body, mind


def _emit(splitter, kind, text):
    """把一段增量文本按「思考 / 正文」归类后产出。"""
    if not text:
        return []
    if kind == "reasoning":
        return [("reasoning", text)]      # 结构化思考字段，原样归类
    return splitter.feed(text)            # 正文里可能内嵌 <think> 标签





# --------------------------------------------------------------------------- #
#  配置读写
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  现代风格：配色与自绘控件（纯 tkinter，无需第三方库）
# --------------------------------------------------------------------------- #
class C:
    """配色表。"""

    BG = "#F4F5F7"           # 应用背景
    SURFACE = "#FFFFFF"      # 卡片
    BORDER = "#E4E7EC"       # 细边框
    TEXT = "#131720"         # 主要文字
    TEXT_2 = "#5A6472"       # 次要文字
    TEXT_3 = "#8B94A3"       # 提示文字
    ACCENT = "#2F6BFF"       # 主题色
    ACCENT_D = "#1F55DB"     # 主题色（悬停）
    ACCENT_SOFT = "#EAF0FF"  # 主题色浅底
    GREEN = "#16A34A"
    RED = "#DC2626"
    RED_SOFT = "#FDECEC"
    GRAY_SOFT = "#F2F4F7"
    GRAY_HOVER = "#E9ECF1"
    THUMB = "#CDD3DC"


class RoundedButton(tk.Canvas):
    """圆角扁平按钮（hover / 按下 / 禁用；兼容 ttk 的 configure(state=) / cget(state)）。"""

    VARIANTS = {
        "primary": dict(fill=C.ACCENT, hover=C.ACCENT_D, fg="#FFFFFF", border="", bold=True),
        "soft": dict(fill=C.GRAY_SOFT, hover=C.GRAY_HOVER, fg=C.TEXT, border="", bold=False),
        "outline": dict(fill=C.SURFACE, hover=C.ACCENT_SOFT, fg=C.ACCENT,
                        border=C.ACCENT, bold=True),
        "chip": dict(fill=C.SURFACE, hover=C.ACCENT_SOFT, fg=C.TEXT_2,
                     border=C.BORDER, bold=False),
        "ghost": dict(fill="", hover=C.GRAY_SOFT, fg=C.TEXT_2, border="", bold=False),
    }

    def __init__(self, parent, text="", command=None, variant="soft", height=34,
                 radius=9, font_size=10, bg=None, padx=16, width=None):
        self._style = dict(self.VARIANTS.get(variant, self.VARIANTS["soft"]))
        self._text = text
        self._command = command
        self._state = "normal"
        self._hover = False
        self._pressed = False
        try:
            bg = bg if bg is not None else parent.cget("background")
        except Exception:
            bg = C.SURFACE
        self._bg = bg
        self._font = tkfont.Font(family=FONT, size=font_size,
                                 weight="bold" if self._style["bold"] else "normal")
        w = width or max(60, self._font.measure(text) + padx * 2)
        super().__init__(parent, width=w, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0)
        self._radius = radius
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._draw()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self.set_state(kw.pop("state"))
        if "text" in kw:
            self._text = kw.pop("text")
            self._draw()
        if "command" in kw:
            self._command = kw.pop("command")
        if kw:
            try:
                super().configure(**kw)
            except Exception:
                pass
        if cnf:
            try:
                super().configure(cnf)
            except Exception:
                pass

    config = configure

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def set_state(self, state):
        self._state = "disabled" if str(state) == "disabled" else "normal"
        self._draw()

    def set_text(self, text):
        self._text = text
        self._draw()

    def _rounded(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self):
        self.delete("all")
        st = self._style
        h, w = int(self.cget("height")), int(self.cget("width"))
        if self._state == "disabled":
            fill, fg = C.GRAY_SOFT, C.TEXT_3
        else:
            fill = st["hover"] if (self._pressed or self._hover) else st["fill"]
            fg = st["fg"]
        if fill:
            self._rounded(1, 1, w - 1, h - 1, self._radius, fill=fill, outline="")
        if st["border"] and self._state != "disabled":
            self._rounded(1, 1, w - 1, h - 1, self._radius, fill="", outline=st["border"])
        self.create_text(w / 2, h / 2 + 1, text=self._text, fill=fg, font=self._font)

    def _on_enter(self, _e):
        self._hover = True
        if self._state != "disabled":
            self.configure(cursor="hand2")
        self._draw()

    def _on_leave(self, _e):
        self._hover = self._pressed = False
        self.configure(cursor="")
        self._draw()

    def _on_press(self, _e):
        if self._state == "disabled":
            return
        self._pressed = True
        self._draw()

    def _on_release(self, _e):
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._state != "disabled" and self._command:
            self._command()


class CheckBox(tk.Canvas):
    """扁平圆角复选框（自绘；直接绑定 BooleanVar）。"""

    def __init__(self, parent, text="", variable=None, command=None,
                 bg=C.SURFACE, font_size=10):
        self._var = variable if variable is not None else tk.BooleanVar()
        self._text = text
        self._command = command
        self._font = tkfont.Font(family=FONT, size=font_size)
        self._hover = False
        w = 26 + self._font.measure(text) + 6
        h = max(24, self._font.metrics("linespace") + 8)
        super().__init__(parent, width=w, height=h, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        try:
            self._var.trace_add("write", lambda *a: self._draw())
        except Exception:
            pass
        self._draw()

    def cget(self, key):
        if key == "text":
            return self._text
        return super().cget(key)

    def _set_hover(self, on):
        self._hover = on
        self.configure(cursor="hand2" if on else "")
        self._draw()

    def _toggle(self, _e=None):
        self._var.set(not bool(self._var.get()))
        self._draw()
        if self._command:
            self._command()

    def _rounded(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self):
        self.delete("all")
        on = bool(self._var.get())
        h = int(self.cget("height"))
        box, y0 = 17, (int(self.cget("height")) - 17) / 2
        fill = C.ACCENT if on else (C.GRAY_SOFT if self._hover else C.SURFACE)
        outline = C.ACCENT if on else (C.TEXT_3 if self._hover else C.BORDER)
        self._rounded(1, y0, 1 + box, y0 + box, 5, fill=fill, outline=outline)
        if on:
            self.create_line(5.5, y0 + 8.5, 8.5, y0 + 11.5, 12.5, y0 + 5,
                             fill="#FFFFFF", width=2, capstyle="round",
                             joinstyle="round")
        self.create_text(box + 10, h / 2, anchor="w", text=self._text,
                         fill=C.TEXT if (on or self._hover) else C.TEXT_2,
                         font=self._font)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in DEFAULT_CONFIG:
                    if k in data and data[k] is not None:
                        cfg[k] = data[k]
    except Exception:
        pass  # 配置损坏时忽略，用默认值启动
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            CONFIG_PATH.chmod(0o600)  # 尽量限制权限（Windows 上无效也无妨）
        except Exception:
            pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  HTTP / OpenAI 兼容接口
# --------------------------------------------------------------------------- #
class LLMError(Exception):
    """带友好提示的异常。"""

    def __init__(self, message: str, status=None):
        super().__init__(message)
        self.status = status


def normalize_base_url(url: str) -> str:
    """把用户填的地址规范化成 Base URL（去掉结尾斜杠和 /chat/completions）。"""
    u = (url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "http://" + u          # 方便直接填 localhost:11434
    u = u.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u.rstrip("/")


def endpoint_candidates(url: str):
    base = normalize_base_url(url)
    if not base:
        return []
    cands = [base + "/chat/completions"]
    if "/v1" not in base and "/v4" not in base:
        cands.append(base + "/v1/chat/completions")   # 用户可能漏了 /v1
    return cands


def models_candidates(url: str):
    base = normalize_base_url(url)
    if not base:
        return []
    cands = [base + "/models"]
    if "/v1" not in base and "/v4" not in base:
        cands.append(base + "/v1/models")
    return cands


def is_local_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        u.startswith(p)
        for p in ("http://localhost", "http://127.", "http://0.0.0.0", "http://[::1]")
    )


def build_headers(api_key: str, url: str = "") -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Yiyan/%s (tkinter; python)" % APP_VERSION,
    }
    key = (api_key or "").strip()
    if key:
        if "openai.azure.com" in (url or "").lower():
            headers["api-key"] = key
        else:
            headers["Authorization"] = "Bearer " + key
    return headers


HTTP_HINTS = {
    400: "请求被拒绝（可能是参数或模型名不对）。",
    401: "鉴权失败：API Key 不正确、已过期或没有填写。",
    402: "账户余额不足。",
    403: "没有权限访问该模型或该接口。",
    404: "接口路径不存在：请检查 Base URL（很多服务必须以 /v1 结尾）。",
    408: "服务端等待超时。",
    422: "参数不被服务接受（通常是模型名不对）。",
    429: "请求过于频繁或超出配额，请稍后重试。",
    500: "服务端内部错误。",
    502: "网关错误（服务商临时故障）。",
    503: "服务暂时不可用（模型可能正在加载）。",
    504: "网关超时。",
}


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:600]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg")
            if msg:
                return str(msg)[:600]
        if isinstance(err, str):
            return err[:600]
        for k in ("message", "msg", "detail", "error_description"):
            if isinstance(data.get(k), str):
                return data[k][:600]
    return json.dumps(data, ensure_ascii=False)[:600]


def to_llm_error(exc: Exception, ctx: str = "请求") -> LLMError:
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        detail = _read_error_body(exc)
        parts = ["%s失败（HTTP %s）：%s" % (ctx, code, HTTP_HINTS.get(code, ""))]
        if detail:
            parts.append("服务端返回：" + detail)
        return LLMError("\n".join(parts), status=code)
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        return LLMError(
            "%s失败：无法连接到服务器。\n原因：%s\n请检查：网络是否可用、地址是否写对、"
            "是否需要代理、本地服务是否已启动。" % (ctx, reason)
        )
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return LLMError("%s超时：服务端长时间没有响应。" % ctx)
    if isinstance(exc, LLMError):
        return exc
    return LLMError("%s失败：%s: %s" % (ctx, type(exc).__name__, exc))


def list_models(base_url: str, api_key: str, timeout: float = 30.0):
    """请求 /models 获取可用模型 id 列表。"""
    cands = models_candidates(base_url)
    if not cands:
        raise LLMError("没有填写接口地址（Base URL）。")
    last = None
    for url in cands:
        req = urllib.request.Request(url, headers=build_headers(api_key, url), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            err = to_llm_error(e, "获取模型列表")
            last = err
            if e.code == 404:
                continue
            raise err
        except Exception as e:
            raise to_llm_error(e, "获取模型列表")
        items = data.get("data") if isinstance(data, dict) else data
        ids = []
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    mid = it.get("id") or it.get("model") or it.get("name")
                    if mid:
                        ids.append(str(mid))
                elif isinstance(it, str):
                    ids.append(it)
        return sorted(set(ids))
    if last:
        raise last
    return []


def chat_completion(base_url, api_key, model, messages, *, stream=True, temperature=None,
                    timeout=120.0, on_delta=None, stop_event=None, on_open=None):
    """发一次对话请求。返回 (回答文本, 思考过程文本, usage)。"""
    if not (model or "").strip():
        raise LLMError("没有填写模型名称。")
    payload = {"model": model.strip(), "messages": messages}
    if stream:
        payload["stream"] = True
    if temperature not in (None, ""):
        try:
            payload["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass

    cands = endpoint_candidates(base_url)
    if not cands:
        raise LLMError("没有填写接口地址（Base URL）。")

    last = None
    for url in cands:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers=build_headers(api_key, url), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            err = to_llm_error(e, "对话请求")
            if e.code == 404:
                last = err
                continue
            raise err
        except Exception as e:
            raise to_llm_error(e, "对话请求")

        if on_open is not None:
            try:
                on_open(resp)
            except Exception:
                pass
        try:
            return _consume_response(resp, stream=stream, on_delta=on_delta,
                                     stop_event=stop_event)
        finally:
            try:
                resp.close()
            except Exception:
                pass
    if last:
        raise last
    raise LLMError("对话请求失败。")


def _consume_response(resp, *, stream, on_delta, stop_event):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if stream and "event-stream" in ctype:
        return _consume_sse(resp, on_delta, stop_event)

    raw = resp.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except Exception:
        raise LLMError("服务端返回的内容不是有效的 JSON（前 300 字）：\n%s" % raw[:300])
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise LLMError("服务端返回错误：%s" % msg)
    text, reasoning = _extract_message(data)
    usage = data.get("usage") if isinstance(data, dict) else None
    if on_delta:
        if reasoning:
            on_delta("reasoning", reasoning)      # 先思考
        if text:
            on_delta("content", text)             # 再正式回复
    return text, reasoning, usage


def _extract_message(data):
    """从非流式响应里取出正文和思考过程。"""
    text, reasoning = "", ""
    try:
        choices = data.get("choices") or []
        if choices:
            ch = choices[0] or {}
            msg = ch.get("message") or {}
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):      # 少数服务返回分段内容
                    content = "".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                raw = content or ""
                text, inline_think = split_think_text(raw)
                reasoning = str(msg.get("reasoning_content")
                                or msg.get("reasoning") or "") + inline_think
            if not text and isinstance(ch.get("text"), str):
                text, extra_think = split_think_text(ch["text"])
                reasoning = (reasoning or "") + extra_think
    except Exception:
        pass
    return (text or "", reasoning or "")


def _consume_sse(resp, on_delta, stop_event):
    text_parts, reason_parts = [], []
    usage = None
    splitter = ThinkTagSplitter()
    try:
        for raw in resp:
            if stop_event is not None and stop_event.is_set():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload in ("[DONE]", "DONE"):
                break
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("usage"):
                usage = obj["usage"]
            if isinstance(obj, dict) and obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise LLMError("流式响应中出错：%s" % msg)
            choices = obj.get("choices") if isinstance(obj, dict) else None
            if not choices:
                continue
            ch = choices[0] or {}
            delta = ch.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, list):
                piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
            reason = delta.get("reasoning_content") or delta.get("reasoning")
            if not piece and not delta and isinstance(ch.get("message"), dict):
                piece = (ch["message"] or {}).get("content")
            for kind, part in _emit(splitter, "reasoning", reason):
                reason_parts.append(part)
                if on_delta:
                    on_delta("reasoning", part)
            for kind, part in _emit(splitter, "content", piece):
                if kind == "reasoning":
                    reason_parts.append(part)
                    if on_delta:
                        on_delta("reasoning", part)
                else:
                    text_parts.append(part)
                    if on_delta:
                        on_delta("content", part)
    except Exception:
        if stop_event is not None and stop_event.is_set():
            pass
        else:
            raise
    for kind, part in splitter.flush():        # 收尾：未闭合的思考也要吐出来
        if kind == "reasoning":
            reason_parts.append(part)
            if on_delta:
                on_delta("reasoning", part)
        else:
            text_parts.append(part)
            if on_delta:
                on_delta("content", part)
    return "".join(text_parts), "".join(reason_parts), usage


# --------------------------------------------------------------------------- #
#  对话总结归档：userdata/record/<时间>.md
# --------------------------------------------------------------------------- #
ROUNDS_PER_SUMMARY = 10        # 每满多少轮对话生成一份总结
REVIEW_ROUNDS = 10             # 每多少次聊天让模型先复习一次历史总结

REVIEW_PREFIX = (
    "【以下是以前对话的总结（由程序自动生成，供你了解此前聊过什么）】\n"
    "这些条目按「原序号」从小到大排列（序号是固定的，不会因为排序而变化）。\n"
    "请**按原序号从小到大**把这些内容回顾一遍，把它当作你已经知道的历史背景，"
    "然后再回答用户接下来的问题。回答时不要复述这份总结，也不要提到它的存在。\n\n"
)
MAX_SUMMARY_CHARS = 2000       # 单个 .md 文件的字数上限
MAX_SUMMARY_FILES = 10         # record 目录里最多保留多少个 .md 文件

SUMMARY_INSTRUCTIONS = (
    "你是对话归档助手。请把【本次新增对话】整理成若干条要点。规则：\n"
    "1. **只整理本次新增的对话，只输出新增的条目**："
    "【已有条目】里写过的内容不要再输出，也不要改写它们（旧条目由程序原样保留）。\n"
    "2. **按条整理**：一个主题（一件事）写成一条；每条必须**单独占一行**，行首用「- 」开头；"
    "本次新增里相似/同主题的内容合并进同一条（用「；」连接），不要重复列条。\n"
    "3. 每条要能独立看懂：谁、做了什么、结论或决定是什么，保留重要事实与数字；"
    "不要复述寒暄，不要评论，不要写标题、编号、前言、结尾语。\n"
    "4. 条与条之间不要空行。\n"
    "5. 本次新增条目合计不超过 %d 个汉字，越精炼越好。\n"
    "6. **不要输出 T(...) / L(...) 之类的参数**，程序会自动计算并加到每条后面。\n"
    "输出格式（每行一条，不要空行）：\n"
    "- <新增条目一>\n"
    "- <新增条目二>"
)


def app_dir() -> Path:
    """程序所在目录（打包成 exe 后用 exe 所在目录）。"""
    return _base_dir()


def record_dir() -> Path:
    """userdata/record 目录（不存在会被自动创建）。"""
    return app_dir() / "userdata" / "record"


def ui_state_path() -> Path:
    """「对话页面 + AI 上下文」的存档位置。

    关闭程序不清空，只有用户点「清空对话」时才删掉。
    这里**只存对话内容**，不存任何接口地址 / API Key。
    """
    return app_dir() / "userdata" / "ui_state.json"


def load_ui_state():
    """读回上次的对话存档；没有或损坏时返回 None。"""
    p = ui_state_path()
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def save_ui_state(history, chat_dump, extra=None) -> bool:
    """把对话页面和 AI 上下文写进存档（原子写入，写一半不会毁掉旧存档）。"""
    p = ui_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": 1, "history": history, "chat_dump": chat_dump}
        if isinstance(extra, dict):
            data.update(extra)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:
        return False


def clear_ui_state() -> None:
    """用户手动「清空对话」时，把存档一起删掉。"""
    p = ui_state_path()
    for q in (p, p.with_name(p.name + ".tmp")):
        try:
            if q.exists():
                q.unlink()
        except OSError:
            pass


def list_summary_files():
    """列出 record 目录里的 .md 文件，按生成时间从早到晚排序。"""
    d = record_dir()
    if not d.is_dir():
        return []
    try:
        files = [p for p in d.glob("*.md") if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def clamp_chars(text: str, limit: int) -> str:
    """把文本截断到 limit 个字符以内（截断时加提示）。"""
    text = text or ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    mark = "\n…（已截断）"
    if limit <= len(mark):
        return mark[:limit]
    return text[:limit - len(mark)] + mark


# --------------------------------------------------------------------------- #
#  总结排版：按条整理、相似合并、每条一行
# --------------------------------------------------------------------------- #
MAX_ITEM_CHARS = 100        # 合并后的单条上限（超过就保持两条）
_ITEM_LINE_RE = re.compile(r"[\r\n]+")
_ITEM_MARK_RE = re.compile(r"^\s*(?:[-*•·—–]|\d+\s*[.、)）]|\(\d+\)|[（(]\d+[)）])\s*")
_ITEM_SENT_RE = re.compile(r"(?<=[。！？!?；;])\s*")


def _split_summary_items(text: str):
    """把模型输出拆成「一条一条」的要点文本。"""
    items = []
    for line in _ITEM_LINE_RE.split((text or "").strip()):
        line = _ITEM_MARK_RE.sub("", line.strip()).strip()
        if not line:
            continue
        pieces = [line] if len(line) <= 60 else [   # 超过一行长度就按句子拆成多条
            p.strip(" 　") for p in _ITEM_SENT_RE.split(line) if p.strip(" 　")]
        for piece in pieces:
            # 没有任何标点的超长文本，按长度硬切成多条，保证「一条一行」
            while len(piece) > 140:
                items.append(piece[:140])
                piece = piece[140:]
            if piece:
                items.append(piece)
    return items


def _longest_common_run(a: str, b: str) -> int:
    """两条要点里最长的一模一样的一段（用来判断是不是同一个主题）。"""
    import difflib
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return m.size


def _summary_similar(a: str, b: str) -> bool:
    """两条要点是否属于同一主题（用于合并）。

    判定：完全相同/包含 → 是；有一段 >=3 个字的共同片段（例如「零依赖」「双击即用」）
    → 是；字符重合度很高 → 是。否则分开成两条。
    """
    a, b = a.strip(), b.strip()
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    if _longest_common_run(a, b) >= 3:      # 例如「零依赖」这种共同关键词
        return True
    sa, sb = set(a), set(b)
    if len(sa & sb) / max(1, len(sa | sb)) >= 0.62:
        return True
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.78


def normalize_summary_items(text: str, budget: int | None = None):
    """整理成「- 一条一行」：拆分 -> 相似合并 -> 去重 -> 按字数预算整条裁剪。

    返回 (正文, 统计)，正文每行以「- 」开头、行间无空行。
    """
    items = _split_summary_items(text)
    merged, merged_n = [], 0
    for it in items:
        hit = -1
        for idx, cur in enumerate(merged):
            if _summary_similar(cur, it):
                hit = idx
                break
        if hit < 0:
            merged.append(it)
            continue
        cur = merged[hit]
        if len(cur) + len(it) + 1 <= MAX_ITEM_CHARS:      # 并进同一条
            merged[hit] = cur.rstrip("。；;，,") + "；" + it.lstrip("。；;，,")
            merged_n += 1
        else:
            merged.append(it)                             # 太长就另起一条（绝不丢弃）
    lines = ["- " + it for it in merged]
    stats = {"items": len(merged), "merged": merged_n, "dropped": 0}
    if budget:
        while len(lines) > 1 and len("\n".join(lines)) > budget:
            lines.pop()
            stats["dropped"] += 1
        if len("\n".join(lines)) > budget:                # 只剩一条还超预算 → 截断它
            keep = max(20, budget - 2)
            lines[0] = lines[0][:keep] + "…"
            stats["dropped"] = max(1, stats["dropped"])
        stats["items"] = len(lines)
    return "\n".join(lines), stats


# --------------------------------------------------------------------------- #
#  总结条目：序号 + T(t),L(l) 参数（条目内容一旦生成就不再改动）
# --------------------------------------------------------------------------- #
_ITEM_TAIL_RE = re.compile(
    r"\s*T\s*\(\s*[\d.]+\s*\)\s*,?\s*L\s*\(\s*[\d.]+\s*\)"
    r"(?:\s*,?\s*W\s*\(\s*[\d.]+\s*\))?"
    r"(?:\s*,?\s*D)?\s*$")


def _summary_line_prefix(body: str):
    """表头行（生成时间/覆盖/说明等）不算条目。"""
    s = (body or "").strip()
    return (s.startswith("#") or s.startswith("- 生成时间") or s.startswith("- 覆盖")
            or s.startswith("- 说明") or s.startswith("- 本次"))


def parse_summary_lines(text: str):
    """从 .md 正文里解析出条目：返回 [{"seq": 序号或None, "text": 文本, "d": 是否带D}]。"""
    out = []
    for line in (text or "").split("\n"):
        ln = line.strip()
        if not ln or _summary_line_prefix(ln):
            continue
        ln = re.sub(r"^\s*[-*•]\s*", "", ln)              # 兼容「- 」写法
        m = re.match(r"^\s*(\d+)\s*[.、)）]\s*", ln)      # 取出序号
        seq = int(m.group(1)) if m else None
        if m:
            ln = ln[m.end():]
        has_d = bool(re.search(r",\s*D\s*$", ln))
        ln = _ITEM_TAIL_RE.sub("", ln).strip()
        if ln:
            out.append({"seq": seq, "text": ln, "d": has_d})
    return out


def parse_summary_items(text: str):
    """只要文本（兼容旧调用）：从 .md 正文里解析出条目文本。"""
    return [it["text"] for it in parse_summary_lines(text)]


def render_summary_items(items, now=None, k=0.5):
    """渲染成「序号. 内容 T(x),L(y),W(z)」（被精简过的条目末尾再加 ,D）。

    items: [{"text": 条目文本, "ts": 生成时间(秒), "d": 是否已精简}]
    * t = 该条距「最新一条消息」的天数（不足 1 天按 1 天计，保留 1 位小数），T = t / 最大t
    * l = 该条字数（含标点），L = l / 最大字数
    * W = k*T + (1-k)*L，k 为「记忆优化的时间权重」（0~1）
    条目会**按 W 从小到大排序显示**，但**序号固定不变**（序号在生成时就定下来，
    AI 复习时按原序号从小到大读；items 会被就地按 W 排序）。
    返回 (正文, 统计)
    """
    now = time.time() if now is None else now
    _orig = items
    items = list(items or [])
    if not items:
        return "", {"items": 0, "t_max": 0.0, "l_max": 0, "newest_t": 0.0}
    # 单位：天；不足 1 天按 1 天算（这样最大 t 至少是 1，T 的除数不会为 0）
    ts = [max(1.0, (now - float(it.get("ts") or now)) / 86400.0) for it in items]
    import math
    # 保留 1 位小数（四舍五入；Python 的 round 是银行家舍入，这里不用它）
    t_days = [max(1.0, math.floor(x * 10 + 0.5) / 10) for x in ts]
    t_max = max(t_days)
    lens = [len(it.get("text") or "") for it in items]
    l_max = max(lens)
    weight = min(1.0, max(0.0, float(k if k is not None else 0.5)))

    def _r2(x):
        """四舍五入到 2 位小数（文件里显示的 T/L/W 与计算完全一致，方便手工核对）。"""
        return math.floor(x * 100 + 0.5) / 100

    rows = []
    for idx, it in enumerate(items):
        t, l = t_days[idx], lens[idx]
        big_t = _r2((t / t_max) if t_max > 0 else 0.0)
        big_l = _r2((l / l_max) if l_max > 0 else 0.0)
        w = _r2(weight * big_t + (1.0 - weight) * big_l)
        it["w"] = w                                 # 存下来：排序 / 精简 / 删除都用它
        rows.append((it, big_t, big_l, w))
    rows.sort(key=lambda r: (r[3], r[0].get("ts") or 0))     # 按 W 从小到大
    if isinstance(_orig, list):                              # 真正写回调用方的列表
        _orig[:] = [r[0] for r in rows]
    lines = []
    for i, (it, big_t, big_l, w) in enumerate(rows, 1):
        seq = int(it.get("seq") or i)        # 序号固定：排序只改显示顺序，不改序号
        it["seq"] = seq
        tail = ",D" if it.get("d") else ""
        lines.append("%d. %s T(%.2f),L(%.2f),W(%.2f)%s"
                     % (seq, it.get("text") or "", big_t, big_l, w, tail))
    return "\n".join(lines), {"items": len(items), "t_max": t_max, "l_max": l_max,
                               "newest_t": t_days[-1], "weight": weight}


def summary_items_path(md_path) -> Path:
    p = Path(md_path)
    return p.with_name(p.stem + ".items.json")


def save_summary_items(md_path, items):
    """把条目（含时间）存成 sidecar JSON，下次总结继续沿用、条目内容不变。"""
    try:
        summary_items_path(md_path).write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def load_summary_items(md_path):
    """读取条目 sidecar；没有则返回 None（调用方会退化为从 .md 里解析）。"""
    try:
        p = summary_items_path(md_path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list):
                out = []
                for idx, d in enumerate(data, 1):
                    if not (isinstance(d, dict) and d.get("text")):
                        continue
                    out.append({"text": str(d.get("text")),
                                "ts": float(d.get("ts") or 0),
                                "w": float(d.get("w") or 0),
                                "d": bool(d.get("d")),      # 已被精简过，必须保留
                                "seq": int(d.get("seq") or idx)})   # 序号固定不变
                if out:
                    return out
    except Exception:
        pass
    return None


MEM_COMPRESS_INSTRUCTIONS = (
    "你是记忆精简助手。下面会给出一批记忆条目，请把它们逐条压缩成更短的表述。要求：\n"
    "1. **逐条输出**：一条一行、行首用「- 」，顺序与输入完全一致，不要合并、不要拆分、"
    "条数必须和输入相同；\n"
    "2. 保留关键信息（谁、做了什么、结论或决定、重要数字与人名），去掉修饰、重复与寒暄；\n"
    "3. 每条尽量压到原文一半以内，但不要丢失信息点，也不要新增输入里没有的内容；\n"
    "4. 不要输出序号、T/L/W 参数或任何前后缀。\n"
    "输出格式（每行一条）：\n"
    "- <精简后的第一条>\n"
    "- <精简后的第二条>"
)


def summary_notes():
    """写在总结文件里的格式说明。"""
    return ["说明：每条格式为「序号. 内容 T(x),L(y),W(z)」；"
            "t = 该条距最新一条消息的天数（不足 1 天按 1 天计，保留 1 位小数），"
            "T = t ÷ 所有条目里最大的 t；l = 该条字数（含标点），L = l ÷ 所有条目里最大的 l；"
            "W = k·T + (1-k)·L（k 为「记忆优化」里的时间权重）；"
            "条目按 W 从小到大排列显示（**序号是固定的，不会因为排序而改变**）；"
            "末尾带「,D」表示该条已被精简过；"
            "字数上限只统计下面的条目，本表头（生成时间/覆盖/说明）不计入。"]


def write_summary_file(body: str, cover: str = "", max_chars=None, max_files=None,
                       notes=None):
    """写一份 .md 总结。返回 (文件路径, 因超出上限而被清理的旧文件名列表)。

    max_chars / max_files 来自「设置 → 对话总结」里用户填的值，留空则用模块默认值。
    """
    limit = max(1, int(max_chars or MAX_SUMMARY_CHARS))
    keep = max(1, int(max_files or MAX_SUMMARY_FILES))
    d = record_dir()
    d.mkdir(parents=True, exist_ok=True)
    header = "# 对话总结\n\n- 生成时间：%s\n- 覆盖：%s\n" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), cover or "最近一段对话")
    for _n in (notes or []):
        header += "- %s\n" % _n
    header += "\n"
    # 字数上限只统计正文（下面这些条目）；表头的生成时间/覆盖/说明是注释，不计入
    body = clamp_chars((body or "").strip(), limit)
    content = header + body + "\n"

    removed = []
    files = list_summary_files()
    while len(files) >= keep:          # 已有 keep 份则先删最早的一份，再写新的
        oldest = files.pop(0)
        try:
            oldest.unlink()
            removed.append(oldest.name)
        except OSError:
            break

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")      # 以生成时间命名
    path = d / (stamp + ".md")
    n = 2
    while path.exists():                            # 同一秒内不重名
        path = d / ("%s_%d.md" % (stamp, n))
        n += 1
    path.write_text(content, encoding="utf-8")
    return path, removed


# --------------------------------------------------------------------------- #
#  屏幕识别：定时截屏 -> 当成用户上传的图片
# --------------------------------------------------------------------------- #
SCREEN_TEMP_DIR = "temppicture"     # 截图存放目录（程序主目录下）
SCREEN_MAX_FILES = 10               # 最多同时保存多少张截图
SCREEN_MIN_INTERVAL = 3             # 最小间隔（秒）
SCREEN_DEFAULT_INTERVAL = 30        # 默认间隔（秒）



def temp_picture_dir() -> Path:
    """程序主目录下的 temppicture 目录（不存在时创建）。"""
    d = app_dir() / SCREEN_TEMP_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _picture_sort_key(p: Path):
    """排序键：优先用文件名里的「日期_时间_序号」，同一秒也不会排错。"""
    m = re.match(r"screen_(\d{8})_(\d{6})(?:_(\d+))?\.png$", p.name)
    if m:
        return (m.group(1), m.group(2), int(m.group(3) or 1))
    try:
        return ("", "%d" % p.stat().st_mtime_ns, 0)
    except OSError:
        return ("", "0", 0)


def list_temp_pictures():
    """temppicture 里的截图，按截取时间从早到晚。"""
    d = app_dir() / SCREEN_TEMP_DIR
    if not d.is_dir():
        return []
    try:
        files = [p for p in d.glob("screen_*.png") if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=_picture_sort_key)


def save_temp_picture(w, h, rgb_rows) -> Path:
    """把截图写成 PNG 存进 temppicture；超过 10 张先删最早的。返回文件路径。"""
    d = temp_picture_dir()
    files = list_temp_pictures()
    while len(files) >= SCREEN_MAX_FILES:          # 上限 10：先删最早的
        oldest = files.pop(0)
        try:
            oldest.unlink()
        except OSError:
            break
    path = d / ("screen_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    n = 2
    while path.exists():
        path = d / ("screen_%s_%d.png" % (time.strftime("%Y%m%d_%H%M%S"), n))
        n += 1
    write_png_rgb(path, w, h, rgb_rows)
    return path


def write_png_rgb(path, w, h, rows):
    """写 8 位 RGB PNG（rows 为每行 w*3 字节）。纯标准库实现。"""
    raw = bytearray()
    for row in rows:
        raw.append(0)                              # 过滤类型 0
        raw.extend(row)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def enum_windows_for_pick():
    """列出可见的顶层窗口：[(hwnd, 标题, 类名)]，用于让用户选截取哪个窗口。"""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        try:
            if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
                return True
            length = u32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, title, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            r = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(r))
            if r.right - r.left < 200 or r.bottom - r.top < 120:
                return True                        # 太小的忽略
            out.append((int(hwnd), title.value, cls.value))
        except Exception:
            pass
        return True

    u32.EnumWindows(_cb, 0)
    return out


def find_window_by_class_title(title, cls):
    """按「标题 + 类名」找回窗口（句柄会变，所以配置里存标题）。"""
    for hwnd, t, c in enum_windows_for_pick():
        if c == cls and (t == title or title in t):
            return hwnd
    for hwnd, t, c in enum_windows_for_pick():
        if title and title in t:
            return hwnd
    return None


class _BI(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BIN(ctypes.Structure):
    _fields_ = [("bmiHeader", _BI), ("bmiColors", wintypes.DWORD * 3)]


def grab_bgra(hwnd=None):
    """抓取窗口（hwnd）或整个屏幕（hwnd=None）：返回 (w, h, BGRA 字节)。"""
    if hwnd:
        r = wintypes.RECT()
        u32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r))
        left, top = r.left, r.top
        w, h = r.right - r.left, r.bottom - r.top
        src = u32.GetWindowDC(wintypes.HWND(hwnd))
        use_print = True
    else:
        left, top = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)   # 虚拟屏左上角
        w, h = u32.GetSystemMetrics(78), u32.GetSystemMetrics(79)        # 虚拟屏宽高
        src = u32.GetDC(0)
        use_print = False
    if w <= 0 or h <= 0:
        raise RuntimeError("截取区域无效（窗口可能已最小化）")
    screen = u32.GetDC(0)
    mem = g32.CreateCompatibleDC(screen)
    bmp = g32.CreateCompatibleBitmap(screen, w, h)
    old = g32.SelectObject(mem, bmp)
    try:
        if use_print:
            u32.PrintWindow(wintypes.HWND(hwnd), mem, 2)
        else:
            g32.BitBlt(mem, 0, 0, w, h, screen, left, top, 0x00CC0020)
        info = _BIN()
        info.bmiHeader.biSize = ctypes.sizeof(_BI)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        if not g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0):
            raise RuntimeError("读取屏幕像素失败")
        return w, h, buf.raw
    finally:
        g32.SelectObject(mem, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(0, screen)
        if hwnd:
            u32.ReleaseDC(wintypes.HWND(hwnd), src)


def bgra_to_rgb_rows(w, h, bgra, half=True):
    """BGRA -> RGB 行；half=True 时宽高各减半（省 token、文件更小）。"""
    step = 2 if half else 1
    out_w, out_h = max(1, w // step), max(1, h // step)
    rows = []
    for y in range(0, h, step):
        row = bgra[y * w * 4:(y + 1) * w * 4]
        rgb = bytearray(out_w * 3)
        rgb[0::3] = row[2::4 * step]        # R（GDI 是 BGRA）
        rgb[1::3] = row[1::4 * step]        # G
        rgb[2::3] = row[0::4 * step]        # B
        rows.append(bytes(rgb))
    return out_w, out_h, rows


def capture_screen_to_file(hwnd=None, half=True):
    """截屏并保存到 temppicture，返回 (路径, 宽, 高)。"""
    w, h, bgra = grab_bgra(hwnd)
    ow, oh, rows = bgra_to_rgb_rows(w, h, bgra, half=half)
    path = save_temp_picture(ow, oh, rows)
    return path, ow, oh


def _log(msg="", exc=False):
    """诊断信息：有控制台就打印；打包成无控制台 exe 时写入程序目录的日志文件。"""
    text = str(msg)
    if exc:
        text = (text + "\n" if text else "") + traceback.format_exc()
    try:
        if sys.stderr is not None:
            print(text, file=sys.stderr, flush=True)
            return
    except Exception:
        pass
    try:
        with open(app_dir() / "llm_chat.log", "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def find_test_image():
    """程序目录下的测试图片（默认 test.jpeg）。"""
    for name in ("test.jpeg", "test.jpg", "test.png", "test.webp", "test.gif",
                 "test.bmp"):
        path = app_dir() / name
        if path.exists():
            return path
    return None


def rel_path(path) -> str:
    try:
        return Path(path).relative_to(app_dir()).as_posix()
    except Exception:
        return str(path)


# --------------------------------------------------------------------------- #
#  图形界面
# --------------------------------------------------------------------------- #
class MyLLMChatApp:
    """主窗口：先显示「连接设置」（引导页），连接成功后显示对话界面。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.history = []                 # [{"role": "user"/"assistant", "content": "..."}]
        self.busy = False
        self.stop_event = threading.Event()
        self.ui_queue = queue.Queue()     # 子线程 -> 主线程 的任务队列
        self._resp = None
        self._job_running = False

        # 生成过程中的统计
        self._session_start = 0.0
        self._first_token_at = None
        self._chars = 0
        self._started_content = False
        self._started_reason = False
        self._live_reason = []
        self._reason_chars = 0
        self._reason_seen = False
        self._phase = "waiting"        # waiting / summarizing

        # 对话总结（每 10 轮一份 .md）
        self.rounds_in_batch = 0       # 距上次总结已进行的轮数
        self.carry_over = None         # 边界轮的回复，顺延到下一批开头
        self._pending_carry = False
        self._summary_note = ""        # 最近一次总结的结果，只显示在底部状态栏
        self._since_review = 0         # 距上次“复习历史总结”已聊了几次
        self.attached_images = []      # 本次待发送的图片路径（发送后清空）
        self.auto_image = None         # 屏幕识别自动上传的那张（最多一张）
        self._screen_on = False        # 屏幕识别是否正在运行
        self._screen_thread = None     # 截屏线程
        self._screen_count = 0         # 已截取次数
        self.screen_target = dict(self.cfg.get("screen_target") or {"kind": "screen"})
        self._vision_total = 0         # 本轮要识别的图片数
        self._vision_note = ""         # 本轮视觉识别结果说明
        self._review_chars = 0         # 本轮复习了多少字的总结
        self.last_summary_file = None  # 上一份总结文件
        self.summary_items = None      # 累计的总结条目 [{"text":..,"ts":..}]（首次用到时载入）

        global FONT
        FONT = pick_font(root)

        self.root.title("%s v%s" % (APP_TITLE, APP_VERSION))
        # 高度自适应屏幕：设置页比较长，尽量让它一屏放得下（放不下时可滚动）
        screen_h = self.root.winfo_screenheight()
        win_h = max(620, min(1010, screen_h - 105))
        self.root.geometry("980x%d" % win_h)
        self.root.minsize(800, 560)
        self._init_style()

        self.container = ttk.Frame(root)
        self.container.pack(fill="both", expand=True)
        self.container.rowconfigure(0, weight=1)
        self.container.columnconfigure(0, weight=1)

        self.setup_frame = self._build_setup(self.container)
        self.chat_frame = self._build_chat(self.container)
        self.setup_frame.grid(row=0, column=0, sticky="nsew")
        self.chat_frame.grid(row=0, column=0, sticky="nsew")

        self._fill_form(self.cfg)
        self._reset_chat_text()
        if self._has_saved_config():
            self.show_chat()               # 已经配置过：跳过引导页，直接进入对话界面
        else:
            self.show_setup(initial=True)
        self._screen_set_on(bool(self.cfg.get("screen_watch")), save=False)
        self._restore_ui_state()           # 接上上次的对话页面与上下文（用户没清空过的话）

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump()
        self._tick()

    # ---------------- 外观 ---------------- #
    def _init_style(self):
        self.root.configure(bg=C.BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        def sc(name, **kw):
            try:
                style.configure(name, **kw)
            except tk.TclError:
                pass

        def sm(name, **kw):
            try:
                style.map(name, **kw)
            except tk.TclError:
                pass

        sc(".", background=C.BG, foreground=C.TEXT, font=(FONT, 11),
           focuscolor=C.BG, bordercolor=C.BORDER)
        sc("TLabel", background=C.BG, foreground=C.TEXT, font=(FONT, 11))
        sc("Card.TLabel", background=C.SURFACE, foreground=C.TEXT, font=(FONT, 11))
        sc("Sub.TLabel", background=C.BG, foreground=C.TEXT_2, font=(FONT, 10))
        sc("Hint.TLabel", background=C.SURFACE, foreground=C.TEXT_3, font=(FONT, 9))
        sc("Field.TLabel", background=C.SURFACE, foreground=C.TEXT,
           font=(FONT, 10, "bold"))
        sc("Status.TLabel", background=C.BG, foreground=C.TEXT_2, font=(FONT, 9))
        sc("Modern.TCombobox", padding=(10, 6), fieldbackground=C.SURFACE,
           background=C.SURFACE, bordercolor=C.BORDER, lightcolor=C.SURFACE,
           darkcolor=C.SURFACE, foreground=C.TEXT, arrowcolor=C.TEXT_2)
        sm("Modern.TCombobox", bordercolor=[("focus", C.ACCENT)],
           lightcolor=[("focus", C.ACCENT)], darkcolor=[("focus", C.ACCENT)],
           fieldbackground=[("readonly", C.SURFACE)],
           selectbackground=[("readonly", C.SURFACE)],
           selectforeground=[("readonly", C.TEXT)])
        for name, trough in (("Modern.Vertical.TScrollbar", C.BG),
                             ("Chat.Vertical.TScrollbar", C.SURFACE)):
            sc(name, background=C.THUMB, troughcolor=trough, bordercolor=trough,
               arrowcolor=trough, arrowsize=0, gripcount=0, relief="flat", width=10)
            sm(name, background=[("active", "#AEB5C0"), ("pressed", "#9AA2AE")])
    # ---------------- 现代风格小工具 ---------------- #
    def _card(self, parent, title=None, subtitle=None, padx=17, pady=6):
        """现代卡片容器（白底 + 1px 细边框 + 标题），返回 (卡片, 内容区)。

        内容区是一个独立的子框架，调用方可以自由使用 grid 排布。
        """
        card = tk.Frame(parent, bg=C.SURFACE, highlightthickness=1,
                        highlightbackground=C.BORDER, highlightcolor=C.BORDER)
        wrap = tk.Frame(card, bg=C.SURFACE)
        wrap.pack(fill="both", expand=True, padx=padx, pady=pady)
        if title:
            head = tk.Frame(wrap, bg=C.SURFACE)
            head.pack(fill="x", pady=(0, 7))
            tk.Label(head, text=title, bg=C.SURFACE, fg=C.TEXT,
                     font=(FONT, 11, "bold")).pack(side="left")
            if subtitle:
                tk.Label(head, text=subtitle, bg=C.SURFACE, fg=C.TEXT_3,
                         font=(FONT, 9)).pack(side="left", padx=(8, 0), pady=(3, 0))
        body = tk.Frame(wrap, bg=C.SURFACE)
        body.pack(fill="both", expand=True)
        return card, body

    def _fold_card(self, parent, title, subtitle=None, expanded=True, padx=17,
                   pady=5, group=None, key=None):
        """可折叠卡片（标题右侧有展开/收起按钮）。返回 (卡片, 内容区, 设置展开状态的函数)。"""
        card = tk.Frame(parent, bg=C.SURFACE, highlightthickness=1,
                        highlightbackground=C.BORDER, highlightcolor=C.BORDER)
        wrap = tk.Frame(card, bg=C.SURFACE)
        wrap.pack(fill="both", expand=True, padx=padx, pady=pady)
        head = tk.Frame(wrap, bg=C.SURFACE)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=C.SURFACE, fg=C.TEXT,
                 font=(FONT, 11, "bold")).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=C.SURFACE, fg=C.TEXT_3,
                     font=(FONT, 9)).pack(side="left", padx=(8, 0), pady=(3, 0))
        body = tk.Frame(wrap, bg=C.SURFACE)
        state = {"open": bool(expanded)}
        btn = RoundedButton(head, text="", variant="ghost", height=26, font_size=9,
                            padx=10)
        btn.pack(side="right")

        def apply(open_it, _btn=None):
            state["open"] = bool(open_it)
            if state["open"]:
                if not body.winfo_ismapped():
                    body.pack(fill="both", expand=True, pady=(5, 0))
                btn.set_text("收起 ▴")
                # 同一个分组里只展开一张卡片（手风琴），避免页面过长
                if group is not None and key is not None:
                    for other_key, closer in list(group.items()):
                        if other_key != key:
                            closer(False)
            else:
                body.pack_forget()
                btn.set_text("展开 ▾")

        btn.configure(command=lambda: apply(not state["open"]))
        if group is not None and key is not None:
            group[key] = apply
        apply(state["open"])
        return card, body, apply

    def _field(self, parent, **kw):
        """扁平输入框（1px 细边框，聚焦变主题色）。"""
        return tk.Entry(parent, relief="flat", highlightthickness=1,
                        highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                        bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
                        font=(FONT, 11), **kw)

    def _vlabel(self, parent, text, **kw):
        return tk.Label(parent, text=text, bg=C.SURFACE, fg=C.TEXT,
                        font=(FONT, 10, "bold"), anchor="w", **kw)

    def _hint(self, parent, text, **kw):
        return tk.Label(parent, text=text, bg=C.SURFACE, fg=C.TEXT_3,
                        font=(FONT, 9), anchor="w", justify="left", **kw)

    def _build_setup(self, parent):
        outer = tk.Frame(parent, bg=C.BG)
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)

        canvas = tk.Canvas(outer, highlightthickness=0, bd=0, bg=C.BG)
        vsb = ttk.Scrollbar(outer, orient="vertical",
                            style="Modern.Vertical.TScrollbar", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        f = tk.Frame(canvas, bg=C.BG)
        win = canvas.create_window((0, 0), window=f, anchor="nw")
        f.columnconfigure(0, weight=1)

        def _resize(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(win, width=canvas.winfo_width())

        f.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)

        def _wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        def chips_of(body, presets, row0, kind="llm"):
            holder = tk.Frame(body, bg=C.SURFACE)
            holder.grid(row=row0, column=0, columnspan=2, sticky="ew")
            for i in range(4):
                holder.columnconfigure(i, weight=1)
            for idx, (name, url, model) in enumerate(presets):
                RoundedButton(holder, text=name, variant="chip", height=26, font_size=9,
                              padx=8,
                              command=lambda u=url, m=model, n=name, k=kind:
                              self._apply_preset(n, u, m, k)).grid(
                                  row=idx // 4, column=idx % 4,
                                                 sticky="ew", padx=3, pady=1)
            return holder

        PAD = 26

        # ---------- 标题 ----------
        head = tk.Frame(f, bg=C.BG)
        head.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(12, 0))
        head.columnconfigure(0, weight=1)
        tk.Label(head, text="连接你的大模型", bg=C.BG, fg=C.TEXT,
                 font=(FONT, 18, "bold")).grid(row=0, column=0, sticky="w")
        RoundedButton(head, text="退出设置 ✕", variant="ghost", height=32,
                      command=self.on_exit_setup).grid(row=0, column=1, sticky="e")
        tk.Label(f, text="填写下面三项即可开始对话　·　配置只保存在本机：%s" % CONFIG_PATH,
                 bg=C.BG, fg=C.TEXT_2, font=(FONT, 10), anchor="w", justify="left",
                 wraplength=880).grid(row=1, column=0, sticky="w", padx=PAD,
                                      pady=(3, 6))

        # ---------- 语言模型（LLM）接口信息 ----------
        group = {}
        card1, inner1, llm_fold = self._fold_card(
            f, "语言模型（LLM）接口信息", "必填 · 也可以点下面的常见服务一键填充",
            group=group, key="llm")
        card1.grid(row=2, column=0, sticky="ew", padx=PAD)
        inner1.columnconfigure(1, weight=1)

        chips_of(inner1, PRESETS, 0, "llm")
        tk.Frame(inner1, bg=C.BORDER, height=1).grid(row=1, column=0, columnspan=2,
                                                    sticky="ew", pady=(7, 5))

        self._vlabel(inner1, "① 接口地址 Base URL").grid(row=2, column=0, sticky="w",
                                                      pady=(2, 0), padx=(0, 14))
        self.var_base = tk.StringVar()
        e_base = self._field(inner1, textvariable=self.var_base)
        e_base.grid(row=2, column=1, sticky="ew", ipady=5)
        self._hint(inner1, "例：https://api.deepseek.com/v1；本地模型填 "
                           "http://localhost:11434/v1",
                   wraplength=660).grid(row=3, column=1, sticky="w", pady=(2, 2))

        self._vlabel(inner1, "② API Key").grid(row=4, column=0, sticky="w", padx=(0, 14))
        keybox = tk.Frame(inner1, bg=C.SURFACE)
        keybox.grid(row=4, column=1, sticky="ew")
        keybox.columnconfigure(0, weight=1)
        self.var_key = tk.StringVar()
        self.ent_key = self._field(keybox, textvariable=self.var_key, show="●")
        self.ent_key.grid(row=0, column=0, sticky="ew", ipady=5)
        self.var_show_key = tk.BooleanVar(value=False)
        CheckBox(keybox, text="显示", variable=self.var_show_key,
                 command=self._toggle_key).grid(row=0, column=1, padx=(10, 0))
        self._hint(inner1, "只在你自己电脑上使用；本地模型可以留空。").grid(
            row=5, column=1, sticky="w", pady=(2, 2))

        self._vlabel(inner1, "③ 模型名称").grid(row=6, column=0, sticky="w", padx=(0, 14))
        modelbox = tk.Frame(inner1, bg=C.SURFACE)
        modelbox.grid(row=6, column=1, sticky="ew")
        modelbox.columnconfigure(0, weight=1)
        self.var_model = tk.StringVar()
        self.cmb_model = ttk.Combobox(modelbox, textvariable=self.var_model,
                                      font=(FONT, 11), style="Modern.TCombobox")
        self.cmb_model.grid(row=0, column=0, sticky="ew", ipady=3)
        self.btn_models = RoundedButton(modelbox, text="获取可用模型", variant="outline",
                                        height=33, font_size=9, padx=12,
                                        command=self.on_get_models)
        self.btn_models.grid(row=0, column=1, padx=(10, 0))
        self._hint(inner1, "如 deepseek-chat、gpt-4o-mini；点右边按钮可自动列出。"
                   ).grid(row=7, column=1, sticky="w", pady=(2, 0))

        self.setup_status = tk.Label(inner1, text="", anchor="w", justify="left",
                                     wraplength=850, font=(FONT, 9), bg=C.SURFACE,
                                     fg=C.TEXT_2)
        self.setup_status.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(5, 0))

        # ---------- 视觉模型（VLM）接口信息 ----------
        card_v, inner_v, vlm_fold = self._fold_card(
            f, "视觉模型（VLM）接口信息",
            "发图片时用它识别图片内容 · 不配置就只能发文字", expanded=False,
            group=group, key="vlm")
        card_v.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(5, 0))
        self._vlm_fold = vlm_fold
        inner_v.columnconfigure(1, weight=1)

        chips_of(inner_v, VLM_PRESETS, 0, "vlm")
        tk.Frame(inner_v, bg=C.BORDER, height=1).grid(row=1, column=0, columnspan=2,
                                                     sticky="ew", pady=(7, 5))

        self._vlabel(inner_v, "① 接口地址 Base URL").grid(row=2, column=0, sticky="w",
                                                       pady=(2, 0), padx=(0, 14))
        self.var_vlm_base = tk.StringVar()
        self._field(inner_v, textvariable=self.var_vlm_base).grid(
            row=2, column=1, sticky="ew", ipady=5)
        self._hint(inner_v, "例：https://api.openai.com/v1；本地视觉模型填 "
                            "http://localhost:11434/v1",
                   wraplength=660).grid(row=3, column=1, sticky="w", pady=(2, 2))

        self._vlabel(inner_v, "② API Key").grid(row=4, column=0, sticky="w", padx=(0, 14))
        vkeybox = tk.Frame(inner_v, bg=C.SURFACE)
        vkeybox.grid(row=4, column=1, sticky="ew")
        vkeybox.columnconfigure(0, weight=1)
        self.var_vlm_key = tk.StringVar()
        self.ent_vlm_key = self._field(vkeybox, textvariable=self.var_vlm_key, show="●")
        self.ent_vlm_key.grid(row=0, column=0, sticky="ew", ipady=5)
        self.var_show_vlm_key = tk.BooleanVar(value=False)
        CheckBox(vkeybox, text="显示", variable=self.var_show_vlm_key,
                 command=self._toggle_vlm_key).grid(row=0, column=1, padx=(10, 0))
        self._hint(inner_v, "只在你自己电脑上使用；本地视觉模型可以留空。").grid(
            row=5, column=1, sticky="w", pady=(2, 2))

        self._vlabel(inner_v, "③ 模型名称").grid(row=6, column=0, sticky="w", padx=(0, 14))
        vmodelbox = tk.Frame(inner_v, bg=C.SURFACE)
        vmodelbox.grid(row=6, column=1, sticky="ew")
        vmodelbox.columnconfigure(0, weight=1)
        self.var_vlm_model = tk.StringVar()
        self.cmb_vlm_model = ttk.Combobox(vmodelbox, textvariable=self.var_vlm_model,
                                          font=(FONT, 11), style="Modern.TCombobox")
        self.cmb_vlm_model.grid(row=0, column=0, sticky="ew", ipady=3)
        self.btn_vlm_models = RoundedButton(vmodelbox, text="获取可用模型",
                                            variant="outline", height=33,
                                            font_size=9, padx=12,
                                            command=self.on_get_vlm_models)
        self.btn_vlm_models.grid(row=0, column=1, padx=(10, 0))
        self._hint(inner_v, "如 gpt-4o-mini、qwen-vl-plus、glm-4v-flash、llava:7b；"
                            "DeepSeek 没有视觉模型，因此不在上面的预设里。"
                   ).grid(row=7, column=1, sticky="w", pady=(2, 0))

        vbtns = tk.Frame(inner_v, bg=C.SURFACE)
        vbtns.grid(row=8, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.btn_vlm_test = RoundedButton(vbtns, text="测试连接", variant="soft",
                                          height=32, font_size=9, padx=14,
                                          command=self.on_test_vlm)
        self.btn_vlm_test.pack(side="left")
        self.btn_vlm_image = RoundedButton(vbtns, text="用 test.jpeg 试识别", variant="chip",
                                           height=32, font_size=9, padx=12,
                                           command=self.on_test_vlm_image)
        self.btn_vlm_image.pack(side="left", padx=(8, 0))

        self.vlm_status = tk.Label(inner_v, text="", anchor="w", justify="left",
                                   wraplength=850, font=(FONT, 9), bg=C.SURFACE,
                                   fg=C.TEXT_2)
        self.vlm_status.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        # ---------- 可选设置 ----------
        card2, inner2 = self._card(
            f, "可选设置", "不过滤时思考内容用灰色小字先输出；思考内容不会回传给模型")
        card2.grid(row=4, column=0, sticky="ew", padx=PAD, pady=(5, 0))
        inner2.columnconfigure(1, weight=1)

        self._vlabel(inner2, "系统提示词").grid(row=0, column=0, sticky="nw",
                                              pady=(3, 0), padx=(0, 14))
        self.txt_sys = tk.Text(inner2, height=2, font=(FONT, 11), wrap="word",
                               relief="flat", highlightthickness=1,
                               highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                               bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
                               padx=8, pady=6)
        self.txt_sys.grid(row=0, column=1, sticky="ew", ipady=2)

        opts = tk.Frame(inner2, bg=C.SURFACE)
        opts.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(opts, text="温度", bg=C.SURFACE, fg=C.TEXT_2,
                 font=(FONT, 10)).pack(side="left")
        self.var_temp = tk.StringVar()
        tk.Spinbox(opts, from_=0.0, to=2.0, increment=0.1, width=5,
                   textvariable=self.var_temp, relief="flat", highlightthickness=1,
                   highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                   bg=C.SURFACE, fg=C.TEXT, buttonbackground=C.GRAY_SOFT,
                   font=(FONT, 10), justify="center").pack(side="left", padx=(6, 20))
        tk.Label(opts, text="超时(秒)", bg=C.SURFACE, fg=C.TEXT_2,
                 font=(FONT, 10)).pack(side="left")
        self.var_timeout = tk.StringVar()
        tk.Spinbox(opts, from_=10, to=1800, increment=10, width=6,
                   textvariable=self.var_timeout, relief="flat", highlightthickness=1,
                   highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                   bg=C.SURFACE, fg=C.TEXT, buttonbackground=C.GRAY_SOFT,
                   font=(FONT, 10), justify="center").pack(side="left", padx=(6, 20))
        self.var_stream = tk.BooleanVar(value=True)
        CheckBox(opts, text="流式输出", variable=self.var_stream).pack(side="left",
                                                                     padx=(0, 16))
        self.var_filter = tk.BooleanVar(value=False)
        CheckBox(opts, text="过滤模型的思考内容", variable=self.var_filter).pack(side="left")

        # ---------- 屏幕识别 ----------
        card_s, inner_s, screen_fold = self._fold_card(
            f, "屏幕识别", "定时截屏并自动当成你上传的图片（最多同时一张）",
            expanded=False, group=group, key="screen")
        card_s.grid(row=5, column=0, sticky="ew", padx=PAD, pady=(5, 0))
        self._screen_fold = screen_fold
        inner_s.columnconfigure(1, weight=1)

        self.var_screen_on = tk.BooleanVar(value=bool(self.cfg.get("screen_watch")))
        CheckBox(inner_s, text="启用屏幕识别（也可以在聊天页按「📷 屏幕识别」开关）",
                 variable=self.var_screen_on).grid(row=0, column=0, columnspan=3,
                                                   sticky="w")

        optrow = tk.Frame(inner_s, bg=C.SURFACE)
        optrow.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tk.Label(optrow, text="每隔", bg=C.SURFACE, fg=C.TEXT,
                 font=(FONT, 10, "bold")).pack(side="left")
        self.var_screen_interval = tk.StringVar(
            value=str(self.cfg.get("screen_interval", SCREEN_DEFAULT_INTERVAL)))
        self._field(optrow, textvariable=self.var_screen_interval, width=5,
                    justify="center").pack(side="left", padx=(8, 4), ipady=4)
        tk.Label(optrow, text="秒截取一次屏幕（最小 %d 秒）" % SCREEN_MIN_INTERVAL,
                 bg=C.SURFACE, fg=C.TEXT_2, font=(FONT, 10)).pack(side="left")
        self.var_screen_half = tk.BooleanVar(value=bool(self.cfg.get("screen_half", True)))
        CheckBox(optrow, text="截图宽高减半（省 token、文件更小）",
                 variable=self.var_screen_half).pack(side="left", padx=(16, 0))

        trow = tk.Frame(inner_s, bg=C.SURFACE)
        trow.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        tk.Label(trow, text="截取目标", bg=C.SURFACE, fg=C.TEXT,
                 font=(FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        self.lbl_screen_target = tk.Label(trow, text="", bg=C.SURFACE, fg=C.TEXT_2,
                                          font=(FONT, 10), anchor="w")
        self.lbl_screen_target.pack(side="left", padx=(0, 10))
        RoundedButton(trow, text="选择窗口…", variant="chip", height=28, font_size=9,
                      padx=10, command=self.on_screen_pick_window).pack(side="left")
        RoundedButton(trow, text="整个屏幕", variant="chip", height=28, font_size=9,
                      padx=10, command=self.on_screen_fullscreen).pack(side="left",
                                                                      padx=(6, 0))
        RoundedButton(trow, text="立即截一张", variant="chip", height=28, font_size=9,
                      padx=10, command=self.on_screen_capture_now).pack(side="left",
                                                                       padx=(6, 0))

        self.screen_status = tk.Label(inner_s, text="", bg=C.SURFACE, fg=C.TEXT_3,
                                      font=(FONT, 9), anchor="w", justify="left",
                                      wraplength=820)
        self.screen_status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._hint(inner_s, "截图保存在 %s（最多 %d 张，超出先删最早的）；"
                            "屏幕识别上传的图片最多只有一张，新截图会自动换掉上一张。"
                 % (rel_path(app_dir() / SCREEN_TEMP_DIR), SCREEN_MAX_FILES),
                   wraplength=820).grid(row=4, column=0, columnspan=3, sticky="w",
                                        pady=(4, 0))

        # ---------- 记忆优化 ----------
        card_m, inner_m, mem_fold = self._fold_card(
            f, "记忆优化", "字数超限时调用该模型精简记忆，并按 W 管理记忆条数",
            expanded=False, group=group, key="mem")
        card_m.grid(row=6, column=0, sticky="ew", padx=PAD, pady=(5, 0))
        self._mem_fold = mem_fold
        inner_m.columnconfigure(1, weight=1)

        holder = tk.Frame(inner_m, bg=C.SURFACE)
        holder.grid(row=0, column=0, columnspan=2, sticky="ew")
        for i in range(4):
            holder.columnconfigure(i, weight=1)
        for idx, (name, url, model) in enumerate(PRESETS):
            RoundedButton(holder, text=name, variant="chip", height=26, font_size=9,
                          padx=8,
                          command=lambda u=url, m=model, n=name: self._apply_preset(
                              n, u, m, "mem")).grid(row=idx // 4, column=idx % 4,
                                                    sticky="ew", padx=3, pady=1)
        tk.Frame(inner_m, bg=C.BORDER, height=1).grid(row=1, column=0, columnspan=2,
                                                     sticky="ew", pady=(7, 5))

        self._vlabel(inner_m, "① 接口地址 Base URL").grid(row=2, column=0, sticky="w",
                                                       pady=(2, 0), padx=(0, 14))
        self.var_mem_base = tk.StringVar()
        self._field(inner_m, textvariable=self.var_mem_base).grid(
            row=2, column=1, sticky="ew", ipady=5)
        self._hint(inner_m, "例：https://api.deepseek.com/v1（不填就用不到记忆优化）").grid(
            row=3, column=1, sticky="w", pady=(2, 2))

        self._vlabel(inner_m, "② API Key").grid(row=4, column=0, sticky="w", padx=(0, 14))
        mkeybox = tk.Frame(inner_m, bg=C.SURFACE)
        mkeybox.grid(row=4, column=1, sticky="ew")
        mkeybox.columnconfigure(0, weight=1)
        self.var_mem_key = tk.StringVar()
        self.ent_mem_key = self._field(mkeybox, textvariable=self.var_mem_key, show="●")
        self.ent_mem_key.grid(row=0, column=0, sticky="ew", ipady=5)
        self.var_show_mem_key = tk.BooleanVar(value=False)
        CheckBox(mkeybox, text="显示", variable=self.var_show_mem_key,
                 command=lambda: self.ent_mem_key.configure(
                     show="" if self.var_show_mem_key.get() else "●")).grid(
            row=0, column=1, padx=(10, 0))

        self._vlabel(inner_m, "③ 模型名称").grid(row=6, column=0, sticky="w",
                                              padx=(0, 14), pady=(5, 0))
        mmodelbox = tk.Frame(inner_m, bg=C.SURFACE)
        mmodelbox.grid(row=6, column=1, sticky="ew", pady=(5, 0))
        mmodelbox.columnconfigure(0, weight=1)
        self.var_mem_model = tk.StringVar()
        self.cmb_mem_model = ttk.Combobox(mmodelbox, textvariable=self.var_mem_model,
                                          font=(FONT, 11), style="Modern.TCombobox")
        self.cmb_mem_model.grid(row=0, column=0, sticky="ew", ipady=3)
        self.btn_mem_models = RoundedButton(mmodelbox, text="获取可用模型",
                                            variant="outline", height=33,
                                            font_size=9, padx=12,
                                            command=self.on_get_mem_models)
        self.btn_mem_models.grid(row=0, column=1, padx=(10, 0))

        mopt = tk.Frame(inner_m, bg=C.SURFACE)
        mopt.grid(row=7, column=0, columnspan=2, sticky="w", pady=(9, 0))
        tk.Label(mopt, text="记忆优化的时间权重 k", bg=C.SURFACE, fg=C.TEXT_2,
                 font=(FONT, 10)).pack(side="left")
        self.var_mem_k = tk.StringVar(value="0.5")
        tk.Spinbox(mopt, from_=0.0, to=1.0, increment=0.1, width=5,
                   textvariable=self.var_mem_k, relief="flat", highlightthickness=1,
                   highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                   bg=C.SURFACE, fg=C.TEXT, buttonbackground=C.GRAY_SOFT,
                   font=(FONT, 10), justify="center").pack(side="left", padx=(6, 18))
        tk.Label(mopt, text="单次精简的记忆条数 x", bg=C.SURFACE, fg=C.TEXT_2,
                 font=(FONT, 10)).pack(side="left")
        self.var_mem_x = tk.StringVar(value=str(MEM_X_DEFAULT))
        tk.Spinbox(mopt, from_=1, to=50, width=4, textvariable=self.var_mem_x,
                   relief="flat", highlightthickness=1,
                   highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                   bg=C.SURFACE, fg=C.TEXT, buttonbackground=C.GRAY_SOFT,
                   font=(FONT, 10), justify="center").pack(side="left", padx=(6, 18))
        tk.Label(mopt, text="最多存在的记忆条数 y", bg=C.SURFACE, fg=C.TEXT_2,
                 font=(FONT, 10)).pack(side="left")
        self.var_mem_y = tk.StringVar(value=str(MEM_Y_DEFAULT))
        tk.Spinbox(mopt, from_=1, to=200, width=4, textvariable=self.var_mem_y,
                   relief="flat", highlightthickness=1,
                   highlightbackground=C.BORDER, highlightcolor=C.ACCENT,
                   bg=C.SURFACE, fg=C.TEXT, buttonbackground=C.GRAY_SOFT,
                   font=(FONT, 10), justify="center").pack(side="left", padx=(6, 0))

        self.mem_status = tk.Label(inner_m, text="", bg=C.SURFACE, fg=C.TEXT_3,
                                   font=(FONT, 9), anchor="w", justify="left",
                                   wraplength=820)
        self.mem_status.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._hint(inner_m, "记忆文件字数达到「单个总结文件的字数」或 2000 字（取小的那个）时，"
                            "程序会调用上面这个模型，把 W 最大的、末尾还没带 D 的 x 条记忆精简，"
                            "精简过的条目会在 W 后面加「,D」；带 D 的超过 y 条时，"
                            "删掉其中 W 最大的 y/2 条。W = k·T + (1-k)·L。",
                   wraplength=820).grid(row=9, column=0, columnspan=2, sticky="w",
                                        pady=(4, 0))

        # ---------- 对话总结 ----------
        card3, inner3 = self._card(
            f, "对话总结", "每生成一份多消耗一次请求；保存在 %s" % rel_path(record_dir()))
        card3.grid(row=7, column=0, sticky="ew", padx=PAD, pady=(5, 0))

        self._vlabel(inner3, "最多同时存在的总结文件").grid(row=0, column=0, sticky="w",
                                                          padx=(0, 12), pady=(2, 6))
        self.var_max_files = tk.StringVar()
        self._field(inner3, textvariable=self.var_max_files, width=6,
                    justify="center").grid(row=0, column=1, ipady=5, pady=(2, 6))
        self._vlabel(inner3, "个").grid(row=0, column=2, sticky="w", padx=(8, 36))
        self._vlabel(inner3, "单个总结文件的字数").grid(row=0, column=3, sticky="w",
                                                     padx=(0, 12))
        self.var_max_chars = tk.StringVar()
        self._field(inner3, textvariable=self.var_max_chars, width=8,
                    justify="center").grid(row=0, column=4, ipady=5, pady=(2, 6))
        self._vlabel(inner3, "字").grid(row=0, column=5, sticky="w", padx=(8, 0))

        tk.Label(inner3, text="每", bg=C.SURFACE, fg=C.TEXT,
                 font=(FONT, 10, "bold")).grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.var_rounds = tk.StringVar()
        self._field(inner3, textvariable=self.var_rounds, width=6,
                    justify="center").grid(row=1, column=1, ipady=5)
        self._vlabel(inner3, "轮对话后生成一次总结文件").grid(row=1, column=2,
                                                       columnspan=4, sticky="w",
                                                       padx=(8, 0))
        self._vlabel(inner3, "每").grid(row=2, column=0, sticky="w", padx=(0, 12),
                                       pady=(6, 0))
        self.var_review = tk.StringVar()
        self._field(inner3, textvariable=self.var_review, width=6,
                    justify="center").grid(row=2, column=1, ipady=5, pady=(6, 0))
        self._vlabel(inner3, "次聊天后对 AI 进行之前的对话内容复习").grid(
            row=2, column=2, columnspan=4, sticky="w", padx=(8, 0), pady=(6, 0))

        self._hint(inner3, "「复习」= 到达设定次数时，先把最新一份 .md 总结发给模型回顾，"
                           "再回答你这次的问题。",
                   wraplength=840).grid(row=3, column=0, columnspan=6, sticky="w",
                                        pady=(8, 0))

        # ---------- 底部 ----------
        actions = tk.Frame(f, bg=C.BG)
        actions.grid(row=8, column=0, sticky="ew", padx=PAD, pady=(10, 14))
        actions.columnconfigure(2, weight=1)
        RoundedButton(actions, text="保存并关闭设置", variant="primary", height=40,
                      padx=22, command=self.on_save_close).grid(row=0, column=0)
        self.btn_test = RoundedButton(actions, text="测试语言模型连接", variant="soft",
                                      height=40, padx=18,
                                      command=self.on_test_connection)
        self.btn_test.grid(row=0, column=1, padx=(10, 0))
        tk.Label(actions, text="保存后会记住配置，下次打开直接进入对话界面。",
                 bg=C.BG, fg=C.TEXT_3, font=(FONT, 9)).grid(row=0, column=2, sticky="w",
                                                            padx=(14, 0))

        e_base.focus_set()
        return outer

    def _build_chat(self, parent):
        f = tk.Frame(parent, bg=C.BG)
        f.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)

        # ---------- 顶栏 ----------
        head = tk.Frame(f, bg=C.SURFACE)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        left = tk.Frame(head, bg=C.SURFACE)
        left.grid(row=0, column=0, sticky="w", padx=(18, 8), pady=(10, 9))
        tk.Label(left, text="●", bg=C.SURFACE, fg=C.ACCENT,
                 font=(FONT, 10)).pack(side="left", padx=(0, 7))
        tk.Label(left, text=APP_TITLE, bg=C.SURFACE, fg=C.TEXT,
                 font=(FONT, 12, "bold")).pack(side="left")
        self.lbl_conn = tk.Label(left, text="", bg=C.SURFACE, fg=C.TEXT_3,
                                 font=(FONT, 9))
        self.lbl_conn.pack(side="left", padx=(12, 0), pady=(3, 0))
        right = tk.Frame(head, bg=C.SURFACE)
        right.grid(row=0, column=1, sticky="e", padx=(8, 14), pady=7)
        for _txt, _cmd in (("设置", self.show_setup), ("清空对话", self.on_clear),
                           ("导出对话", self.on_export)):
            RoundedButton(right, text=_txt, variant="ghost", height=30, font_size=9,
                          padx=12, command=_cmd).pack(side="left", padx=2)
        tk.Frame(head, bg=C.BORDER, height=1).grid(row=1, column=0, columnspan=2,
                                                  sticky="ew")

        # ---------- 对话区 ----------
        wrap = tk.Frame(f, bg=C.SURFACE, highlightthickness=1,
                        highlightbackground=C.BORDER, highlightcolor=C.BORDER)
        wrap.grid(row=1, column=0, sticky="nsew", padx=14, pady=(12, 8))
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.chat = tk.Text(wrap, wrap="word", font=(FONT, 11), relief="flat", bd=0,
                            highlightthickness=0, padx=14, pady=12, spacing1=1,
                            bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
                            selectbackground=C.ACCENT_SOFT, selectforeground=C.TEXT,
                            cursor="arrow")
        self.chat.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            style="Chat.Vertical.TScrollbar", command=self.chat.yview)
        vsb.grid(row=0, column=1, sticky="ns", pady=3, padx=(0, 2))
        self.chat.configure(yscrollcommand=vsb.set)
        self._init_chat_tags()
        # 只读（仍可用鼠标选择、Ctrl+C 复制）
        self.chat.bind("<Key>", self._block_edit)
        self.chat.bind("<Control-v>", lambda e: "break")
        self.chat.bind("<<Paste>>", lambda e: "break")
        self.chat.bind("<Button-3>", lambda e: "break")

        # ---------- 输入区 ----------
        bottom = tk.Frame(f, bg=C.BG)
        bottom.grid(row=2, column=0, sticky="ew", padx=14)
        bottom.columnconfigure(0, weight=1)
        box = tk.Frame(bottom, bg=C.SURFACE, highlightthickness=1,
                       highlightbackground=C.BORDER, highlightcolor=C.ACCENT)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)

        # 输入框左上角：添加图片按钮 + 已选图片（可多张、可单个删除）
        strip = tk.Frame(box, bg=C.SURFACE)
        strip.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 0))
        self.btn_add_image = RoundedButton(strip, text="＋ 图片", variant="chip",
                                           height=28, font_size=9, padx=10,
                                           command=self.on_add_images)
        self.btn_add_image.pack(side="left")
        self.btn_screen_toggle = RoundedButton(strip, text="📷 屏幕识别：关",
                                               variant="chip", height=28, font_size=9,
                                               padx=10, command=self.on_screen_toggle)
        self.btn_screen_toggle.pack(side="left", padx=(6, 0))
        self.attach_bar = tk.Frame(strip, bg=C.SURFACE)
        self.attach_bar.pack(side="left", padx=(6, 0))
        tk.Label(strip, text="可多选，也可把图片直接拖进输入框", bg=C.SURFACE,
                 fg=C.TEXT_3, font=(FONT, 9)).pack(side="left", padx=(10, 0))

        self.input = tk.Text(box, height=3, font=(FONT, 11), wrap="word", relief="flat",
                             bd=0, highlightthickness=0, padx=12, pady=9,
                             bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
                             selectbackground=C.ACCENT_SOFT, selectforeground=C.TEXT)
        self.input.grid(row=1, column=0, sticky="ew")
        self.input.tag_configure("ph", foreground=C.TEXT_3)
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<FocusIn>", self._on_input_focus_in)
        self._input_show_placeholder()
        # 支持把图片文件拖进输入框（Windows 原生拖放，失败也不影响其它功能）
        self._drop_ok = bool(enable_file_drop(self.input, self._on_files_dropped))
        enable_file_drop(box, self._on_files_dropped)
        enable_file_drop(strip, self._on_files_dropped)

        btns = tk.Frame(bottom, bg=C.BG)
        btns.grid(row=0, column=1, sticky="n", padx=(10, 0))
        self.btn_send = RoundedButton(btns, text="发送 ⏎", variant="primary", height=40,
                                      width=104, command=self.on_send)
        self.btn_send.pack()
        self.btn_stop = RoundedButton(btns, text="停止", variant="soft", height=36,
                                      width=104, command=self.on_stop)
        self.btn_stop.pack(pady=(8, 0))
        self.btn_stop.set_state("disabled")

        # ---------- 底部状态栏 ----------
        tk.Frame(f, bg=C.BORDER, height=1).grid(row=3, column=0, sticky="ew",
                                               pady=(10, 0))
        bar = tk.Frame(f, bg=C.BG)
        bar.grid(row=4, column=0, sticky="ew", padx=16, pady=(6, 9))
        bar.columnconfigure(0, weight=1)
        self.lbl_status = tk.Label(bar, text="就绪", bg=C.BG, fg=C.TEXT_2,
                                   font=(FONT, 9), anchor="w", justify="left")
        self.lbl_status.grid(row=0, column=0, sticky="ew")
        return f

    def _init_chat_tags(self):
        c = self.chat
        c.tag_configure("user_label", foreground=C.ACCENT, font=(FONT, 9, "bold"),
                        justify="right", spacing1=14, rmargin=14)
        c.tag_configure("user", background=C.ACCENT_SOFT, foreground="#17264A",
                        justify="right", lmargin1=110, lmargin2=110, rmargin=14,
                        spacing1=2, spacing3=6)
        c.tag_configure("assistant_label", foreground=C.TEXT_2, font=(FONT, 9, "bold"),
                        spacing1=14, lmargin1=4)
        c.tag_configure("assistant", background=C.GRAY_SOFT, foreground=C.TEXT,
                        lmargin1=4, lmargin2=4, rmargin=110, spacing1=2, spacing3=6)
        c.tag_configure("reason_label", foreground=C.TEXT_3, font=(FONT, 9, "bold"),
                        spacing1=10, lmargin1=4)
        c.tag_configure("reason", foreground="#6B7280", font=(FONT, 9),
                        lmargin1=4, lmargin2=4, rmargin=110, spacing3=2)
        c.tag_configure("err_label", foreground=C.RED, font=(FONT, 10, "bold"),
                        spacing1=12, lmargin1=4)
        c.tag_configure("error", background=C.RED_SOFT, foreground="#7F1D1D",
                        lmargin1=4, lmargin2=4, rmargin=110, spacing3=6)
        c.tag_configure("sys", foreground=C.TEXT_3, font=(FONT, 9),
                        lmargin1=4, lmargin2=4, rmargin=60, spacing3=4)
        c.tag_configure("attach", foreground=C.ACCENT, font=(FONT, 9),
                        justify="right", lmargin1=110, lmargin2=110, rmargin=14,
                        spacing3=4)
        c.tag_configure("vision", foreground="#6B7280", font=(FONT, 9),
                        lmargin1=4, lmargin2=4, rmargin=110, spacing1=4, spacing3=4)
        c.tag_configure("summary", foreground="#2B4A78", background="#EEF3FB",
                        font=(FONT, 9), lmargin1=4, lmargin2=4, rmargin=60,
                        spacing1=6, spacing3=6)

    def post(self, fn, *args):
        """把要在主线程执行的函数丢进队列。"""
        self.ui_queue.put(lambda: fn(*args))

    def _pump(self):
        while True:
            try:
                job = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                job()
            except Exception:
                _log(exc=True)
        self.root.after(50, self._pump)

    def _tick(self):
        if self.busy and self._session_start:
            el = time.time() - self._session_start
            filtering = bool(self.cfg.get("hide_reasoning"))
            if self._phase == "vision":
                self.lbl_status.configure(
                    text="正在识别图片（%d 张）…  %.1fs" % (self._vision_total, el))
            elif self._phase == "summarizing":
                self.lbl_status.configure(text="正在总结最近 %d 轮对话…  %.1fs"
                                               % (self._summary_settings()[0], el))
            elif self._first_token_at:
                extra = ""
                if filtering and self._reason_chars:
                    extra = "  ·  已过滤思考 %d 字" % self._reason_chars
                self.lbl_status.configure(
                    text="正在生成…  %.1fs  ·  已输出 %d 字%s" % (el, self._chars, extra))
            elif self._reason_seen:
                self.lbl_status.configure(
                    text="模型正在深度思考…  %.1fs%s"
                         % (el, "（思考内容已按设置过滤）" if filtering else ""))
            else:
                self.lbl_status.configure(text="已发送，等待模型响应…  %.1fs" % el)
        self.root.after(200, self._tick)

    def _insert(self, index, text, tags=()):
        self.chat.insert(index, text, tags)

    def _toggle_key(self):
        self.ent_key.configure(show="" if self.var_show_key.get() else "●")

    def _toggle_vlm_key(self):
        self.ent_vlm_key.configure(show="" if self.var_show_vlm_key.get() else "●")

    def _apply_preset(self, name, url, model, kind="llm"):
        if kind == "mem":
            self.var_mem_base.set(url)
            if not self.var_mem_model.get().strip():
                self.var_mem_model.set(model)
            self._set_mem_status("已填入「%s」的地址，模型名默认 %s（可改）。"
                                 % (name, model), "info")
            return
        if kind == "vlm":
            self.var_vlm_base.set(url)
            if not self.var_vlm_model.get().strip():
                self.var_vlm_model.set(model)
            self._set_vlm_status("已填入「%s」的视觉模型地址，模型名默认 %s（可改）。"
                                 % (name, model), "info")
            return
        self.var_base.set(url)
        if not self.var_model.get().strip():
            self.var_model.set(model)
        self._set_setup_status("已填入「%s」的地址，模型名默认 %s（可改）。" % (name, model), "info")

    def _set_setup_status(self, msg, kind="info"):
        colors = {"info": C.TEXT_2, "ok": C.GREEN, "error": C.RED, "busy": C.ACCENT}
        self.setup_status.configure(text=msg, fg=colors.get(kind, C.TEXT_2))

    def _set_vlm_status(self, msg, kind="info"):
        colors = {"info": C.TEXT_2, "ok": C.GREEN, "error": C.RED, "busy": C.ACCENT}
        self.vlm_status.configure(text=msg, fg=colors.get(kind, C.TEXT_2))

    def _fill_form(self, cfg):
        self.var_base.set(cfg.get("base_url", ""))
        self.var_key.set(cfg.get("api_key", ""))
        self.var_model.set(cfg.get("model", ""))
        self.var_temp.set(str(cfg.get("temperature", "0.7")))
        self.var_timeout.set(str(cfg.get("timeout", "120")))
        self.var_stream.set(bool(cfg.get("stream", True)))
        self.var_filter.set(bool(cfg.get("hide_reasoning", False)))
        self.var_vlm_base.set(cfg.get("vlm_base_url", ""))
        self.var_vlm_key.set(cfg.get("vlm_api_key", ""))
        self.var_vlm_model.set(cfg.get("vlm_model", ""))
        self.var_mem_base.set(cfg.get("mem_base_url", ""))
        self.var_mem_key.set(cfg.get("mem_api_key", ""))
        self.var_mem_model.set(cfg.get("mem_model", ""))
        self.var_mem_k.set(str(cfg.get("mem_k", "0.5")))
        self.var_mem_x.set(str(cfg.get("mem_x", MEM_X_DEFAULT)))
        self.var_mem_y.set(str(cfg.get("mem_y", MEM_Y_DEFAULT)))
        self.var_screen_on.set(bool(cfg.get("screen_watch", False)))
        self.var_screen_interval.set(str(cfg.get("screen_interval",
                                                 SCREEN_DEFAULT_INTERVAL)))
        self.var_screen_half.set(bool(cfg.get("screen_half", True)))
        self.screen_target = dict(cfg.get("screen_target") or {"kind": "screen"})
        self._update_screen_target_label()
        self.var_rounds.set(str(cfg.get("summary_rounds", ROUNDS_PER_SUMMARY)))
        self.var_review.set(str(cfg.get("review_rounds", REVIEW_ROUNDS)))
        self.var_max_files.set(str(cfg.get("summary_max_files", MAX_SUMMARY_FILES)))
        self.var_max_chars.set(str(cfg.get("summary_max_chars", MAX_SUMMARY_CHARS)))
        self.txt_sys.delete("1.0", "end")
        self.txt_sys.insert("1.0", cfg.get("system_prompt", ""))

    def _read_form(self) -> dict:
        temp = self.var_temp.get().strip() or "0.7"
        timeout = self.var_timeout.get().strip() or "120"

        def num(var, default):
            """空着用默认值；合法的正整数转成 int；非法内容原样返回，交给 _validate 报错。"""
            raw = var.get().strip()
            if raw == "":
                return default
            return int(raw) if raw.isdigit() else raw

        return {
            "base_url": self.var_base.get().strip(),
            "api_key": self.var_key.get().strip(),
            "model": self.var_model.get().strip(),
            "system_prompt": self.txt_sys.get("1.0", "end-1c").strip(),
            "temperature": temp,
            "stream": bool(self.var_stream.get()),
            "hide_reasoning": bool(self.var_filter.get()),
            "timeout": timeout,
            "vlm_base_url": self.var_vlm_base.get().strip(),
            "vlm_api_key": self.var_vlm_key.get().strip(),
            "vlm_model": self.var_vlm_model.get().strip(),
            "summary_rounds": num(self.var_rounds, ROUNDS_PER_SUMMARY),
            "mem_base_url": self.var_mem_base.get().strip(),
            "mem_api_key": self.var_mem_key.get().strip(),
            "mem_model": self.var_mem_model.get().strip(),
            "mem_k": (self.var_mem_k.get().strip() or "0.5"),
            "mem_x": num(self.var_mem_x, MEM_X_DEFAULT),
            "mem_y": num(self.var_mem_y, MEM_Y_DEFAULT),
            "screen_watch": bool(self.var_screen_on.get()),
            "screen_interval": num(self.var_screen_interval, SCREEN_DEFAULT_INTERVAL),
            "screen_half": bool(self.var_screen_half.get()),
            "screen_target": dict(self.screen_target),
            "review_rounds": num(self.var_review, REVIEW_ROUNDS),
            "summary_max_files": num(self.var_max_files, MAX_SUMMARY_FILES),
            "summary_max_chars": num(self.var_max_chars, MAX_SUMMARY_CHARS),
        }

    def _validate(self, cfg, need_model=True):
        if not cfg["base_url"]:
            return "请填写「接口地址 Base URL」，例如 https://api.deepseek.com/v1"
        if "://" not in cfg["base_url"]:
            return "接口地址要以 http:// 或 https:// 开头（本地模型可写 http://localhost:11434/v1）"
        if need_model and not cfg["model"]:
            return "请填写「模型名称」，或点右边的「🔄 获取可用模型」自动列出。"
        for key, label, lo, hi in (
                ("summary_max_files", "最多同时存在的总结文件", 1, 9999),
                ("summary_max_chars", "单个总结文件的字数", 200, 200000),
                ("summary_rounds", "每多少轮对话后生成一次总结文件", 1, 9999),
                ("review_rounds", "每多少次聊天后对 AI 复习历史总结", 1, 9999)):
            raw = str(cfg.get(key, "")).strip()
            if raw == "":
                continue                      # 留空就用默认值
            if not raw.isdigit() or not (lo <= int(raw) <= hi):
                return "「对话总结」里的「%s」请填 %d ~ %d 之间的整数。" % (label, lo, hi)
        if cfg.get("vlm_base_url") and not cfg.get("vlm_model"):
            return "「视觉模型（VLM）」填了接口地址，还需要填模型名称（可点「获取可用模型」）。"
        try:
            kv = float(str(cfg.get("mem_k", "0.5")).strip())
        except (TypeError, ValueError):
            return "「记忆优化」的时间权重 k 请填 0 ~ 1 之间的数字（例如 0.5）。"
        if not (0.0 <= kv <= 1.0):
            return "「记忆优化」的时间权重 k 请填 0 ~ 1 之间的数字（例如 0.5）。"
        for key, label, lo, hi in (("mem_x", "单次精简的记忆条数", 1, 50),
                                   ("mem_y", "最多存在的记忆条数", 1, 200)):
            raw2 = str(cfg.get(key, "")).strip()
            if raw2 == "" or not raw2.isdigit() or not (lo <= int(raw2) <= hi):
                return "「记忆优化」的「%s」请填 %d ~ %d 之间的整数。" % (label, lo, hi)
        raw = str(cfg.get("screen_interval", "")).strip()
        if raw == "" or not raw.isdigit() or not (SCREEN_MIN_INTERVAL <= int(raw) <= 3600):
            return ("「屏幕识别」的间隔请填 %d ~ 3600 之间的整数（秒）。"
                    % SCREEN_MIN_INTERVAL)
        if cfg.get("vlm_model") and not cfg.get("vlm_base_url"):
            return "「视觉模型（VLM）」填了模型名称，还需要填接口地址 Base URL。"
        return None

    def _read_vlm_form(self):
        """读取「视觉模型（VLM）」三个输入框。"""
        return {
            "base_url": self.var_vlm_base.get().strip(),
            "api_key": self.var_vlm_key.get().strip(),
            "model": self.var_vlm_model.get().strip(),
        }

    def _vlm_settings(self):
        """已保存的视觉模型配置 (base_url, api_key, model)。"""
        return (str(self.cfg.get("vlm_base_url", "") or "").strip(),
                str(self.cfg.get("vlm_api_key", "") or "").strip(),
                str(self.cfg.get("vlm_model", "") or "").strip())

    def _vision_ready(self):
        base, _key, model = self._vlm_settings()
        return bool(base and model)

    def _find_test_image(self):
        """程序目录下的测试图片（默认 test.jpeg）。"""
        return find_test_image()

    def _review_rounds(self):
        """每多少次聊天让模型复习一次历史总结（非法值回退默认）。"""
        try:
            v = int(str(self.cfg.get("review_rounds", REVIEW_ROUNDS)).strip())
        except (TypeError, ValueError):
            return REVIEW_ROUNDS
        return max(1, min(9999, v))

    def _summary_settings(self):
        """返回用户设置的 (每几轮总结, 最多几份文件, 单份字数上限)，非法值回退默认。"""
        def pick(key, default, lo, hi):
            try:
                v = int(str(self.cfg.get(key, default)).strip())
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        return (pick("summary_rounds", ROUNDS_PER_SUMMARY, 1, 9999),
                pick("summary_max_files", MAX_SUMMARY_FILES, 1, 9999),
                pick("summary_max_chars", MAX_SUMMARY_CHARS, 200, 200000))

    def _has_saved_config(self):
        """是否已经保存过可用的配置（有地址和模型名）。"""
        if not normalize_base_url(self.cfg.get("base_url")) or not self.cfg.get("model"):
            return False
        return bool(self.cfg.get("configured", True))   # 老版本配置文件没有这个键

    # ---------------- 页面切换 ---------------- #
    def show_setup(self, initial=False):
        if not initial:
            self._fill_form(self.cfg)
        self.setup_frame.tkraise()
        if not initial:
            self._set_setup_status("可以修改配置后重新「保存并关闭设置」。", "info")

    def on_exit_setup(self):
        """右上角「退出设置」：不保存本次填写，直接离开设置界面。"""
        if self._has_saved_config():
            self._fill_form(self.cfg)      # 丢弃未保存的修改，恢复成已保存的配置
            self.lbl_status.configure(text="已退出设置，本次修改未保存。")
            self.show_chat()
            return
        if messagebox.askyesno(
                APP_TITLE,
                "还没有保存过可用的配置。\n退出设置后程序会关闭，确定吗？"):
            self.root.destroy()

    def show_chat(self):
        base = normalize_base_url(self.cfg["base_url"])
        self.lbl_conn.configure(
            text="接口 %s  ·  模型 %s  ·  %s  ·  %s" % (
                base, self.cfg["model"],
                "流式输出" if self.cfg.get("stream") else "整段返回",
                "已过滤思考" if self.cfg.get("hide_reasoning") else "显示思考"))
        self.chat_frame.tkraise()
        self.input.focus_set()

    # ---------------- 测试连接 / 获取模型 ---------------- #
    def on_test_connection(self):
        if self._job_running:
            return
        cfg = self._read_form()
        err = self._validate(cfg, need_model=False)
        if err:
            self._set_setup_status("⚠️ " + err, "error")
            return
        self._job_running = True
        self.btn_test.configure(state="disabled")
        self._set_setup_status("⏳ 正在连接 %s …" % normalize_base_url(cfg["base_url"]), "busy")

        def work():
            base, key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
            timeout = 20.0
            try:
                ids = list_models(base, key, timeout=timeout)
                if ids:
                    self.post(self._test_ok, ids)
                    return
                raise LLMError("接口没有返回任何模型。")
            except LLMError as e:
                # /models 不可用时，退化为一次极短的对话请求
                if not model:
                    self.post(self._test_fail, str(e))
                    return
                try:
                    chat_completion(
                        base, key, model,
                        [{"role": "user", "content": "你好"}],
                        stream=False, timeout=timeout,
                    )
                    self.post(self._set_setup_status,
                              "✅ 连接成功！（该服务不提供模型列表接口，但对话接口可用）", "ok")
                except Exception as e2:
                    self.post(self._test_fail, str(to_llm_error(e2, "对话请求")))
            except Exception as e:
                self.post(self._test_fail, str(to_llm_error(e, "连接测试")))
            finally:
                self.post(self._test_done)

        threading.Thread(target=work, daemon=True).start()

    def _test_ok(self, ids):
        self.cmb_model.configure(values=ids)
        if not self.var_model.get().strip():
            self.var_model.set(ids[0])
        self._set_setup_status(
            "✅ 连接成功！共发现 %d 个模型，已填入下拉框（第一个：%s）。" % (len(ids), ids[0]), "ok")

    def _test_fail(self, msg):
        self._set_setup_status("❌ " + msg, "error")

    def _test_done(self):
        self._job_running = False
        self.btn_test.configure(state="normal")

    def on_test_vlm(self):
        """测试「视觉模型（VLM）」接口是否可用。"""
        if self._job_running:
            return
        cfg = self._read_vlm_form()
        if not cfg["base_url"]:
            self._set_vlm_status("⚠️ 请先填写视觉模型的接口地址 Base URL。", "error")
            return
        self._job_running = True
        self.btn_vlm_test.set_state("disabled")
        self._set_vlm_status("⏳ 正在连接 %s …" % normalize_base_url(cfg["base_url"]), "busy")

        def work():
            try:
                ids = list_models(cfg["base_url"], cfg["api_key"], timeout=20.0)
                if ids:
                    self.post(self._vlm_test_ok, ids)
                    return
                raise LLMError("接口没有返回任何模型。")
            except LLMError as e:
                if not cfg["model"]:
                    self.post(self._vlm_test_fail, str(e))
                    return
                try:
                    chat_completion(cfg["base_url"], cfg["api_key"], cfg["model"],
                                    [{"role": "user", "content": "你好"}],
                                    stream=False, timeout=20.0)
                    self.post(self._set_vlm_status,
                              "✅ 视觉模型接口连接成功（该服务不提供模型列表，但对话接口可用）。",
                              "ok")
                except Exception as e2:
                    self.post(self._vlm_test_fail, str(to_llm_error(e2, "视觉模型请求")))
            except Exception as e:
                self.post(self._vlm_test_fail, str(to_llm_error(e, "视觉模型连接测试")))
            finally:
                self.post(self._vlm_job_done)

        threading.Thread(target=work, daemon=True).start()

    def _vlm_test_ok(self, ids):
        self.cmb_vlm_model.configure(values=ids)
        if not self.var_vlm_model.get().strip():
            self.var_vlm_model.set(ids[0])
        self._set_vlm_status("✅ 连接成功！共发现 %d 个模型，已填入下拉框（第一个：%s）。"
                             % (len(ids), ids[0]), "ok")

    def _vlm_test_fail(self, msg):
        self._set_vlm_status("❌ " + msg, "error")

    def _vlm_job_done(self):
        self._job_running = False
        self.btn_vlm_test.set_state("normal")
        self.btn_vlm_image.set_state("normal")

    def on_get_vlm_models(self):
        """拉取视觉模型可用模型列表。"""
        if self._job_running:
            return
        cfg = self._read_vlm_form()
        if not cfg["base_url"]:
            self._set_vlm_status("⚠️ 请先填写视觉模型的接口地址 Base URL。", "error")
            return
        self._job_running = True
        self.btn_vlm_models.set_state("disabled")
        self._set_vlm_status("⏳ 正在获取视觉模型列表…", "busy")

        def work():
            try:
                ids = list_models(cfg["base_url"], cfg["api_key"], timeout=20.0)
                if not ids:
                    raise LLMError("接口没有返回任何模型，请手动填写模型名称。")
                self.post(self._vlm_got_models, ids)
            except Exception as e:
                self.post(self._set_vlm_status,
                          "❌ " + str(to_llm_error(e, "获取模型列表")), "error")
            finally:
                self.post(self._vlm_models_done)

        threading.Thread(target=work, daemon=True).start()

    def _vlm_got_models(self, ids):
        self.cmb_vlm_model.configure(values=ids)
        if not self.var_vlm_model.get().strip():
            self.var_vlm_model.set(ids[0])
        self._set_vlm_status("✅ 找到 %d 个模型，点下拉箭头即可选择。" % len(ids), "ok")

    def _vlm_models_done(self):
        self._job_running = False
        self.btn_vlm_models.set_state("normal")

    def on_test_vlm_image(self):
        """用程序目录里的 test.jpeg 试一次图片识别，确认 VLM 真的能看图。"""
        if self._job_running:
            return
        cfg = self._read_vlm_form()
        if not cfg["base_url"] or not cfg["model"]:
            self._set_vlm_status("⚠️ 请先填写视觉模型的接口地址和模型名称，再测试识别。",
                                 "error")
            return
        img = self._find_test_image()
        if img is None:
            self._set_vlm_status("⚠️ 没找到测试图片：请在程序目录放一张 test.jpeg。", "error")
            return
        self._job_running = True
        self.btn_vlm_image.set_state("disabled")
        size_kb = img.stat().st_size / 1024.0
        self._set_vlm_status("⏳ 正在把 %s（%.0f KB，%s）发给视觉模型识别…"
                             % (img.name, size_kb, image_mime(img)), "busy")

        def work():
            try:
                payload = [{"role": "user", "content": [
                    {"type": "text", "text": VLM_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_data_url(img)}}]}]
                text, _r, _u = chat_completion(
                    cfg["base_url"], cfg["api_key"], cfg["model"], payload,
                    stream=False, temperature=0.2, timeout=90.0)
                text = (text or "").strip()
                if not text:
                    raise LLMError("视觉模型没有返回任何描述内容。")
                self.post(self._vlm_image_ok, img.name, text)
            except Exception as e:
                self.post(self._vlm_test_fail, str(to_llm_error(e, "图片识别测试")))
            finally:
                self.post(self._vlm_job_done)

        threading.Thread(target=work, daemon=True).start()

    def _vlm_image_ok(self, name, text):
        self._set_vlm_status("✅ 视觉模型能识别图片（%s），共返回 %d 字。" % (name, len(text)),
                             "ok")
        messagebox.showinfo("视觉模型识别结果（%s）" % name, text[:3000])

    def on_get_models(self):
        if self._job_running:
            return
        cfg = self._read_form()
        err = self._validate(cfg, need_model=False)
        if err:
            self._set_setup_status("⚠️ " + err, "error")
            return
        self._job_running = True
        self.btn_models.configure(state="disabled")
        self._set_setup_status("⏳ 正在获取模型列表…", "busy")

        def work():
            try:
                ids = list_models(cfg["base_url"], cfg["api_key"], timeout=20.0)
                if not ids:
                    raise LLMError("接口没有返回任何模型，请手动填写模型名称。")
                self.post(self._got_models, ids)
            except Exception as e:
                self.post(self._set_setup_status, "❌ " + str(to_llm_error(e, "获取模型列表")), "error")
            finally:
                self.post(self._models_done)

        threading.Thread(target=work, daemon=True).start()

    def _got_models(self, ids):
        self.cmb_model.configure(values=ids)
        if not self.var_model.get().strip():
            self.var_model.set(ids[0])
        self._set_setup_status("✅ 找到 %d 个模型，点下拉箭头即可选择。" % len(ids), "ok")

    def _models_done(self):
        self._job_running = False
        self.btn_models.configure(state="normal")

    # ---------------- 开始对话 ---------------- #
    def on_save_close(self):
        """「保存并关闭设置」：校验 -> 保存到本机 -> 回到对话界面。"""
        cfg = self._read_form()
        err = self._validate(cfg)
        if err:
            self._set_setup_status("⚠️ " + err, "error")
            return
        base = normalize_base_url(cfg["base_url"])
        if not cfg["api_key"] and not is_local_url(base):
            if not messagebox.askyesno(
                    "没有填写 API Key",
                    "大多数在线服务都要求 API Key，否则会返回 401 未授权。\n\n"
                    "确定要直接继续吗？（本地模型可以忽略这个提示）"):
                return
        cfg["base_url"] = base
        cfg["configured"] = True       # 记住“配置已完成”，下次打开直接进对话界面
        self.cfg = cfg
        self.var_base.set(base)
        save_config(cfg)
        self._set_setup_status("✅ 配置已保存到 %s" % CONFIG_PATH, "ok")
        self._screen_set_on(bool(self.cfg.get("screen_watch")), save=False)
        self.show_chat()

    on_start_chat = on_save_close      # 兼容旧名称

    # ---------------- 发送消息 ---------------- #
    def _on_return(self, event):
        if event.state & 0x0001:      # 按住 Shift：换行
            return None
        self.on_send()
        return "break"

    def _block_edit(self, event):
        if event.state & 0x0004 and event.keysym.lower() in ("c", "a", "insert"):
            return None               # 允许 Ctrl+C / Ctrl+A / Ctrl+Insert
        nav = ("Up", "Down", "Left", "Right", "Home", "End", "Prior", "Next", "Tab",
               "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
               "Escape", "Caps_Lock", "Num_Lock")
        if event.keysym in nav:
            return None
        return "break"                # 其它按键一律不修改文本

    def _input_is_placeholder(self):
        return self.input.get("1.0", "end-1c").strip() == INPUT_PLACEHOLDER

    def _input_show_placeholder(self):
        if not self.input.get("1.0", "end-1c").strip():
            self.input.insert("1.0", INPUT_PLACEHOLDER, ("ph",))

    def _on_input_focus_in(self, _event):
        if self._input_is_placeholder():
            self.input.delete("1.0", "end")

    def _on_input_focus_out(self, _event=None):
        self._input_show_placeholder()

    def on_send(self, event=None):
        if self.busy:
            return
        text = self.input.get("1.0", "end-1c").strip()
        if text == INPUT_PLACEHOLDER:
            text = ""
        images = list(self.attached_images)
        if not text and not images:
            return
        if images and not self._vision_ready():
            self.lbl_status.configure(
                text="⚠️ 已附 %d 张图片，但还没配置视觉模型（VLM）。" % len(images))
            self._insert("end-1c",
                         "\n⚠️ 没有配置视觉模型（VLM），图片无法识别：请到"
                         "「设置 → 视觉模型（VLM）接口信息」填写接口地址与模型名称。\n",
                         ("error",))
            self.chat.see("end")
            return
        self.input.delete("1.0", "end")

        self._session_start = time.time()
        self._first_token_at = None
        self._chars = 0
        self._started_content = False
        self._started_reason = False
        self._live_reason = []
        self._reason_chars = 0
        self._reason_seen = False
        self.busy = True
        self.stop_event.clear()
        self._resp = None
        self._live_text = []
        self._phase = "waiting"
        self._pending_carry = False
        self._summary_note = ""
        self._review_chars = 0
        self._vision_total = 0
        self._vision_note = ""
        self.btn_send.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self._insert("end", "\n你\n", ("user_label",))
        if text:
            self._insert("end-1c", text, ("user",))
        if images:
            self._insert("end-1c",
                         ("\n" if text else "")
                         + "📎 已附加 %d 张图片：%s"
                         % (len(images), "、".join(p.name for p in images)),
                         ("attach",))
        self.chat.see("end")
        self._clear_attachments()      # 发送后清空已上传的图片

        # 计数：满 N 轮先生成 .md 总结文件；满 M 次聊天先让模型复习历史总结
        self.rounds_in_batch += 1
        need_summary = self.rounds_in_batch >= self._summary_settings()[0]
        self._since_review += 1
        need_review = self._since_review >= self._review_rounds()
        if need_review:
            self._since_review = 0
        self.lbl_status.configure(
            text=("正在总结最近 %d 轮对话…" % self._summary_settings()[0])
            if need_summary else "已发送，等待模型响应…")

        threading.Thread(target=self._worker,
                         args=(need_summary, need_review, text, tuple(images)),
                         daemon=True).start()

    # ---------------- 图片上传（发送给视觉模型） ---------------- #
    def on_add_images(self):
        """选择本地图片（可多选）。"""
        paths = filedialog.askopenfilenames(
            title="选择图片（可多选）",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.webp *.gif *.bmp"),
                       ("所有文件", "*.*")])
        if paths:
            self._add_images(paths)

    def _on_files_dropped(self, paths):
        """拖拽进来自动添加（只收图片文件）。"""
        self._add_images(paths)

    def _add_images(self, paths):
        added, skipped = [], []
        for raw in paths or ():
            try:
                path = Path(raw)
            except Exception:
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in IMAGE_EXTS:
                skipped.append(path.name)
                continue
            if path not in self.attached_images:
                self.attached_images.append(path)
                added.append(path.name)
        self._render_attachments()
        if added:
            self.lbl_status.configure(
                text="已添加 %d 张图片（%s）：发送时会先交给视觉模型识别。"
                     % (len(added), "、".join(added)))
        elif skipped:
            self.lbl_status.configure(
                text="⚠️ 只支持图片文件（%s），已忽略：%s"
                     % (" ".join(IMAGE_EXTS), "、".join(skipped)))

    def _render_attachments(self):
        for child in self.attach_bar.winfo_children():
            child.destroy()
        for path in list(self.attached_images):
            name = path.name
            if path == self.auto_image:
                name = "📷 " + name           # 标记出「屏幕识别」自动上传的那张
            if len(name) > 20:
                name = name[:17] + "..."
            RoundedButton(self.attach_bar, text="%s ✕" % name, variant="chip",
                          height=26, font_size=9, padx=9,
                          command=lambda p=path: self._remove_image(p)).pack(
                side="left", padx=(0, 6))

    def _remove_image(self, path):
        if path in self.attached_images:
            self.attached_images.remove(path)
        self._render_attachments()

    def _clear_attachments(self):
        """发送后清空所有待发图片。"""
        self.attached_images = []
        self.auto_image = None
        self._render_attachments()

    def _build_messages(self, review_text=""):
        msgs = []
        sp = (self.cfg.get("system_prompt") or "").strip()
        if sp:
            msgs.append({"role": "system", "content": sp})
        if review_text:
            # 让模型先回顾以前的对话总结，再回答本次问题
            msgs.append({"role": "system", "content": REVIEW_PREFIX + review_text})
        for m in self.history:
            # 只回传角色和正文：思考内容绝不发给模型，避免污染上下文
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def _note_review(self, lo, hi, count, chars):
        """在聊天区显示一行提示：这次让模型按原序号回顾了以前的记忆。"""
        if self._started_content or self._started_reason:
            return
        if count:
            what = "按原序号 %d~%d 共 %d 条、%d 字" % (lo, hi, count, chars)
        else:
            what = "共 %d 字" % chars
        self._insert("end-1c", "\n📖 已让模型回顾以前的记忆（%s）\n" % what, ("sys",))
        self.chat.see("end")

    def _status_chat(self, text):
        self.lbl_status.configure(text=text)

    def _on_open(self, resp):
        self._resp = resp

    def _on_delta(self, kind, piece):
        self.post(self._append_delta, kind, piece)

    def _append_delta(self, kind, piece):
        if kind == "reasoning":
            self._reason_chars += len(piece)
            self._reason_seen = True
            if self.cfg.get("hide_reasoning"):
                return                    # 开启过滤：思考内容不显示，只在状态栏计数
            if not self._started_reason:
                self._started_reason = True
                self._insert("end-1c", "💭 思考中…\n", ("reason_label",))
            self._insert("end-1c", piece, ("reason",))
            self._live_reason.append(piece)
        else:
            if not self._started_content:
                self._started_content = True
                if self._started_reason:
                    self._insert("end-1c", "\n\n", ("assistant",))
            self._insert("end-1c", piece, ("assistant",))
            self._live_text.append(piece)
            self._chars += len(piece)
        if self._first_token_at is None:
            self._first_token_at = time.time()
        self.chat.see("end")

    def _begin_assistant(self):
        """插入「助手」标题（放在总结提示之后，保证“先生成文件、后输出回复”）。"""
        self._insert("end", "\n助手\n", ("assistant_label",))
        self.chat.see("end")

    def _worker(self, need_summary=False, need_review=False, user_text="", images=()):
        text, reasoning, usage, err, stopped = "", "", None, None, False
        try:
            vision_texts = []
            if images:
                self._phase = "vision"
                self._vision_total = len(images)
                try:
                    vision_texts = self._recognize_images(images)
                except Exception as exc:
                    self.post(self._restore_attachments, images)
                    self.post(self._begin_assistant)
                    stopped = self.stop_event.is_set()
                    err = None if stopped else to_llm_error(exc, "图片识别（视觉模型）")
                    if err:
                        _log(exc=True)
                    self.post(self._finish, "", "", None, err, stopped)
                    return
                self._phase = "waiting"
                self._vision_note = "👁 已识别 %d 张图片" % len(vision_texts)
            # 本轮用户消息写入上下文（含图片识别内容）
            self.history.append({"role": "user",
                                 "content": self._compose_user_text(vision_texts, user_text)})
        except Exception as e:
            if self.stop_event.is_set():
                stopped = True
            else:
                err = to_llm_error(e, "请求失败")
                _log(exc=True)
            self.post(self._finish, "", "", None, err, stopped)
            return
        try:
            if need_summary:
                self._phase = "summarizing"
                self._pending_carry = self._do_summary()   # 先生成 .md 文件
                self._phase = "waiting"
                if self.stop_event.is_set():
                    self.post(self._begin_assistant)
                    self.post(self._finish, "", "", None, None, True)
                    return
            review_text = ""
            if need_review:
                review_text = self._read_previous_summary()
                if review_text:
                    self._review_chars = len(review_text)
                    seqs = [int(it.get("seq") or 0)
                            for it in self._load_summary_state()]
                    seqs = [s for s in seqs if s > 0]
                    self.post(self._note_review,
                              min(seqs) if seqs else 0,
                              max(seqs) if seqs else 0,
                              len(seqs), self._review_chars)
                else:
                    self.post(self._status_chat,
                              "ℹ️ 本轮该复习历史总结了，但还没有任何 .md 总结可复习，已跳过")
            self.post(self._begin_assistant)
            text, reasoning, usage = chat_completion(
                self.cfg["base_url"], self.cfg["api_key"], self.cfg["model"],
                self._build_messages(review_text),
                stream=bool(self.cfg.get("stream", True)),
                temperature=self.cfg.get("temperature"),
                timeout=float(self.cfg.get("timeout") or 120),
                on_delta=self._on_delta,
                stop_event=self.stop_event,
                on_open=self._on_open,
            )
        except Exception as e:
            if self.stop_event.is_set():
                stopped = True
            else:
                err = to_llm_error(e, "对话请求")
                _log(exc=True)
        else:
            stopped = self.stop_event.is_set()
        self.post(self._finish, text, reasoning, usage, err, stopped)

    def _finish(self, text, reasoning, usage, err, stopped):
        """一轮结束的收尾：先做界面更新，再把对话与上下文落盘。"""
        try:
            self._finish_impl(text, reasoning, usage, err, stopped)
        finally:
            self._save_ui_state()

    def _finish_impl(self, text, reasoning, usage, err, stopped):
        self.busy = False
        self._resp = None
        self.btn_send.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        elapsed = time.time() - self._session_start if self._session_start else 0.0

        if not text.strip():
            text = "".join(self._live_text)   # 出错/中断时保留已经收到的内容

        if err is not None:
            self._insert("end-1c", "\n")
            self._insert("end", "\n⚠️ 请求失败\n", ("err_label",))
            self._insert("end-1c", str(err), ("error",))
            self.chat.see("end")
            self.lbl_status.configure(text="❌ 失败（%.1fs）— 详情看上方提示" % elapsed)
            return

        if not text.strip():
            if self._started_reason:
                self._insert("end-1c", "\n", ("reason",))
            if self._reason_seen and self.cfg.get("hide_reasoning"):
                self._insert(
                    "end-1c",
                    "\n（本轮模型只输出了思考内容（共 %d 字），已按设置过滤。"
                    "想查看它：在「设置」里取消勾选「过滤模型的思考内容」）\n"
                    % self._reason_chars, ("sys",))
            elif stopped:
                self._insert("end-1c", "\n（已停止，本次没有产生回复）\n", ("sys",))
            else:
                self._insert("end-1c", "\n（模型没有返回任何内容）\n", ("sys",))
            self.chat.see("end")
            self.lbl_status.configure(
                text=("⏹ 已停止（%.1fs）" if stopped else "⚠️ 空回复（%.1fs）") % elapsed)
            return

        record = {"role": "assistant", "content": text}
        mind = (reasoning or "".join(self._live_reason) or "").strip()
        if mind and not self.cfg.get("hide_reasoning"):
            record["reasoning"] = mind      # 仅用于导出；回传模型前会被剔除
        self.history.append(record)
        if self._pending_carry:
            # 本次是总结边界那一轮：这条回复顺延，下次生成总结时放在最前面
            self.carry_over = text
            self._pending_carry = False
        self._insert("end-1c", "\n")
        self.chat.see("end")

        ttft = ""
        if self._first_token_at:
            ttft = "首字 %.2fs  ·  " % (self._first_token_at - self._session_start)
        extra = ""
        if isinstance(usage, dict):
            tk_ = usage.get("total_tokens")
            if tk_:
                extra = "  ·  tokens %s" % tk_
        if self.cfg.get("hide_reasoning") and self._reason_chars:
            extra += "  ·  已过滤思考 %d 字" % self._reason_chars
        if self._summary_note:
            extra += "  ·  " + self._summary_note        # 本轮生成过总结则一并显示
        if self._review_chars:
            extra += "  ·  📖 已复习历史总结 %d 字" % self._review_chars
        if self._vision_note:
            extra += "  ·  " + self._vision_note
        if stopped:
            self.lbl_status.configure(
                text="⏹ 已停止  ·  %s总耗时 %.1fs  ·  输出 %d 字%s"
                     % (ttft, elapsed, len(text), extra))
        else:
            self.lbl_status.configure(
                text="✅ 完成  ·  %s总耗时 %.1fs  ·  输出 %d 字%s"
                     % (ttft, elapsed, len(text), extra))

    # ---------------- 图片 -> 视觉模型 ---------------- #
    def _restore_attachments(self, images):
        """视觉识别失败时把图片放回待发列表，方便用户重试。"""
        for path in images:
            p = Path(path)
            if p not in self.attached_images:
                self.attached_images.append(p)
        self._render_attachments()
        self.lbl_status.configure(text="图片识别失败，图片已保留在输入框，可改好设置后重发。")

    def _recognize_images(self, images):
        """把图片逐张发给视觉模型，返回每张图的描述（返回顺序与图片顺序一致）。"""
        base, key, model = self._vlm_settings()
        if not (base and model):
            raise LLMError("没有配置视觉模型（VLM），无法识别图片。")
        limit = MAX_IMAGE_MB * 1024 * 1024
        results = []
        for idx, path in enumerate(images, 1):
            if self.stop_event.is_set():
                break
            path = Path(path)
            size = path.stat().st_size if path.exists() else 0
            if size > limit:
                raise LLMError("图片 %s 太大（%.1f MB），请压缩到 %.0f MB 以内再上传。"
                               % (path.name, size / 1048576.0, MAX_IMAGE_MB))
            self.post(self._status_chat,
                      "正在识别图片 %d/%d：%s" % (idx, len(images), path.name))
            payload = [{"role": "user", "content": [
                {"type": "text", "text": VLM_PROMPT},
                {"type": "image_url", "image_url": {"url": image_data_url(path)}}]}]
            out, _reason, _usage = chat_completion(
                base, key, model, payload, stream=False, temperature=0.2,
                timeout=max(90.0, float(self.cfg.get("timeout") or 120)),
                stop_event=self.stop_event, on_open=self._on_open)
            out = (out or "").strip() or "（视觉模型没有返回描述）"
            results.append(out)
            self.post(self._vision_done, path.name, out)
        return results

    def _vision_done(self, name, text):
        """把视觉模型的识别结果显示在对话里（灰色小字）。"""
        self._insert("end-1c", "\n👁 %s 识别结果：%s" % (name, text), ("vision",))
        self.chat.see("end")

    def _compose_user_text(self, vision_texts, user_text):
        """按需求拼出发给语言模型的内容：先逐张说明图片（带视觉模型输出），再接用户文字。"""
        parts = ["用户刚刚发送了一张（%s）的图片，" % t for t in vision_texts]
        return "".join(parts) + (user_text or "")

    # ---------------- 记忆优化 ---------------- #
    def _set_mem_status(self, msg, kind="info"):
        colors = {"info": C.TEXT_3, "ok": C.GREEN, "error": C.RED, "busy": C.ACCENT}
        try:
            self.mem_status.configure(text=msg, fg=colors.get(kind, C.TEXT_3))
        except Exception:
            pass

    def _memory_settings(self):
        """(base_url, api_key, model, k, x, y)"""
        def num(key, default, lo, hi):
            try:
                v = int(str(self.cfg.get(key, default)).strip())
            except Exception:
                v = default
            return max(lo, min(hi, v))
        try:
            k = float(str(self.cfg.get("mem_k", "0.5")).strip())
        except Exception:
            k = 0.5
        return (str(self.cfg.get("mem_base_url") or "").strip(),
                str(self.cfg.get("mem_api_key") or "").strip(),
                str(self.cfg.get("mem_model") or "").strip(),
                min(1.0, max(0.0, k)),
                num("mem_x", MEM_X_DEFAULT, 1, 50),
                num("mem_y", MEM_Y_DEFAULT, 1, 200))

    def on_get_mem_models(self):
        """拉取「记忆优化模型」可用模型列表。"""
        if self._job_running:
            return
        base = self.var_mem_base.get().strip()
        key = self.var_mem_key.get().strip()
        if not base:
            self._set_mem_status("⚠️ 请先填写记忆优化模型的接口地址 Base URL。", "error")
            return
        self._job_running = True
        self.btn_mem_models.set_state("disabled")
        self._set_mem_status("⏳ 正在获取模型列表…", "busy")

        def work():
            try:
                ids = list_models(base, key, timeout=20.0)
                if not ids:
                    raise LLMError("接口没有返回任何模型，请手动填写模型名称。")
                self.post(self._mem_got_models, ids)
            except Exception as e:
                self.post(self._set_mem_status,
                          "❌ " + str(to_llm_error(e, "获取模型列表")), "error")
            finally:
                self.post(self._mem_models_done)

        threading.Thread(target=work, daemon=True).start()

    def _mem_got_models(self, ids):
        self.cmb_mem_model.configure(values=ids)
        if not self.var_mem_model.get().strip():
            self.var_mem_model.set(ids[0])
        self._set_mem_status("✅ 找到 %d 个模型，点下拉箭头即可选择。" % len(ids), "ok")

    def _mem_models_done(self):
        self._job_running = False
        self.btn_mem_models.set_state("normal")

    def _compress_items(self, batch):
        """（工作线程）让「记忆优化模型」把这批条目逐条精简，返回精简后的文本列表。"""
        base, key, model, _k, _x, _y = self._memory_settings()
        if not (base and model):
            raise LLMError("没有配置「记忆优化模型」（接口地址 / 模型名称），无法精简记忆。")
        payload = "\n".join("- " + str(it.get("text") or "") for it in batch)
        out, _reason, _usage = chat_completion(
            base, key, model,
            [{"role": "system", "content": MEM_COMPRESS_INSTRUCTIONS},
             {"role": "user", "content": payload}],
            stream=False, temperature=0.2,
            timeout=float(self.cfg.get("timeout") or 120),
            stop_event=self.stop_event, on_open=self._on_open)
        return [t for t in parse_summary_items(out or "") if t]

    def _optimize_memory(self, items, max_chars, log=None, k=0.5):
        """记忆字数超限时精简（并管理带 D 的记忆条数）。

        * 触发：正文（条目）字数超过 min(用户设定字数, 2000)；表头/说明不计入
        * 精简：挑 W 最大、末尾还没带 D 的 x 条交给记忆优化模型，压短后打上 D
        * 清理：带 D 的条数超过 y 时，删掉其中 W 最大的 y/2 条
        返回统计字典（失败不抛异常，保证总结照常写出）。
        """
        def _log(msg):
            if log:
                log(msg)

        # 只按「正文条目」的字数判断，表头/说明不算
        limit = min(int(max_chars or MAX_SUMMARY_CHARS), 2000)
        stats = {"compressed": 0, "deleted": 0, "rounds": 0, "note": ""}
        for _ in range(20):
            body, _st = render_summary_items(items, k=k)
            if len(body) <= limit:
                break
            cands = [it for it in items if not it.get("d")]
            if not cands:
                stats["note"] = "所有条目都已精简过"
                break
            _base, _key, _model, _k, x, _y = self._memory_settings()
            cands.sort(key=lambda it: -(it.get("w") or 0))
            batch = cands[:x]
            try:
                shrunk = self._compress_items(batch)
            except Exception as exc:
                stats["note"] = str(exc).splitlines()[0]
                _log("记忆优化失败：%s" % stats["note"])
                break
            if not shrunk:
                stats["note"] = "模型没有返回精简内容"
                break
            changed = 0
            for it, new_text in zip(batch, shrunk):
                if not new_text or not new_text.strip():
                    continue
                if len(new_text) < len(it.get("text") or ""):
                    changed += 1
                it["text"] = new_text.strip()
                it["d"] = True
            stats["compressed"] += changed
            stats["rounds"] += 1
            if changed == 0:
                stats["note"] = "模型没有给出更短的表述"
                break
        if stats["compressed"]:
            _log("记忆优化：精简 %d 条（耗时 %d 轮）" % (stats["compressed"], stats["rounds"]))
        if stats["note"]:
            _log("记忆优化提示：%s" % stats["note"])

        # 带 D 的记忆太多：删掉其中 W 最大的 y/2 条
        _b2, _k2, _m, _kk, _x2, y = self._memory_settings()
        d_items = [it for it in items if it.get("d")]
        if len(d_items) > y:
            d_items.sort(key=lambda it: -(it.get("w") or 0))
            drop_n = max(1, int(y) // 2)
            for it in d_items[:drop_n]:
                items.remove(it)
                stats["deleted"] += 1
            _log("带 D 的记忆超过 %d 条，删除 W 最大的 %d 条" % (y, stats["deleted"]))
        return stats

    # ---------------- 对话总结（每 10 轮一份 .md） ---------------- #
    def _collect_recent_rounds(self, rounds):
        """从对话历史里取「最近 rounds 轮」（一轮 = 一条用户消息 + 其后的回复）。"""
        msgs = [m for m in self.history if m["role"] in ("user", "assistant")]
        user_idx = [i for i, m in enumerate(msgs) if m["role"] == "user"]
        start = user_idx[-rounds] if len(user_idx) > rounds else 0
        return [(m["role"], m["content"]) for m in msgs[start:]]

    def _load_summary_state(self):
        """取出上一次总结的条目（含各自时间）：条目内容从此不再改动，只重算 T/L。"""
        if self.summary_items is not None:
            return self.summary_items
        items = []
        path = self.last_summary_file
        if path is None or not Path(path).exists():
            files = list_summary_files()
            path = files[-1] if files else None
        if path is not None:
            path = Path(path)
            items = load_summary_items(path) or []
            if not items:                     # 没有 sidecar：从 .md 里解析（时间用文件时间近似）
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = time.time()
                items = [{"text": d["text"], "ts": mtime, "d": d["d"],
                          "seq": int(d["seq"] or i)}
                         for i, d in enumerate(parse_summary_lines(text), 1)]
                if items:
                    _log("从 %s 解析出 %d 条已有条目（时间用文件时间近似）"
                         % (path.name, len(items)))
            self.last_summary_file = path
        self.summary_items = items
        return items

    def _build_summary_input(self, carry, prev_items, rounds):
        lines = []
        if carry:
            lines.append("助手（上一批顺延到本批的回复，时间上排在最前）：" + carry)
        picked = self._collect_recent_rounds(rounds)
        for role, content in picked:
            lines.append(("我：" if role == "user" else "助手：") + content)
        old = "\n".join("- " + t for t in (prev_items or [])) or "（无，这是第一份总结）"
        return ("【本次新增对话】（按时间先后排列，共 %d 条）\n%s\n\n"
                "【已有条目】（这些已经在总结里了，请**不要重复输出**）\n%s\n"
                % (len(lines), "\n\n".join(lines), old))

    def _read_previous_summary(self):
        """给模型复习用：**按原序号从小到大**列出已有记忆条目。

        没有条目数据时退回读最新的 .md 全文。
        """
        items = self._load_summary_state()
        if items:
            ordered = sorted(items, key=lambda it: int(it.get("seq") or 0))
            return "\n".join("%d. %s" % (int(it.get("seq") or i + 1), it.get("text") or "")
                              for i, it in enumerate(ordered))
        path = self.last_summary_file
        if path is None or not Path(path).exists():
            files = list_summary_files()
            path = files[-1] if files else None
        if path is None:
            return ""
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        self.last_summary_file = Path(path)
        return text

    def _do_summary(self):
        """（在工作线程里执行）生成 .md 总结文件；返回是否成功。"""
        carry = self.carry_over                 # 只有成功后才清掉
        try:
            max_rounds, keep_files, max_chars = self._summary_settings()
            items = list(self._load_summary_state())          # 已有条目（内容保持不变）
            old_texts = [it["text"] for it in items]
            text, _reason, _usage = chat_completion(
                self.cfg["base_url"], self.cfg["api_key"], self.cfg["model"],
                [{"role": "system",
                  "content": SUMMARY_INSTRUCTIONS % max(200, max_chars // 3)},
                 {"role": "user",
                  "content": self._build_summary_input(carry, old_texts, max_rounds)}],
                stream=False, temperature=0.3,
                timeout=float(self.cfg.get("timeout") or 120),
                stop_event=self.stop_event, on_open=self._on_open)
            text = (text or "").strip()
            if not text:
                raise LLMError("模型没有返回总结内容。")
            # 只整理「本次新增」：拆条 + 本次内部相似合并（不触碰旧条目）
            new_body, stats = normalize_summary_items(
                text, budget=max(120, max_chars // 3))
            new_texts = parse_summary_items(new_body)
            if not new_texts:
                raise LLMError("本次没有整理出任何新条目。")
            now = time.time()
            base_seq = max([int(it.get("seq") or 0) for it in items] or [0])
            for k, t in enumerate(new_texts, 1):
                items.append({"text": t, "ts": now, "seq": base_seq + k, "d": False})
            _log("本次新增 %d 条（内部合并 %d 次），累计 %d 条"
                 % (len(new_texts), stats["merged"], len(items)))

            # 字数超限：先请「记忆优化模型」精简 W 最大的条目（打 D 标记），
            # 实在压不下去，再兜底丢弃 W 最大的几条
            _, _, _, mem_k, _x, _y = self._memory_settings()
            opt = self._optimize_memory(items, max_chars, log=_log, k=mem_k)
            dropped = 0
            while len(items) > len(new_texts):
                probe, _st = render_summary_items(items, now=now, k=mem_k)
                if len(probe) <= max_chars:      # 同样只看正文
                    break
                items.pop()          # 列表已按 W 升序，末尾即 W 最大
                dropped += 1
            body, rst = render_summary_items(items, now=now, k=mem_k)
            cover = "本次新增 %d 条，累计 %d 条" % (len(new_texts), len(items))
            if carry:
                cover += " + 上一批顺延的回复"
            if opt["compressed"]:
                cover += "；已精简 %d 条（标 D）" % opt["compressed"]
            if opt["deleted"]:
                cover += "；已删除 %d 条带 D 的记忆" % opt["deleted"]
            if dropped:
                cover += "；为控制字数丢弃 W 最大的 %d 条" % dropped
            path, removed = write_summary_file(body, cover, max_chars=max_chars,
                                               max_files=keep_files,
                                               notes=summary_notes())
            # 旧总结文件保留（不删除）；只有目录里已满 MAX_SUMMARY_FILES 份时，
            # 才在写入前把最早生成的那份清理掉（见 write_summary_file）。
            save_summary_items(path, items)          # 条目+时间存 sidecar，下次继续累加
            self.summary_items = items
            self.last_summary_file = path
            self.rounds_in_batch = 0
            self.carry_over = None
            extra = ""
            if opt["compressed"]:
                extra += "，精简 %d 条" % opt["compressed"]
            if opt["deleted"]:
                extra += "，删 D 记忆 %d 条" % opt["deleted"]
            self.post(self._summary_done, str(path), removed, len(new_texts),
                      len(items), extra)
            return True
        except Exception as e:
            if self.stop_event.is_set():
                return False
            self.rounds_in_batch = 0        # 本批内容保留，10 轮后再试
            self.post(self._summary_failed, str(to_llm_error(e, "生成对话总结")))
            return False

    def _summary_done(self, path, removed, added=0, total=0, extra_note=""):
        """总结结果只提示在底部状态栏（聊天区不插提示行）。"""
        name = Path(path).name
        detail = "（新增 %d 条，共 %d 条%s）" % (added, total, extra_note) if total else ""
        self._summary_note = "📄 已生成总结 %s%s" % (name, detail)
        self.lbl_status.configure(
            text="📄 已生成对话总结：%s%s" % (rel_path(path), detail))

    def _summary_failed(self, msg):
        """失败也只在状态栏提示；详细原因打印到控制台，便于排查。"""
        first = (msg or "").splitlines()[0]
        _log("[对话总结失败] %s" % msg)
        self._summary_note = "⚠️ 对话总结生成失败（稍后自动重试）"
        self.lbl_status.configure(
            text="⚠️ 对话总结生成失败：%s（不影响继续对话）" % first[:60])

    def on_stop(self):
        if not self.busy:
            return
        self.stop_event.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        self.lbl_status.configure(text="正在停止…")

    # ---------------- 屏幕识别 ---------------- #
    def _screen_status(self, msg, kind="info"):
        colors = {"info": C.TEXT_3, "ok": C.GREEN, "error": C.RED, "busy": C.ACCENT}
        try:
            self.screen_status.configure(text=msg, fg=colors.get(kind, C.TEXT_3))
        except Exception:
            pass

    def _screen_interval_seconds(self):
        try:
            return max(SCREEN_MIN_INTERVAL,
                       int(str(self.cfg.get("screen_interval")
                               or SCREEN_DEFAULT_INTERVAL)))
        except Exception:
            return SCREEN_DEFAULT_INTERVAL

    def _screen_target_text(self):
        t = self.screen_target or {}
        if t.get("kind") == "window":
            return "窗口「%s」" % (t.get("title") or "?")
        return "整个屏幕"

    def _update_screen_target_label(self):
        try:
            self.lbl_screen_target.configure(
                text=("窗口：%s" % (self.screen_target or {}).get("title", "")[:34])
                if (self.screen_target or {}).get("kind") == "window" else "整个屏幕")
        except Exception:
            pass

    def _update_screen_ui(self):
        try:
            self.btn_screen_toggle.set_text("📷 屏幕识别：开" if self._screen_on
                                            else "📷 屏幕识别：关")
        except Exception:
            pass

    def on_screen_fullscreen(self):
        self.screen_target = {"kind": "screen"}
        self._update_screen_target_label()
        self._screen_status("✅ 已设为截取整个屏幕", "ok")

    def on_screen_pick_window(self):
        wins = enum_windows_for_pick()
        if not wins:
            self._screen_status("没有找到可截取的窗口（请先打开目标窗口）", "error")
            return
        top = tk.Toplevel(self.root)
        top.title("选择要定时截取的窗口")
        top.configure(bg=C.BG)
        top.transient(self.root)
        try:
            top.grab_set()
        except Exception:
            pass
        tk.Label(top, text="选择要定时截取的窗口（标题 — 类名）", bg=C.BG, fg=C.TEXT,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=14, pady=(12, 6))
        box = tk.Frame(top, bg=C.SURFACE, highlightthickness=1,
                       highlightbackground=C.BORDER)
        box.pack(fill="both", expand=True, padx=14)
        lb = tk.Listbox(box, height=12, width=80, font=(FONT, 10), bd=0,
                        highlightthickness=0, bg=C.SURFACE, fg=C.TEXT,
                        selectbackground=C.ACCENT_SOFT, selectforeground=C.TEXT)
        lb.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        sb = ttk.Scrollbar(box, orient="vertical", style="Chat.Vertical.TScrollbar",
                           command=lb.yview)
        sb.pack(side="right", fill="y")
        lb.configure(yscrollcommand=sb.set)
        for _hwnd, title, cls in wins:
            lb.insert("end", "%s　—　%s" % (title[:58], cls))

        def ok(_event=None):
            sel = lb.curselection()
            if not sel:
                return
            _hwnd, title, cls = wins[sel[0]]
            self.screen_target = {"kind": "window", "title": title, "class": cls}
            self._update_screen_target_label()
            self._screen_status("✅ 已选择窗口：%s" % title[:44], "ok")
            top.destroy()

        btns = tk.Frame(top, bg=C.BG)
        btns.pack(pady=12)
        RoundedButton(btns, text="确定", variant="primary", height=34, padx=20,
                      command=ok).pack(side="left")
        RoundedButton(btns, text="取消", variant="soft", height=34, padx=20,
                      command=top.destroy).pack(side="left", padx=(8, 0))
        lb.bind("<Double-Button-1>", ok)
        top.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width()
                                              - top.winfo_reqwidth()) // 2)
        top.geometry("+%d+%d" % (max(0, x), self.root.winfo_rooty() + 90))

    def on_screen_toggle(self):
        """输入框上方那个按钮：开 / 关 屏幕识别。"""
        self._screen_set_on(not self._screen_on)

    def on_screen_capture_now(self):
        if not self._vision_ready():
            self._screen_status("⚠️ 还没配置视觉模型（VLM），截图发出去也没法识别；"
                                "请先到「视觉模型（VLM）接口信息」里填好。", "error")
        self._screen_status("正在截取屏幕…", "busy")
        threading.Thread(target=self._screen_capture_guarded, daemon=True).start()

    def _screen_set_on(self, flag, save=True):
        flag = bool(flag)
        self._screen_on = flag
        self.cfg["screen_watch"] = flag
        if save:
            save_config(self.cfg)
        if flag:
            if not self._vision_ready():
                self._screen_status("⚠️ 尚未配置视觉模型（VLM）：截图会被拦住，"
                                    "请先到「视觉模型（VLM）接口信息」填写。", "error")
            if self._screen_thread is None or not self._screen_thread.is_alive():
                self._screen_count = 0
                self._screen_thread = threading.Thread(target=self._screen_loop,
                                                       daemon=True)
                self._screen_thread.start()
            self.lbl_status.configure(
                text="📷 屏幕识别已开启：每 %d 秒截取一次（%s）"
                     % (self._screen_interval_seconds(), self._screen_target_text()))
            self._screen_status("✅ 屏幕识别已开启（%s），截图存在 %s"
                                % (self._screen_target_text(),
                                   rel_path(app_dir() / SCREEN_TEMP_DIR)), "ok")
        else:
            self.lbl_status.configure(text="📷 屏幕识别已关闭")
            self._screen_status("已停止屏幕识别", "info")
        try:
            self.var_screen_on.set(flag)
        except Exception:
            pass
        self._update_screen_ui()

    def _screen_loop(self):
        """后台线程：先立刻截一张，然后每隔 N 秒截一张。"""
        try:
            self._screen_capture_guarded()
        except Exception:
            pass
        while self._screen_on:
            interval = self._screen_interval_seconds()
            waited = 0.0
            while waited < interval and self._screen_on:
                time.sleep(0.2)
                waited += 0.2
            if not self._screen_on:
                break
            self._screen_capture_guarded()

    def _screen_capture_guarded(self):
        try:
            self._screen_capture_once()
        except Exception as e:
            self.post(self._screen_status, "❌ 截屏失败：%s" % e, "error")
            _log(exc=True)

    def _screen_capture_once(self):
        """（在后台线程里）截一张并存到 temppicture，然后交给界面。"""
        target = dict(self.cfg.get("screen_target") or {"kind": "screen"})
        hwnd = None
        if target.get("kind") == "window":
            hwnd = find_window_by_class_title(target.get("title", ""),
                                              target.get("class", ""))
            if not hwnd:
                self.post(self._screen_status,
                          "⚠️ 目标窗口没找到（可能已关闭），本次改为截取整个屏幕",
                          "error")
        path, w, h = capture_screen_to_file(
            hwnd, half=bool(self.cfg.get("screen_half", True)))
        self.post(self._screen_got_image, str(path), w, h)

    def _screen_got_image(self, path, w, h):
        """（主线程）把新截图放进待发送图片：自动截图始终只保留最新一张。"""
        old = self.auto_image
        if old is not None and old in self.attached_images:
            self.attached_images.remove(old)
        p = Path(path)
        if p not in self.attached_images:
            self.attached_images.append(p)
        self.auto_image = p
        self._screen_count += 1
        self._render_attachments()
        self.lbl_status.configure(
            text="📷 已截取第 %d 张屏幕（%dx%d），已放进待发送图片"
                 % (self._screen_count, w, h))
        self._screen_status("✅ 第 %d 张：%s（已放进输入框，等你按发送）"
                            % (self._screen_count, p.name), "ok")

    # ---------------- 对话页面 / 上下文的存档 ---------------- #
    def _chat_dump(self):
        """把对话区里的文字和样式导出成可回放的记录。"""
        try:
            return self.chat.dump("1.0", "end-1c", tag=True, text=True)
        except Exception:
            _log(exc=True)
            return []

    def _replay_chat_dump(self, dump):
        """按导出顺序把对话区原样还原回来（含各种样式标签）。"""
        self.chat.delete("1.0", "end")
        active = []
        for item in dump or ():
            try:
                kind, value, index = item
            except Exception:
                continue
            if kind == "tagon":
                if value not in active:
                    active.append(value)
            elif kind == "tagoff":
                if value in active:
                    active.remove(value)
            elif kind == "text":
                self.chat.insert(index, value, tuple(active))
        self.chat.see("end")

    def _history_for_save(self):
        """只留下回传模型真正需要的字段（思考内容照旧不进上下文）。"""
        out = []
        for m in self.history:
            if not isinstance(m, dict):
                continue
            if m.get("role") not in ("user", "assistant"):
                continue
            if not isinstance(m.get("content"), str):
                continue
            rec = {"role": m["role"], "content": m["content"]}
            if isinstance(m.get("reasoning"), str) and m["reasoning"]:
                rec["reasoning"] = m["reasoning"]
            out.append(rec)
        return out

    def _save_ui_state(self):
        """保存对话页面 + AI 上下文（不含任何接口地址与密钥）。"""
        try:
            save_ui_state(
                self._history_for_save(), self._chat_dump(),
                {"rounds_in_batch": int(self.rounds_in_batch or 0),
                 "since_review": int(self._since_review or 0),
                 "carry_over": self.carry_over or ""})
        except Exception:
            _log(exc=True)

    def _restore_ui_state(self):
        """打开程序时把上次的对话接上（用户没手动清空过的话）。"""
        data = load_ui_state()
        if not data:
            return False
        try:
            hist = data.get("history")
            if isinstance(hist, list):
                self.history = [m for m in hist if isinstance(m, dict)
                                and m.get("role") in ("user", "assistant")
                                and isinstance(m.get("content"), str)]
            dump = data.get("chat_dump")
            shown = False
            if isinstance(dump, list) and dump:
                self._replay_chat_dump(dump)
                shown = True
            try:
                self.rounds_in_batch = int(data.get("rounds_in_batch") or 0)
                self._since_review = int(data.get("since_review") or 0)
            except (TypeError, ValueError):
                self.rounds_in_batch, self._since_review = 0, 0
            co = data.get("carry_over")
            self.carry_over = co if isinstance(co, str) and co else None
        except Exception:
            _log(exc=True)
            return False
        if shown or self.history:
            self.lbl_status.configure(
                text="🕘 已恢复上次的对话（共 %d 条消息）；点「清空对话」才会清除。"
                     % len(self.history))
        return True

    # ---------------- 其它操作 ---------------- #
    def _reset_chat_text(self):
        self.chat.delete("1.0", "end")
        self._insert("end", "👋 欢迎使用 %s！\n" % APP_TITLE, ("assistant_label",))
        self._insert(
            "end-1c",
            "· 在下面输入问题，按 Enter 发送，Shift+Enter 换行。\n"
            "· 想换模型或改地址：点右上角「设置」。\n"
            "· 想保存这段对话：点「导出对话」。\n"
            "· 不想看到模型的思考过程：点「设置」勾选「过滤模型的思考内容」。\n"
            "· 想发图片：点输入框左上角的「＋ 图片」（可多选），或把图片直接拖进输入框；"
            "图片会先交给视觉模型识别，再把识别结果和你的文字一起发给语言模型。\n"
            "· 每满 %d 轮对话会自动生成一份 .md 总结（%s）；"
            "每 %d 次聊天会让模型先回顾一次历史总结。\n"
            % (self._summary_settings()[0], rel_path(record_dir()),
               self._review_rounds()),
            ("sys",))

    def on_clear(self):
        if self.busy:
            messagebox.showinfo(APP_TITLE, "正在生成回复，请先点「停止」。")
            return
        if self.history and not messagebox.askyesno(APP_TITLE, "确定要清空当前对话吗？"):
            return
        self.history.clear()
        self.rounds_in_batch = 0
        self._since_review = 0
        self.carry_over = None
        self._pending_carry = False
        self.summary_items = None      # 清空对话后重新从磁盘上的 .md 接续
        clear_ui_state()               # 手动清空：把「关闭也不丢」的存档一起删掉
        self._reset_chat_text()
        self.lbl_status.configure(
            text="已清空对话，上下文重新开始（轮次计数已重置，已有的 .md 总结保留）。")

    def on_export(self):
        if not self.history:
            messagebox.showinfo(APP_TITLE, "还没有可以导出的对话内容。")
            return
        path = filedialog.asksaveasfilename(
            title="导出对话", defaultextension=".md",
            initialfile="对话记录.md",
            filetypes=[("Markdown", "*.md"), ("纯文本", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        lines = ["# 与 %s 的对话" % (self.cfg.get("model") or "LLM"),
                 "", "- 接口：%s" % self.cfg.get("base_url", ""),
                 "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
        for m in self.history:
            lines.append("**%s**：" % ("我" if m["role"] == "user" else "助手"))
            lines.append("")
            mind = (m.get("reasoning") or "").strip()
            if mind:
                lines.append("> 💭 思考过程（模型输出，仅供参考）")
                for ln in mind.splitlines():
                    lines.append("> " + ln)
                lines.append("")
            lines.append(m["content"])
            lines.append("")
        try:
            Path(path).write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            messagebox.showerror(APP_TITLE, "导出失败：%s" % e)
            return
        messagebox.showinfo(APP_TITLE, "已导出到：\n%s" % path)

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno(APP_TITLE, "正在生成回复，确定要退出吗？"):
                return
            self.stop_event.set()
        self._screen_on = False            # 停掉屏幕识别线程
        self._save_ui_state()              # 关闭前保存对话页面与上下文（下次打开自动接上）
        try:
            self.root.destroy()
        except Exception:
            pass

# --------------------------------------------------------------------------- #
#  离线自测：用本地模拟服务验证网络模块（不需要联网、不需要界面）
# --------------------------------------------------------------------------- #
def _selftest() -> int:
    import http.server
    import socketserver

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if "bad" in (self.headers.get("Authorization") or ""):
                return self._json({"error": {"message": "Invalid API key"}}, 401)
            if self.path.endswith("/models"):
                return self._json({"object": "list", "data": [
                    {"id": "mock-small", "object": "model"},
                    {"id": "mock-large", "object": "model"},
                ]})
            return self._json({"error": {"message": "Not Found"}}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                payload = {}
            if not self.path.endswith("/chat/completions"):
                return self._json({"error": {"message": "Not Found"}}, 404)
            if "bad" in (self.headers.get("Authorization") or ""):
                return self._json({"error": {"message": "Invalid API key"}}, 401)
            model = payload.get("model") or ""
            if model == "mock-think":       # 思考直接写在正文里（很多中转站 / 本地模型）
                answer = "<think>先算一下：2+2=4</think>答案是 42。"
                pieces = [answer[i:i + 6] for i in range(0, len(answer), 6)]
            elif model == "mock-reason":    # 官方 DeepSeek-R1 风格：独立字段
                answer = "答案是 42。"
                pieces = [answer[i:i + 4] for i in range(0, len(answer), 4)]
            else:
                answer = "你好，世界！"
                pieces = ["你", "好", "，", "世界！"]

            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                chunks = []
                if model == "mock-reason":
                    for p in ("我需要", "认真算一下"):
                        chunks.append({"choices": [{"delta": {"reasoning_content": p}}]})
                for p in pieces:
                    chunks.append({"choices": [{"delta": {"content": p}}]})
                for c in chunks:
                    self.wfile.write(("data: %s\n\n" % json.dumps(c, ensure_ascii=False)).encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                msg = {"role": "assistant", "content": answer}
                if model == "mock-reason":
                    msg["reasoning_content"] = "我需要认真算一下"
                self._json({"choices": [{"message": msg}], "usage": {"total_tokens": 7}})

    class QuietServer(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            pass          # 客户端主动断开（例如点“停止”）时不要打印堆栈

    srv = QuietServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/v1" % port

    failures = []

    def check(name, cond, extra=""):
        print(("  ✅ " if cond else "  ❌ ") + name + (("  " + str(extra)) if extra else ""))
        if not cond:
            failures.append(name)

    print("本地模拟服务：%s" % base)

    print("\n[1] 地址规范化")
    check("自动补全 /chat/completions", endpoint_candidates("http://x/v1")[0].endswith("/v1/chat/completions"))
    check("去掉多余后缀", normalize_base_url("https://api.deepseek.com/v1/chat/completions/") == "https://api.deepseek.com/v1")
    check("自动补 http://", normalize_base_url("localhost:11434/v1") == "http://localhost:11434/v1")
    check("缺 /v1 时提供备选地址", len(endpoint_candidates("http://localhost:1234")) == 2)

    print("\n[2] 获取模型列表")
    try:
        ids = list_models(base, "sk-test")
        check("返回模型 id 列表", ids == ["mock-large", "mock-small"], ids)
    except Exception as e:
        check("返回模型 id 列表", False, e)

    print("\n[3] 非流式对话")
    try:
        text, reasoning, usage = chat_completion(base, "sk-test", "mock-small",
                                                 [{"role": "user", "content": "hi"}], stream=False)
        check("得到回答", text == "你好，世界！", text)
        check("解析 usage", isinstance(usage, dict) and usage.get("total_tokens") == 7)
    except Exception as e:
        check("非流式对话", False, e)

    print("\n[4] 流式对话（SSE 增量）")
    pieces = []
    try:
        text, reasoning, usage = chat_completion(
            base, "sk-test", "mock-small", [{"role": "user", "content": "hi"}],
            stream=True, on_delta=lambda kind, p: pieces.append((kind, p)))
        check("拼接后文本正确", text == "你好，世界！", text)
        check("回调逐字触发", len(pieces) == 4, pieces)
    except Exception as e:
        check("流式对话", False, e)

    print("\n[5] 停止生成")
    stop = threading.Event()
    got = []
    stop.set()
    try:
        text, _, _ = chat_completion(base, "sk-test", "mock-small",
                                     [{"role": "user", "content": "hi"}],
                                     stream=True, stop_event=stop,
                                     on_delta=lambda k, p: got.append(p))
        check("停止后不再输出", len(got) <= 1, got)
    except Exception as e:
        check("停止生成", False, e)

    print("\n[6] 错误提示是否友好")
    try:
        chat_completion(base, "sk-bad", "mock-small", [{"role": "user", "content": "hi"}], stream=False)
        check("401 抛错", False, "居然成功了")
    except LLMError as e:
        check("401 提示包含中文说明", "401" in str(e) and "API Key" in str(e), str(e).splitlines()[0])
    try:
        list_models("http://127.0.0.1:1/v1", "sk-test", timeout=3)
        check("连不上时抛错", False, "居然成功了")
    except LLMError as e:
        check("连不上时给出排查建议", "无法连接" in str(e))

    print("\n[7] 思考内容识别与切分")
    sp = ThinkTagSplitter()
    got = sp.feed("<think>先算一下：2+2=4</think>答案是 42。") + sp.flush()
    check("标签正常切开", [k for k, _ in got] == ["reasoning", "content"], got)
    check("思考/正文内容各自正确",
          got[0][1] == "先算一下：2+2=4" and got[1][1] == "答案是 42。", got)

    sp = ThinkTagSplitter()
    got = []
    for piece in ["<thi", "nk>想", "一下：2+2=4</thi", "nk>答案", "是 42。"]:
        got += sp.feed(piece)
    got += sp.flush()
    check("标签被流式分片切断也能识别",
          "".join(t for k, t in got if k == "reasoning") == "想一下：2+2=4"
          and "".join(t for k, t in got if k == "content") == "答案是 42。", got)

    body, mind = split_think_text("<thinking>分析中</thinking>结论：42")
    check("兼容 <thinking> 标签", (body, mind) == ("结论：42", "分析中"), (body, mind))
    body, mind = split_think_text(r"<\think\>先想</think\>正文")
    check("兼容 <\\think\\> 写法", (body, mind) == ("正文", "先想"), (body, mind))
    body, mind = split_think_text("5 < 6 是成立的，比如 a<b")
    check("普通小于号不被误认为标签", (body, mind) == ("5 < 6 是成立的，比如 a<b", ""), (body, mind))
    body, mind = split_think_text("<think>没闭合的思考")
    check("未闭合标签算作思考内容", (body, mind) == ("", "没闭合的思考"), (body, mind))
    body, mind = split_think_text("普通回答，没有思考")
    check("无标签时原样输出", (body, mind) == ("普通回答，没有思考", ""), (body, mind))

    print("\n[8] 正文内嵌 <think> 标签（流式 / 非流式）")
    kinds = []
    text, reasoning, _ = chat_completion(base, "sk-test", "mock-think",
                                         [{"role": "user", "content": "2+2"}],
                                         stream=True, on_delta=lambda k, p: kinds.append(k))
    check("流式：正文已剔除思考", text == "答案是 42。", text)
    check("流式：思考被单独取出", reasoning == "先算一下：2+2=4", reasoning)
    check("流式：先输出思考再输出正文", kinds[:1] == ["reasoning"] and kinds[-1] == "content", kinds)
    text, reasoning, _ = chat_completion(base, "sk-test", "mock-think",
                                         [{"role": "user", "content": "2+2"}], stream=False)
    check("非流式：正文已剔除思考", text == "答案是 42。", text)
    check("非流式：思考被单独取出", reasoning == "先算一下：2+2=4", reasoning)

    print("\n[9] reasoning_content 字段（DeepSeek-R1 类模型）")
    kinds = []
    text, reasoning, _ = chat_completion(base, "sk-test", "mock-reason",
                                         [{"role": "user", "content": "2+2"}],
                                         stream=True, on_delta=lambda k, p: kinds.append(k))
    check("正文正确", text == "答案是 42。", text)
    check("思考来自 reasoning_content", reasoning == "我需要认真算一下", reasoning)
    check("回调顺序：先思考后正文",
          kinds[:2] == ["reasoning", "reasoning"] and set(kinds[2:]) == {"content"}, kinds)

    srv.shutdown()
    print("\n%s  失败 %d 项" % ("🎉 全部通过" if not failures else "⚠️ 有失败", len(failures)))
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
#  入口
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in argv:
        # 控制台/管道不是 UTF-8 时（中文 Windows 常见），先切成 UTF-8，
        # 否则自测里的 ✅ 之类字符会抛 UnicodeEncodeError。
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name, None)
            if stream is not None:
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
        # 打包成无控制台 exe 时 stdout/stderr 为空，把自测输出写进报告文件
        report = None
        if sys.stdout is None or "--report" in argv:
            try:
                report = open(app_dir() / "selftest-report.txt", "w",
                              encoding="utf-8")
                sys.stdout = report
                sys.stderr = report
            except Exception:
                report = None
        try:
            print("运行方式：%s" % ("打包后的 exe" if getattr(sys, "frozen", False)
                                else "源码运行"))
            print("程序目录：%s" % app_dir())
            print("运行模式：%s" % ("便携模式（配置跟随 exe）" if portable_mode()
                                else "常规模式（配置在用户主目录）"))
            print("配置目录：%s" % CONFIG_PATH)
            print("总结目录：%s" % record_dir())
            print("测试图片：%s" % (find_test_image() or "（未找到）"))
            print("Python：%s" % sys.version.split()[0])
            print("-" * 46)
            code = _selftest()
            print("-" * 46)
            print("退出码：%d" % code)
            try:
                sys.stdout.flush()
            except Exception:
                pass
            return code
        finally:
            try:
                if report is not None:
                    report.close()
            except Exception:
                pass

    if sys.version_info < (3, 8):
        _log("需要 Python 3.8 或更高版本，当前为 %s" % sys.version.split()[0])
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as e:
        _log("无法启动图形界面：%s" % e)
        return 1
    MyLLMChatApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())