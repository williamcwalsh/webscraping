#!/usr/bin/env python3
"""Scrape visible Reddit comments from subreddit HTML pages.

This intentionally does not use Reddit's API or JSON endpoints. It parses
old.reddit.com HTML, which means Reddit may still block requests and fields
that are not visible in the page, such as true downvotes, cannot be recovered.
When Reddit includes vote attributes in the HTML, they are written to the CSV.
"""

import argparse
import csv
import os
import ssl
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


BASE_URL = "https://old.reddit.com"
USER_AGENT = "Mozilla/5.0 (compatible; research-comment-scraper/1.0)"
DEFAULT_RATE_LIMIT_RETRIES = 5
DEFAULT_RATE_LIMIT_DELAY = 60.0
RATE_LIMIT_BACKOFF = 2.0
FIELDNAMES = [
    "comment_id",
    "post_id",
    "post_title",
    "post_permalink",
    "subreddit",
    "comment",
    "upvotes",
    "downvotes",
    "score",
    "date_posted",
]


def classes(attrs):
    value = attrs.get("class", "")
    return set(value.split())


def short_error_body(exc):
    message = exc.read().decode("utf-8", errors="replace").strip()
    if len(message) > 500:
        message = message[:500] + "..."
    return message


def clean_vote_value(value):
    if not value:
        return ""

    value = value.replace(",", "").strip()
    return value if value.lstrip("-").isdigit() else ""


def score_from_text(value):
    value = " ".join(value.split()).lower()
    if not value or value in {"score hidden", "[score hidden]"}:
        return ""

    first_word = value.split()[0].replace(",", "")
    if first_word.lstrip("-").isdigit():
        return first_word
    return ""


def url_for_request(url):
    parts = urlsplit(url)
    netloc = parts.netloc.encode("idna").decode("ascii")
    return urlunsplit(
        (
            parts.scheme,
            netloc,
            quote(parts.path, safe="/%"),
            quote(parts.query, safe="=&%:+,/?"),
            quote(parts.fragment, safe="=&%:+,/?"),
        )
    )


def retry_after_seconds(exc, fallback):
    headers = exc.headers or {}
    value = headers.get("Retry-After", "").strip()
    if not value:
        return fallback

    try:
        return max(float(value), 0.0)
    except ValueError:
        return fallback


def fetch_html(
    url,
    verify_ssl=True,
    *,
    rate_limit_retries=DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_delay=DEFAULT_RATE_LIMIT_DELAY,
):
    context = None if verify_ssl else ssl._create_unverified_context()
    request = Request(
        url_for_request(url),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    for attempt in range(rate_limit_retries + 1):
        try:
            with urlopen(request, timeout=30, context=context) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code == 429:
                if attempt >= rate_limit_retries:
                    raise RuntimeError(
                        "Reddit returned HTTP 429 for {0}. Reddit is rate limiting "
                        "these requests after {1} retries; try again later or use a "
                        "larger --delay / --batch-delay / --rate-limit-delay.".format(
                            url,
                            rate_limit_retries,
                        )
                    ) from exc

                fallback_delay = rate_limit_delay * (RATE_LIMIT_BACKOFF ** attempt)
                wait_seconds = retry_after_seconds(exc, fallback_delay)
                print(
                    "Reddit returned HTTP 429 for {0}; waiting {1:.1f} seconds "
                    "before retry {2}/{3}.".format(
                        url,
                        wait_seconds,
                        attempt + 1,
                        rate_limit_retries,
                    ),
                    file=sys.stderr,
                )
                time.sleep(wait_seconds)
                continue

            raise RuntimeError(
                "Reddit returned HTTP {0} for {1}: {2}".format(
                    exc.code, url, short_error_body(exc)
                )
            ) from exc
        except URLError as exc:
            raise RuntimeError("Could not reach Reddit at {0}: {1}".format(url, exc.reason)) from exc

    raise RuntimeError("Could not fetch Reddit page at {0}".format(url))


class PostListingParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.posts = []
        self.current = None
        self.div_depth = 0
        self.capture_title = False
        self.title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        class_names = classes(attrs)

        if self.current is not None and tag == "div":
            self.div_depth += 1

        if tag == "div" and "thing" in class_names and attrs.get("data-type") == "link":
            self.current = {
                "post_id": attrs.get("data-fullname", "").replace("t3_", ""),
                "fullname": attrs.get("data-fullname", ""),
                "permalink": urljoin(BASE_URL, attrs.get("data-permalink", "")),
                "title": "",
            }
            self.div_depth = 1
            self.title_parts = []
            return

        if self.current is None:
            return

        if tag == "a" and "title" in class_names:
            self.capture_title = True
            self.title_parts = []

    def handle_endtag(self, tag):
        if self.current is None:
            return

        if tag == "a" and self.capture_title:
            self.current["title"] = " ".join("".join(self.title_parts).split())
            self.capture_title = False

        if tag == "div":
            self.div_depth -= 1
            if self.div_depth <= 0:
                if self.current.get("permalink") and self.current.get("post_id"):
                    self.posts.append(self.current)
                self.current = None
                self.capture_title = False

    def handle_data(self, data):
        if self.capture_title:
            self.title_parts.append(data)


class CommentParser(HTMLParser):
    def __init__(self, post):
        HTMLParser.__init__(self)
        self.post = post
        self.comments = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        class_names = classes(attrs)

        if tag == "div":
            for comment in self.stack:
                comment["depth"] += 1

            if "thing" in class_names and "comment" in class_names:
                self.stack.append(
                    {
                        "depth": 1,
                        "comment_id": attrs.get("data-fullname", "").replace("t1_", ""),
                        "subreddit": attrs.get("data-subreddit", ""),
                        "upvotes": clean_vote_value(attrs.get("data-ups", "")),
                        "downvotes": clean_vote_value(attrs.get("data-downs", "")),
                        "score": clean_vote_value(attrs.get("data-score", "")),
                        "date_posted": "",
                        "body_parts": [],
                        "body_depth": 0,
                        "capture_score": False,
                        "score_parts": [],
                        "score_priority": 0,
                        "pending_score_priority": 0,
                    }
                )
                return

        if not self.stack:
            return

        current = self.stack[-1]

        if tag == "time" and not current["date_posted"]:
            current["date_posted"] = attrs.get("datetime", "")

        if tag in {"span", "div"} and "score" in class_names:
            priority = 1
            if "likes" in class_names or "dislikes" in class_names:
                priority = 2
            if "unvoted" in class_names:
                priority = 3

            current["capture_score"] = True
            current["score_parts"] = []
            current["pending_score_priority"] = priority

        if tag == "div" and "md" in class_names and current["body_depth"] == 0:
            current["body_depth"] = 1
        elif current["body_depth"] > 0 and tag in {"div", "p", "blockquote", "li"}:
            current["body_depth"] += 1

    def handle_endtag(self, tag):
        if not self.stack:
            return

        current = self.stack[-1]

        if tag in {"span", "div"} and current["capture_score"]:
            score = score_from_text("".join(current["score_parts"]))
            if score and current["pending_score_priority"] >= current["score_priority"]:
                current["score"] = score
                current["score_priority"] = current["pending_score_priority"]
            current["capture_score"] = False
            current["score_parts"] = []
            current["pending_score_priority"] = 0

        if current["body_depth"] > 0 and tag in {"div", "p", "blockquote", "li"}:
            current["body_depth"] -= 1

        if tag == "div":
            for comment in list(self.stack):
                comment["depth"] -= 1

            while self.stack and self.stack[-1]["depth"] <= 0:
                finished = self.stack.pop()
                body = " ".join("".join(finished["body_parts"]).split())
                if body and body not in {"[deleted]", "[removed]"}:
                    self.comments.append(
                        {
                            "comment_id": finished["comment_id"],
                            "post_id": self.post["post_id"],
                            "post_title": self.post["title"],
                            "post_permalink": self.post["permalink"],
                            "subreddit": finished["subreddit"],
                            "comment": body,
                            "upvotes": finished["upvotes"],
                            "downvotes": finished["downvotes"],
                            "score": finished["score"],
                            "date_posted": normalize_date(finished["date_posted"]),
                        }
                    )

    def handle_data(self, data):
        if not self.stack:
            return

        current = self.stack[-1]
        if current["body_depth"] > 0:
            current["body_parts"].append(data)
        elif current["capture_score"]:
            current["score_parts"].append(data)


def normalize_date(value):
    if not value:
        return ""

    try:
        parsed = datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return parsed.isoformat()
    except ValueError:
        return value


def parse_posts(html):
    parser = PostListingParser()
    parser.feed(html)
    return parser.posts


def parse_comments(html, post):
    parser = CommentParser(post)
    parser.feed(html)
    return parser.comments


def iter_subreddit_posts(
    subreddit,
    sort,
    post_limit,
    verify_ssl=True,
    rate_limit_retries=DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_delay=DEFAULT_RATE_LIMIT_DELAY,
):
    fetched = 0
    after = None

    while fetched < post_limit:
        params = {"count": fetched}
        if after:
            params["after"] = after

        url = "{0}/r/{1}/{2}/?{3}".format(
            BASE_URL,
            subreddit,
            sort,
            urlencode(params),
        )
        posts = parse_posts(
            fetch_html(
                url,
                verify_ssl=verify_ssl,
                rate_limit_retries=rate_limit_retries,
                rate_limit_delay=rate_limit_delay,
            )
        )
        if not posts:
            break

        for post in posts:
            fetched += 1
            yield post
            if fetched >= post_limit:
                break

        after = posts[-1].get("fullname")
        if not after:
            break


def scrape_comments(
    subreddit: str,
    *,
    max_comments: int,
    sort: str,
    comment_sort: str,
    post_limit: int,
    delay: float,
    verify_ssl: bool,
    skip_comment_ids=None,
    rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY,
) -> List[Dict[str, Any]]:
    rows = []  # type: List[Dict[str, Any]]
    seen_comment_ids = set(skip_comment_ids or [])

    for post in iter_subreddit_posts(
        subreddit,
        sort,
        post_limit,
        verify_ssl=verify_ssl,
        rate_limit_retries=rate_limit_retries,
        rate_limit_delay=rate_limit_delay,
    ):
        if len(rows) >= max_comments:
            break

        params = urlencode({"limit": 500, "sort": comment_sort})
        url = "{0}?{1}".format(post["permalink"], params)
        comments = parse_comments(
            fetch_html(
                url,
                verify_ssl=verify_ssl,
                rate_limit_retries=rate_limit_retries,
                rate_limit_delay=rate_limit_delay,
            ),
            post,
        )

        for comment in comments:
            comment_id = comment["comment_id"]
            if comment_id in seen_comment_ids:
                continue

            seen_comment_ids.add(comment_id)
            if not comment["subreddit"]:
                comment["subreddit"] = subreddit
            rows.append(comment)
            if len(rows) >= max_comments:
                break

        time.sleep(delay)

    return rows


def read_existing_comment_ids(output_path):
    comment_ids = set()
    try:
        with open(output_path, newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                comment_id = row.get("comment_id", "").strip()
                if comment_id:
                    comment_ids.add(comment_id)
    except FileNotFoundError:
        pass

    return comment_ids


def write_csv(rows, output_path, append=False):
    write_header = True
    mode = "w"
    if append:
        mode = "a"
        write_header = not os.path.exists(output_path) or os.path.getsize(output_path) == 0

    with open(output_path, mode, newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape up to 1000 visible Reddit comments from subreddit HTML."
    )
    parser.add_argument("subreddit", help="Subreddit name, without the r/ prefix.")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="CSV output path. Defaults to reddit_<subreddit>_comments.csv.",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=1000,
        help="Maximum comments to scrape. Default: 1000.",
    )
    parser.add_argument(
        "--sort",
        choices=("hot", "new", "top", "rising"),
        default="hot",
        help="Post listing to scrape. Default: hot.",
    )
    parser.add_argument(
        "--comment-sort",
        choices=("confidence", "top", "new", "controversial", "old", "qa"),
        default="top",
        help="Comment sort to request on each inspected post. Default: top.",
    )
    parser.add_argument(
        "--post-limit",
        type=int,
        default=100,
        help="Maximum posts to inspect while collecting comments. Default: 100.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between post-comment requests. Default: 1.0.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append new comments to the output CSV and skip comment IDs already present.",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Keep scraping batches until stopped. Implies --append.",
    )
    parser.add_argument(
        "--batch-delay",
        type=float,
        default=1800.0,
        help="Seconds to wait between continuous batches. Default: 1800.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Maximum continuous batches to run. Default: 0, meaning no batch cap.",
    )
    parser.add_argument(
        "--rate-limit-retries",
        type=int,
        default=DEFAULT_RATE_LIMIT_RETRIES,
        help="Number of times to retry a request after HTTP 429. Default: 5.",
    )
    parser.add_argument(
        "--rate-limit-delay",
        type=float,
        default=DEFAULT_RATE_LIMIT_DELAY,
        help="Initial seconds to wait after HTTP 429 when Reddit does not send Retry-After. Default: 60.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification. Use only if your local Python certificates are broken.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    subreddit = args.subreddit.strip("/")
    if subreddit.startswith("r/"):
        subreddit = subreddit[2:]

    if args.max_comments < 1:
        print("--max-comments must be at least 1", file=sys.stderr)
        return 2
    if args.post_limit < 1:
        print("--post-limit must be at least 1", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("--delay must be 0 or greater", file=sys.stderr)
        return 2
    if args.batch_delay < 0:
        print("--batch-delay must be 0 or greater", file=sys.stderr)
        return 2
    if args.max_batches < 0:
        print("--max-batches must be 0 or greater", file=sys.stderr)
        return 2
    if args.rate_limit_retries < 0:
        print("--rate-limit-retries must be 0 or greater", file=sys.stderr)
        return 2
    if args.rate_limit_delay < 0:
        print("--rate-limit-delay must be 0 or greater", file=sys.stderr)
        return 2

    output = args.output or f"reddit_{subreddit}_comments.csv"
    append = args.append or args.continuous
    seen_comment_ids = read_existing_comment_ids(output) if append else set()
    batch_number = 0

    try:
        while True:
            batch_number += 1
            try:
                rows = scrape_comments(
                    subreddit,
                    max_comments=args.max_comments,
                    sort=args.sort,
                    comment_sort=args.comment_sort,
                    post_limit=args.post_limit,
                    delay=args.delay,
                    verify_ssl=not args.insecure,
                    skip_comment_ids=seen_comment_ids,
                    rate_limit_retries=args.rate_limit_retries,
                    rate_limit_delay=args.rate_limit_delay,
                )
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                print(
                    "This version does not use Reddit's API. If Reddit returns HTTP 429, "
                    "the site is rate limiting direct HTML scraping from your network. "
                    "Try again later or use a larger --delay / --batch-delay.",
                    file=sys.stderr,
                )
                print(
                    "If Reddit returns HTTP 403, the site is blocking direct HTML scraping "
                    "from your network.",
                    file=sys.stderr,
                )
                print(
                    "If this is an SSL certificate error on an old Python install, try "
                    "running again with --insecure or update Python's certificates.",
                    file=sys.stderr,
                )

                if not args.continuous:
                    return 1

                print(
                    "Batch {0} failed; waiting {1:.1f} seconds before trying again.".format(
                        batch_number,
                        args.batch_delay,
                    ),
                    file=sys.stderr,
                )
                time.sleep(args.batch_delay)
                if args.max_batches and batch_number >= args.max_batches:
                    break
                continue

            write_csv(rows, output, append=append)
            for row in rows:
                comment_id = row.get("comment_id", "")
                if comment_id:
                    seen_comment_ids.add(comment_id)

            verb = "Appended" if append else "Wrote"
            print("{0} {1} comments to {2}".format(verb, len(rows), output))

            if not args.continuous:
                return 0
            if args.max_batches and batch_number >= args.max_batches:
                break

            print(
                "Batch {0} complete; waiting {1:.1f} seconds before the next batch.".format(
                    batch_number,
                    args.batch_delay,
                )
            )
            time.sleep(args.batch_delay)
    except KeyboardInterrupt:
        print("Stopped by user.", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
