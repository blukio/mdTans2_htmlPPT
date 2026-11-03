#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Markdown 讲义编译成一个单文件 HTML 幻灯片。

用法：
    python3 build.py example.md -o slides.html -t "课程标题"

依赖：
    pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import base64
import html as html_module
import json
import mimetypes
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import unquote

try:
    import markdown
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 Markdown，请先执行：pip install -r requirements.txt")

try:
    from jinja2 import Template
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 Jinja2，请先执行：pip install -r requirements.txt")


# ---------------------------------------------------------------------------
# Markdown 解析配置
# ---------------------------------------------------------------------------
# 注意：故意不使用 codehilite（服务端高亮），代码块交给浏览器端的
# highlight.js 处理，这样 HTML 里保留 <code class="language-xxx">，
# 代码高亮的主题和语言可以随时在模板里换。
MD_EXTENSIONS = [
    "fenced_code",   # ```code```
    "tables",        # 表格
    "sane_lists",    # 更符合直觉的列表
    "attr_list",     # 给元素加 class / id
    "def_list",      # 定义列表
    "footnotes",     # 脚注
    "md_in_html",    # 允许 HTML 块里写 Markdown
]

# 分页标记：独占一行的 ---（代码围栏内不算）
PAGE_BREAK_RE = re.compile(r"^\s*---\s*$")
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")

# 行内/块级公式，交给 MathJax 渲染
MATH_RE = re.compile(r"(\$\$[\s\S]+?\$\$|\$[^\$\n]+?\$)")
MATH_TOKEN_RE = re.compile(r"@@MATH_(\d+)@@")

# <img ... src="xxx">
IMG_SRC_RE = re.compile(r"(<img\b[^>]*?\bsrc\s*=\s*)([\"'])(.*?)\2", re.IGNORECASE)

REMOTE_SRC_RE = re.compile(r"^(?:data:|[a-z][a-z0-9+.-]*://|//|#)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1. 切页（跳过代码围栏）
# ---------------------------------------------------------------------------
def split_slides(text: str):
    slides = []
    buf = []
    fence = None       # (marker_char, marker_len)
    in_code = False

    for line in text.splitlines():
        stripped = line.rstrip("\r\n")
        m = FENCE_RE.match(stripped)

        if m:
            marker = m.group(2)
            if not in_code:
                # 开启围栏
                in_code = True
                fence = (marker[0], len(marker))
                buf.append(line)
                continue
            if (marker[0] == fence[0]
                    and len(marker) >= fence[1]
                    and not m.group(3).strip()):
                # 关闭围栏
                in_code = False
                fence = None
                buf.append(line)
                continue

        if not in_code and PAGE_BREAK_RE.match(stripped):
            slides.append("\n".join(buf).strip("\n"))
            buf = []
        else:
            buf.append(line)

    slides.append("\n".join(buf).strip("\n"))
    return [s for s in slides if s.strip()]


# ---------------------------------------------------------------------------
# 2. 保护公式，避免 Markdown 把 _ ^ * 当成强调符号
# ---------------------------------------------------------------------------
def split_code_segments(text: str):
    """把文本切成 [('text'|'code', 片段)]，公式只保护 text 片段。"""
    segments = []
    cur = []
    cur_kind = "text"
    in_code = False
    fence = None

    for line in text.splitlines(keepends=True):
        m = FENCE_RE.match(line.rstrip("\r\n"))
        if m:
            marker = m.group(2)
            if not in_code:
                if cur:
                    segments.append(("text", "".join(cur)))
                    cur = []
                in_code = True
                fence = (marker[0], len(marker))
                cur_kind = "code"
                cur.append(line)
                continue
            if (marker[0] == fence[0]
                    and len(marker) >= fence[1]
                    and not m.group(3).strip()):
                cur.append(line)
                segments.append(("code", "".join(cur)))
                cur = []
                in_code = False
                fence = None
                cur_kind = "text"
                continue
        cur.append(line)

    if cur:
        segments.append((cur_kind, "".join(cur)))
    return segments


def protect_math(text: str):
    """返回 (替换后的文本, 公式原文列表)。"""
    store = []

    def repl(m):
        store.append(m.group(1))
        return "@@MATH_%d@@" % (len(store) - 1)

    parts = []
    for kind, chunk in split_code_segments(text):
        parts.append(chunk if kind == "code" else MATH_RE.sub(repl, chunk))
    return "".join(parts), store


def restore_math(rendered: str, store):
    def repl(m):
        idx = int(m.group(1))
        raw = store[idx] if 0 <= idx < len(store) else ""
        # 只转义会破坏 HTML 结构的字符，反斜杠保持原样给 MathJax
        return html_module.escape(raw, quote=False)

    return MATH_TOKEN_RE.sub(repl, rendered)


# ---------------------------------------------------------------------------
# 3. 把本地图片内联为 base64（可选）
# ---------------------------------------------------------------------------
def inline_images(rendered: str, base_dir: Path):
    def repl(m):
        prefix, quote, src = m.group(1), m.group(2), m.group(3)
        if REMOTE_SRC_RE.match(src):
            return m.group(0)

        path = (base_dir / unquote(src.split("?")[0].split("#")[0])).resolve()
        if not path.is_file():
            print("  ! 找不到图片，保留原路径：%s" % src, file=sys.stderr)
            return m.group(0)

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return "%s%sdata:%s;base64,%s%s" % (prefix, quote, mime, encoded, quote)

    return IMG_SRC_RE.sub(repl, rendered)


# ---------------------------------------------------------------------------
# 4. 单页 Markdown -> HTML
# ---------------------------------------------------------------------------
def render_slide(source: str, base_dir: Path, inline_local_images: bool) -> str:
    protected, store = protect_math(source)

    md = markdown.Markdown(extensions=MD_EXTENSIONS, output_format="html5")
    body = md.convert(protected)
    md.reset()

    body = restore_math(body, store)
    if inline_local_images:
        body = inline_images(body, base_dir)
    return body


# ---------------------------------------------------------------------------
# 5. 命令行
# ---------------------------------------------------------------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Markdown -> 单文件 HTML 幻灯片",
    )
    parser.add_argument("source", help="Markdown 源文件，例如 example.md")
    parser.add_argument("-o", "--output", help="输出 HTML 路径（默认与源文件同名）")
    parser.add_argument("-t", "--title", help="浏览器标签页标题（默认取文件名）")
    parser.add_argument("--template", help="模板路径（默认同目录下的 slides.html）")
    parser.add_argument(
        "--no-inline-images",
        action="store_true",
        help="不把本地图片转成 base64，保留相对路径",
    )
    parser.add_argument("--open", action="store_true", help="构建完成后用默认浏览器打开")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    source_path = Path(args.source).expanduser().resolve()
    if not source_path.is_file():
        sys.exit("找不到源文件：%s" % source_path)

    template_path = Path(args.template).expanduser().resolve() if args.template else (
        Path(__file__).resolve().parent / "slides.html"
    )
    if not template_path.is_file():
        sys.exit("找不到模板文件：%s" % template_path)

    output_path = Path(args.output).expanduser().resolve() if args.output else (
        source_path.with_suffix(".html")
    )

    title = args.title or source_path.stem
    text = source_path.read_text(encoding="utf-8")

    raw_slides = split_slides(text)
    if not raw_slides:
        sys.exit("没有解析到任何幻灯片：请用单独一行的 --- 分页")

    base_dir = source_path.parent
    slides = [
        render_slide(s, base_dir, inline_local_images=not args.no_inline_images)
        for s in raw_slides
    ]

    # 内嵌到 <script type="application/json"> 中，需要防止提前闭合标签
    slides_json = json.dumps(slides, ensure_ascii=False).replace("</", "<\\/")

    template = Template(template_path.read_text(encoding="utf-8"), autoescape=False)
    rendered = template.render(
        title=title,
        slides_json=slides_json,
        slide_count=len(slides),
    )

    output_path.write_text(rendered, encoding="utf-8")

    size_kb = output_path.stat().st_size / 1024
    print("✓ 生成 %s" % output_path)
    print("  %d 页，%.1f KB" % (len(slides), size_kb))

    if args.open:
        webbrowser.open(output_path.as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
