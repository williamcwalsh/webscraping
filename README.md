# Reddit Comment Scraper

Scrapes visible comments from a specific subreddit into a CSV file by parsing Reddit HTML pages. This version does not use Reddit's API, OAuth, or JSON endpoints.

It records each comment's text, subreddit, visible upvote/score fields when present in the page, downvotes when present, and posting date.

## Usage

```bash
python3 reddit_scraper.py AskReddit
```

By default this writes up to 1000 comments to `reddit_AskReddit_comments.csv`.

Useful options:

```bash
python3 reddit_scraper.py learnpython --sort new --max-comments 1000 --post-limit 200 --output learnpython_comments.csv
```

To keep collecting throughout the day, run in continuous append mode. This
writes up to 1000 new comments per batch, skips comment IDs already present in
the CSV, and sleeps between batches:

```bash
python3 reddit_scraper.py Embedded --sort new --continuous --delay 5 --batch-delay 1800 --rate-limit-retries 8 --rate-limit-delay 120
```

Use `Ctrl+C` to stop a continuous run. The scraper honors HTTP 429 rate-limit
responses by waiting and retrying, but direct HTML scraping can still be blocked
or rate limited by Reddit.

If an older local Python install fails with an SSL certificate verification error, either update Python's certificates or run:

```bash
python3 reddit_scraper.py learnpython --insecure
```

Columns:

- `comment_id`
- `post_id`
- `post_title`
- `post_permalink`
- `subreddit`
- `comment`
- `upvotes`
- `downvotes`
- `score`
- `date_posted`

## Notes

Reddit may still block direct HTML scraping with HTTP 403. If that happens, the scraper cannot bypass the block without using an approved API path or a different network.

Reddit does not publicly show true comment downvote counts in normal page HTML, so `downvotes` is often blank.

## User Comments In One Subreddit

To scrape comments from a specific user in a specific subreddit:

```bash
python3 reddit_user_subreddit_scraper.py username AskReddit
```

By default this paginates through the user's visible comment history and writes up to 100 matching comments to `reddit_<username>_<subreddit>_comments.csv`.

Useful options:

```bash
python3 reddit_user_subreddit_scraper.py username learnpython --max-comments 50 --output user_learnpython_comments.csv
```

If this command returns HTTP 404 for the `/user/<username>/comments/` URL,
old Reddit did not return a public comment listing for that username. Check the
spelling first; the account may also be deleted, suspended, or unavailable on
old Reddit.

Columns:

- `comment_id`
- `post_id`
- `post_title`
- `post_permalink`
- `subreddit`
- `comment`
- `score`
- `date_posted`

This user/subreddit scraper keeps only `score`, not separate `upvotes` or `downvotes`.

## Active Users In One Subreddit

To find users with at least 100 visible comments in a subreddit:

```bash
python3 reddit_active_users_scraper.py AskReddit
```

By default this checks up to 1000 unique usernames while inspecting up to 500
recent posts, then writes up to 100 qualifying users to
`reddit_<subreddit>_active_users.csv`.

Useful options:

```bash
python3 reddit_active_users_scraper.py learnpython --max-users-checked 2000 --max-users-recorded 50 --post-limit 1000
```

If Reddit returns HTTP 429, it is rate limiting the scraper. The active-user
scraper retries 429 responses by default, but a slower run is usually gentler:

```bash
python3 reddit_active_users_scraper.py embedded --delay 5 --rate-limit-retries 8 --rate-limit-delay 90
```

Columns:

- `username`
- `comment_count`

This scraper counts visible comments on the inspected post pages. Comments
hidden behind Reddit's "load more comments" controls are not included.
