"""CNKI metadata collector and CNKI RefWorks batch processor (Python 3.10+).

Offline commands use only the standard library. Browser commands need Playwright.
No private API, CAPTCHA solver or full-text download is used.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
import hashlib
from html.parser import HTMLParser
import json
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urljoin

BASE = Path(__file__).resolve().parent
TAGS = "RT SR A1 AD T1 JF YR vo IS OP K1 AB SN CN LA DS LK DO FD PB CL U1 U2".split()
STANDARD_TAGS = "RT SR A1 AD T1 JF YR IS vo OP K1 AB SN CN LA DS LK DO FD PB CL U1 U2".split()
REQUIRED_STANDARD_TAGS = "A1 AD T1 JF OP K1 AB SN CN LA DS LK".split()
TAG_CANON = {t.upper(): t for t in TAGS}
TAG_RE = re.compile(r"^([A-Za-z][A-Za-z0-9])(?:\s+(.*)|\s*)$")
NOW = lambda: datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def read_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if cfg["start_year"] > cfg["end_year"]:
        raise ValueError("start_year 必须小于等于 end_year")
    if not cfg["keywords"] or not cfg["journals"]:
        raise ValueError("关键词及期刊列表不能为空")
    if any("'" in v for v in cfg["keywords"] + [j["name"] for j in cfg["journals"]]):
        raise ValueError("检索词中不能包含未转义的单引号")
    return cfg


def make_tasks(cfg, split="journal"):
    tasks = []
    for journal in cfg["journals"]:
        names = [journal["name"]] + journal.get("aliases", [])
        periods = [(y, y) for y in range(cfg["start_year"], cfg["end_year"] + 1)] if split == "year" else [(cfg["start_year"], cfg["end_year"])]
        for lo, hi in periods:
            # Logical recipe: check professional-search field syntax in the current UI.
            topic = " AND ".join(f"SU='{k}'" for k in cfg["keywords"])
            sources = " OR ".join(f"JN='{n}'" for n in names)
            year = f"YE={lo}" if lo == hi else f"YE>={lo} AND YE<={hi}"
            query = f"({topic}) AND ({sources}) AND ({year})"
            body = {"journal": journal["name"], "names": names, "start_year": lo,
                    "end_year": hi, "keywords": cfg["keywords"], "query": query}
            body["task_id"] = digest(json.dumps(body, ensure_ascii=False, sort_keys=True))
            tasks.append(body)
    return tasks


def decode_text(path):
    data = Path(path).read_bytes()
    encodings = ["utf-16", "utf-8-sig", "gb18030"] if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ["utf-8-sig", "gb18030"]
    for encoding in encodings:
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    raise ValueError(f"无法识别文件编码：{path}")


def parse_refworks(text):
    """Keep repeated tags and continuation lines; RT starts a record.

    Unknown tags with two-character labels are preserved, instead of dropped.
    Blank lines never terminate a record, because AB may contain blank lines.
    """
    records, current, previous = [], None, None
    for line in text.lstrip("\ufeff").splitlines():
        match = TAG_RE.match(line)
        if match and (match[1].upper() in TAG_CANON or match[1].isupper()):
            tag = TAG_CANON.get(match[1].upper(), match[1])
            value = match[2] or ""
            if tag == "RT":
                if current is not None:
                    records.append(current)
                current, previous = {}, None
            if current is None:
                continue
            current.setdefault(tag, []).append(value)
            previous = tag
        elif current is not None and previous and line.strip():
            current[previous][-1] += "\n" + line.strip()
    if current is not None:
        records.append(current)
    return records


def scalar(fields, tag):
    return ";".join(clean(x) for x in fields.get(tag, []) if clean(x))


def split_semicolons(values):
    return [clean(part) for value in values for part in re.split(r"[;；]+", value) if clean(part)]


def normalize_fields(fields):
    """Normalize syntax only; never invent institutions, ISSNs or language."""
    result = {tag: [clean(v) for v in values if clean(v)] for tag, values in fields.items()}
    for tag in ("A1", "K1", "AD", "LA"):
        if result.get(tag):
            parts = split_semicolons(result[tag])
            if tag == "LA":
                language_names = {"zh": "中文", "zh-cn": "中文", "chi": "中文", "zho": "中文", "chinese": "中文"}
                parts = [language_names.get(part.casefold(), part) for part in parts]
            result[tag] = [";".join(parts) + (";" if tag in ("AD", "LA") and parts else "")] if parts else []
    issue = scalar(result, "IS")
    if re.fullmatch(r"\d+", issue):
        result["IS"] = [str(int(issue))]
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", scalar(result, "DO"), flags=re.I).strip().rstrip(";；")
    if doi:
        result["DO"] = [doi]
        if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            host = "https://link.cnki.net/doi/" if ".cnki." in doi.casefold() else "https://doi.org/"
            result["LK"] = [host + doi]
    return result


def missing_standard_fields(fields, cfg):
    required = cfg.get("standard_export", {}).get("required_tags", REQUIRED_STANDARD_TAGS)
    return [tag for tag in required if not scalar(fields, tag)]


def same_article(left, right):
    """A DOI alone cannot override an inconsistent title, journal or author list."""
    ld, rd = doi_key(left), doi_key(right)
    if ld and rd and ld != rd:
        return False
    for tag in ("T1", "JF", "A1"):
        lv, rv = scalar(left, tag), scalar(right, tag)
        if not lv or not rv:
            return False
        if tag == "A1":
            lv, rv = ";".join(split_semicolons([lv])), ";".join(split_semicolons([rv]))
        if normalized(lv) != normalized(rv):
            return False
    return bool(ld and rd) or bool(scalar(left, "YR") and scalar(left, "YR") == scalar(right, "YR"))


class ReferenceIndex:
    """User-provided/exported article records, keyed and matched per article."""
    def __init__(self, directory):
        self.records = []
        directory = Path(directory)
        for path in sorted(directory.glob("*.txt")) if directory.exists() else []:
            text, _ = decode_text(path)
            for fields in parse_refworks(text):
                if scalar(fields, "RT").casefold() == "journal article":
                    self.records.append((normalize_fields(fields), str(path)))

    def find(self, fields):
        matches = [(row, path) for row, path in self.records if same_article(fields, row)]
        if not matches:
            return None
        signatures = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row, _ in matches}
        if len(signatures) > 1:
            raise ValueError("同一篇文章有互相冲突的标准记录，请先核对 reference_dir")
        row, path = matches[0]
        return row, {"method": "user_reference_import", "source_file": path, "matched_by": "doi/title/journal/authors"}


def reference_index(cfg):
    directory = Path(cfg.get("standard_export", {}).get("reference_dir", "official_refworks"))
    return ReferenceIndex(directory if directory.is_absolute() else BASE / directory)


def supplement_fields(fields, reference):
    if not same_article(fields, reference):
        raise ValueError("RefWorks 记录与详情页的 DOI/题名/期刊/作者不一致，拒绝补充")
    merged = {**fields, **{tag: values for tag, values in reference.items() if values}}
    return normalize_fields(merged)


def doi_key(fields):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", scalar(fields, "DO"), flags=re.I).casefold()


def identifiers(fields):
    keys = []
    if doi_key(fields):
        keys.append("doi:" + doi_key(fields))
    link = scalar(fields, "LK")
    query = {k.lower(): v for k, v in parse_qs(urlsplit(link).query).items()}
    if query.get("filename"):
        keys.append("cnki:" + query["filename"][0].casefold())
    title, journal, year = (scalar(fields, k) for k in ("T1", "JF", "YR"))
    # Fallback is conservative: distinct years or author lists remain distinct.
    if title and journal:
        keys.append("bib:" + digest("|".join([normalized(title), normalized(journal), year, normalized(scalar(fields, "A1"))])))
    if not keys:
        keys.append("raw:" + digest(json.dumps(fields, ensure_ascii=False, sort_keys=True)))
    return keys


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY, fields TEXT NOT NULL, evidence TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS identities(key TEXT PRIMARY KEY, record_id INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, definition TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', stats TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS items(task_id TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL, error TEXT, PRIMARY KEY(task_id,url));
        """)

    def add_tasks(self, tasks):
        for task in tasks:
            self.db.execute("INSERT OR IGNORE INTO tasks(task_id,definition) VALUES (?,?)", (task["task_id"], json.dumps(task, ensure_ascii=False)))
        self.db.commit()

    def add(self, fields, evidence, prefer_official=False):
        fields = {k: [clean(v) for v in values if clean(v)] for k, values in fields.items() if k != "SR"}
        keys = identifiers(fields)
        ids = {row[0] for key in keys for row in self.db.execute("SELECT record_id FROM identities WHERE key=?", (key,))}
        if len(ids) > 1:
            # Do not silently collapse incompatible identifiers.
            raise ValueError("去重标识冲突，需要人工核对 DOI/题名/作者")
        if ids:
            rid = ids.pop()
            row = self.db.execute("SELECT fields,evidence FROM records WHERE id=?", (rid,)).fetchone()
            old, events = json.loads(row["fields"]), json.loads(row["evidence"])
            for tag, values in fields.items():
                if not old.get(tag):
                    old[tag] = values
                elif values and old[tag] != values:
                    evidence.setdefault("field_conflicts", {})[tag] = values
                    if prefer_official:
                        evidence.setdefault("replaced_fields", {})[tag] = old[tag]
                        old[tag] = values
            events.append(evidence)
            self.db.execute("UPDATE records SET fields=?,evidence=? WHERE id=?", (json.dumps(old, ensure_ascii=False), json.dumps(events, ensure_ascii=False), rid))
        else:
            cur = self.db.execute("INSERT INTO records(fields,evidence) VALUES (?,?)", (json.dumps(fields, ensure_ascii=False), json.dumps([evidence], ensure_ascii=False)))
            rid = cur.lastrowid
        for key in keys:
            self.db.execute("INSERT OR IGNORE INTO identities VALUES (?,?)", (key, rid))
        self.db.commit()
        return rid

    def item(self, task_id, url, status, error=""):
        self.db.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?)", (task_id, url, status, error))
        self.db.commit()

    def status(self, task_id, status, stats):
        self.db.execute("UPDATE tasks SET status=?,stats=? WHERE task_id=?", (status, json.dumps(stats, ensure_ascii=False), task_id))
        self.db.commit()


def classify(fields, cfg, include_undated=False, strict_keywords=False):
    reasons = []
    if scalar(fields, "RT").casefold() != "journal article":
        return "excluded", ["not_journal_article"]
    if not scalar(fields, "T1"):
        return "review", ["missing_title"]
    accepted_names = {normalized(n) for j in cfg["journals"] for n in [j["name"]] + j.get("aliases", [])}
    if not scalar(fields, "JF"):
        return "review", ["missing_journal"]
    if normalized(scalar(fields, "JF")) not in accepted_names:
        return "excluded", ["journal_out_of_scope"]
    year = scalar(fields, "YR")
    if not re.fullmatch(r"\d{4}", year):
        # Explicit date metadata is acceptable; a year inside DOI is not evidence.
        fd = scalar(fields, "FD")
        found = re.match(r"^(\d{4})(?:[-/.年]|$)", fd)
        year = found[1] if found else ""
    if not year:
        reasons.append("missing_year")
        if not include_undated:
            return "review", reasons
    elif not cfg["start_year"] <= int(year) <= cfg["end_year"]:
        return "excluded", ["year_out_of_scope"]
    visible_text = " ".join(scalar(fields, tag) for tag in ("T1", "K1", "AB"))
    if not all(word in visible_text for word in cfg["keywords"]):
        reasons.append("keywords_not_all_visible")
        if strict_keywords:
            return "excluded", reasons
    reasons.extend("missing_" + tag for tag in ("A1", "AD", "AB", "K1", "SN", "CN", "DO", "OP") if not scalar(fields, tag))
    return "accepted", reasons


def refworks_text(records):
    blocks = []
    for fields in records:
        fields = normalize_fields(fields)
        # SR is a source-type marker, not the position of a record in this file.
        lines = ["RT " + (scalar(fields, "RT") or "Journal Article"), "SR 1"]
        ordered = [tag for tag in STANDARD_TAGS if tag not in ("RT", "SR")]
        ordered += sorted(tag for tag in fields if tag not in TAGS)
        for tag in ordered:
            values = [clean(v) for v in fields.get(tag, []) if clean(v)]
            if not values:
                continue
            value = ";".join(values) if tag in ("A1", "AD", "K1") else " ".join(values)
            separator = "  " if tag == "A1" else " "
            lines.append(f"{tag}{separator}{value}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def export_all(store, cfg, out, include_undated=False, strict_keywords=False, require_complete=None):
    out.mkdir(parents=True, exist_ok=True)
    accepted, review, audit, json_records, all_fields = [], [], [], [], []
    require_complete = cfg.get("standard_export", {}).get("require_complete", True) if require_complete is None else require_complete
    for row in store.db.execute("SELECT * FROM records ORDER BY id"):
        fields = normalize_fields(json.loads(row["fields"]))
        status, reasons = classify(fields, cfg, include_undated, strict_keywords)
        missing = missing_standard_fields(fields, cfg)
        reasons = list(dict.fromkeys(reasons + ["missing_" + tag for tag in missing]))
        if require_complete and status == "accepted" and missing:
            status = "review"
        all_fields.append(fields)
        metadata = {"record_id": row["id"], "status": status, "reasons": reasons,
                    "fields": fields, "evidence": json.loads(row["evidence"])}
        json_records.append(metadata)
        if status == "accepted":
            accepted.append(fields)
        elif status == "review":
            review.append(fields)
        audit.append({"record_id": row["id"], "status": status, "reasons": ";".join(reasons),
                      **{tag: scalar(fields, tag) for tag in TAGS if tag not in ("RT", "SR")}})
    for name, records in [("cnki_refworks.txt", accepted), ("needs_review_refworks.txt", review), ("all_refworks.txt", all_fields)]:
        (out / name).write_text(refworks_text(records), encoding="utf-8")
    columns = ["record_id", "status", "reasons"] + [t for t in TAGS if t not in ("RT", "SR")]
    with (out / "metadata_audit.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(audit)
    (out / "records.json").write_text(json.dumps(json_records, ensure_ascii=False, indent=2), encoding="utf-8")
    tasks = [{**json.loads(r["definition"]), "status": r["status"], "stats": json.loads(r["stats"])} for r in store.db.execute("SELECT * FROM tasks")]
    (out / "task_log.json").write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"exported_at": NOW(), "accepted": len(accepted), "review": len(review),
               "excluded": len(audit)-len(accepted)-len(review), "tasks": len(tasks),
               "config": cfg, "include_undated": include_undated, "strict_keywords": strict_keywords,
               "require_complete": require_complete, "total_records": len(all_fields)}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"导出：{len(accepted)} 条；待核对：{len(review)} 条；排除：{summary['excluded']} 条。目录：{out}")


DETAIL_JS = r"""(cfg) => {
  const cleanedText = elm => {const node=elm.cloneNode(true); node.querySelectorAll('script,style,sup,button,.more,.reset').forEach(n=>n.remove()); return node.textContent.trim();};
  const texts = selector => selector ? [...document.querySelectorAll(selector)].map(cleanedText).filter(Boolean) : [];
  const first = selector => texts(selector)[0] || '';
  const meta = {};
  for (const e of document.querySelectorAll('meta[name],meta[property]')) {
    const k = (e.getAttribute('name') || e.getAttribute('property')).toLowerCase();
    (meta[k] ||= []).push(e.getAttribute('content') || '');
  }
  const labels = {};
  for (const e of document.querySelectorAll(cfg.labels)) {
    const label = e.innerText.replace(/[：:]\s*$/, '').trim();
    const parent = e.parentElement.cloneNode(true);
    parent.querySelectorAll('[class^="rowtit"],script,style,button,.more,.reset').forEach(n => n.remove());
    labels[label] = parent.textContent.trim();
  }
  const names = cfg.authors ? [...document.querySelectorAll(cfg.authors)].map(e => {
    const n = e.cloneNode(true); n.querySelectorAll('sup').forEach(s => s.remove()); return n.textContent.trim();
  }).filter(Boolean) : [];
  const institutions=texts(cfg.institutions || '.wx-tit .orgn');
  const inputValue = selector => document.querySelector(selector)?.value || '';
  return {meta, labels, title:first(cfg.title), authors:names, institutions,
    abstract:first(cfg.abstract), keywords:texts(cfg.keywords), publication:first(cfg.publication),
    online_date:first(cfg.online_date), export_controls:{url:inputValue('#export-url'),id:inputValue('#export-id')}};
}"""


def detail_fields(data, url):
    fields = {"RT": ["Journal Article"], "DS": ["CNKI"], "LK": [url]}
    meta, labels = data.get("meta", {}), data.get("labels", {})

    def put(tag, value):
        values = value if isinstance(value, list) else [value]
        values = [clean(v) for v in values if clean(v)]
        if values:
            fields[tag] = values

    mapping = {"T1": "citation_title", "A1": "citation_author", "AD": "citation_author_institution",
               "JF": "citation_journal_title", "YR": "citation_year", "FD": "citation_publication_date",
               "vo": "citation_volume", "IS": "citation_issue", "SN": "citation_issn",
               "DO": "citation_doi", "LA": "citation_language", "AB": "citation_abstract", "CN": "citation_cn"}
    for tag, name in mapping.items():
        put(tag, meta.get(name, []))
    for tag, key in {"T1": "title", "A1": "authors", "AD": "institutions", "AB": "abstract", "K1": "keywords", "FD": "online_date"}.items():
        if not fields.get(tag):
            put(tag, data.get(key, ""))
    for tag, candidates in {"DO": ["DOI"], "AD": ["作者单位", "机构"], "SN": ["ISSN", "国际标准刊号"], "CN": ["CN", "国内统一刊号", "国内刊号"], "LA": ["语种", "语言"], "OP": ["页码"], "K1": ["关键词"], "AB": ["摘要"]}.items():
        if not fields.get(tag):
            put(tag, next((labels.get(k) for k in candidates if labels.get(k)), ""))
    if not fields.get("K1"):
        put("K1", meta.get("citation_keywords", meta.get("keywords", [])))
    first = clean((meta.get("citation_firstpage") or [""])[0])
    last = clean((meta.get("citation_lastpage") or [""])[0])
    if first and not fields.get("OP"):
        put("OP", first + ("-" + last if last and last != first else ""))
    # Parse only an explicit journal/year boundary, never infer year from DOI.
    pub = clean(data.get("publication", ""))
    match = re.match(r"^(.+?)\s*[.．]\s*((?:19|20)\d{2})(?:\s*,\s*(\d*))?\s*(?:\((\d+)\))?(?:\s*[:：]\s*([\d,\s-]+))?", pub)
    if match:
        for tag, value in zip(("JF", "YR", "vo", "IS", "OP"), match.groups()):
            if not fields.get(tag):
                put(tag, value)
    elif not fields.get("JF"):
        # Online-first headers may contain a journal followed by a period, with no year.
        put("JF", re.split(r"[.．]\s", pub, maxsplit=1)[0].rstrip(".．"))
    if not fields.get("YR"):
        date = scalar(fields, "FD")
        match = re.match(r"^((?:19|20)\d{2})(?:[-/.年]|$)", date)
        if match:
            put("YR", match[1])
    return normalize_fields(fields)


class ReferenceHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.ignored += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.ignored = max(0, self.ignored - 1)
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def reference_texts(payload):
    """Read tagged text only; other response formats are not silently re-labelled."""
    if isinstance(payload, str):
        if re.search(r"<\s*(?:br|div|p|pre)\b", payload, re.I):
            parser = ReferenceHTML()
            parser.feed(payload)
            payload = "".join(parser.parts)
        if re.search(r"^\s*RT\s+Journal Article\s*$", payload, re.M):
            yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from reference_texts(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from reference_texts(value)


def choose_reference(payload, expected):
    matches = []
    for native in reference_texts(payload):
        for row in parse_refworks(native):
            if same_article(expected, row):
                matches.append((normalize_fields(row), native))
    if not matches:
        raise ValueError("导出响应中未找到与当前 DOI/题名/期刊/作者一致的 RefWorks 记录")
    signatures = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row, _ in matches}
    if len(signatures) > 1:
        raise ValueError("导出响应有多条互相冲突的同文题录，未自动采纳")
    return matches[0]


def fetch_official_reference(page, raw, expected, cfg):
    """Use the export endpoint and record ID actually advertised by this page.

    No guessed filename, replayed security token, private API discovery or
    browser credential extraction is involved. Unsupported responses stay pending.
    """
    controls = raw.get("export_controls", {})
    if not controls.get("url") or not controls.get("id"):
        raise ValueError("详情页没有提供 export-url/export-id，无法自动补全官方题录")
    target = urljoin(page.url, controls["url"])
    origin, host = urlsplit(page.url), urlsplit(target)
    if host.scheme not in ("https", "http") or not host.hostname:
        raise ValueError("导出地址不是有效 HTTP(S) 地址")
    if host.netloc != origin.netloc and not (host.scheme == "https" and (host.hostname == "cnki.net" or host.hostname.endswith(".cnki.net"))):
        raise ValueError("导出地址不属于当前站点或知网，未发送请求")
    settings = cfg.get("standard_export", {})
    response = page.request.post(target, form={"filename": controls["id"],
        "displaymode": settings.get("displaymode", "Refworks"),
        "uniplatform": settings.get("uniplatform", "NZKPT")},
        headers={"Referer": page.url}, timeout=cfg["timeout_ms"])
    if response.status != 200:
        raise ValueError(f"官方题录导出返回 HTTP {response.status}，需要人工检查访问状态")
    try:
        payload = response.json()
    except Exception:
        payload = response.text()
    fields, native = choose_reference(payload, expected)
    return fields, native, {"method": "official_refworks_api", "export_url": target}


def strip_reference_ui(text):
    lines, active, previous = [], False, ""
    for raw in text.splitlines():
        line = raw.strip()
        if re.fullmatch(r"RT\s+Journal Article", line):
            active, previous = True, "RT"
            lines.extend(([""] if lines else []) + [line])
        elif active:
            if line in ("复制到剪贴板", "打印", "导出", "预览", "关闭", "xls", "doc"):
                active = False
                continue
            match = TAG_RE.match(line)
            if match and (match[1].upper() in TAG_CANON or match[1].isupper()):
                previous = match[1].upper()
                lines.append(line)
            elif previous == "AB" and line:
                lines.append(line)
    return "\n".join(lines)


def fetch_reference_via_ui(page, expected, cfg):
    """Per-article citation fallback; this does not select a search-result batch."""
    settings, before, original_url = cfg.get("standard_export", {}), list(page.context.pages), page.url

    def relevant_pages():
        return [p for p in page.context.pages if p not in before and not p.is_closed()] + [page]

    def one_visible(locators):
        visible = [loc for loc in locators if loc.is_visible()]
        if len(visible) > 1:
            raise ValueError("引用/Refworks 控件不唯一，需要配置 standard_export.reference_button")
        return visible[0] if visible else None

    selector = settings.get("reference_button")
    trigger = one_visible(page.locator(selector).all()) if selector else None
    if not trigger and not selector:
        for text in ("引用", "导出/参考文献", "导出文献"):
            trigger = one_visible(page.get_by_text(text, exact=True).all())
            if not trigger:
                trigger = one_visible(page.locator(f"[title='{text}'],[aria-label='{text}']").all())
            if trigger:
                break
    if not trigger:
        raise ValueError("未找到当前文章的引用按钮，需要配置 standard_export.reference_button 或导入官方 TXT")
    try:
        trigger.click()
        deadline, selected_format = time.monotonic() + cfg["timeout_ms"] / 1000, False
        while time.monotonic() < deadline:
            for view in relevant_pages():
                for frame in view.frames:
                    try:
                        if not selected_format:
                            option = one_visible(frame.get_by_text(re.compile(r"^Refworks$", re.I), exact=True).all())
                            if option:
                                option.click()
                                selected_format = True
                        texts = frame.evaluate("""() => {const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';return [...document.querySelectorAll('textarea,pre')].filter(visible).map(e=>e.value??e.innerText).concat(document.body?.innerText||'')}""")
                    except Exception as exc:
                        if any(message in str(exc).casefold() for message in ("execution context was destroyed", "frame was detached", "has been closed")):
                            continue
                        raise
                    for text in texts:
                        if "RT Journal Article" not in text:
                            continue
                        native = strip_reference_ui(text)
                        try:
                            fields, _ = choose_reference(native, expected)
                            return fields, native, {"method": "official_refworks_ui_article", "export_url": frame.url}
                        except ValueError:
                            continue
            page.wait_for_timeout(150)
        raise ValueError("引用页面未出现与当前文章一致的 RefWorks 记录")
    finally:
        opened = [p for p in page.context.pages if p not in before and not p.is_closed()]
        if opened:
            for extra in opened:
                extra.close()
        elif not page.is_closed():
            close_selector = settings.get("reference_close")
            close = one_visible(page.locator(close_selector).all()) if close_selector else None
            if not close:
                close = one_visible(page.get_by_text("关闭", exact=True).all())
            if close:
                close.click()
            else:
                page.keyboard.press("Escape")
            if page.url != original_url:
                page.go_back(wait_until="domcontentloaded")


def enrich_detail(page, raw, fields, cfg, references, out):
    cached = references.find(fields)
    if cached:
        official, source = cached
        return supplement_fields(fields, official), {**source, "reference_status": "matched"}
    if not cfg.get("standard_export", {}).get("fetch_official", True):
        return fields, {"reference_status": "disabled"}
    try:
        time.sleep(cfg["delay_seconds"])
        try:
            official, native, source = fetch_official_reference(page, raw, fields, cfg)
        except Exception as api_error:
            if not cfg.get("standard_export", {}).get("ui_fallback", True):
                raise
            try:
                official, native, source = fetch_reference_via_ui(page, fields, cfg)
            except Exception as ui_error:
                raise ValueError(f"接口补全：{api_error}；引用页面补全：{ui_error}") from ui_error
        path = out / "official_refworks" / f"{digest(doi_key(official) or scalar(official, 'T1'))}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(native, encoding="utf-8")
        return supplement_fields(fields, official), {**source, "reference_status": "matched", "reference_file": str(path)}
    except Exception as exc:
        return fields, {"reference_status": "pending", "reference_error": str(exc)}


def repair_saved(store, cfg, references):
    counts = {"records": 0, "normalized": 0, "reference_matched": 0, "incomplete": 0, "conflicts": []}
    for record in store.db.execute("SELECT * FROM records ORDER BY id").fetchall():
        old = json.loads(record["fields"])
        fields = normalize_fields(old)
        evidence = {"method": "format_repair", "repaired_at": NOW()}
        try:
            match = references.find(fields)
            if match:
                official, source = match
                fields = supplement_fields(fields, official)
                evidence.update(source)
                evidence["reference_status"] = "matched"
                counts["reference_matched"] += 1
            for key in identifiers(fields):
                owner = store.db.execute("SELECT record_id FROM identities WHERE key=?", (key,)).fetchone()
                if owner and owner[0] != record["id"]:
                    raise ValueError("补全后标识与其他记录冲突，需要人工核对")
            if fields != old:
                evidence["replaced_fields"] = {tag: values for tag, values in old.items() if fields.get(tag) != values}
                events = json.loads(record["evidence"]) + [evidence]
                store.db.execute("UPDATE records SET fields=?,evidence=? WHERE id=?", (json.dumps(fields, ensure_ascii=False), json.dumps(events, ensure_ascii=False), record["id"]))
                for key in identifiers(fields):
                    store.db.execute("INSERT OR IGNORE INTO identities VALUES (?,?)", (key, record["id"]))
                counts["normalized"] += 1
            counts["incomplete"] += bool(missing_standard_fields(fields, cfg))
        except ValueError as exc:
            counts["conflicts"].append({"record_id": record["id"], "error": str(exc)})
        counts["records"] += 1
    store.db.commit()
    return counts


def copy_database_for_repair(source_directory, destination_directory):
    source, destination = Path(source_directory).resolve(), Path(destination_directory).resolve()
    if source == destination:
        raise ValueError("repair 必须输出到新目录，不能覆盖原始采集数据")
    source_db, destination_db = source / "progress.sqlite3", destination / "progress.sqlite3"
    if not source_db.is_file():
        raise ValueError(f"没有找到原采集数据库：{source_db}")
    if destination_db.exists():
        raise ValueError("目标目录已有数据库，请指定另一个 --out 新目录")
    destination.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination_db)) as dst:
            src.backup(dst)


def playwright_api():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as exc:
        raise SystemExit("浏览器模式需要：python -m pip install -r requirements.txt；python -m playwright install chromium") from exc


def gate(page):
    while True:
        url = page.url.casefold()
        body = page.locator("body").inner_text(timeout=10000)[:5000]
        if "/verify/" not in url and not any(x in body for x in ("请完成安全验证", "向右滑动完成验证", "访问过于频繁")):
            return
        input("当前页面需要验证/等待，请在浏览器中人工处理，处理完成后按回车（Ctrl+C 退出）：")


def safe_visit(page, url, delay, timeout):
    time.sleep(delay)
    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if response and response.status in (401, 403, 429):
        input(f"页面返回 HTTP {response.status}，请检查访问状态并在浏览器处理后回车：")
    elif response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}")
    gate(page)


def auto_search(page, task, cfg):
    steps = cfg["ui"].get("auto_search_steps", [])
    if not steps:
        raise ValueError("auto_search_steps 为空；请先 inspect 并按文档校准，或使用默认人工检索模式")
    page.goto(cfg["entry_url"], wait_until="domcontentloaded")
    gate(page)
    values = {**task, "start_date": f"{task['start_year']}-01-01", "end_date": f"{task['end_year']}-12-31"}
    for step in steps:
        loc = page.locator(step["selector"])
        action = step["action"]
        value = str(step.get("value", "")).format_map(values)
        if action == "fill":
            loc.fill(value)
        elif action == "select":
            loc.select_option(value)
        elif action == "click":
            loc.click()
        elif action == "check":
            loc.check()
        elif action == "uncheck":
            loc.uncheck()
        else:
            raise ValueError(f"不支持的 UI 步骤：{action}")
    gate(page)


def crawl_task(context, page, task, cfg, store, out, max_pages, references=None, refresh_existing=False):
    tid = task["task_id"]
    ui = cfg["ui"]
    detail = context.new_page()
    references = references if references is not None else reference_index(cfg)
    visited = set()
    stats = {"pages_this_run": 0, "discovered_unique_this_run": 0, "parsed_this_run": 0,
             "filters_confirmed_by_user": True, "started_at": NOW()}
    store.status(tid, "running", stats)
    try:
        for _ in range(max_pages):
            gate(page)
            page.locator(ui["result_rows"]).first.wait_for(timeout=cfg["timeout_ms"])
            links = page.locator(ui["result_rows"]).evaluate_all(
                "(rows, selector) => rows.map(r => {const a=r.querySelector(selector); return a ? {url:a.href,title:a.innerText} : null;}).filter(Boolean)", ui["title_link"])
            links = [x for x in links if x["url"].startswith(("https://", "http://"))]
            if not links:
                raise RuntimeError("结果行存在但没有详情链接，请检查 title_link")
            signature = digest("|".join(x["url"] for x in links))
            if signature in visited:
                raise RuntimeError("翻页后链接没有变化；已停止，避免将重复页当作完整结果")
            visited.add(signature)
            stats["pages_this_run"] += 1
            stats["discovered_unique_this_run"] = len({*stats.get("urls", []), *(x["url"] for x in links)})
            stats["urls"] = list({*stats.get("urls", []), *(x["url"] for x in links)})
            for link in links:
                url = link["url"]
                done = store.db.execute("SELECT status FROM items WHERE task_id=? AND url=?", (tid, url)).fetchone()
                if done and done[0] == "done" and not refresh_existing:
                    continue
                store.item(tid, url, "pending")
                try:
                    safe_visit(detail, url, cfg["delay_seconds"], cfg["timeout_ms"])
                    title_selector = ui["detail"].get("title")
                    if title_selector:
                        detail.locator(title_selector).first.wait_for(timeout=cfg["timeout_ms"])
                    raw = detail.evaluate(DETAIL_JS, ui["detail"])
                    fields = detail_fields(raw, detail.url)
                    if not scalar(fields, "T1"):
                        raise ValueError("详情页缺少题名，请检查详情页选择器")
                    fields, reference_evidence = enrich_detail(detail, raw, fields, cfg, references, out)
                    snapshot = out / "raw" / f"{digest(url)}.json"
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    snapshot.write_text(json.dumps({"url": url, "captured_at": NOW(), "metadata": raw}, ensure_ascii=False, indent=2), encoding="utf-8")
                    store.add(fields, {"method": "browser_detail", "task_id": tid, "url": url, "captured_at": NOW(),
                                      "raw_file": str(snapshot), **reference_evidence}, prefer_official=True)
                    missing = missing_standard_fields(fields, cfg)
                    if missing:
                        print(f"待补字段 {','.join(missing)}：{link['title']}；{reference_evidence.get('reference_error', '')}")
                    store.item(tid, url, "done")
                    stats["parsed_this_run"] += 1
                except Exception as exc:
                    store.item(tid, url, "failed", str(exc)[:1000])
                    print(f"失败，已记录：{link['title']}：{exc}")
            store.status(tid, "running", stats)
            next_loc = page.locator(ui["next_page"]) if ui.get("next_page") else None
            visible = [loc for loc in next_loc.all() if loc.is_visible()] if next_loc else []
            usable = [loc for loc in visible if loc.is_enabled() and loc.get_attribute("aria-disabled") != "true" and not re.search(r"disabled|disable", loc.get_attribute("class") or "", re.I)]
            if len(usable) > 1:
                raise RuntimeError("匹配到多个下一页控件，请校准 next_page 使其唯一")
            if usable:
                old = [x["url"] for x in links]
                time.sleep(cfg["delay_seconds"])
                usable[0].click()
                gate(page)
                page.wait_for_function("([rowSelector,linkSelector,old]) => {const now=[...document.querySelectorAll(rowSelector)].map(r=>r.querySelector(linkSelector)?.href).filter(Boolean); return now.length && JSON.stringify(now)!==JSON.stringify(old);}", arg=[ui["result_rows"], ui["title_link"], old], timeout=cfg["timeout_ms"])
                continue
            answer = input("未找到可用的下一页。确认已到末页输入 end；手动翻页后回车；暂停输入 quit：").strip().lower()
            if answer == "quit":
                store.status(tid, "paused", stats)
                return
            if answer == "end":
                # Completed means a human confirmed the page boundary, not total-count proof.
                unresolved = store.db.execute("SELECT COUNT(*) FROM items WHERE task_id=? AND status!='done'", (tid,)).fetchone()[0]
                stats["unresolved_items"] = unresolved
                stats["end_confirmed_by_user"] = True
                store.status(tid, "needs_retry" if unresolved else "collected_end_confirmed", stats)
                return
        store.status(tid, "page_limit_reached", stats)
    except Exception as exc:
        stats["error"] = str(exc)
        store.status(tid, "needs_retry", stats)
        print(f"任务中断，已保存进度：{exc}")
    finally:
        detail.close()


def browser_command(args, cfg, store, out):
    sync_playwright = playwright_api()
    tasks = make_tasks(cfg, args.split)
    if args.task_id:
        tasks = [task for task in tasks if task["task_id"] == args.task_id]
        if not tasks:
            raise ValueError("task-id 不存在；请确保 plan/crawl 的 split 参数相同")
    store.add_tasks(tasks)
    references = reference_index(cfg)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(Path(args.profile).resolve()), headless=False)
        context.set_default_timeout(cfg["timeout_ms"])
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(cfg["entry_url"], wait_until="domcontentloaded")
            input("浏览器已打开。请人工登录/完成验证，准备好后按回车：")
            gate(page)
            if args.command == "inspect":
                input("请在浏览器进入要检查的检索/结果/详情页面，按回车输出 DOM 控件信息：")
                data = page.evaluate("""() => [...document.querySelectorAll('input,select,textarea,button,a')].map(e=>({tag:e.tagName,id:e.id,name:e.getAttribute('name'),type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder'),text:e.innerText?.trim().slice(0,100),options:e.tagName==='SELECT'?[...e.options].map(o=>({value:o.value,text:o.text})):undefined})).filter(e=>e.id||e.name||e.text)""")
                out.mkdir(parents=True, exist_ok=True)
                (out / "ui_inspection.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"已写入 {out / 'ui_inspection.json'}；未读取输入框值或 Cookie。")
                return
            for task in tasks:
                row = store.db.execute("SELECT status FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
                if not args.task_id and not args.refresh_existing and row[0] in ("collected_end_confirmed", "empty_confirmed"):
                    continue
                print(f"\n任务 {task['task_id']}：{task['journal']}，{task['start_year']}—{task['end_year']}\n主题 AND：{', '.join(task['keywords'])}\n逻辑检索配方：{task['query']}")
                if args.auto_search:
                    auto_search(page, task, cfg)
                answer = input("请确认学术期刊、两个主题 AND、精确来源、年份及第一页：回车开始；无结果输入 empty；跳过 skip；退出 quit：").strip().lower()
                if answer == "quit":
                    break
                if answer == "skip":
                    continue
                if answer == "empty":
                    store.status(task["task_id"], "empty_confirmed", {"confirmed_at": NOW()})
                    continue
                crawl_task(context, page, task, cfg, store, out, args.max_pages, references, args.refresh_existing)
                export_all(store, cfg, out, args.include_undated, args.strict_local_keywords)
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "crawl", "inspect", "import", "export", "repair"])
    parser.add_argument("--config", default=str(BASE / "config.json"))
    parser.add_argument("--out", help="输出目录；repair 默认 output_v1_fixed，其他命令默认 output")
    parser.add_argument("--from-out", default=str(BASE / "output"), help="repair：原采集目录，仅读取")
    parser.add_argument("--reference-dir", help="逐篇匹配的官方/用户标准 RefWorks TXT 所在目录")
    parser.add_argument("--refresh-existing", action="store_true", help="重新访问已完成任务和链接以补全字段")
    parser.add_argument("--split", choices=["journal", "year"], default="journal")
    parser.add_argument("--input", nargs="+", help="import：RefWorks TXT 文件或目录；目录仅读取 *.txt")
    parser.add_argument("--profile", default=str(BASE / ".browser-profile"))
    parser.add_argument("--task-id")
    parser.add_argument("--auto-search", action="store_true")
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--include-undated", action="store_true", help="允许无明确年份的记录进入主输出（带审计标记）")
    parser.add_argument("--strict-local-keywords", action="store_true", help="另行要求本地题名/摘要/关键词可见文字包含所有词；不同于数据库 SU 检索")
    args = parser.parse_args()
    cfg = read_config(args.config)
    if args.reference_dir:
        cfg.setdefault("standard_export", {})["reference_dir"] = str(Path(args.reference_dir).resolve())
    if cfg["delay_seconds"] < 1 or args.max_pages < 1:
        parser.error("delay_seconds 至少为 1，max-pages 至少为 1")
    out = Path(args.out or BASE / ("output_v1_fixed" if args.command == "repair" else "output")).resolve()
    if args.command == "repair":
        copy_database_for_repair(args.from_out, out)
    store = Store(out / "progress.sqlite3")
    try:
        if args.command == "plan":
            tasks = make_tasks(cfg, args.split)
            store.add_tasks(tasks)
            with (out / "search_plan.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["task_id", "journal", "start_year", "end_year", "query"])
                writer.writeheader()
                writer.writerows({k: task[k] for k in writer.fieldnames} for task in tasks)
            print(f"已生成 {len(tasks)} 个任务：{out / 'search_plan.csv'}")
        elif args.command == "import":
            if not args.input:
                parser.error("import 需要 --input 文件/目录")
            inputs = set()
            for value in args.input:
                path = Path(value).resolve()
                inputs.update(path.glob("*.txt") if path.is_dir() else [path])
            if not inputs:
                parser.error("没有找到 TXT 文件")
            rejected = []
            for path in sorted(inputs):
                text, encoding = decode_text(path)
                records = parse_refworks(text)
                if not records:
                    raise ValueError(f"{path} 没有 RT 开头的记录；请确认是 RefWorks TXT")
                for index, fields in enumerate(records, 1):
                    try:
                        store.add(fields, {"method": "official_export_import", "source_file": str(path), "record_number": index, "encoding": encoding, "imported_at": NOW()}, prefer_official=True)
                    except ValueError as exc:
                        rejected.append({"source_file": str(path), "record_number": index, "error": str(exc), "fields": fields})
                print(f"读取 {path.name}：{len(records)} 条，{encoding}")
            if rejected:
                (out / f"import_conflicts_{int(time.time())}.json").write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{len(rejected)} 条标识冲突已单独保存，需要人工核对。")
        elif args.command in ("crawl", "inspect"):
            browser_command(args, cfg, store, out)
        elif args.command in ("repair", "export"):
            report = repair_saved(store, cfg, reference_index(cfg))
            (out / "repair_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"规范化 {report['normalized']} 条；逐篇标准匹配 {report['reference_matched']} 条；仍缺字段 {report['incomplete']} 条。")
        if args.command not in ("inspect", "plan"):
            export_all(store, cfg, out, args.include_undated, args.strict_local_keywords)
    except KeyboardInterrupt:
        export_all(store, cfg, out, args.include_undated, args.strict_local_keywords)
        print("已停止，数据库和已采集记录已保留。")
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
