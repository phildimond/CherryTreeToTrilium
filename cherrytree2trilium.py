#!/usr/bin/env python3
"""
cherrytree2trilium.py - convert a CherryTree document into a ZIP that
Trilium Notes imports with the tree, formatting, images, tables, code
boxes, embedded files and node links intact.

Supported input:
    .ctb  CherryTree SQLite document (unprotected)
    .ctd  CherryTree XML document    (unprotected)
Password-protected .ctz / .ctx files must first be saved unprotected
from CherryTree (File > Save As, choose SQLite or XML, no password).

Usage:
    python3 cherrytree2trilium.py MyNotes.ctb               -> MyNotes-trilium.zip
    python3 cherrytree2trilium.py MyNotes.ctb -o out.zip
    python3 cherrytree2trilium.py MyNotes.ctb --no-wrap      (no container note)

Import in Trilium: right-click the note to import under > Import into
note > choose the ZIP > leave "Safe import" ticked > Import.

Only the Python 3 standard library is needed (3.8+).
"""

import argparse
import base64
import html
import json
import urllib.parse
import mimetypes
import os
import random
import re
import sqlite3
import string
import sys
import xml.etree.ElementTree as ET
import zipfile

# --------------------------------------------------------------------------
# Intermediate model
# --------------------------------------------------------------------------


class Obj:
    """Something anchored in the text: image, embedded file, table, codebox, anchor."""

    def __init__(self, kind, offset, **data):
        self.kind = kind          # "image" | "file" | "table" | "code" | "anchor"
        self.offset = offset
        self.data = data


class Node:
    def __init__(self, ct_id, name):
        self.ct_id = ct_id
        self.name = name or "Untitled"
        self.syntax = "custom-colors"
        self.tags = ""
        self.readonly = False
        self.runs = []            # [(text, {attr: value})]
        self.objects = []         # [Obj]
        self.children = []
        # filled in during layout
        self.note_id = None
        self.dir_path = ""        # zip directory holding this note's data file
        self.data_file = None
        self.dir_file = None


def _attrs(el):
    return {k: v for k, v in el.attrib.items()}


def parse_rich_xml(xml_text):
    """CherryTree node text (ctb 'txt' column) -> list of runs."""
    runs = []
    if not xml_text:
        return runs
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    for rt in root.iter("rich_text"):
        runs.append((rt.text or "", _attrs(rt)))
    return runs


def parse_table_xml(xml_text):
    root = ET.fromstring(xml_text.encode("utf-8"))
    return table_rows(root)


def table_rows(table_el):
    rows = []
    for row in table_el.iter("row"):
        rows.append([(c.text or "") for c in row.iter("cell")])
    return rows


# ---------------------------- .ctb (SQLite) -------------------------------


def load_ctb(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    nodes = {}
    for r in db.execute("SELECT * FROM node"):
        n = Node(r["node_id"], r["name"])
        n.syntax = r["syntax"] or "custom-colors"
        n.tags = r["tags"] or ""
        n.readonly = bool(r["is_ro"]) if "is_ro" in r.keys() else False
        if n.syntax == "custom-colors" or r["is_richtxt"]:
            n.runs = parse_rich_xml(r["txt"])
        else:
            # plain text / code node: txt is the raw text
            txt = r["txt"] or ""
            if txt.lstrip().startswith("<?xml"):
                n.runs = parse_rich_xml(txt)
            else:
                n.runs = [(txt, {})]
        nodes[n.ct_id] = n

    for r in db.execute("SELECT node_id, offset, justification, anchor, png, filename, link FROM image"):
        n = nodes.get(r["node_id"])
        if not n:
            continue
        if r["anchor"]:
            n.objects.append(Obj("anchor", r["offset"], name=r["anchor"]))
        elif r["filename"]:
            n.objects.append(Obj("file", r["offset"], filename=r["filename"], blob=bytes(r["png"])))
        else:
            n.objects.append(Obj("image", r["offset"], blob=bytes(r["png"]), link=r["link"] or "",
                                 justification=r["justification"]))

    for r in db.execute("SELECT node_id, offset, justification, txt FROM grid"):
        n = nodes.get(r["node_id"])
        if n:
            n.objects.append(Obj("table", r["offset"], rows=parse_table_xml(r["txt"]),
                                 justification=r["justification"]))

    for r in db.execute("SELECT node_id, offset, justification, txt, syntax FROM codebox"):
        n = nodes.get(r["node_id"])
        if n:
            n.objects.append(Obj("code", r["offset"], text=r["txt"] or "", syntax=r["syntax"] or ""))

    roots = []
    rels = db.execute("SELECT node_id, father_id, sequence FROM children ORDER BY father_id, sequence").fetchall()
    for r in rels:
        n = nodes.get(r["node_id"])
        if not n:
            continue
        if r["father_id"] and r["father_id"] in nodes:
            nodes[r["father_id"]].children.append(n)
        else:
            roots.append(n)
    return roots, nodes


# ---------------------------- .ctd (XML) ----------------------------------


def load_ctd(path):
    tree = ET.parse(path)
    root = tree.getroot()
    nodes = {}

    def walk(el):
        n = Node(int(el.get("unique_id", len(nodes) + 1)), el.get("name"))
        n.syntax = el.get("prog_lang", "custom-colors")
        n.tags = el.get("tags", "")
        n.readonly = el.get("readonly", "False") in ("True", "1", "true")
        nodes[n.ct_id] = n
        for child in el:
            t = child.tag
            if t == "rich_text":
                n.runs.append((child.text or "", _attrs(child)))
            elif t == "encoded_png":
                off = int(child.get("char_offset", 0))
                if child.get("anchor"):
                    n.objects.append(Obj("anchor", off, name=child.get("anchor")))
                    continue
                blob = base64.b64decode(child.text or "")
                if child.get("filename"):
                    n.objects.append(Obj("file", off, filename=child.get("filename"), blob=blob))
                else:
                    n.objects.append(Obj("image", off, blob=blob, link=child.get("link", ""),
                                         justification=child.get("justification", "left")))
            elif t == "table":
                n.objects.append(Obj("table", int(child.get("char_offset", 0)), rows=table_rows(child),
                                     justification=child.get("justification", "left")))
            elif t == "codebox":
                n.objects.append(Obj("code", int(child.get("char_offset", 0)), text=child.text or "",
                                     syntax=child.get("syntax_highlighting", "")))
            elif t == "node":
                n.children.append(walk(child))
        return n

    roots = [walk(el) for el in root if el.tag == "node"]
    return roots, nodes


# --------------------------------------------------------------------------
# Rich text -> HTML
# --------------------------------------------------------------------------

BULLETS = "•◇▪-→⇨◆■●○◦‣⇒➤►▸"
TODO = {"☐": False, "☑": True, "☒": True}
NUM_RE = re.compile(r"^(\d+)[.)]\s")
HEADING_MAP = {"h1": "h2", "h2": "h3", "h3": "h4", "h4": "h5", "h5": "h6", "h6": "h6"}

# gtksourceview language ids (CherryTree) -> Trilium code mimes
SYNTAX_MIME = {
    "plain-text": "text/plain", "sh": "text/x-sh", "bash": "text/x-sh", "python": "text/x-python",
    "python3": "text/x-python", "c": "text/x-csrc", "cpp": "text/x-c++src", "chdr": "text/x-csrc",
    "cpphdr": "text/x-c++src", "c-sharp": "text/x-csharp", "java": "text/x-java", "js": "text/javascript",
    "javascript": "text/javascript", "json": "application/json", "html": "text/html", "xml": "text/xml",
    "css": "text/css", "sql": "text/x-sql", "php": "text/x-php", "perl": "text/x-perl", "ruby": "text/x-ruby",
    "lua": "text/x-lua", "go": "text/x-go", "rust": "text/x-rustsrc", "markdown": "text/x-markdown",
    "yaml": "text/x-yaml", "ini": "text/x-properties", "diff": "text/x-diff", "makefile": "text/plain",
    "cmake": "text/x-cmake", "dosbatch": "application/x-bat", "powershell": "application/x-powershell",
    "latex": "text/x-latex", "vhdl": "text/x-vhdl", "verilog": "text/x-verilog", "asm": "text/x-gas",
    "nasm": "text/x-gas", "pascal": "text/x-pascal", "kotlin": "text/x-kotlin", "swift": "text/x-swift",
    "scala": "text/x-scala", "haskell": "text/x-haskell", "tcl": "text/x-tcl", "fortran": "text/x-fortran",
    "dockerfile": "text/x-dockerfile", "nginx": "text/x-nginx-conf", "objc": "text/x-objectivec",
    "typescript": "application/typescript", "r": "text/x-rsrc", "matlab": "text/x-octave", "octave": "text/x-octave",
}


def code_mime(syntax):
    return SYNTAX_MIME.get((syntax or "").lower(), "text/plain")


def code_class(syntax):
    return "language-" + re.sub(r"[\W_]+", "-", code_mime(syntax).lower())


def urlq(path):
    return html.escape(urllib.parse.quote(path), quote=True)


def esc(s):
    return html.escape(s, quote=False)


def link_href(link, ctx):
    """CherryTree link attribute -> href (or None)."""
    parts = link.split(" ", 2)
    kind = parts[0]
    if kind == "webs" and len(parts) > 1:
        url = link[5:].strip()
        if "javascript:" in url.lower():
            return None
        # Trilium's importer treats any href without "//" (mailto:, tel:)
        # as a path inside the ZIP and turns it into a broken note link,
        # so those are kept as plain text instead.
        if "//" not in url:
            return None
        return url
    if kind == "node" and len(parts) > 1:
        try:
            target = ctx["nodes"].get(int(parts[1]))
        except ValueError:
            target = None
        if target and target.data_file:
            return urllib.parse.quote(relpath(target.dir_path + target.data_file, ctx["node"].dir_path))
        return None
    if kind in ("file", "fold") and len(parts) > 1:
        try:
            p = base64.b64decode(parts[1]).decode("utf-8")
        except Exception:
            p = parts[1]
        p = p.replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return "file://" + p
    return None


def relpath(target, from_dir):
    """Relative path between two zip paths ('a/b/c.html' from 'a/d/')."""
    t = target.split("/")
    f = [x for x in from_dir.split("/") if x]
    i = 0
    while i < len(f) and i < len(t) - 1 and f[i] == t[i]:
        i += 1
    return "/".join([".."] * (len(f) - i) + t[i:])


def inline_html(text, a, ctx):
    if not text:
        return ""
    out = esc(text)
    if a.get("family") == "monospace":
        out = f"<code>{out}</code>"
    if a.get("weight") == "heavy":
        out = f"<strong>{out}</strong>"
    if a.get("style") == "italic":
        out = f"<em>{out}</em>"
    if a.get("underline") in ("single", "double", "low", "error"):
        out = f"<u>{out}</u>"
    if a.get("strikethrough") == "true":
        out = f"<s>{out}</s>"
    scale = a.get("scale")
    if scale == "sup":
        out = f"<sup>{out}</sup>"
    elif scale == "sub":
        out = f"<sub>{out}</sub>"
    elif scale == "small":
        out = f'<span class="text-small">{out}</span>'
    styles = []
    fg = a.get("foreground")
    if fg and not is_neutral(fg, dark=True):
        styles.append(f"color:{css_color(fg)}")
    bg = a.get("background")
    if bg and not is_neutral(bg, dark=False):
        styles.append(f"background-color:{css_color(bg)}")
    if styles:
        out = f'<span style="{";".join(styles)}">{out}</span>'
    if a.get("link"):
        href = link_href(a["link"], ctx)
        if href:
            out = f'<a href="{html.escape(href, quote=True)}">{out}</a>'
    return out


def is_neutral(c, dark):
    """True for near-black/grey text or near-white backgrounds (usually web-paste noise)
    that would only fight the Trilium theme, especially a dark one."""
    c = css_color(c)
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
        return False
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    if max(r, g, b) - min(r, g, b) > 24:
        return False
    return max(r, g, b) < 0x90 if dark else min(r, g, b) > 0xe8


def css_color(c):
    # CherryTree sometimes stores 12-digit GTK colours (#rrrrggggbbbb)
    if re.fullmatch(r"#[0-9a-fA-F]{12}", c):
        return "#" + c[1:3] + c[5:7] + c[9:11]
    return c


class Line:
    def __init__(self):
        self.pieces = []          # [(text, attrs)]

    def plain(self):
        return "".join(t for t, _ in self.pieces)

    def empty(self):
        return not self.plain().strip()


def node_to_html(node, ctx, attachments):
    """Return the HTML body for a rich-text node."""
    # 1. Merge runs and anchored objects into one stream. Each object
    #    occupies one character position in CherryTree's buffer.
    objs = sorted(node.objects, key=lambda o: o.offset)
    stream = []
    pos = 0                       # position in pure text
    oi = 0
    for text, a in node.runs:
        i = 0
        while i < len(text):
            while oi < len(objs) and objs[oi].offset - oi <= pos:
                stream.append(objs[oi])
                oi += 1
            nxt = objs[oi].offset - oi if oi < len(objs) else None
            take = len(text) - i if nxt is None else min(len(text) - i, nxt - pos)
            stream.append((text[i:i + take], a))
            i += take
            pos += take
    stream.extend(objs[oi:])

    # 2. Build block-level items: Line objects and Obj objects.
    blocks = []
    cur = Line()
    for item in stream:
        if isinstance(item, Obj):
            if item.kind == "anchor":
                continue
            if not cur.empty():
                blocks.append(cur)
            cur = Line()
            blocks.append(item)
            continue
        text, a = item
        parts = text.split("\n")
        for k, p in enumerate(parts):
            if k > 0:
                blocks.append(cur)
                cur = Line()
            if p:
                cur.pieces.append((p, a))
    blocks.append(cur)

    # Drop a leading heading/line that just repeats the node name (it
    # becomes the Trilium note title anyway).
    for i, b in enumerate(blocks):
        if isinstance(b, Line) and b.empty():
            continue
        if isinstance(b, Line) and heading_tag(b) and \
                b.plain().strip().lower() == node.name.strip().lower():
            blocks[i] = Line()
        break

    # 3. Render, grouping list lines into <ul>/<ol>/todo lists.
    out = []
    list_kind = None

    def close_list():
        nonlocal list_kind
        if list_kind:
            out.append("</ol>" if list_kind == "ol" else "</ul>")
            list_kind = None

    def open_list(kind):
        nonlocal list_kind
        if list_kind != kind:
            close_list()
            out.append({"ul": "<ul>", "ol": "<ol>", "todo": '<ul class="todo-list">'}[kind])
            list_kind = kind

    for b in blocks:
        if isinstance(b, Obj):
            close_list()
            out.append(render_obj(b, node, ctx, attachments))
            continue
        if b.empty():
            continue
        kind, pieces = classify_line(b)
        inner = "".join(inline_html(t, a, ctx) for t, a in pieces).strip()
        if kind in ("ul", "ol"):
            open_list(kind)
            out.append(f"<li>{inner}</li>")
            continue
        if kind and kind.startswith("todo"):
            open_list("todo")
            checked = " checked" if kind == "todo-done" else ""
            out.append('<li><label class="todo-list__label"><input type="checkbox" disabled'
                       f'{checked}><span class="todo-list__label__description">{inner}</span></label></li>')
            continue
        close_list()
        tag = heading_tag(b)
        just = next((a.get("justification") for t, a in b.pieces if t.strip() and a.get("justification")), None)
        style = f' style="text-align:{just};"' if just in ("center", "right", "fill") else ""
        style = style.replace("fill", "justify")
        if tag:
            # bold inside headings is redundant
            inner = re.sub(r"</?strong>", "", inner)
            out.append(f"<{tag}{style}>{inner}</{tag}>")
        else:
            out.append(f"<p{style}>{inner}</p>")
    close_list()
    return "\n".join(out)


def classify_line(line):
    """Detect CherryTree list prefixes; return (kind, pieces without prefix)."""
    pieces = list(line.pieces)
    first = pieces[0][0]
    stripped = first.lstrip(" \t")
    kind = None
    cut = 0
    if stripped[:1] in TODO and stripped[1:2] == " ":
        kind = "todo-done" if TODO[stripped[0]] else "todo"
        cut = len(first) - len(stripped) + 2
    elif stripped[:1] in BULLETS and stripped[1:2] == " ":
        kind = "ul"
        cut = len(first) - len(stripped) + 2
    else:
        m = NUM_RE.match(stripped)
        if m:
            kind = "ol"
            cut = len(first) - len(stripped) + m.end()
    if kind:
        rest = first[cut:]
        pieces[0] = (rest, pieces[0][1])
        # the prefix often sits in its own unformatted run, so the list text
        # may be entirely in following pieces
        pieces = [p for p in pieces if p[0]]
        if not "".join(t for t, _ in pieces).strip():
            return None, line.pieces
    return kind, pieces


def heading_tag(line):
    scales = {a.get("scale") for t, a in line.pieces if t.strip()}
    if len(scales) == 1:
        s = next(iter(scales))
        if s in HEADING_MAP:
            return HEADING_MAP[s]
    elif scales and all(s in HEADING_MAP for s in scales):
        return HEADING_MAP[min(scales)]
    return None


def guess_image_ext(blob):
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if blob[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if blob[:4] == b"GIF8":
        return "gif", "image/gif"
    return "png", "image/png"


def render_obj(o, node, ctx, attachments):
    if o.kind == "image":
        ext, mime = guess_image_ext(o.data["blob"])
        n = len(attachments) + 1
        fname = f"{node.data_file[:-5]}_img{n}.{ext}"
        attachments.append({"role": "image", "mime": mime, "title": f"image{n}.{ext}",
                            "dataFileName": fname, "blob": o.data["blob"]})
        img = f'<img src="{urlq(fname)}">'
        link = o.data.get("link") or ""
        href = link_href(link, ctx) if link and "data:" not in link[:12] else None
        if href:
            img = f'<a href="{html.escape(href, quote=True)}">{img}</a>'
        return f'<figure class="image">{img}</figure>'
    if o.kind == "file":
        orig = o.data["filename"]
        n = len(attachments) + 1
        safe = safe_name(orig)
        fname = f"{node.data_file[:-5]}_file{n}_{safe}"
        mime = mimetypes.guess_type(orig)[0] or "application/octet-stream"
        attachments.append({"role": "file", "mime": mime, "title": orig,
                            "dataFileName": fname, "blob": o.data["blob"]})
        return f'<p><a href="{urlq(fname)}">📎 {esc(orig)}</a></p>'
    if o.kind == "table":
        rows = o.data["rows"]
        if not rows:
            return ""
        header, body = rows[-1], rows[:-1]     # CherryTree stores the header row last

        def cell(t, tag):
            return f"<{tag}>{esc(t).replace(chr(10), '<br>')}</{tag}>"
        h = "<tr>" + "".join(cell(c, "th") for c in header) + "</tr>"
        b = "".join("<tr>" + "".join(cell(c, "td") for c in r) + "</tr>" for r in body)
        return f'<figure class="table"><table><thead>{h}</thead><tbody>{b}</tbody></table></figure>'
    if o.kind == "code":
        return f'<pre><code class="{code_class(o.data["syntax"])}">{esc(o.data["text"])}</code></pre>'
    return ""


# --------------------------------------------------------------------------
# Layout + ZIP writing
# --------------------------------------------------------------------------


def new_id():
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(12))


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip().strip(".")
    return (s or "note")[:80]


def layout(nodes_list, dir_path):
    used = set()
    for n in nodes_list:
        base = safe_name(n.name)
        cand, k = base, 2
        while cand.lower() in used:
            cand, k = f"{base}_{k}", k + 1
        used.add(cand.lower())
        n.note_id = new_id()
        n.dir_path = dir_path
        n.data_file = cand + ".html"
        if n.children:
            n.dir_file = cand
            layout(n.children, dir_path + cand + "/")


def label_name(tag):
    t = re.sub(r"[^\w:]", "_", tag.strip())
    return t if t and not t[0].isdigit() else "tag_" + t


def build(node, ctx, zf, stats):
    ctx["node"] = node
    attrs = []
    for tag in re.split(r"[,\s]+", node.tags or ""):
        if tag:
            attrs.append({"type": "label", "name": label_name(tag), "value": "", "isInheritable": False})
    if node.readonly:
        attrs.append({"type": "label", "name": "readOnly", "value": "", "isInheritable": False})
    for i, a in enumerate(attrs):
        a["position"] = (i + 1) * 10

    attachments = []
    is_rich = node.syntax == "custom-colors"
    if is_rich:
        body = node_to_html(node, ctx, attachments)
        content = ("<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">"
                   f"<title>{esc(node.name)}</title></head><body>\n{body}\n</body></html>\n")
        ntype, mime = "text", "text/html"
        data_file = node.data_file
    else:
        content = "".join(t for t, _ in node.runs)
        ntype, mime = "code", code_mime(node.syntax)
        data_file = node.data_file
    zf.writestr(node.dir_path + data_file, content.encode("utf-8"))
    stats["notes"] += 1

    meta_atts = []
    for i, a in enumerate(attachments):
        zf.writestr(node.dir_path + a["dataFileName"], a["blob"])
        meta_atts.append({"attachmentId": new_id(), "title": a["title"], "role": a["role"],
                          "mime": a["mime"], "position": (i + 1) * 10, "dataFileName": a["dataFileName"]})
        stats["images" if a["role"] == "image" else "files"] += 1
    stats["tables"] += sum(1 for o in node.objects if o.kind == "table")
    stats["codeboxes"] += sum(1 for o in node.objects if o.kind == "code")

    meta = {"noteId": node.note_id, "notePath": ctx["path"] + [node.note_id], "isClone": False,
            "title": node.name, "notePosition": ctx["pos"], "prefix": None, "isExpanded": False,
            "type": ntype, "mime": mime, "attributes": attrs, "format": "html",
            "dataFileName": data_file, "attachments": meta_atts}
    if node.children:
        meta["dirFileName"] = node.dir_file
        kids = []
        saved_path = ctx["path"]
        ctx["path"] = saved_path + [node.note_id]
        for i, c in enumerate(node.children):
            ctx["pos"] = (i + 1) * 10
            kids.append(build(c, ctx, zf, stats))
        ctx["path"] = saved_path
        meta["children"] = kids
    return meta


def convert(src, dst, wrap=True):
    ext = os.path.splitext(src)[1].lower()
    if ext == ".ctb":
        roots, nodes = load_ctb(src)
    elif ext == ".ctd":
        roots, nodes = load_ctd(src)
    elif ext in (".ctz", ".ctx"):
        sys.exit("This is a password-protected CherryTree file. In CherryTree use File > Save As, "
                 "pick SQLite (.ctb) or XML (.ctd) without a password, then convert that copy.")
    else:
        sys.exit(f"Unrecognised file type '{ext}' (expected .ctb or .ctd)")

    doc_name = os.path.splitext(os.path.basename(src))[0]
    if wrap:
        top = Node(-1, doc_name)
        top.children = roots
        roots = [top]
    layout(roots, "")

    stats = {"notes": 0, "images": 0, "files": 0, "tables": 0, "codeboxes": 0}
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        ctx = {"nodes": nodes, "path": ["root"], "pos": 10}
        files = []
        for i, r in enumerate(roots):
            ctx["pos"] = (i + 1) * 10
            files.append(build(r, ctx, zf, stats))
        meta = {"formatVersion": 2, "appVersion": "cherrytree2trilium", "files": files}
        zf.writestr("!!!meta.json", json.dumps(meta, indent=1, ensure_ascii=False))
    if wrap:
        stats["notes"] -= 1
    return stats


def main():
    ap = argparse.ArgumentParser(description="Convert a CherryTree .ctb/.ctd file into a Trilium import ZIP.")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--no-wrap", action="store_true",
                    help="don't put everything under a note named after the file")
    args = ap.parse_args()
    out = args.output or os.path.splitext(args.input)[0] + "-trilium.zip"
    s = convert(args.input, out, wrap=not args.no_wrap)
    print(f"Wrote {out}: {s['notes']} notes, {s['images']} images, {s['files']} embedded files, "
          f"{s['tables']} tables, {s['codeboxes']} code boxes")


if __name__ == "__main__":
    main()
