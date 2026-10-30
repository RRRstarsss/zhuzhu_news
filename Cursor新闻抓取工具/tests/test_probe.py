"""Offline URL-probe regressions; no network, real workbook, or news TXT writes."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))
import check_urls  # noqa: E402
import crawl_news  # noqa: E402


def row(number, url):
    return {"headline_id": f"{number:016x}", "headline": f"Headline {number}", "url": url}


def dns_answer(address):
    if ":" in address:
        return (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))


class Response:
    def __init__(self, raw=b"<html><title>A &amp; B</title><body>PRIVATE_BODY_MARKER</body></html>"):
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        self.read = mock.Mock(return_value=raw)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


def redirect(location):
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError("https://public.example/start", 302, "Found", headers, io.BytesIO())


class ProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="news-probe-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "新闻项目"
        self.output = self.project / "New Output"
        self.current = self.project / "Current Output" / "txt"
        self.tool = self.root / "tool"
        for folder in (self.output, self.current, self.tool):
            folder.mkdir(parents=True)
        self.original_txt = {}
        self.original_workbooks = {}
        self.patch(check_urls, "HERE", self.tool)
        self.patch(crawl_news, "HERE", self.tool)
        self.patch(check_urls.time, "sleep", return_value=None)
        self.dns = self.patch(check_urls.socket, "getaddrinfo", side_effect=AssertionError("Unexpected DNS/network call"))
        self.opener = mock.Mock()
        self.opener.open.side_effect = AssertionError("Unexpected HTTP/network call")
        self.opener_factory = self.patch(check_urls, "public_opener", return_value=self.opener)

        # A config lookup is an immediate test failure, even if a file is absent.
        real_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name == "sources.json":
                raise AssertionError("Probe must not read sources.json")
            return real_open(path, *args, **kwargs)

        self.patch(Path, "open", autospec=True, side_effect=guarded_open)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def tearDown(self):
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*.txt")}, self.original_txt)
        for path, original in self.original_workbooks.items():
            self.assertEqual(path.read_bytes(), original, "Probe changed the workbook")

    def workbook(self, records):
        path = self.project / "input.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "headline_urls"
        fields = ["headline_id", "headline", "url"]
        sheet.append(fields)
        for record in records:
            sheet.append([record.get(field) for field in fields])
        book.save(path)
        book.close()
        self.original_workbooks[path] = path.read_bytes()
        return path

    def existing(self, number, text, folder=None):
        path = (folder or self.current) / f"{number:016x}.txt"
        original = text.encode("utf-8")
        path.write_bytes(original)
        self.original_txt[path] = original
        return path

    def args(self, workbook, *extra):
        return crawl_news.parser().parse_args([
            "--project-dir", str(self.project), "--workbook", str(workbook),
            "--output", str(self.output), "--workers", "1", "--delay", "1", "--no-system-proxy", *extra,
        ])

    def run_probe(self, workbook, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            result = check_urls.probe_all(self.args(workbook, *extra))
        self.assertEqual(result, 0)
        reports = list((self.tool / "runs").glob("access_*.csv"))
        self.assertEqual(len(reports), 1)
        with reports[0].open(encoding="utf-8-sig", newline="") as stream:
            records = list(csv.DictReader(stream))
        journals = list((self.tool / "runs").glob("access_*.jsonl"))
        self.assertEqual(len(journals), 1)
        entries = [json.loads(line) for line in journals[0].read_text().splitlines()]
        return records, entries

    @staticmethod
    def good_result(url, *unused):
        return {"status": "http_ok", "http_status": 200, "final_url": url,
                "title": "Public page title", "access_hint": "page_returned_body_completeness_not_checked", "seconds": 0}

    def allow_public_dns(self):
        self.dns.side_effect = None
        self.dns.return_value = [dns_answer("8.8.8.8")]

    def test_probe_without_sources_config_records_title_but_never_body_or_txt(self):
        workbook = self.workbook([row(1, "https://unconfigured.example/story")])
        self.allow_public_dns()
        response = Response()
        self.opener.open.side_effect = None
        self.opener.open.return_value = response

        records, entries = self.run_probe(workbook)

        self.assertFalse((self.tool / "sources.json").exists())
        self.assertEqual(records[0]["status"], "http_ok")
        self.assertEqual(records[0]["title"], "A & B")
        self.assertEqual(len(entries), 1)
        response.read.assert_called_once_with(check_urls.MAX_BYTES)
        for path in (self.tool / "runs").iterdir():
            self.assertNotIn("PRIVATE_BODY_MARKER", path.read_text(encoding="utf-8-sig"))

    def test_existing_ids_and_url_aliases_are_skipped_without_requests(self):
        self.existing(1, "Original article without source metadata.\n")
        self.existing(2, "Second article\nSource URL: https://saved.example/second?utm_source=old\n", self.output)
        workbook = self.workbook([
            row(1, "https://old.example/first"),
            row(3, "https://old.example/first?utm_medium=alias#section"),
            row(2, "https://saved.example/second"),
            row(4, "https://saved.example/second#comments"),
        ])
        probe = self.patch(check_urls, "probe_url", side_effect=AssertionError("Existing article was requested"))

        # The workbook row mapping the first saved ID is outside this range;
        # its URL alias must still be recognized by the global existing-state scan.
        records, entries = self.run_probe(workbook, "--start-row", "3")

        probe.assert_not_called()
        self.assertEqual([r["status"] for r in records], ["existing_url", "existing_id", "existing_url"])
        self.assertEqual(entries, [])

    def test_same_normalized_url_uses_one_request_and_updates_every_excel_row(self):
        workbook = self.workbook([
            row(7, "https://fresh.example/story?id=7&utm_source=mail"),
            row(9, "https://fresh.example/story?id=7#comments"),
            row(5, "https://fresh.example/story?utm_medium=feed&id=7"),
        ])
        probe = self.patch(check_urls, "probe_url", side_effect=self.good_result)

        records, entries = self.run_probe(workbook)

        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0], "https://fresh.example/story?id=7")
        self.assertEqual([r["status"] for r in records], ["http_ok"] * 3)
        self.assertTrue(all("shared_request_for_same_url" in r["access_hint"] for r in records))
        self.assertEqual(entries[0]["excel_rows"], [2, 3, 4])

    def test_csv_preserves_physical_excel_order_when_request_order_differs(self):
        workbook = self.workbook([
            row(9, "https://a.example/first"), {},
            row(2, "https://a.example/second"),
            row(6, "https://b.example/third"),
            row(1, "https://c.example/outside-range"),
        ])
        probe = self.patch(check_urls, "probe_url", side_effect=self.good_result)

        records, entries = self.run_probe(workbook, "--end-row", "5")

        requested = [call.args[0] for call in probe.call_args_list]
        self.assertEqual(requested, ["https://a.example/first", "https://b.example/third", "https://a.example/second"])
        self.assertEqual([r["excel_row"] for r in records], ["2", "4", "5"])
        self.assertEqual([r["headline_id"] for r in records], [f"{n:016x}" for n in [9, 2, 6]])
        self.assertCountEqual([r["excel_rows"] for r in entries], [[2], [5], [4]])

    def test_max_requests_counts_unique_urls_and_selects_in_workbook_order(self):
        workbook = self.workbook([
            row(1, "https://a.example/first"),
            row(2, "https://a.example/first?utm_source=alias"),
            row(3, "https://a.example/second"),
            row(4, "https://b.example/third"),
            row(5, "https://b.example/third#alias"),
        ])
        probe = self.patch(check_urls, "probe_url", side_effect=self.good_result)

        records, entries = self.run_probe(workbook, "--max-requests", "2")

        self.assertEqual([c.args[0] for c in probe.call_args_list], ["https://a.example/first", "https://a.example/second"])
        self.assertEqual([r["status"] for r in records], ["http_ok"] * 3 + ["not_requested_limit"] * 2)
        self.assertEqual(len(entries), 2)

    def test_invalid_urls_and_ids_do_not_consume_requests(self):
        workbook = self.workbook([
            row(1, "file:///etc/passwd"),
            {"headline_id": "bad-id", "headline": "Invalid id", "url": "https://a.example/story"},
            row(2, "https://a.example/valid"),
        ])
        probe = self.patch(check_urls, "probe_url", side_effect=self.good_result)
        records, _ = self.run_probe(workbook, "--max-requests", "1")
        self.assertEqual([r["status"] for r in records], ["invalid_url", "invalid_headline_id", "http_ok"])
        self.assertEqual(probe.call_count, 1)

    def test_non_public_ipv4_and_ipv6_are_rejected_before_http_request(self):
        for address in ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1", "ff02::1"]:
            with self.subTest(address=address):
                self.opener.open.reset_mock()
                self.dns.side_effect = None
                self.dns.return_value = [dns_answer(address)]
                result = check_urls.probe_url("https://private.example/story", check_urls.RequestPacer(0), 1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["access_hint"].split(":", 1)[0], "non_public_address")
                self.opener.open.assert_not_called()

    def test_mixed_public_and_private_dns_answers_are_rejected(self):
        self.dns.side_effect = None
        self.dns.return_value = [dns_answer("8.8.8.8"), dns_answer("10.0.0.1")]
        result = check_urls.probe_url("https://mixed.example/story", check_urls.RequestPacer(0), 1)
        self.assertEqual(result["access_hint"].split(":", 1)[0], "non_public_address")
        self.opener.open.assert_not_called()

    def test_redirect_target_is_resolved_and_private_target_is_not_requested(self):
        def resolve(host, *args, **kwargs):
            return [dns_answer("8.8.8.8" if host == "public.example" else "127.0.0.1")]
        self.dns.side_effect = resolve
        self.opener.open.side_effect = [redirect("https://private.example/secret")]
        result = check_urls.probe_url("https://public.example/start", check_urls.RequestPacer(0), 1)
        self.assertEqual(result["access_hint"].split(":", 1)[0], "non_public_address")
        self.assertEqual([c.args[0] for c in self.dns.call_args_list], ["public.example", "private.example"])
        self.assertEqual(self.opener.open.call_count, 1)

    def test_https_to_http_redirect_is_rejected_case_insensitively(self):
        self.allow_public_dns()
        for scheme in ["http", "HTTP", "HtTp"]:
            with self.subTest(scheme=scheme):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = [redirect(f"{scheme}://other.example/story"), Response()]
                result = check_urls.probe_url("https://public.example/start", check_urls.RequestPacer(0), 1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["access_hint"], "insecure_redirect")
                self.assertEqual(self.opener.open.call_count, 1)

    def test_non_web_credential_and_nonstandard_port_redirects_are_rejected(self):
        self.allow_public_dns()
        for target in ["file:///etc/passwd", "ftp://public.example/file", "https://user:password@other.example/story", "https://other.example:8080/story"]:
            with self.subTest(target=target):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = [redirect(target)]
                result = check_urls.probe_url("https://public.example/start", check_urls.RequestPacer(0), 1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["access_hint"], "invalid_url")
                self.assertEqual(self.opener.open.call_count, 1)

    def test_public_https_redirect_can_cross_unconfigured_hosts(self):
        self.allow_public_dns()
        response = Response()
        self.opener.open.side_effect = [redirect("https://other.example/final?utm_source=link"), response]
        result = check_urls.probe_url("https://public.example/start", check_urls.RequestPacer(0), 1)
        self.assertEqual(result["status"], "http_ok")
        self.assertEqual(result["final_url"], "https://other.example/final")
        self.assertEqual(result["title"], "A & B")
        self.assertEqual([c.args[0].full_url for c in self.opener.open.call_args_list], ["https://public.example/start", "https://other.example/final"])

    def test_http_403_blocks_further_requests_to_same_host(self):
        self.allow_public_dns()
        self.opener.open.side_effect = urllib.error.HTTPError("https://public.example/first", 403, "Forbidden", Message(), io.BytesIO())
        pacer = check_urls.RequestPacer(0)
        first = check_urls.probe_url("https://public.example/first", pacer, 1)
        second = check_urls.probe_url("https://public.example/second", pacer, 1)
        self.assertEqual((first["status"], first["http_status"]), ("http_error", 403))
        self.assertEqual(second["access_hint"], "host_blocked_this_run")
        self.assertEqual(self.opener.open.call_count, 1)

    def test_http_200_challenge_is_not_reported_as_accessible_article(self):
        self.allow_public_dns()
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response(b"<title>Checking your browser...</title><p>Verify you are human</p>")
        result = check_urls.probe_url("https://public.example/story", check_urls.RequestPacer(0), 1)
        self.assertEqual(result["status"], "challenge")
        self.assertEqual(result["access_hint"], "challenge_or_access_denied")


if __name__ == "__main__":
    unittest.main()
