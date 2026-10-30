"""Generic mode workflow regressions; no real downloads or user files."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import crawl_news
from extractor import ExtractionError
from test_workflow import ARTICLE_BODY, make_workbook


class AllSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.tool = self.project / "tool"
        self.tool.mkdir()
        self.workbook = self.project / "news_urls(1).xlsx"
        self.output = self.project / "New Output"
        self.output.mkdir()
        self.row = {
            "headline_id": "aaaaaaaaaaaaaaaa",
            "headline": "Facebook CEO Mark Zuckerberg testifies before Senate committee",
            "url": "https://unconfigured.example/story",
            "earliest_published_et": "2018-04-10",
            "match_quality": "high",
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(crawl_news, "HERE", self.tool).start()
        mock.patch.object(crawl_news.time, "sleep").start()
        self.fetch = mock.patch.object(crawl_news, "fetch_html", side_effect=AssertionError("Unexpected network")).start()

    def run_mode(self, *options):
        args = crawl_news.parser().parse_args([
            "--project-dir", str(self.project), "--all-sources", "--limit", "1", "--no-system-proxy", *options,
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            return crawl_news.run(args)

    def test_preview_works_without_sources_file(self):
        make_workbook(self.workbook, [self.row])
        self.assertFalse((self.tool / "sources.json").exists())
        self.assertEqual(self.run_mode("--dry-run"), 0)
        report = next((self.tool / "runs").glob("preview_*.csv"))
        with report.open(encoding="utf-8-sig", newline="") as f:
            records = list(csv.DictReader(f))
        self.assertEqual(records[0]["status"], "ready")
        self.fetch.assert_not_called()
        self.assertEqual(list(self.output.glob("*.txt")), [])

    def test_generic_html_is_saved_under_id_without_sources_file(self):
        make_workbook(self.workbook, [self.row])
        raw = ("<html><head><title>" + self.row["headline"] + "</title>"
               '<meta property="article:published_time" content="2018-04-10T12:00:00Z">'
               '</head><body><article><h1>' + self.row["headline"] + '</h1><p>'
               + ARTICLE_BODY + '</p></article></body></html>').encode()
        with mock.patch.object(crawl_news, "fetch_html", return_value=(raw, self.row["url"])) as fetch:
            self.assertEqual(self.run_mode("--download"), 0)
        self.assertIsNone(fetch.call_args.args[1])
        saved = self.output / (self.row["headline_id"] + ".txt")
        self.assertIn(ARTICLE_BODY, saved.read_text())
        self.assertIn("completeness requires review", saved.read_text())
        manifest = next((self.output / "provenance/cursor_runs").glob("*.json"))
        article = json.loads(manifest.read_text())["articles"][0]
        self.assertEqual(article["source_mode"], "generic")
        self.assertEqual(article["terms_url"], "")

    def test_existing_id_and_url_alias_skip_even_without_configuration(self):
        saved = self.output / (self.row["headline_id"] + ".txt")
        original = b"Existing article must stay unchanged\n"
        saved.write_bytes(original)
        make_workbook(self.workbook, [self.row, {
            **self.row, "headline_id": "bbbbbbbbbbbbbbbb",
            "url": self.row["url"] + "?utm_source=mail",
        }])
        self.assertEqual(self.run_mode("--download"), 2)
        self.fetch.assert_not_called()
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual(list(self.output.glob("*.txt")), [saved])

    def test_access_gate_does_not_trigger_generic_fallback(self):
        make_workbook(self.workbook, [self.row])
        with mock.patch.object(crawl_news, "fetch_html", return_value=(b"<html/>", self.row["url"])), \
                mock.patch.object(crawl_news, "extract_article", side_effect=ExtractionError("needs_browser_review")), \
                mock.patch.object(crawl_news, "extract_generic_article") as generic:
            self.assertEqual(self.run_mode("--download"), 2)
        generic.assert_not_called()
        self.assertEqual(list(self.output.glob("*.txt")), [])

    def test_default_mode_still_uses_configured_sources(self):
        self.assertEqual(crawl_news.classify({**self.row, "excel_row": 2}, set(), set(), {}, 2, 501), "unreviewed_source")
        self.assertEqual(crawl_news.classify({**self.row, "excel_row": 2}, set(), set(), None, 2, 501), "ready")


if __name__ == "__main__":
    unittest.main()
