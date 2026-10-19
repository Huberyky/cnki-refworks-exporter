"""Pure-data checks for official export; all titles/authors here are test fixtures."""
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cnki_crawler as core
import cnki_refworks_v2 as v2

SAMPLE = """RT Journal Article
SR 1
A1 测试作者甲;测试作者乙
AD 虚构测试机构;
T1 民族文化测试题录（虚构）
JF 民族研究
OP 1-12
K1 民族;文化
AB 软件测试首行。
软件测试第二行。
SN 0000-0000
CN 00-0000/C
LA 中文
DS CNKI
LK https://example.invalid/urlid/test.20261009
"""


class OfficialExportTests(unittest.TestCase):
    def test_remove_toolbar_without_polluting_link_or_abstract(self):
        text = "导出文献\nRefworks\n" + SAMPLE + "复制到剪贴板\n打印\n页面底部杂文"
        native, fields = v2.validate_batch(text, ["民族文化测试题录（虚构）"])
        self.assertNotIn("复制", native)
        self.assertNotIn("杂文", native)
        self.assertEqual(fields[0]["AD"], ["虚构测试机构;"])
        self.assertIn("第二行", fields[0]["AB"][0])
        self.assertEqual(fields[0]["SN"], ["0000-0000"])
        self.assertEqual(fields[0]["CN"], ["00-0000/C"])
        self.assertEqual(fields[0]["LA"], ["中文"])
        self.assertEqual(fields[0]["LK"], ["https://example.invalid/urlid/test.20261009"])

    def test_count_and_title_checks_reject_stale_or_truncated_exports(self):
        with self.assertRaises(v2.ExportError):
            v2.validate_batch(SAMPLE, ["民族文化测试题录（虚构）", "另一篇"])
        with self.assertRaises(v2.ExportError):
            v2.validate_batch(SAMPLE, ["上一页同样数量但题名不同"])
        with self.assertRaises(v2.ExportError):
            v2.validate_batch(SAMPLE, ["民族文化测试题录（虚构）"] * 2)

    def test_exact_multiplicity_and_reordered_records(self):
        second = SAMPLE.replace("民族文化测试题录（虚构）", "测试二")
        self.assertEqual(len(v2.validate_batch(second + "\n" + SAMPLE, ["民族文化测试题录（虚构）", "测试二"])[1]), 2)

    def test_missing_year_preserved_in_complete_aggregate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            out = Path(temporary)
            store = core.Store(out / "state.sqlite3")
            try:
                v2.initialize(store)
                cfg = core.read_config(core.BASE / "config.json")
                fields = core.parse_refworks(SAMPLE)[0]
                store.add(fields, {"method": "official_refworks_ui"}, prefer_official=True)
                v2.export_outputs(store, cfg, out)
                all_fields = core.parse_refworks((out / "all_refworks.txt").read_text(encoding="utf-8"))
                self.assertEqual(len(all_fields), 1)
                self.assertNotIn("YR", all_fields[0])
                self.assertEqual((out / "cnki_refworks.txt").read_text(encoding="utf-8"), "")
                self.assertEqual(len(core.parse_refworks((out / "needs_review_refworks.txt").read_text(encoding="utf-8"))), 1)
            finally:
                store.db.close()

    def test_official_fields_replace_old_scraped_link_with_audit_trail(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temporary:
            store = core.Store(Path(temporary) / "state.sqlite3")
            try:
                original = core.parse_refworks(SAMPLE)[0]
                original["LK"] = ["https://example.invalid/kcms2?v=temporary"]
                original["AB"] = ["详情页不完整摘要"]
                rid = store.add(original, {"method": "browser_detail"})
                store.add(core.parse_refworks(SAMPLE)[0], {"method": "official_refworks_ui"}, prefer_official=True)
                row = store.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
                self.assertEqual(json.loads(row["fields"])["LK"], ["https://example.invalid/urlid/test.20261009"])
                self.assertIn("LK", json.loads(row["evidence"])[-1]["replaced_fields"])
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
