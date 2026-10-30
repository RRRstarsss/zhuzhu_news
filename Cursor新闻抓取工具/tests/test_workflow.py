"""Offline regression tests. All fixtures live in temporary directories."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))
import crawl_news  # noqa: E402


ARTICLE_BODY = (
    "The Senate committee opened its public hearing on Wednesday morning. "
    "Lawmakers asked the company chief to explain how the service collects "
    "information and how customers can control the use of their accounts. "
    "The executive said a new review would examine every application with "
    "access to private records and report its findings to the committee. "
    "Several senators requested a written timetable for the promised changes. "
    "A separate House panel plans to meet next week to consider the same issues. "
    "Consumer representatives will also be invited to give evidence at that session. "
    "The chair closed the meeting after members agreed to publish further questions."
)


def make_workbook(path: Path, records: list[dict]) -> None:
    """Build a disposable workbook without opening or changing the user's file."""
    wb = Workbook()
    ws = wb.active
    ws.title = "headline_urls"
    columns = [
        "headline_id", "headline", "url", "earliest_published_et", "match_quality"
    ]
    ws.append(columns)
    for record in records:
        ws.append([record.get(column) for column in columns])
    wb.save(path)
    wb.close()


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="news-workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "新闻 project"
        self.project.mkdir()
        self.output = self.project / "New Output"
        self.output.mkdir()
        self.current = self.project / "Current Output" / "txt"
        self.current.mkdir(parents=True)
        self.tool = Path(self.temp.name) / "tool"
        self.tool.mkdir()
        (self.tool / "sources.json").write_text(json.dumps({"sources": [{
            "hosts": ["example.org"], "terms_url": "https://example.org/terms"
        }]}), encoding="utf-8")
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(crawl_news, "HERE", self.tool).start()
        self.no_network = mock.patch.object(
            crawl_news, "fetch_html", side_effect=AssertionError("Tests must not access the network")
        ).start()
        mock.patch.object(crawl_news.time, "sleep", return_value=None).start()

    def args(self, workbook: Path, *options: str):
        return crawl_news.parser().parse_args([
            "--project-dir", str(self.project), "--workbook", str(workbook),
            "--output", str(self.output), "--delay", "1", "--no-system-proxy", *options,
        ])

    def quiet_run(self, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return crawl_news.run(args)

    def journal_records(self):
        return [
            json.loads(line)
            for path in sorted((self.tool / "runs").glob("download_*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()
        ]

    def test_tracking_parameters_and_fragment_do_not_defeat_url_deduplication(self):
        actual = crawl_news.normalize_url(
            "https://example.org/article?b=2&utm_source=newsletter&a=1"
            "&utm_campaign=weekly#comments"
        )
        self.assertEqual(actual, "https://example.org/article?a=1&b=2")

    def test_meaningful_query_parameters_are_preserved(self):
        first = crawl_news.normalize_url("https://example.org/story?id=1")
        second = crawl_news.normalize_url("https://example.org/story?id=2")
        self.assertNotEqual(first, second)

    def test_read_rows_preserves_physical_excel_order_and_row_numbers(self):
        workbook = self.project / "input.xlsx"
        make_workbook(workbook, [
            {"headline_id": "cccccccccccccccc", "headline": "Third alphabetically", "url": "https://z.example/a"},
            {},
            {"headline_id": "aaaaaaaaaaaaaaaa", "headline": "First alphabetically", "url": "https://a.example/a"},
            {"headline_id": "bbbbbbbbbbbbbbbb", "headline": "Second alphabetically", "url": "https://m.example/a"},
        ])
        rows = crawl_news.read_rows(workbook)
        self.assertEqual([r["headline_id"] for r in rows], [
            "cccccccccccccccc", "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"
        ])
        self.assertEqual([r["excel_row"] for r in rows], [2, 4, 5])
        self.assertEqual(rows[1]["url"], "https://a.example/a")

    def test_existing_state_recognizes_103_ids_and_url_aliases(self):
        rows = []
        expected_ids = set()
        before = {}
        for number in range(103):
            ident = f"{number + 1:016x}"
            expected_ids.add(ident)
            url = f"https://example.org/articles/{number}"
            rows.append({"headline_id": ident, "headline": f"Article {number}", "url": url, "excel_row": number + 2})
            directory = self.current if number < 3 else self.output
            source_label = "Source:" if number % 2 else "Source URL:"
            if number % 2:
                content = f"Article {number}\n\n{ARTICLE_BODY}\n\n{source_label} {url}?utm_source=mail\n"
            else:
                content = f"Article {number}\n\n{source_label} {url}#top\n\n{ARTICLE_BODY}\n"
            path = directory / f"{ident}.txt"
            path.write_text(content, encoding="utf-8")
            before[path] = path.read_bytes()
        alias_id = "ffffffffffffffff"
        rows.append({"headline_id": alias_id, "headline": "Alias", "url": "https://example.org/articles/0?utm_medium=email", "excel_row": 105})

        ids, urls, bodies = crawl_news.existing_state(self.project, self.output, rows)

        self.assertEqual(ids, expected_ids)
        self.assertNotIn(alias_id, ids)
        self.assertIn(crawl_news.normalize_url(rows[-1]["url"]), urls)
        self.assertEqual(set(bodies), expected_ids)
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original)

    def test_legacy_txt_source_labels_are_recognized_without_workbook_rows(self):
        (self.current / "1111111111111111.txt").write_text(
            f"First\n\n{ARTICLE_BODY}\n\nSource: https://example.org/a?utm_source=x\n",
            encoding="utf-8",
        )
        (self.output / "2222222222222222.txt").write_text(
            f"Second\n\nSource URL: https://example.org/b#video\n\n{ARTICLE_BODY}\n",
            encoding="utf-8",
        )
        _, urls, _ = crawl_news.existing_state(self.project, self.output, [])
        self.assertTrue({"https://example.org/a", "https://example.org/b"}.issubset(urls))

    def test_nearly_identical_article_is_identified(self):
        changed = ARTICLE_BODY.replace("Wednesday morning", "Thursday morning")
        self.assertEqual(
            crawl_news.similar_to(changed, {"known": ARTICLE_BODY}), "known"
        )

    def test_unrelated_article_is_not_identified_as_duplicate(self):
        unrelated = (
            "The wildlife team returned to the northern valley after the winter snow melted. "
            "Researchers counted nesting birds along the river and mapped the forests where "
            "rare plants were beginning to flower. Their observations will help local groups "
            "restore damaged habitats and plan footpaths that avoid sensitive breeding areas."
        )
        self.assertIsNone(crawl_news.similar_to(unrelated, {"known": ARTICLE_BODY}))

    def row_and_article(self):
        title = "Facebook CEO Mark Zuckerberg testifies before Senate committee"
        row = {
            "headline_id": "aaaaaaaaaaaaaaaa", "headline": title,
            "earliest_published_et": "2018-04-10T09:00:00",
            "page_date": "2018-04-10", "url": "https://example.org/story",
        }
        article = {
            "title": title, "body": [ARTICLE_BODY], "author": "Reporter",
            "published": "2018-04-10T18:00:00+00:00", "method": "test-fixture",
            "copyright": ["Copyright Example Publisher"],
        }
        return row, article

    def test_matching_title_and_date_are_accepted(self):
        row, article = self.row_and_article()
        self.assertIsNone(crawl_news.validate_article(row, article))

    def test_wrong_article_title_is_rejected(self):
        row, article = self.row_and_article()
        article["title"] = "Rare birds return to mountain wildlife reserve after winter"
        with self.assertRaises(ValueError):
            crawl_news.validate_article(row, article)

    def test_generic_one_word_heading_is_not_a_matching_article_title(self):
        row, article = self.row_and_article()
        article["title"] = "Facebook"
        with self.assertRaises(ValueError):
            crawl_news.validate_article(row, article)

    def test_article_more_than_a_week_from_expected_date_is_rejected(self):
        row, article = self.row_and_article()
        article["published"] = "2018-05-10T18:00:00+00:00"
        with self.assertRaises(ValueError):
            crawl_news.validate_article(row, article)

    def test_default_preview_is_offline_and_honors_inclusive_excel_row_range(self):
        workbook = self.project / "input.xlsx"
        make_workbook(workbook, [
            {"headline_id": f"{n:016x}", "headline": f"Article {n}", "url": f"https://example.org/story/{n}"}
            for n in [9, 3, 7, 1]
        ])
        self.assertEqual(self.quiet_run(self.args(workbook, "--start-row", "3", "--end-row", "4")), 0)
        self.no_network.assert_not_called()
        self.assertEqual(list(self.output.glob("*.txt")), [])
        reports = list((self.tool / "runs").glob("preview_*.csv"))
        self.assertEqual(len(reports), 1)
        with reports[0].open(encoding="utf-8-sig", newline="") as source:
            records = list(csv.DictReader(source))
        self.assertEqual([r["excel_row"] for r in records], ["3", "4"])
        self.assertEqual([r["headline_id"] for r in records], [f"{3:016x}", f"{7:016x}"])

    def test_download_skips_existing_id_and_same_url_alias_without_network(self):
        workbook = self.project / "input.xlsx"
        old_id, alias_id = "1111111111111111", "2222222222222222"
        original = f"Existing title\n\n{ARTICLE_BODY}\n".encode("utf-8")
        existing = self.current / f"{old_id}.txt"
        existing.write_bytes(original)
        make_workbook(workbook, [
            {"headline_id": old_id, "headline": "Existing title", "url": "https://example.org/story"},
            {"headline_id": alias_id, "headline": "Alias title", "url": "https://example.org/story?utm_source=mail#top"},
        ])
        self.assertEqual(self.quiet_run(self.args(workbook, "--download", "--limit", "1")), 2)
        self.no_network.assert_not_called()
        self.assertEqual(existing.read_bytes(), original)
        self.assertFalse((self.output / f"{alias_id}.txt").exists())
        self.assertEqual([r["status"] for r in self.journal_records()], ["existing_id", "duplicate_url"])

    def test_http_403_does_not_save_a_fake_article_or_retry_same_host(self):
        workbook = self.project / "input.xlsx"
        row, _ = self.row_and_article()
        row2 = {**row, "headline_id": "bbbbbbbbbbbbbbbb", "url": "https://example.org/second"}
        make_workbook(workbook, [row, row2])
        error = urllib.error.HTTPError(row["url"], 403, "Forbidden", {}, None)
        with mock.patch.object(crawl_news, "fetch_html", side_effect=error) as fetch:
            self.assertEqual(self.quiet_run(self.args(workbook, "--download", "--limit", "1")), 2)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(list(self.output.glob("*.txt")), [])
        self.assertEqual([r["status"] for r in self.journal_records()], ["http_403", "host_blocked_this_run"])

    def test_http_200_challenge_page_is_not_saved_as_news(self):
        workbook = self.project / "input.xlsx"
        row, _ = self.row_and_article()
        make_workbook(workbook, [row])
        challenge = (
            b"<html><title>Just a moment...</title><body>"
            b"<h1>Verify you are human</h1><p>Enable JavaScript and cookies to continue.</p>"
            b"</body></html>"
        )
        with mock.patch.object(crawl_news, "fetch_html", return_value=(challenge, row["url"])):
            self.assertEqual(self.quiet_run(self.args(workbook, "--download", "--limit", "1")), 2)
        self.assertEqual(list(self.output.glob("*.txt")), [])
        self.assertEqual(self.journal_records()[0]["status"], "failed")

    def test_file_created_during_fetch_cannot_be_overwritten(self):
        workbook = self.project / "input.xlsx"
        row, article = self.row_and_article()
        make_workbook(workbook, [row])
        destination = self.output / f"{row['headline_id']}.txt"
        sentinel = b"An existing local file must be preserved.\n"

        def competing_write(url, allowed_hosts, timeout):
            destination.write_bytes(sentinel)
            return b"<html></html>", url

        with mock.patch.object(crawl_news, "fetch_html", side_effect=competing_write), \
                mock.patch.object(crawl_news, "extract_article", return_value=article):
            self.assertEqual(self.quiet_run(self.args(workbook, "--download", "--limit", "1")), 2)
        self.assertEqual(destination.read_bytes(), sentinel)
        self.assertEqual(self.journal_records()[0]["status"], "failed")

    def test_success_limit_counts_saved_articles_and_preserves_attempt_order(self):
        workbook = self.project / "input.xlsx"
        row, article = self.row_and_article()
        records = [
            {**row, "headline_id": f"{n:016x}", "url": f"https://example.org/story/{n}"}
            for n in [8, 2, 5]
        ]
        make_workbook(workbook, records)
        unavailable = urllib.error.HTTPError(records[0]["url"], 404, "Not Found", {}, None)
        with mock.patch.object(crawl_news, "fetch_html", side_effect=[
                unavailable, (b"<html></html>", records[1]["url"])
        ]) as fetch, mock.patch.object(crawl_news, "extract_article", return_value=article):
            self.assertEqual(self.quiet_run(self.args(workbook, "--download", "--limit", "1")), 0)
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [records[0]["url"], records[1]["url"]])
        self.assertEqual({p.stem for p in self.output.glob("*.txt")}, {records[1]["headline_id"]})
        self.assertEqual([r["status"] for r in self.journal_records()], ["http_404", "saved"])


if __name__ == "__main__":
    unittest.main()
