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
