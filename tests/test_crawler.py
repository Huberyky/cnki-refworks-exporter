"""Offline regression checks: no CNKI requests or real research records."""
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cnki_crawler as c


def article(**overrides):
    fields = {"RT": ["Journal Article"], "T1": ["民族与文化研究（测试用虚构题录）"],
              "JF": ["民族研究"], "YR": ["2026"], "A1": ["测试作者甲", "测试作者乙"],
              "AB": ["仅用于软件测试。"], "K1": ["民族;文化"]}
    fields.update(overrides)
    return fields


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = c.read_config(c.BASE / "config.json")

    def test_plan_scope(self):
        self.assertEqual(len(c.make_tasks(self.cfg)), 15)
        self.assertEqual(len(c.make_tasks(self.cfg, "year")), 735)
        self.assertIn("SU='民族' AND SU='文化'", c.make_tasks(self.cfg)[0]["query"])

    def test_multiline_repeated_tags_and_unknown_fields(self):
        rows = c.parse_refworks("\ufeffRT Journal Article\nA1 甲\nA1 乙\nT1 标题\nAB 第一行\n\nIn this paper we study culture.\n第二行\nZZ 保留字段\nRT Journal Article\nT1 二\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["A1"], ["甲", "乙"])
        self.assertIn("In this paper", rows[0]["AB"][0])
        self.assertEqual(rows[0]["ZZ"], ["保留字段"])

    def test_roundtrip(self):
        text = c.refworks_text([article(), article(T1=["第二条"] )])
        rows = c.parse_refworks(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(c.scalar(rows[0], "A1"), "测试作者甲;测试作者乙")
        self.assertEqual(rows[1]["SR"], ["1"])

    def test_scope_and_undated(self):
        self.assertEqual(c.classify(article(), self.cfg)[0], "accepted")
        self.assertEqual(c.classify(article(YR=["1977"]), self.cfg)[0], "excluded")
        self.assertEqual(c.classify(article(YR=["2027"]), self.cfg)[0], "excluded")
        self.assertEqual(c.classify(article(JF=["西北民族大学学报(哲学社会科学版)"]), self.cfg)[0], "excluded")
        row = article(YR=[], DO=["10.1234/test.20261010.006"])
        self.assertEqual(c.classify(row, self.cfg)[0], "review")
        self.assertEqual(c.classify(row, self.cfg, include_undated=True)[0], "accepted")

    def test_chinese_parentheses_and_date(self):
        row = article(JF=["中央民族大学学报(哲学社会科学版)"], YR=[], FD=["2026-10-10"])
        self.assertEqual(c.classify(row, self.cfg)[0], "accepted")

    def test_visible_keywords_are_not_database_subject_search(self):
        row = article(T1=["测试题名"], K1=[])
        self.assertEqual(c.classify(row, self.cfg)[0], "accepted")
        self.assertEqual(c.classify(row, self.cfg, strict_keywords=True)[0], "excluded")

    def test_dedup_preserves_conflicts_and_provenance(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            store = c.Store(Path(tmp) / "test.sqlite3")
            try:
                one = store.add(article(DO=["10.1234/ABC"], AD=[]), {"source": "one"})
                two = store.add(article(DO=["https://doi.org/10.1234/abc"], AD=["测试机构"], AB=["不同摘要"]), {"source": "two"})
                self.assertEqual(one, two)
                row = store.db.execute("SELECT * FROM records").fetchone()
                self.assertEqual(json.loads(row["fields"])["AD"], ["测试机构"])
                self.assertEqual(len(json.loads(row["evidence"])), 2)
                self.assertIn("field_conflicts", json.loads(row["evidence"])[1])
                c.export_all(store, self.cfg, Path(tmp), require_complete=False)
                self.assertEqual(len(c.parse_refworks((Path(tmp) / "cnki_refworks.txt").read_text(encoding="utf-8"))), 1)
            finally:
                store.db.close()

    def test_detail_meta_and_publication_parser(self):
        raw = {"meta": {}, "labels": {"DOI": "10.1234/test"}, "title": "民族文化测试", "authors": ["甲"],
               "publication": "民族研究. 2026,(03): 1-10", "abstract": "测试摘要", "keywords": ["民族;", "文化;"]}
        fields = c.detail_fields(raw, "https://example.invalid/article")
        self.assertEqual(fields["JF"], ["民族研究"])
        self.assertEqual(fields["YR"], ["2026"])
        self.assertEqual(fields["IS"], ["3"])
        self.assertEqual(fields["OP"], ["1-10"])
        self.assertEqual(fields["DO"], ["10.1234/test"])

    def test_gb18030_file(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_bytes("RT Journal Article\nT1 民族文化\n".encode("gb18030"))
            text, encoding = c.decode_text(path)
            self.assertEqual(encoding, "gb18030")
            self.assertEqual(c.parse_refworks(text)[0]["T1"], ["民族文化"])


if __name__ == "__main__":
    unittest.main()
