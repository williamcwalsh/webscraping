#!/usr/bin/env python3
"""Scrape visible Reddit comments from subreddit HTML pages.

This intentionally does not use Reddit's API or JSON endpoints. It parses
old.reddit.com HTML, which means Reddit may still block requests and fields
that are not visible in the page, such as true downvotes, cannot be recovered.
When Reddit includes vote attributes in the HTML, they are written to the CSV.
"""

import argparse
import csv
import ssl
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


BASE_URL = "https://old.reddit.com"
USER_AGENT = "Mozilla/5.0 (compatible; research-comment-scraper/1.0)"


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


def fetch_html(url, verify_ssl=True):
    context = None if verify_ssl else ssl._create_unverified_context()
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    try:
        with urlopen(request, timeout=30, context=context) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(
            "Reddit returned HTTP {0} for {1}: {2}".format(
                exc.code, url, short_error_body(exc)
            )
        ) from exc
    except URLError as exc:
        raise RuntimeError("Could not reach Reddit at {0}: {1}".format(url, exc.reason)) from exc


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


def iter_subreddit_posts(subreddit, sort, post_limit, verify_ssl=True):
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
        posts = parse_posts(fetch_html(url, verify_ssl=verify_ssl))
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
    post_limit: int,
    delay: float,
    verify_ssl: bool,
) -> List[Dict[str, Any]]:
    rows = []  # type: List[Dict[str, Any]]

    for post in iter_subreddit_posts(subreddit, sort, post_limit, verify_ssl=verify_ssl):
        if len(rows) >= max_comments:
            break

        params = urlencode({"limit": 500, "sort": "top"})
        url = "{0}?{1}".format(post["permalink"], params)
        comments = parse_comments(fetch_html(url, verify_ssl=verify_ssl), post)

        for comment in comments:
            if not comment["subreddit"]:
                comment["subreddit"] = subreddit
            rows.append(comment)
            if len(rows) >= max_comments:
                break

        time.sleep(delay)

    return rows


def write_csv(rows, output_path):
    fieldnames = [
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

    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
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
        default=15000,
        help="Maximum comments to scrape. Default: 1000.",
    )
    parser.add_argument(
        "--sort",
        choices=("hot", "new", "top", "rising"),
        default="hot",
        help="Post listing to scrape. Default: hot.",
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

    output = args.output or f"reddit_{subreddit}_comments.csv"
    try:
        rows = scrape_comments(
            subreddit,
            max_comments=args.max_comments,
            sort=args.sort,
            post_limit=args.post_limit,
            delay=args.delay,
            verify_ssl=not args.insecure,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print(
            "This version does not use Reddit's API. If Reddit returns HTTP 403, "
            "the site is blocking direct HTML scraping from your network.",
            file=sys.stderr,
        )
        print(
            "If this is an SSL certificate error on an old Python install, try "
            "running again with --insecure or update Python's certificates.",
            file=sys.stderr,
        )
        return 1

    write_csv(rows, output)
    print(f"Wrote {len(rows)} comments to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
