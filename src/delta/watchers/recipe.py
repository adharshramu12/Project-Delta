"""A watcher's recipe: which pages of a site to read and how to cut each into items, with no AI at run time.

    {"sections": [
       {"name": "Upcoming exams", "url": "/exams", "rows": "table#exams tbody tr",
        "fields": {"course": "td:nth-child(1)", "date": "td:nth-child(2)"}, "key": ["course", "date"]},
       {"name": "Timetable", "url": "/timetable"},                       # auto: the best matching table or list
       {"name": "Notices", "url": "/notices", "rows": "ul.notices li"}   # each row's text is one item
    ]}

`url` is a path on the login's own site (or a full address on exactly that origin; anything else is refused).
Without `rows` the section is read automatically: a table whose caption or heading matches the section's name,
else the page's first table with headers, else a list under a matching heading. `fields` maps item fields to CSS
selectors inside a row; without them a table row's cells are named by its column headers and any other row is one
`text` field. `key` names the fields that identify an item (to tell "changed" from "new"); without it the whole item
is its identity.

Recipes are learned per site (the model inspects the pages with inspect_portal and saves one with
save_watch_recipe, which is checked against the live pages first) and kept as a skill, so they can be seen, retired
and shared like any skill. Nothing personal is built in: no site, address or account is hardcoded.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, Field, field_validator

from delta.logins.origin import Origin, origin_of

MAX_SECTIONS = 8
MAX_ITEMS = 200  # items kept per section (bounded snapshots)
MAX_FIELD = 300  # characters per field
MAX_FIELDS = 12
_CSS_BAD = re.compile(r"[{}<>]|javascript:|expression\(", re.I)


class Section(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    url: str = Field(default="/", max_length=500)
    rows: str = Field(default="", max_length=300)
    fields: dict[str, str] = Field(default_factory=dict)
    key: list[str] = Field(default_factory=list)

    @field_validator("rows")
    @classmethod
    def _css(cls, v: str) -> str:
        if _CSS_BAD.search(v):
            raise ValueError("rows must be a plain CSS selector")
        return v.strip()

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > MAX_FIELDS:
            raise ValueError(f"at most {MAX_FIELDS} fields")
        out = {}
        for k, sel in v.items():
            name = re.sub(r"[^a-z0-9_]+", "_", str(k).lower()).strip("_")[:30]
            if not name or _CSS_BAD.search(str(sel)):
                raise ValueError(f"bad field {k!r}")
            out[name] = str(sel).strip()[:200]
        return out


class Recipe(BaseModel):
    sections: list[Section] = Field(min_length=1, max_length=MAX_SECTIONS)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_defaults=False)

    @classmethod
    def parse(cls, raw: str | dict[str, Any]) -> Recipe:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls.model_validate(data)


def section_url(origin: Origin, url: str) -> str:
    """The full address of a section, which must be on the login's exact origin. ValueError otherwise."""
    full = urljoin(str(origin) + "/", (url or "/").strip())
    if origin_of(full) != origin:
        raise ValueError(f"{full} is not on {origin}; a watcher only reads the site its login belongs to")
    return full


def default_recipe(what: list[str]) -> Recipe:
    """A starting recipe from the user's words ("timetable, exams, notices"): each read automatically from the page
    the site opens after signing in, until a learned recipe says where each one really is."""
    names = [w.strip()[:60] for w in what if w.strip()][:MAX_SECTIONS] or ["Updates"]
    return Recipe(sections=[Section(name=n, url="") for n in names])


# Extraction, inside the page. Arguments: {rows, fields, name, limit, chars}. Returns
# {status: "ok" | "empty" | "missing", items: [...], how: "..."}.
EXTRACT_JS = r"""(a) => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, a.chars);
  const words = (s) => clean(s).toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2);
  const want = words(a.name);
  const near = (el) => {
    let t = '';
    const cap = el.querySelector && el.querySelector('caption');
    if (cap) t += ' ' + cap.innerText;
    if (el.getAttribute) {
      t += ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.id || '') + ' ' + (el.className || '');
    }
    let p = el, hops = 0;
    while (p && hops < 4) {
      let s = p.previousElementSibling, n = 0;
      while (s && n < 3) {
        if (/^H[1-6]$|^LEGEND$|^LABEL$/.test(s.tagName) || s.matches('.title,.heading,.card-header')) {
          t += ' ' + s.innerText; break;
        }
        s = s.previousElementSibling; n++;
      }
      const head = p.querySelector && p !== el ? p.querySelector('h1,h2,h3,h4,.card-header,.title') : null;
      if (head) t += ' ' + head.innerText;
      p = p.parentElement; hops++;
    }
    return t.toLowerCase();
  };
  const score = (el) => want.filter((w) => near(el).includes(w)).length;
  const headers = (table) => {
    let ths = Array.from(table.querySelectorAll('thead th'));
    if (!ths.length) { const r = table.querySelector('tr'); if (r) ths = Array.from(r.querySelectorAll('th')); }
    const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
    return ths.map((th, i) => (slug(clean(th.innerText)) || ('col' + (i + 1))));
  };
  const fromRow = (row, hdr) => {
    const item = {};
    if (Object.keys(a.fields).length) {
      for (const [k, sel] of Object.entries(a.fields)) {
        let el = null; try { el = row.querySelector(sel); } catch (e) { el = null; }
        item[k] = el ? clean(el.innerText || el.value || '') : '';
      }
      return item;
    }
    const cells = row.tagName === 'TR' ? Array.from(row.querySelectorAll('td,th')) : [];
    if (cells.length) {
      cells.forEach((c, i) => { item[(hdr && hdr[i]) || ('col' + (i + 1))] = clean(c.innerText); });
      return item;
    }
    item.text = clean(row.innerText);
    return item;
  };
  const keep = (rows, hdr) => rows.filter((r) => clean(r.innerText)).slice(0, a.limit).map((r) => fromRow(r, hdr));
  if (a.rows) {
    let rows = [];
    try { rows = Array.from(document.querySelectorAll(a.rows)); }
    catch (e) { return {status: 'missing', items: [], how: 'bad selector'}; }
    const headerRow = (r) => r.tagName === 'TR' && r.querySelector('th') && !r.querySelector('td');
    const visible = rows.filter((r) => !(r.closest('thead')) && !headerRow(r));
    if (visible.length) {
      const table = visible[0].closest('table');
      return {status: 'ok', items: keep(visible, table ? headers(table) : null), how: a.rows};
    }
    const parent = a.rows.trim().split(/\s+/).slice(0, -1).join(' ');
    let box = null; try { box = parent ? document.querySelector(parent) : null; } catch (e) { box = null; }
    return {status: box ? 'empty' : 'missing', items: [], how: a.rows};
  }
  const tables = Array.from(document.querySelectorAll('table')).filter((t) => t.querySelector('td'));
  const lists = Array.from(document.querySelectorAll('ul,ol'))
    .filter((l) => !l.closest('nav,header,footer,menu') && l.children.length);
  const cards = Array.from(document.querySelectorAll('[class*=card],[class*=item],[class*=notice],[class*=event]'))
    .filter((c) => c.children.length && !c.closest('nav,header,footer'));
  const best = (els) => els.map((e) => [score(e), e]).sort((x, y) => y[0] - x[0])[0];
  const t = best(tables), l = best(lists);
  if (t && (t[0] > 0 || !want.length)) {
    const rows = Array.from(t[1].querySelectorAll('tr')).filter((r) => r.querySelector('td'));
    return {status: rows.length ? 'ok' : 'empty', items: keep(rows, headers(t[1])), how: 'table'};
  }
  if (l && l[0] > 0) return {status: 'ok', items: keep(Array.from(l[1].children), null), how: 'list'};
  const c = best(cards);
  if (c && c[0] > 0) {
    const kids = Array.from(c[1].parentElement ? c[1].parentElement.children : [])
      .filter((k) => k.className === c[1].className);
    return {status: 'ok', items: keep(kids.length ? kids : [c[1]], null), how: 'cards'};
  }
  if (t && tables.length === 1) {
    const rows = Array.from(t[1].querySelectorAll('tr')).filter((r) => r.querySelector('td'));
    return {status: 'ok', items: keep(rows, headers(t[1])), how: 'table'};
  }
  const empty = /\b(no (upcoming|new|pending|records?|data|items?|notifications?|events?|exams?)|nothing to show)\b/i;
  const body = document.body.innerText;
  const named = want.some((w) => body.toLowerCase().includes(w));
  return {status: (named && empty.test(body)) ? 'empty' : 'missing', items: [], how: 'auto'};
}"""

# For inspect_portal: what can be read on the page, with a selector the model can put in a recipe.
INSPECT_JS = r"""() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const css = (el) => {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return el.tagName.toLowerCase() + '#' + el.id;
    const cls = Array.from(el.classList).filter((c) => /^[A-Za-z][\w-]*$/.test(c)).slice(0, 2);
    if (cls.length) return el.tagName.toLowerCase() + '.' + cls.join('.');
    const sibs = Array.from(el.parentElement ? el.parentElement.children : []).filter((x) => x.tagName === el.tagName);
    return el.tagName.toLowerCase() + ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
  };
  const heading = (el) => {
    let p = el, hops = 0;
    while (p && hops < 4) {
      let s = p.previousElementSibling;
      while (s) { if (/^H[1-6]$/.test(s.tagName)) return clean(s.innerText); s = s.previousElementSibling; }
      p = p.parentElement; hops++;
    }
    const cap = el.querySelector('caption'); return cap ? clean(cap.innerText) : '';
  };
  const out = [];
  for (const t of Array.from(document.querySelectorAll('table')).slice(0, 8)) {
    const rows = Array.from(t.querySelectorAll('tr')).filter((r) => r.querySelector('td'));
    if (!rows.length) continue;
    const hdr = Array.from(t.querySelectorAll('th')).map((th) => clean(th.innerText)).slice(0, 8);
    out.push({kind: 'table', heading: heading(t), rows: css(t) + ' tr', count: rows.length, columns: hdr,
              sample: clean(rows[0].innerText)});
  }
  for (const l of Array.from(document.querySelectorAll('ul,ol')).slice(0, 20)) {
    if (l.closest('nav,header,footer,menu') || l.children.length < 1) continue;
    out.push({kind: 'list', heading: heading(l), rows: css(l) + ' > li', count: l.children.length, columns: [],
              sample: clean(l.children[0].innerText)});
    if (out.length >= 12) break;
  }
  const links = Array.from(document.querySelectorAll('a[href]')).filter((a) => a.href.startsWith(location.origin))
    .slice(0, 40).map((a) => ({text: clean(a.innerText), path: a.getAttribute('href')}));
  return {parts: out, links};
}"""
