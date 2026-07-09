#!/usr/bin/env python3
"""Find active Reddit users in one subreddit by parsing old.reddit.com HTML.

This intentionally does not use Reddit's API or JSON endpoints. It counts
visible comments from inspected subreddit posts, so Reddit may still block
requests and comments hidden behind "load more comments" controls are not
included.
"""

import argparse
import csv
import re
import ssl
import sys
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen


BASE_URL = "https://old.reddit.com"
USER_AGENT = "Mozilla/5.0 (compatible; research-active-user-scraper/1.0)"
DEFAULT_MIN_COMMENTS = 10
DEFAULT_MAX_USERS_CHECKED = 100
DEFAULT_MAX_USERS_RECORDED = 100
DEFAULT_POST_LIMIT = 500
COMMENTS_PER_POST = 500
DEFAULT_RATE_LIMIT_RETRIES = 5
DEFAULT_RATE_LIMIT_DELAY = 60.0
RATE_LIMIT_BACKOFF = 2.0


def classes(attrs: Dict[str, str]) -> set:
    return set(attrs.get("class", "").split())


def short_error_body(exc: HTTPError) -> str:
    message = exc.read().decode("utf-8", errors="replace").strip()
    if len(message) > 500:
        message = message[:500] + "..."
    return message


def url_for_request(url: str) -> str:
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


def retry_after_seconds(exc: HTTPError, fallback: float) -> float:
    headers = exc.headers or {}
    value = headers.get("Retry-After", "").strip()
    if not value:
        return fallback

    try:
        return max(float(value), 0.0)
    except ValueError:
        return fallback


def fetch_html(
    url: str,
    verify_ssl: bool = True,
    *,
    rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY,
) -> str:
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
                        "larger --delay / --rate-limit-delay.".format(
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
    def __init__(self) -> None:
        HTMLParser.__init__(self)
        self.posts = []  # type: List[Dict[str, str]]
        self.current = None  # type: Optional[Dict[str, str]]
        self.div_depth = 0
        self.capture_title = False
        self.title_parts = []  # type: List[str]

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {name: value or "" for name, value in attrs_list}
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

    def handle_endtag(self, tag: str) -> None:
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

    def handle_data(self, data: str) -> None:
        if self.capture_title:
            self.title_parts.append(data)


class CommentAuthorParser(HTMLParser):
    def __init__(self) -> None:
        HTMLParser.__init__(self)
        self.comments = []  # type: List[Dict[str, str]]
        self.stack = []  # type: List[Dict[str, Any]]
        self.capture_author = False
        self.author_parts = []  # type: List[str]

    def handle_starttag(self, tag: str, attrs_list: List[Tuple[str, Optional[str]]]) -> None:
        attrs = {name: value or "" for name, value in attrs_list}
        class_names = classes(attrs)

        if tag == "div":
            for comment in self.stack:
                comment["depth"] += 1

            if "thing" in class_names and "comment" in class_names:
                self.stack.append(
                    {
                        "depth": 1,
                        "comment_id": attrs.get("data-fullname", "").replace("t1_", ""),
                        "username": attrs.get("data-author", "").strip(),
                        "subreddit": attrs.get("data-subreddit", "").strip(),
                    }
                )
                return

        if (
            self.stack
            and tag == "a"
            and "author" in class_names
            and not self.stack[-1]["username"]
        ):
            self.capture_author = True
            self.author_parts = []

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            return

        if tag == "a" and self.capture_author:
            self.stack[-1]["username"] = " ".join("".join(self.author_parts).split())
            self.capture_author = False
            self.author_parts = []

        if tag == "div":
            for comment in list(self.stack):
                comment["depth"] -= 1

            while self.stack and self.stack[-1]["depth"] <= 0:
                finished = self.stack.pop()
                username = finished["username"].strip()
                comment_id = finished["comment_id"].strip()
                if username and username != "[deleted]" and comment_id:
                    self.comments.append(
                        {
                            "comment_id": comment_id,
                            "username": username,
                            "subreddit": finished["subreddit"],
                        }
                    )

    def handle_data(self, data: str) -> None:
        if self.capture_author:
            self.author_parts.append(data)


def parse_posts(html: str) -> List[Dict[str, str]]:
    parser = PostListingParser()
    parser.feed(html)
    return parser.posts


def parse_comment_authors(html: str) -> List[Dict[str, str]]:
    parser = CommentAuthorParser()
    parser.feed(html)
    return parser.comments


def normalize_subreddit(value: str) -> str:
    value = value.strip().strip("/")
    if value.lower().startswith("r/"):
        value = value[2:]
    return value


def default_output_path(subreddit: str) -> str:
    safe_subreddit = re.sub(r"[^A-Za-z0-9_]+", "_", subreddit).strip("_") or "subreddit"
    return "reddit_{0}_active_users.csv".format(safe_subreddit)


def iter_subreddit_posts(
    subreddit: str,
    sort: str,
    post_limit: int,
    verify_ssl: bool = True,
    rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
    rate_limit_delay: float = DEFAULT_RATE_LIMIT_DELAY,
):
    fetched = 0
    after = None

    while fetched < post_limit:
        params = {"count": fetched}
        if after:
            params["after"] = after

        url = "{0}/r/{1}/{2}/?{3}".format(
            BASE_URL,
            quote(subreddit, safe=""),
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


def find_active_users(
    subreddit: str,
    *,
    min_comments: int,
    max_users_checked: int,
    max_users_recorded: int,
    post_limit: int,
    sort: str,
    comment_sort: str,
    delay: float,
    rate_limit_retries: int,
    rate_limit_delay: float,
    verify_ssl: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    counts = {}  # type: Dict[str, int]
    usernames = {}  # type: Dict[str, str]
    seen_comment_ids = set()
    target_subreddit = subreddit.lower()
    stats = {
        "posts_inspected": 0,
        "visible_comments_seen": 0,
        "comments_counted": 0,
        "new_user_comments_skipped": 0,
    }

    for post in iter_subreddit_posts(
        subreddit,
        sort,
        post_limit,
        verify_ssl=verify_ssl,
        rate_limit_retries=rate_limit_retries,
        rate_limit_delay=rate_limit_delay,
    ):
        stats["posts_inspected"] += 1

        params = urlencode({"limit": COMMENTS_PER_POST, "sort": comment_sort})
        url = "{0}?{1}".format(post["permalink"], params)
        comments = parse_comment_authors(
            fetch_html(
                url,
                verify_ssl=verify_ssl,
                rate_limit_retries=rate_limit_retries,
                rate_limit_delay=rate_limit_delay,
            )
        )

        for comment in comments:
            comment_id = comment["comment_id"]
            if comment_id in seen_comment_ids:
                continue
            seen_comment_ids.add(comment_id)

            comment_subreddit = comment["subreddit"].lower()
            if comment_subreddit and comment_subreddit != target_subreddit:
                continue

            username = comment["username"].strip()
            if not username or username == "[deleted]":
                continue

            stats["visible_comments_seen"] += 1
            username_key = username.lower()
            if username_key not in usernames:
                if len(usernames) >= max_users_checked:
                    stats["new_user_comments_skipped"] += 1
                    continue
                usernames[username_key] = username
                counts[username_key] = 0

            counts[username_key] += 1
            stats["comments_counted"] += 1

        if delay > 0:
            time.sleep(delay)

    rows = [
        {"username": usernames[username_key], "comment_count": counts[username_key]}
        for username_key in counts
        if counts[username_key] >= min_comments
    ]
    rows.sort(key=lambda row: (-int(row["comment_count"]), row["username"].lower()))
    stats["users_checked"] = len(usernames)
    stats["users_recorded"] = min(len(rows), max_users_recorded)
    return rows[:max_users_recorded], stats


def csv_fieldnames() -> List[str]:
    return ["username", "comment_count"]


def write_csv(rows: List[Dict[str, Any]], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_fieldnames())
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find users with at least 100 visible comments in a subreddit by "
            "scraping old.reddit.com HTML."
        )
    )
    parser.add_argument("subreddit", help="Subreddit name, without the r/ prefix.")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="CSV output path. Defaults to reddit_<subreddit>_active_users.csv.",
    )
    parser.add_argument(
        "--min-comments",
        type=int,
        default=DEFAULT_MIN_COMMENTS,
        help="Minimum counted comments required to record a user. Default: 100.",
    )
    parser.add_argument(
        "--max-users-checked",
        type=int,
        default=DEFAULT_MAX_USERS_CHECKED,
        help="Maximum unique usernames to track while scraping. Default: 1000.",
    )
    parser.add_argument(
        "--max-users-recorded",
        type=int,
        default=DEFAULT_MAX_USERS_RECORDED,
        help="Maximum qualifying usernames to write. Default: 100.",
    )
    parser.add_argument(
        "--post-limit",
        type=int,
        default=DEFAULT_POST_LIMIT,
        help="Maximum subreddit posts to inspect. Default: 500.",
    )
    parser.add_argument(
        "--sort",
        choices=("hot", "new", "top", "rising"),
        default="new",
        help="Subreddit post listing to scrape. Default: new.",
    )
    parser.add_argument(
        "--comment-sort",
        choices=("confidence", "top", "new", "controversial", "old", "qa"),
        default="new",
        help="Comment sort to request on each inspected post. Default: new.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between post-comment requests. Default: 1.0.",
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


def main() -> int:
    args = parse_args()
    subreddit = normalize_subreddit(args.subreddit)

    if not subreddit:
        print("subreddit is required", file=sys.stderr)
        return 2
    if args.min_comments < 1:
        print("--min-comments must be at least 1", file=sys.stderr)
        return 2
    if args.max_users_checked < 1:
        print("--max-users-checked must be at least 1", file=sys.stderr)
        return 2
    if args.max_users_recorded < 1:
        print("--max-users-recorded must be at least 1", file=sys.stderr)
        return 2
    if args.post_limit < 1:
        print("--post-limit must be at least 1", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("--delay must be 0 or greater", file=sys.stderr)
        return 2
    if args.rate_limit_retries < 0:
        print("--rate-limit-retries must be 0 or greater", file=sys.stderr)
        return 2
    if args.rate_limit_delay < 0:
        print("--rate-limit-delay must be 0 or greater", file=sys.stderr)
        return 2

    output = args.output or default_output_path(subreddit)
    try:
        rows, stats = find_active_users(
            subreddit,
            min_comments=args.min_comments,
            max_users_checked=args.max_users_checked,
            max_users_recorded=args.max_users_recorded,
            post_limit=args.post_limit,
            sort=args.sort,
            comment_sort=args.comment_sort,
            delay=args.delay,
            rate_limit_retries=args.rate_limit_retries,
            rate_limit_delay=args.rate_limit_delay,
            verify_ssl=not args.insecure,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        print(
            "This version does not use Reddit's API. If Reddit returns HTTP 429, "
            "the site is rate limiting direct HTML scraping from your network. "
            "Try again later or use a larger --delay.",
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
        return 1

    write_csv(rows, output)
    print(
        "Wrote {0} active users to {1} after checking {2} users across {3} posts.".format(
            len(rows),
            output,
            stats["users_checked"],
            stats["posts_inspected"],
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
