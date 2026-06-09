#!/usr/bin/env python3
"""Scrape Reddit comments from a subreddit into a CSV file.

Uses Reddit's JSON endpoints. Reddit commonly blocks anonymous scraping, so
OAuth app credentials are recommended. Reddit does not reliably expose true
downvote counts.
"""

import argparse
import base64
import csv
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PUBLIC_BASE_URL = "https://old.reddit.com"
OAUTH_BASE_URL = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
USER_AGENT = "webscraping-research-script/1.0"


class RedditClient:
    def __init__(
        self,
        client_id=None,
        client_secret=None,
        user_agent=USER_AGENT,
        verify_ssl=True,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_agent = user_agent
        self.verify_ssl = verify_ssl
        self.access_token = None
        self.base_url = OAUTH_BASE_URL if client_id and client_secret else PUBLIC_BASE_URL

    def context(self):
        return None if self.verify_ssl else ssl._create_unverified_context()

    def headers(self):
        headers = {"User-Agent": self.user_agent}
        if self.access_token:
            headers["Authorization"] = "Bearer {0}".format(self.access_token)
        return headers

    def authenticate(self):
        if not self.client_id or not self.client_secret:
            return

        auth = "{0}:{1}".format(self.client_id, self.client_secret).encode("utf-8")
        headers = {
            "Authorization": "Basic {0}".format(base64.b64encode(auth).decode("ascii")),
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = urlencode({"grant_type": "client_credentials"}).encode("utf-8")
        request = Request(TOKEN_URL, data=body, headers=headers)

        try:
            with urlopen(request, timeout=30, context=self.context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                "Reddit OAuth failed with HTTP {0}: {1}".format(
                    exc.code, short_error_body(exc)
                )
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                "Could not reach Reddit OAuth at {0}: {1}".format(TOKEN_URL, exc.reason)
            ) from exc

        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Reddit OAuth response did not include an access token.")
        self.access_token = token

    def get(self, path, params=None):
        if self.client_id and self.client_secret and not self.access_token:
            self.authenticate()

        query = "?{0}".format(urlencode(params)) if params else ""
        url = "{0}{1}{2}".format(self.base_url, path, query)
        request = Request(url, headers=self.headers())

        try:
            with urlopen(request, timeout=30, context=self.context()) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                "Reddit returned HTTP {0} for {1}: {2}".format(
                    exc.code, url, short_error_body(exc)
                )
            ) from exc
        except URLError as exc:
            raise RuntimeError("Could not reach Reddit at {0}: {1}".format(url, exc.reason)) from exc


def short_error_body(exc):
    message = exc.read().decode("utf-8", errors="replace").strip()
    if len(message) > 500:
        message = message[:500] + "..."
    return message


def iter_subreddit_posts(client, subreddit, sort, post_limit):
    fetched = 0
    after = None

    while fetched < post_limit:
        batch_limit = min(100, post_limit - fetched)
        params = {"limit": batch_limit, "raw_json": 1}
        if after:
            params["after"] = after

        listing = client.get(
            f"/r/{subreddit}/{sort}.json",
            params,
        )
        data = listing.get("data", {})
        children = data.get("children", [])

        if not children:
            break

        for child in children:
            if child.get("kind") == "t3":
                fetched += 1
                yield child["data"]

        after = data.get("after")
        if not after:
            break


def flatten_comments(children):
    stack = list(reversed(children))

    while stack:
        child = stack.pop()
        if child.get("kind") != "t1":
            continue

        comment = child.get("data", {})
        yield comment

        replies = comment.get("replies")
        if isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            stack.extend(reversed(reply_children))


def iso_date(timestamp):
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def scrape_comments(
    subreddit: str,
    *,
    max_comments: int,
    sort: str,
    post_limit: int,
    delay: float,
    client: RedditClient,
) -> List[Dict[str, Any]]:
    rows = []  # type: List[Dict[str, Any]]

    for post in iter_subreddit_posts(client, subreddit, sort, post_limit):
        if len(rows) >= max_comments:
            break

        post_id = post["id"]
        params = {"limit": 500, "sort": "top", "raw_json": 1}
        thread = client.get(
            f"/r/{subreddit}/comments/{post_id}.json",
            params,
        )

        if len(thread) < 2:
            continue

        comments = thread[1].get("data", {}).get("children", [])
        for comment in flatten_comments(comments):
            body = comment.get("body", "")
            if not body or body in {"[deleted]", "[removed]"}:
                continue

            rows.append(
                {
                    "comment_id": comment.get("id", ""),
                    "post_id": post_id,
                    "post_title": post.get("title", ""),
                    "post_permalink": f"{PUBLIC_BASE_URL}{post.get('permalink', '')}",
                    "subreddit": comment.get("subreddit") or subreddit,
                    "comment": body,
                    "upvotes": comment.get("ups", ""),
                    "downvotes": comment.get("downs", ""),
                    "score": comment.get("score", ""),
                    "date_posted": iso_date(comment.get("created_utc")),
                }
            )

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
        description="Scrape up to 1000 Reddit comments from a specific subreddit."
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
        "--client-id",
        default=os.environ.get("REDDIT_CLIENT_ID"),
        help="Reddit app client ID. Can also be set with REDDIT_CLIENT_ID.",
    )
    parser.add_argument(
        "--client-secret",
        default=os.environ.get("REDDIT_CLIENT_SECRET"),
        help="Reddit app client secret. Can also be set with REDDIT_CLIENT_SECRET.",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("REDDIT_USER_AGENT", USER_AGENT),
        help="Reddit API user agent. Can also be set with REDDIT_USER_AGENT.",
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
    client = RedditClient(
        client_id=args.client_id,
        client_secret=args.client_secret,
        user_agent=args.user_agent,
        verify_ssl=not args.insecure,
    )
    try:
        rows = scrape_comments(
            subreddit,
            max_comments=args.max_comments,
            sort=args.sort,
            post_limit=args.post_limit,
            delay=args.delay,
            client=client,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        if not args.client_id or not args.client_secret:
            print(
                "Reddit often blocks anonymous JSON scraping with HTTP 403. "
                "Create a Reddit app and set REDDIT_CLIENT_ID and "
                "REDDIT_CLIENT_SECRET, or pass --client-id and --client-secret.",
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
