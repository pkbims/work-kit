#!/usr/bin/env python3
"""Export an html-worker page to Markdown — the page is the single source of truth.

Decision blocks are rendered with the answer recorded in state.json, so the markdown
and the page cannot drift. Mermaid blocks become ```mermaid fences, which GitHub renders.
"""
import json, os, re, html
from html.parser import HTMLParser
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
state = json.load(open(os.path.join(HERE, "state.json")))
DEC = state["decisions"]

VOID = {"br", "img", "input", "hr", "meta", "link"}


class Node:
    def __init__(self, tag=None, attrs=None):
        self.tag, self.attrs, self.kids = tag, dict(attrs or {}), []
    def cls(self):
        return self.attrs.get("class", "").split()


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root"); self.stack = [self.root]
    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs); self.stack[-1].kids.append(n)
        if tag not in VOID: self.stack.append(n)
    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(Node(tag, attrs))
    def handle_endtag(self, tag):
        if tag in VOID: return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]; return
    def handle_data(self, data):
        if data.strip(): self.stack[-1].kids.append(data)


def raw(n, br="\n"):
    """Plain text of a subtree. `br` is what a <br> becomes."""
    if isinstance(n, str): return n
    if n.tag == "br": return br
    if n.tag == "button": return ""
    return "".join(raw(k, br) for k in n.kids)


def inline(n):
    """Inline markdown of a subtree."""
    if isinstance(n, str): return re.sub(r"\s+", " ", n)
    if n.tag == "br": return " "
    if n.tag == "button": return ""                      # comment buttons are UI, not content
    if "num" in n.cls(): return ""                       # section numbers come from the heading order
    if n.tag == "a" and n.attrs.get("href"):             # keep links — quotes must stay traceable
        return f"[{''.join(inline(k) for k in n.kids).strip()}]({n.attrs['href']})"
    if n.tag == "s": return "~~" + "".join(inline(k) for k in n.kids).strip() + "~~"
    inner = "".join(inline(k) for k in n.kids)
    if n.tag in ("strong", "b"): return f"**{inner.strip()}**"
    if n.tag in ("em", "i"): return f"*{inner.strip()}*"
    if n.tag == "code": return f"`{inner.strip()}`"
    if n.tag == "cite": return f" — {inner.strip()}"
    if n.tag == "del": return f"~~{inner.strip()}~~"
    return inner


def find(n, pred):
    if not isinstance(n, str):
        if pred(n): yield n
        for k in n.kids:
            yield from find(k, pred)


def decision_md(n):
    """Render a .d block using the recorded answer."""
    grp = next((k for k in find(n, lambda x: "data-radio" in x.attrs or "data-check" in x.attrs)), None)
    if grp is None: return ""
    did = grp.attrs.get("data-radio") or grp.attrs.get("data-check")
    q = next((raw(x).strip() for x in find(n, lambda x: "q" in x.cls())), did)
    why = next((raw(x).strip() for x in find(n, lambda x: "why" in x.cls())), "")
    chosen = DEC.get(did)
    chosen = chosen if isinstance(chosen, list) else ([chosen] if chosen else [])

    out = [f"**{q}**", ""]
    if why: out += [f"*{why}*", ""]
    for lab in find(grp, lambda x: x.tag == "label"):
        inp = next((x for x in find(lab, lambda y: y.tag == "input")), None)
        if inp is None: continue
        val = inp.attrs.get("value", "")
        head = next((raw(x).strip() for x in find(lab, lambda x: "oh" in x.cls())), val)
        desc = next((raw(x).strip() for x in find(lab, lambda x: "od" in x.cls())), "")
        mark = "**→**" if val in chosen else "  ·"
        line = f"{mark} **{head}**" if val in chosen else f"{mark} {head}"
        if desc: line += f" — {desc}"
        out.append(line)
    if not chosen: out += ["", "> **Not yet decided.**"]
    out.append("")
    return "\n".join(out)


def table_md(n):
    rows = []
    for tr in find(n, lambda x: x.tag == "tr"):
        cells = [inline(c).strip() for c in tr.kids
                 if not isinstance(c, str) and c.tag in ("td", "th")]
        if cells: rows.append(cells)
    if not rows: return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join([" --- "] * width) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out) + "\n"


def block(n, depth=2):
    if isinstance(n, str): return ""
    c = n.cls()

    if n.tag == "pre" and "mermaid" in c:
        return "```mermaid\n" + raw(n, br="<br/>").strip() + "\n```\n"
    if n.tag == "pre":
        return "```\n" + raw(n).strip() + "\n```\n"
    if n.tag == "table":
        return table_md(n)
    if "d" in c and any(find(n, lambda x: "data-radio" in x.attrs or "data-check" in x.attrs)):
        return decision_md(n)
    if "plain" in c:
        return "> " + inline(n).strip() + "\n"
    if "risk" in c:
        return "> [!WARNING]\n> " + inline(n).strip().replace("\n", "\n> ") + "\n"
    if n.tag == "blockquote":
        return "> " + inline(n).strip() + "\n"
    if n.tag == "h2":
        txt = inline(n).strip()
        txt = re.sub(r"^\s*(\d+)\s*", r"\1. ", txt)
        return f"\n## {txt}\n"
    if n.tag == "h3":
        return f"\n### {inline(n).strip()}\n"
    if n.tag == "h4":
        return f"\n**{inline(n).strip()}**\n"
    if n.tag == "p":
        t = inline(n).strip()
        return (f"*{t}*\n" if "sub" in c else f"{t}\n") if t else ""
    if n.tag in ("ul", "ol"):
        out = []
        for i, li in enumerate([k for k in n.kids if not isinstance(k, str) and k.tag == "li"], 1):
            bullet = f"{i}." if n.tag == "ol" else "-"
            out.append(f"{bullet} {inline(li).strip()}")
        return "\n".join(out) + "\n"
    if n.tag == "details":
        summ = next((inline(x).strip() for x in find(n, lambda x: x.tag == "summary")), "Details")
        inner = "".join(block(k, depth) + "\n" for k in n.kids
                        if not isinstance(k, str) and k.tag != "summary")
        return f"\n### {summ}\n\n{inner}"
    if n.tag in ("div", "section", "main"):
        return "".join(block(k, depth) + "\n" for k in n.kids if not isinstance(k, str))
    return ""


def screens_md(page_src):
    """Phone mockups are built in JS; pull their titles and notes out for the doc."""
    m = re.search(r"const SCREENS=\[(.*?)\n\];", page_src, re.S)
    if not m: return ""
    out = []
    for ent in re.finditer(r"\{k:'([^']+)',t:'([^']+)'.*?m:\[(.*?)\]\}", m.group(1), re.S):
        notes = re.findall(r"'((?:[^'\\]|\\.)*)'", ent.group(3))
        notes = [re.sub(r"<[^>]+>", "", html.unescape(n.replace("\\'", "'"))) for n in notes]
        out.append(f"**{ent.group(2)}**\n\n" + "\n".join(f"- {n}" for n in notes) + "\n")
    return ("\n".join(out) + "\n") if out else ""


page = open(os.path.join(HERE, "index.html")).read()
t = Tree(); t.feed(page)
main = next(find(t.root, lambda n: n.tag == "main"))
sections = [n for n in main.kids if not isinstance(n, str) and n.tag == "section"]

parts, last_group = [], None
for sec in sections:
    grp = sec.attrs.get("data-group")
    if grp != last_group:
        parts.append(f"\n---\n\n# {grp}\n"); last_group = grp
    body = "".join(block(k) + "\n" for k in sec.kids
                   if not isinstance(k, str) and k.tag != "button")
    if sec.attrs.get("data-anchor") == "ui":
        marker = "\n**U1 —"
        sc = screens_md(page)
        if sc:
            body = (body.split(marker)[0] + "\n" + sc + marker + marker.join(body.split(marker)[1:])
                    if marker in body else body + "\n" + sc)
    parts.append(re.sub(r"\n{3,}", "\n\n", body))

decided = len(DEC)
groups = len(list(find(t.root, lambda n: "data-radio" in n.attrs or "data-check" in n.attrs)))
TITLE = os.environ.get("TITLE", "Document")
DIR = os.path.basename(HERE)
header = f"""# {TITLE}

Generated {datetime.now().strftime('%Y-%m-%d')} from `{DIR}/index.html` and `{DIR}/state.json`.
{len(state['batches'])} review rounds · {decided} decisions recorded · {groups} decision points in the document.

> Generated file — do not edit by hand. Change the page or the decisions and re-run
> `OUT=<file> TITLE="<title>" python3 {DIR}/export-md.py`.
"""

out = header + "".join(parts)
out = re.sub(r"\n{3,}", "\n\n", out)
open(os.path.join(APP, os.environ.get("OUT", "OUTPUT.md")), "w").write(out)
print(f"markdown written — {len(sections)} sections, {groups} decision points, {len(out.splitlines())} lines")
