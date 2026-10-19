"""CNKI RefWorks Exporter: batch official citation export through the CNKI UI.

Run: python cnki_refworks_exporter.py crawl
Shared bibliography parsing and storage utilities live in cnki_crawler.py.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import time

import cnki_crawler as core

BASE = Path(__file__).resolve().parent
EXPORT_DEFAULTS = {
    "batch_size": 50,
    "manual_fallback": True,
    "row_checkbox": "input[type='checkbox']",
    "select_all": ".result-table-list > thead input[type='checkbox']",
    "clear_selection": "",
    "export_menu": "",
    "export_submenu": "",
    "refworks_option": "",
    "content_selector": "",
    "close_export": "",
}
STOP_TEXT = re.compile(r"^(?:复制(?:到|至)?剪[贴粘]板|打印|导出|预览|返回|关闭|xls|doc|文献导出格式|共\s*\d+\s*条(?:记录)?)[ \t]*$", re.I)
CONTENT_JS = r"""(selector) => {
  const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
  const nodes = selector ? [...document.querySelectorAll(selector)] : [...document.querySelectorAll('textarea, pre, [contenteditable="true"]')];
  const values = nodes.filter(visible).map(e => ({kind:e.tagName, text:e.value ?? e.innerText ?? ''}));
  if (!selector && document.body) values.push({kind:'body',text:document.body.innerText});
  return values.filter(x=>/^\s*RT[^\S\r\n]+Journal[^\S\r\n]+Article\s*$/mi.test(x.text));
}"""


class ExportError(RuntimeError):
    pass


class UserStopped(ExportError):
    pass


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def extract_refworks(text):
    """Separate tagged records from export-page navigation without inventing fields.

    Preserve tag order and values. Only AB gets untagged continuation lines;
    toolbar/footer text must not become part of LK, DO, CN or other identifiers.
    """
    lines, active, previous = [], False, ""
    for raw in text.lstrip("\ufeff").replace("\r\n", "\n").splitlines():
        line = raw.strip()
        if re.fullmatch(r"RT[^\S\r\n]+Journal[^\S\r\n]+Article", line, re.I):
            if lines:
                lines.append("")
            lines.append("RT Journal Article")
            active, previous = True, "RT"
            continue
        if not active:
            continue
        if STOP_TEXT.fullmatch(line):
            active, previous = False, ""
            continue
        match = core.TAG_RE.match(line)
        if match and (match[1].upper() in core.TAG_CANON or match[1].isupper()):
            previous = core.TAG_CANON.get(match[1].upper(), match[1])
            lines.append(line)
        elif previous == "AB" and line:
            lines.append(line)
    result = "\n".join(lines).strip()
    return result + ("\n" if result else "")


def validate_batch(text, expected_titles):
    native = extract_refworks(text)
    fields = core.parse_refworks(native)
    expected = Counter(core.normalized(t) for t in expected_titles)
    actual = Counter(core.normalized(core.scalar(f, "T1")) for f in fields)
    if len(fields) != len(expected_titles) or actual != expected:
        missing = list((expected - actual).elements())
        extra = list((actual - expected).elements())
        raise ExportError(f"导出不完整或混入旧选择：应有 {len(expected_titles)} 条，实际 {len(fields)} 条；缺失题名 {missing[:3]}；额外题名 {extra[:3]}。请清除跨页已选，或减小 --batch-size。")
    if not fields or any(not core.scalar(f, "T1") or not core.scalar(f, "JF") for f in fields):
        raise ExportError("导出记录缺少 T1/JF，不能确认是完整的 RefWorks 题录")
    return native, fields


def initialize(store):
    store.db.executescript("""
    CREATE TABLE IF NOT EXISTS export_batches(
      batch_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, status TEXT NOT NULL,
      details TEXT NOT NULL, updated_at TEXT NOT NULL);
    """)
    store.db.commit()


def record_batch(store, batch_id, task_id, status, details):
    store.db.execute("INSERT OR REPLACE INTO export_batches VALUES (?,?,?,?,?)",
                     (batch_id, task_id, status, json.dumps(details, ensure_ascii=False), core.NOW()))
    store.db.commit()


def export_outputs(store, cfg, out, include_undated=False, strict_keywords=False):
    core.export_all(store, cfg, out, include_undated, strict_keywords, require_complete=False)
    fields = [json.loads(row[0]) for row in store.db.execute("SELECT fields FROM records ORDER BY id")]
    atomic_text(out / "all_refworks.txt", core.refworks_text(fields))
    batches = [{"batch_id": row["batch_id"], "task_id": row["task_id"], "status": row["status"],
                "updated_at": row["updated_at"], **json.loads(row["details"])}
               for row in store.db.execute("SELECT * FROM export_batches ORDER BY updated_at,batch_id")]
    atomic_text(out / "batch_log.json", json.dumps(batches, ensure_ascii=False, indent=2))
    print(f"完整汇总：{len(fields)} 条 → {out / 'all_refworks.txt'}（未按期刊/年份排除）")


def scopes(pages):
    for page in pages:
        if not page.is_closed():
            yield from page.frames


def visible_matches(pages, selector="", text=None):
    matches = []
    for frame in scopes(pages):
        try:
            locators = frame.locator(selector) if selector else frame.get_by_text(text, exact=True)
            for loc in locators.all():
                if loc.is_visible():
                    matches.append(loc)
        except Exception:
            continue
    return matches


def one_control(pages, selector="", text=None):
    matches = visible_matches(pages, selector, text)
    if len(matches) > 1:
        raise ExportError(f"控件不唯一：{selector or text}，请在 config_v2.json 的 export_ui 中指定唯一选择器")
    return matches[0] if matches else None


def wait_control(pages_fn, selector="", text=None, timeout_ms=800):
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        pages = pages_fn()
        control = one_control(pages, selector, text)
        if control or time.monotonic() >= deadline:
            return control
        pages[-1].wait_for_timeout(100)


def snapshot(page, cfg):
    ui = cfg["ui"]
    page.locator(ui["result_rows"]).first.wait_for(state="visible", timeout=cfg["timeout_ms"])
    rows = page.locator(ui["result_rows"])
    entries = rows.evaluate_all("""(rows, selector) => rows.map((r,index)=>{const a=r.querySelector(selector);return a?{index,title:a.innerText.trim(),url:a.href}:null;}).filter(Boolean)""", ui["title_link"])
    if not entries or any(not entry["title"] for entry in entries):
        raise ExportError("无法读取本页题名，请检查 result_rows/title_link")
    return entries


def set_rows(page, cfg, entries, selected_indices):
    selector = cfg["export_ui"]["row_checkbox"]
    rows = page.locator(cfg["ui"]["result_rows"])
    for entry in entries:
        loc = rows.nth(entry["index"]).locator(selector)
        if loc.count() != 1:
            raise ExportError(f"第 {entry['index']+1} 行复选框不唯一，请校准 export_ui.row_checkbox")
        wanted = entry["index"] in selected_indices
        if loc.is_checked() != wanted:
            loc.set_checked(wanted, timeout=cfg["timeout_ms"])


def select_batch(page, cfg, entries, chosen):
    exp = cfg["export_ui"]
    clear = one_control([page], exp["clear_selection"]) if exp["clear_selection"] else None
    if clear:
        clear.click()
    # Uncheck before the header action so indeterminate header states are harmless.
    set_rows(page, cfg, entries, set())
    if len(entries) == len(chosen) and exp["select_all"]:
        header = one_control([page], exp["select_all"])
        if header:
            header.check()
    set_rows(page, cfg, entries, {e["index"] for e in chosen})
    rows = page.locator(cfg["ui"]["result_rows"])
    selected = [e["title"] for e in entries if rows.nth(e["index"]).locator(exp["row_checkbox"]).is_checked()]
    if Counter(selected) != Counter(e["title"] for e in chosen):
        raise ExportError("本页实际勾选与预期不符，已停止当前批次")


def current_export_pages(context, page, before):
    return [p for p in context.pages if p not in before and not p.is_closed()] + [page]


def open_refworks(context, page, cfg, before):
    exp = cfg["export_ui"]
    menu = one_control([page], exp["export_menu"], "导出与分析")
    if not menu:
        raise ExportError("未找到“导出与分析”，请配置 export_ui.export_menu")
    menu.hover()
    submenu = wait_control(lambda: [page], exp["export_submenu"], "导出文献")
    if not submenu:
        menu.click()
        submenu = wait_control(lambda: [page], exp["export_submenu"], "导出文献")
    if submenu:
        submenu.hover()
    option = wait_control(lambda: current_export_pages(context, page, before), exp["refworks_option"], re.compile(r"^Refworks$", re.I))
    if not option and submenu:
        submenu.click()
        # Some versions open an export page before the format is selected.
        deadline = time.monotonic() + cfg["timeout_ms"] / 1000
        while time.monotonic() < deadline:
            option = one_control(current_export_pages(context, page, before), exp["refworks_option"], re.compile(r"^Refworks$", re.I))
            if option:
                break
            page.wait_for_timeout(150)
    if not option:
        raise ExportError("未找到 Refworks 格式菜单，请配置 export_ui.refworks_option")
    option.click()


def read_export(context, page, cfg, before, expected_titles):
    deadline = time.monotonic() + cfg["timeout_ms"] / 1000
    last_error, last_candidate = "未出现 RT Journal Article 内容", ""
    while time.monotonic() < deadline:
        for frame in scopes(current_export_pages(context, page, before)):
            try:
                candidates = frame.evaluate(CONTENT_JS, cfg["export_ui"]["content_selector"])
            except Exception:
                continue
            for candidate in candidates:
                last_candidate = candidate["text"]
                try:
                    native, fields = validate_batch(last_candidate, expected_titles)
                    return native, fields, {"export_url": frame.url, "read_from": candidate["kind"]}
                except ExportError as exc:
                    last_error = str(exc)
        page.wait_for_timeout(150)
    error = ExportError(last_error)
    error.candidate_text = last_candidate
    raise error


def restore_results(context, page, cfg, before, original_url, original_titles):
    new_pages = [p for p in context.pages if p not in before and not p.is_closed()]
    if new_pages:
        for extra in new_pages:
            extra.close()
    else:
        close = one_control([page], cfg["export_ui"]["close_export"], "关闭")
        if close:
            close.click()
        else:
            page.keyboard.press("Escape")
        if page.url != original_url:
            page.go_back(wait_until="domcontentloaded")
    actual = snapshot(page, cfg)
    if [core.normalized(e["title"]) for e in actual] != [core.normalized(t) for t in original_titles]:
        raise ExportError("导出后没有回到原结果页；已保存的批次仍有效，请手动返回再重跑")
    return actual


def capture_batch(context, page, cfg, expected_titles):
    before, original_url = list(context.pages), page.url
    original_titles = [e["title"] for e in snapshot(page, cfg)]
    try:
        open_refworks(context, page, cfg, before)
        result = read_export(context, page, cfg, before, expected_titles)
    except Exception as exc:
        if not cfg["export_ui"]["manual_fallback"]:
            raise
        print(f"自动打开/读取失败：{exc}")
        print("请在当前浏览器手动选择 导出与分析 → 导出文献 → Refworks，并停在完整导出内容页面。")
        answer = input("准备好后回车重读；输入 quit 保留进度退出：").strip().lower()
        if answer == "quit":
            raise UserStopped("用户暂停当前批次") from exc
        # No fallback to detail scraping: only official tagged text is accepted.
        result = read_export(context, page, cfg, before, expected_titles)
    return result, (before, original_url, original_titles)


def next_result_page(page, cfg, previous_titles):
    selector = cfg["ui"].get("next_page")
    matches = visible_matches([page], selector) if selector else []
    matches = [loc for loc in matches if loc.is_enabled() and loc.get_attribute("aria-disabled") != "true"
               and not re.search(r"disable", loc.get_attribute("class") or "", re.I)]
    if len(matches) > 1:
        raise ExportError("下一页控件不唯一，请校准 ui.next_page")
    if not matches:
        answer = input("未找到可用下一页：确认末页输入 end；手动翻页后回车；暂停输入 quit：").strip().lower()
        if answer == "end":
            return False
        if answer == "quit":
            raise UserStopped("用户暂停翻页")
    else:
        time.sleep(cfg["delay_seconds"])
        matches[0].click()
    core.gate(page)
    page.wait_for_function("""([rows,link,old])=>{const titles=[...document.querySelectorAll(rows)].map(r=>r.querySelector(link)?.innerText.trim()).filter(Boolean);return titles.length&&JSON.stringify(titles)!==JSON.stringify(old)}""",
                           arg=[cfg["ui"]["result_rows"], cfg["ui"]["title_link"], previous_titles], timeout=cfg["timeout_ms"])
    return True


def run_task(context, page, task, cfg, store, out, max_pages=1000):
    initialize(store)
    tid, seen = task["task_id"], set()
    stats = {"method": "official_refworks_ui", "started_at": core.NOW(), "pages_this_run": 0,
             "batches_this_run": 0, "records_this_run": 0, "filters_confirmed_by_user": True}
    store.status(tid, "running", stats)
    batch_id, details = None, {}
    try:
        for page_index in range(max_pages):
            core.gate(page)
            entries = snapshot(page, cfg)
            titles = [e["title"] for e in entries]
            signature = core.digest(json.dumps([core.normalized(t) for t in titles], ensure_ascii=False))
            if signature in seen:
                raise ExportError("同一页题名重复出现，已停止，未将其视为采集完成")
            seen.add(signature)
            stats["pages_this_run"] += 1
            size = cfg["export_ui"]["batch_size"]
            for start in range(0, len(entries), size):
                chosen = entries[start:start+size]
                expected = [e["title"] for e in chosen]
                batch_id = core.digest(tid + json.dumps([core.normalized(t) for t in expected], ensure_ascii=False))
                old = store.db.execute("SELECT status FROM export_batches WHERE batch_id=?", (batch_id,)).fetchone()
                if old and old[0] == "done":
                    print(f"跳过已验证批次 {batch_id}（{len(chosen)} 条）")
                    continue
                details = {"selected_count": len(chosen), "expected_titles": expected,
                           "search_url": page.url, "page_this_run": page_index+1}
                record_batch(store, batch_id, tid, "pending", details)
                select_batch(page, cfg, entries, chosen)
                time.sleep(cfg["delay_seconds"])
                (native, fields, source), restore = capture_batch(context, page, cfg, expected)
                raw_file = out / "raw_refworks" / f"{batch_id}.txt"
                atomic_text(raw_file, native)
                # Keep batch status pending until every validated record is committed.
                for index, fields_row in enumerate(fields, 1):
                    store.add(fields_row, {"method": "official_refworks_ui", "task_id": tid,
                        "batch_id": batch_id, "batch_record": index, "raw_file": str(raw_file),
                        "captured_at": core.NOW(), **source}, prefer_official=True)
                details.update({"raw_file": str(raw_file), "exported_count": len(fields), **source})
                record_batch(store, batch_id, tid, "done", details)
                stats["batches_this_run"] += 1
                stats["records_this_run"] += len(fields)
                print(f"本轮第 {page_index+1} 页：已核对并保存 {len(fields)} 条 RefWorks。")
                export_outputs(store, cfg, out)
                entries = restore_results(context, page, cfg, *restore)
                set_rows(page, cfg, entries, set())
                batch_id = None
                if start + size < len(entries):
                    time.sleep(cfg["delay_seconds"])
            if page_index + 1 >= max_pages:
                store.status(tid, "page_limit_reached", stats)
                return
            if not next_result_page(page, cfg, titles):
                stats["end_confirmed_by_user"] = True
                store.status(tid, "collected_end_confirmed", stats)
                return
            store.status(tid, "running", stats)
    except Exception as exc:
        if batch_id:
            row = store.db.execute("SELECT status FROM export_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row and row[0] != "done":
                details["error"] = str(exc)
                candidate = getattr(exc, "candidate_text", "")
                if candidate:
                    failed_file = out / "failed_batches" / f"{batch_id}_{int(time.time())}.txt"
                    atomic_text(failed_file, candidate)
                    details["failed_candidate_file"] = str(failed_file)
                record_batch(store, batch_id, tid, "failed", details)
        stats["error"] = str(exc)
        store.status(tid, "paused" if isinstance(exc, UserStopped) else "needs_retry", stats)
        print(f"当前任务停止，已保存进度：{exc}")
    finally:
        export_outputs(store, cfg, out)


def diagnose(page, cfg, out):
    dump = []
    for frame in page.frames:
        try:
            items = frame.evaluate("""() => [...document.querySelectorAll('a,button,input,textarea,select,[role=button],[role=checkbox],iframe')].map(e=>({tag:e.tagName,id:e.id,class:e.className,name:e.getAttribute('name'),type:e.getAttribute('type'),text:e.innerText?.trim().slice(0,100),title:e.getAttribute('title'),ariaLabel:e.getAttribute('aria-label')}))""")
            dump.append({"url": frame.url, "controls": items})
        except Exception:
            pass
    atomic_text(out / "ui_inspection_v2.json", json.dumps(dump, ensure_ascii=False, indent=2))
    page.screenshot(path=str(out / "ui_inspection_v2.png"), full_page=True)
    print(f"已写入 {out / 'ui_inspection_v2.json'} 和截图；未读取 Cookie 或输入框值。")


def browser_run(args, cfg, store, out):
    tasks = core.make_tasks(cfg, args.split)
    if args.task_id:
        tasks = [task for task in tasks if task["task_id"] == args.task_id]
        if not tasks:
            raise ExportError("task-id 不匹配，请检查 split/config 参数")
    store.add_tasks(tasks)
    with core.playwright_api()() as p:
        context = p.chromium.launch_persistent_context(str(Path(args.profile).resolve()), headless=False)
        context.set_default_timeout(cfg["timeout_ms"])
        try:
            page = next((p for p in context.pages if "kns.cnki.net" in p.url), context.pages[0] if context.pages else context.new_page())
            if page.url in ("about:blank", ""):
                page.goto(cfg["entry_url"], wait_until="domcontentloaded")
            input("请在打开的浏览器完成人工登录/验证（或沿用当前页面），回到终端按回车：")
            core.gate(page)
            if args.command == "inspect":
                input("请进入要核对的结果页或 RefWorks 导出页面，回车后生成控件诊断：")
                diagnose(page, cfg, out)
                return
            for task in tasks:
                status = store.db.execute("SELECT status FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()[0]
                if status in ("empty_confirmed", "collected_end_confirmed") and not args.task_id:
                    continue
                print(f"\n任务 {task['task_id']}：{task['journal']}；{task['start_year']}—{task['end_year']}；主题 {' AND '.join(task['keywords'])}")
                answer = input("请设置该任务检索并进入第一页，清除历史已选：回车开始；零结果 empty；跳过 skip；退出 quit：").strip().lower()
                if answer == "quit":
                    break
                if answer == "skip":
                    continue
                if answer == "empty":
                    store.status(task["task_id"], "empty_confirmed", {"confirmed_at": core.NOW()})
                    continue
                run_task(context, page, task, cfg, store, out, args.max_pages)
        finally:
            context.close()


def load_config(path, batch_size=None):
    cfg = core.read_config(path)
    cfg["export_ui"] = {**EXPORT_DEFAULTS, **cfg.get("export_ui", {})}
    if batch_size is not None:
        cfg["export_ui"]["batch_size"] = batch_size
    if cfg["export_ui"]["batch_size"] < 1 or cfg["delay_seconds"] < 1:
        raise ValueError("batch_size 和 delay_seconds 必须至少为 1")
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["crawl", "plan", "inspect", "export", "import"])
    parser.add_argument("--config", default=str(BASE / "config_v2.json"))
    parser.add_argument("--out", default=str(BASE / "output_v2"))
    parser.add_argument("--profile", default=str(BASE / ".browser-profile"))
    parser.add_argument("--split", choices=["journal", "year"], default="journal")
    parser.add_argument("--task-id")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--input", nargs="+")
    parser.add_argument("--include-undated", action="store_true")
    parser.add_argument("--strict-local-keywords", action="store_true")
    args = parser.parse_args()
    cfg, out = load_config(args.config, args.batch_size), Path(args.out).resolve()
    if args.max_pages < 1:
        parser.error("max-pages 必须至少为 1")
    out.mkdir(parents=True, exist_ok=True)
    store = core.Store(out / "progress.sqlite3")
    initialize(store)
    try:
        if args.command == "plan":
            tasks = core.make_tasks(cfg, args.split)
            store.add_tasks(tasks)
            import csv
            with (out / "search_plan.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["task_id", "journal", "start_year", "end_year", "query"])
                writer.writeheader()
                writer.writerows({k: t[k] for k in writer.fieldnames} for t in tasks)
            print(f"仅生成 {len(tasks)} 个检索任务，尚未采集文献：{out / 'search_plan.csv'}")
        elif args.command in ("crawl", "inspect"):
            browser_run(args, cfg, store, out)
        elif args.command == "import":
            if not args.input:
                parser.error("import 需要 --input RefWorks文件或目录")
            paths = set()
            for value in args.input:
                p = Path(value).resolve()
                paths.update(p.glob("*.txt") if p.is_dir() else [p])
            if not paths:
                parser.error("未找到 TXT 文件")
            for path in sorted(paths):
                text, encoding = core.decode_text(path)
                native = extract_refworks(text)
                fields = core.parse_refworks(native)
                if not fields:
                    raise ExportError(f"文件没有 RefWorks 期刊题录：{path}")
                file_id = core.digest(str(path) + native)
                raw = out / "raw_refworks" / f"import_{file_id}.txt"
                atomic_text(raw, native)
                for row in fields:
                    store.add(row, {"method": "official_export_import", "source_file": str(path),
                                   "raw_file": str(raw), "encoding": encoding, "imported_at": core.NOW()}, prefer_official=True)
                print(f"导入 {path.name}：{len(fields)} 条")
        if args.command not in ("plan", "inspect"):
            export_outputs(store, cfg, out, args.include_undated, args.strict_local_keywords)
    except KeyboardInterrupt:
        export_outputs(store, cfg, out, args.include_undated, args.strict_local_keywords)
        print("用户中断，已保存已完成批次。")
    except Exception as exc:
        export_outputs(store, cfg, out, args.include_undated, args.strict_local_keywords)
        parser.exit(1, f"错误：{exc}\n")
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
