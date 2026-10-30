#!/usr/bin/env python3
"""不依赖 sources.json，批量检查公开 URL；只保存状态和标题，不保存文章正文。"""
from __future__ import annotations

import csv
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import unescape
from pathlib import Path

from crawl_news import HERE, ID_RE, existing_state, normalize_url, read_rows
from public_http import public_opener, validate_target

MAX_BYTES = 256 * 1024


def public_url(url, system_proxy=False):
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError("invalid_url")
    validate_target(normalized, system_proxy=system_proxy)
    return normalized


class RequestPacer:
    """同一网站串行并限速；不同网站可并行。"""
    def __init__(self, delay):
        self.delay = delay
        self.guard = threading.Lock()
        self.locks = {}
        self.last = {}
        self.blocked = set()

    def run(self, url, operation):
        host = urllib.parse.urlsplit(url).hostname
        with self.guard:
            lock = self.locks.setdefault(host, threading.Lock())
        with lock:
            if host in self.blocked:
                raise ValueError("host_blocked_this_run")
            wait = self.delay - (time.monotonic() - self.last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            try:
                return operation()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 429):
                    self.blocked.add(host)
                raise
            finally:
                self.last[host] = time.monotonic()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def inspect_html(raw, encoding):
    try:
        text = raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    title_match = re.search(r"<title\b[^>]*>(.*?)</title>", text, re.I | re.S)
    title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", title_match.group(1)))).strip() if title_match else ""
    visible = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.I | re.S)
    visible = unescape(re.sub(r"<[^>]+>", " ", visible))
    if re.search(r"just a moment|checking your browser|access denied|attention required|captcha", title, re.I):
        hint = "challenge_or_access_denied"
    elif re.search(r"verify (?:that )?you are human|enable javascript and cookies to continue", visible, re.I):
        hint = "challenge_hint"
    elif re.search(r"subscribe to (?:continue reading|read this article|unlock)|sign in to continue reading", visible, re.I):
        hint = "subscription_or_login_hint"
    else:
        hint = "page_returned_body_completeness_not_checked"
    return title[:600], hint


def probe_url(url, pacer, timeout, system_proxy=False):
    started = time.monotonic()
    current = url
    result = {"status": "failed", "http_status": "", "final_url": url, "title": "", "access_hint": ""}
    opener = public_opener(NoRedirect(), system_proxy=system_proxy)
    try:
        for hop in range(6):
            current = public_url(current, system_proxy=system_proxy)
            result["final_url"] = current

            def request():
                req = urllib.request.Request(current, headers={
                    "User-Agent": "NewsURLChecker/1.0 (personal research)",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.5",
                })
                try:
                    with opener.open(req, timeout=timeout) as response:
                        raw = response.read(MAX_BYTES)
                        return response.status, response.headers, raw
                except urllib.error.HTTPError as exc:
                    if exc.code in (301, 302, 303, 307, 308):
                        headers = exc.headers
                        exc.close()
                        return exc.code, headers, b""
                    raise

            status, headers, raw = pacer.run(current, request)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location")
                if not location:
                    raise ValueError("redirect_without_location")
                target = urllib.parse.urljoin(current, location)
                if urllib.parse.urlsplit(current).scheme.lower() == "https" and urllib.parse.urlsplit(target).scheme.lower() == "http":
                    raise ValueError("insecure_redirect")
                current = target
                continue
            result.update(status="http_ok", http_status=status, content_type=headers.get_content_type())
            if headers.get_content_type() in {"text/html", "application/xhtml+xml"}:
                result["title"], result["access_hint"] = inspect_html(raw, headers.get_content_charset())
                if result["access_hint"] in {"challenge_or_access_denied", "challenge_hint"}:
                    result["status"] = "challenge"
                    with pacer.guard:
                        pacer.blocked.add(urllib.parse.urlsplit(current).hostname)
            else:
                result["access_hint"] = "non_html_response"
            break
        else:
            raise ValueError("too_many_redirects")
    except urllib.error.HTTPError as exc:
        result.update(status="http_error", http_status=exc.code, access_hint=str(exc.reason))
        exc.close()
    except (OSError, ValueError, urllib.error.URLError) as exc:
        result["access_hint"] = str(exc)[:400]
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def round_robin(urls):
    domains = OrderedDict()
    for url in urls:
        domains.setdefault(urllib.parse.urlsplit(url).hostname, deque()).append(url)
    while domains:
        for host in list(domains):
            yield domains[host].popleft()
            if not domains[host]:
                del domains[host]


def probe_all(args):
    project = Path(args.project_dir).expanduser().resolve()
    workbook = Path(args.workbook).expanduser().resolve() if args.workbook else project / "news_urls(1).xlsx"
    output = Path(args.output).expanduser().resolve() if args.output else project / "New Output"
    rows = read_rows(workbook, args.sheet)
    ids, existing_urls, _ = existing_state(project, output, rows)
    records, groups = [], defaultdict(list)
    for row in rows:
        if row["excel_row"] < args.start_row or (args.end_row and row["excel_row"] > args.end_row):
            continue
        record = {k: row[k] for k in ("excel_row", "headline_id", "headline", "url")}
        record.update(status="pending", http_status="", final_url="", title="", access_hint="", seconds="")
        url = normalize_url(row["url"])
        if row["headline_id"] in ids:
            record["status"] = "existing_id"
        elif url and url in existing_urls:
            record["status"] = "existing_url"
        elif not url:
            record["status"] = "invalid_url"
        elif not ID_RE.fullmatch(row["headline_id"]):
            record["status"] = "invalid_headline_id"
        else:
            groups[url].append(len(records))
        records.append(record)
    # 截取依据仍为原表行序；只在发起请求时交错不同域名，提高并发利用率。
    urls = list(groups)[:args.max_requests]
    for url in list(groups)[args.max_requests:]:
        for i in groups[url]:
            records[i]["status"] = "not_requested_limit"
    logdir = HERE / "runs"
    logdir.mkdir(exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    journal = logdir / ("access_" + stamp + ".jsonl")
    report = logdir / ("access_" + stamp + ".csv")
    print("不读取 sources.json。已有 {} 篇；范围内 {} 行；本次检查最多 {} 个不同 URL。".format(len(ids), len(records), len(urls)), flush=True)
    pacer = RequestPacer(args.delay)
    completed = 0
    with journal.open("x", encoding="utf-8") as log, ThreadPoolExecutor(max_workers=args.workers) as pool:
        probe_options = {"system_proxy": True} if args.system_proxy else {}
        futures = {pool.submit(probe_url, u, pacer, args.timeout, **probe_options): u for u in round_robin(urls)}
        for future in as_completed(futures):
            url = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {"status": "failed", "access_hint": "unexpected: " + str(exc)[:400]}
            for i in groups[url]:
                records[i].update({k: v for k, v in result.items() if k in records[i]})
                if len(groups[url]) > 1:
                    records[i]["access_hint"] += "; shared_request_for_same_url"
            completed += 1
            log.write(json.dumps({"url": url, "excel_rows": [records[i]["excel_row"] for i in groups[url]], **result}, ensure_ascii=False) + "\n")
            log.flush()
            print("[{}/{}] {} {} {}".format(completed, len(urls), result.get("http_status", ""), result["status"], url), flush=True)
    with report.open("x", encoding="utf-8-sig", newline="") as f:
        fields = ["excel_row", "headline_id", "headline", "url", "status", "http_status", "final_url", "title", "access_hint", "seconds"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(records)
    print("逐行结果：" + str(report))
    print("统计：" + json.dumps(Counter(r["status"] for r in records), ensure_ascii=False))
    print("只保存访问状态和网页标题；没有保存新闻正文，也没有改动原TXT或Excel。")
    return 0


if __name__ == "__main__":
    from crawl_news import parser
    args = parser().parse_args()
    if args.download or args.dry_run:
        raise SystemExit("访问检查请去掉 --download/--dry-run；本文本扫描器不会保存正文。")
    if args.max_requests < 1 or args.start_row < 2 or not 1 <= args.workers <= 8 or args.delay < 1 or args.timeout <= 0:
        raise SystemExit("max-requests为正数，start-row至少2，workers为1–8，delay至少1秒，timeout为正数。")
    if args.end_row and args.end_row < args.start_row:
        raise SystemExit("end-row不能小于start-row。")
    raise SystemExit(probe_all(args))
