"""Doc -> Markdown (the cleaned text, with the writing rules applied), for notes apps, GitHub or editing."""

from __future__ import annotations

from delta.addons.writer.document import Block, Doc


def _block(b: Block) -> str:
    if b.type == "heading":
        return "#" * min(6, b.level + 1) + " " + b.text
    if b.type == "paragraph":
        return b.text
    if b.type == "bullets":
        return "\n".join(f"- {i}" for i in b.items)
    if b.type == "numbered":
        return "\n".join(f"{n}. {i}" for n, i in enumerate(b.items, 1))
    if b.type == "table" and b.rows:
        head = "| " + " | ".join(b.rows[0]) + " |"
        sep = "|" + "|".join(" --- " for _ in b.rows[0]) + "|"
        rows = ["| " + " | ".join(c.replace("|", "/") for c in r) + " |" for r in b.rows[1:]]
        return "\n".join([head, sep, *rows])
    if b.type == "callout":
        return f"> **{b.label}:** {b.text}"
    if b.type == "quote":
        return f"> {b.text}"
    if b.type == "code":
        return f"```\n{b.text}\n```"
    if b.type == "math":
        return f"$$\n{b.text}\n$$"
    if b.type == "pagebreak":
        return "<!-- pagebreak -->"
    return ""


def render_markdown(doc: Doc) -> str:
    out = [f"# {doc.title}"]
    if doc.subtitle:
        out.append(f"*{doc.subtitle}*")
    meta = [x for x in (doc.meta.get("contact", ""), doc.author, doc.date) if x]
    if meta:
        out.append(" · ".join(meta))
    for key in ("from", "to", "subject", "number", "due"):
        if doc.meta.get(key):
            out.append(f"**{key.title()}:** {doc.meta[key]}")
    out += [_block(b) for b in doc.blocks]
    if doc.meta.get("signature"):
        out.append(doc.meta["signature"].replace(" / ", "  \n"))
    return "\n\n".join(x for x in out if x).strip() + "\n"
