#!/usr/bin/env python3
"""Scrape one Reddit user's visible comments from a specific subreddit.

This intentionally does not use Reddit's API or JSON endpoints. It parses
old.reddit.com HTML, so Reddit may still block requests and only comments
visible in the user's public comment listing can be recovered.
"""

import argparse
import csv
import re
import ssl
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


BASE_URL = "https://old.reddit.com"
USER_AGENT = "Mozilla/5.0 (compatible; research-user-comment-scraper/1.0)"
COMMENT_CAP = 100


def classes(attrs: Dict[str, str]) -> set:
    return set(attrs.get("class", "").split())


def short_error_body(exc: HTTPError) -> str:
    message = exc.read().decode("utf-8", errors="replace").strip()
    if len(message) > 500:
        message = message[:500] + "..."
    return message


def clean_vote_value(value: str) -> str:
    if not value:
        return ""

    value = value.replace(",", "").strip()
    return value if value.lstrip("-").isdigit() else ""


def score_from_text(value: str) -> str:
    value = " ".join(value.split()).lower()
    if not value or value in {"score hidden", "[score hidden]"}:
        return ""

    first_word = value.split()[0].replace(",", "")
    if first_word.lstrip("-").isdigit():
        return first_word
    return ""


def normalize_date(value: str) -> str:
    if not value:
        return ""

    try:
        parsed = datetime.strptime(value.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return parsed.isoformat()
    except ValueError:
        return value


def fetch_html(url: str, verify_ssl: bool = True) -> str:
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


def post_details_from_url(url: str) -> Tuple[str, str]:
    absolute = urljoin(BASE_URL, url)
    parsed = urlparse(absolute)
    parts = [part for part in parsed.path.split("/") if part]

    try:
        comments_index = parts.index("comments")
    except ValueError:
        return "", absolute

    if comments_index + 1 >= len(parts):
        return "", absolute

    post_id = parts[comments_index + 1]
    post_parts = parts[: comments_index + 3]
    post_path = "/" + "/".join(post_parts) + "/"
    post_permalink = urlunparse((parsed.scheme, parsed.netloc, post_path, "", "", ""))
    return post_id, post_permalink


class UserCommentListingParser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.comments = []  # type: List[Dict[str, str]]
        self.current = None  # type: Optional[Dict[str, Any]]
        self.div_depth = 0
        self.next_href = ""
        self.in_next_button = False

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {name: value or "" for name, value in attrs_list}
        class_names = classes(attrs)

        if tag == "span" and "next-button" in class_names:
            self.in_next_button = True

        if self.in_next_button and tag == "a" and attrs.get("href"):
            self.next_href = urljoin(BASE_URL, attrs["href"])

        if self.current is not None and tag == "div":
            self.div_depth += 1

        if (
            self.current is None
            and tag == "div"
            and "thing" in class_names
            and "comment" in class_names
        ):
            post_id = attrs.get("data-link-id", "").replace("t3_", "")
            post_permalink = ""
            if attrs.get("data-permalink"):
                parsed_post_id, parsed_permalink = post_details_from_url(attrs["data-permalink"])
                post_id = post_id or parsed_post_id
                post_permalink = parsed_permalink

            self.current = {
                "comment_id": attrs.get("data-fullname", "").replace("t1_", ""),
                "fullname": attrs.get("data-fullname", ""),
                "post_id": post_id,
                "post_title": "",
                "post_permalink": post_permalink,
                "subreddit": attrs.get("data-subreddit", ""),
                "comment": "",
                "score": clean_vote_value(attrs.get("data-score", "")),
                "date_posted": "",
                "body_parts": [],
                "body_depth": 0,
                "capture_score": False,
                "score_parts": [],
                "score_priority": 0,
                "pending_score_priority": 0,
                "capture_title": False,
                "title_parts": [],
                "parent_depth": 0,
            }
            self.div_depth = 1
            return

        if self.current is None:
            return

        current = self.current

        if tag == "time" and not current["date_posted"]:
            current["date_posted"] = attrs.get("datetime", "")

        if tag == "p" and "parent" in class_names:
            current["parent_depth"] = 1
        elif current["parent_depth"] > 0 and tag in {"p", "span", "a"}:
            current["parent_depth"] += 1

        if tag == "a" and (
            "title" in class_names or (current["parent_depth"] > 0 and not current["post_title"])
        ):
            current["capture_title"] = True
            current["title_parts"] = []
            if attrs.get("href"):
                post_id, post_permalink = post_details_from_url(attrs["href"])
                current["post_id"] = current["post_id"] or post_id
                current["post_permalink"] = current["post_permalink"] or post_permalink

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

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self.in_next_button:
            self.in_next_button = False

        if self.current is None:
            return

        current = self.current

        if tag == "a" and current["capture_title"]:
            title = " ".join("".join(current["title_parts"]).split())
            if title:
                current["post_title"] = title
            current["capture_title"] = False
            current["title_parts"] = []

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

        if current["parent_depth"] > 0 and tag in {"p", "span", "a"}:
            current["parent_depth"] -= 1

        if tag == "div":
            self.div_depth -= 1
            if self.div_depth <= 0:
                body = " ".join("".join(current["body_parts"]).split())
                if body and body not in {"[deleted]", "[removed]"}:
                    self.comments.append(
                        {
                            "comment_id": current["comment_id"],
                            "fullname": current["fullname"],
                            "post_id": current["post_id"],
                            "post_title": current["post_title"],
                            "post_permalink": current["post_permalink"],
                            "subreddit": current["subreddit"],
                            "comment": body,
                            "score": current["score"],
                            "date_posted": normalize_date(current["date_posted"]),
                        }
                    )
                self.current = None
                self.div_depth = 0

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return

        current = self.current
        if current["body_depth"] > 0:
            current["body_parts"].append(data)
        elif current["capture_score"]:
            current["score_parts"].append(data)
        elif current["capture_title"]:
            current["title_parts"].append(data)


def parse_user_comments(html: str) -> Tuple[List[Dict[str, str]], str]:
    parser = UserCommentListingParser()
    parser.feed(html)
    return parser.comments, parser.next_href


def normalize_subreddit(value: str) -> str:
    value = value.strip().strip("/")
    if value.lower().startswith("r/"):
        value = value[2:]
    return value


def normalize_username(value: str) -> str:
    value = value.strip().strip("/")
    value = re.sub(r"^u/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^user/", "", value, flags=re.IGNORECASE)
    return value


def default_output_path(username: str, subreddit: str) -> str:
    safe_username = re.sub(r"[^A-Za-z0-9_-]+", "_", username).strip("_") or "user"
    safe_subreddit = re.sub(r"[^A-Za-z0-9_]+", "_", subreddit).strip("_") or "subreddit"
    return "reddit_{0}_{1}_comments.csv".format(safe_username, safe_subreddit)


def scrape_user_subreddit_comments(
    username: str,
    subreddit: str,
    *,
    sort: str,
    max_comments: int,
    max_pages: int,
    delay: float,
    verify_ssl: bool,
) -> List[Dict[str, str]]:
    rows = []  # type: List[Dict[str, str]]
    seen_comment_ids = set()
    target_subreddit = subreddit.lower()
    page_number = 0
    url = "{0}/user/{1}/comments/?{2}".format(
        BASE_URL,
        quote(username, safe=""),
        urlencode({"sort": sort}),
    )

    while url:
        page_number += 1
        if max_pages and page_number > max_pages:
            break

        comments, next_url = parse_user_comments(fetch_html(url, verify_ssl=verify_ssl))
        if not comments:
            break

        for comment in comments:
            if comment["subreddit"].lower() != target_subreddit:
                continue
            if comment["comment_id"] in seen_comment_ids:
                continue

            seen_comment_ids.add(comment["comment_id"])
            rows.append({key: comment[key] for key in csv_fieldnames()})
            if len(rows) >= max_comments:
                return rows

        if not next_url:
            break

        url = next_url
        if delay > 0:
            time.sleep(delay)

    return rows


def csv_fieldnames() -> List[str]:
    return [
        "comment_id",
        "post_id",
        "post_title",
        "post_permalink",
        "subreddit",
        "comment",
        "score",
        "date_posted",
    ]


def write_csv(rows: List[Dict[str, str]], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_fieldnames())
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape one user's visible Reddit comments from a subreddit."
    )
    parser.add_argument("username", help="Reddit username, without the u/ prefix.")
    parser.add_argument("subreddit", help="Subreddit name, without the r/ prefix.")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="CSV output path. Defaults to reddit_<username>_<subreddit>_comments.csv.",
    )
    parser.add_argument(
        "--sort",
        choices=("new", "top", "controversial"),
        default="new",
        help="User comment listing sort. Default: new.",
    )
    parser.add_argument(
        "--max-comments",
        type=int,
        default=COMMENT_CAP,
        help="Maximum matching comments to write, up to 100. Default: 100.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum user-comment listing pages to inspect. Default: 0, meaning no page cap.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between user-comment listing requests. Default: 1.0.",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification. Use only if your local Python certificates are broken.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    username = normalize_username(args.username)
    subreddit = normalize_subreddit(args.subreddit)

    if not username:
        print("username is required", file=sys.stderr)
        return 2
    if not subreddit:
        print("subreddit is required", file=sys.stderr)
        return 2
    if args.max_comments < 1 or args.max_comments > COMMENT_CAP:
        print("--max-comments must be between 1 and {0}".format(COMMENT_CAP), file=sys.stderr)
        return 2
    if args.max_pages < 0:
        print("--max-pages must be 0 or greater", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("--delay must be 0 or greater", file=sys.stderr)
        return 2

    output = args.output or default_output_path(username, subreddit)
    try:
        rows = scrape_user_subreddit_comments(
            username,
            subreddit,
            sort=args.sort,
            max_comments=args.max_comments,
            max_pages=args.max_pages,
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
    print("Wrote {0} comments to {1}".format(len(rows), output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
