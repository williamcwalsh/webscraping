# Reddit Comment Scraper

Scrapes comments from a specific subreddit into a CSV file using Reddit's JSON API. It records each comment's text, subreddit, upvotes, downvotes when available, score, and UTC posting date.

Reddit often blocks anonymous `.json` scraping with HTTP 403. For reliable runs, create a Reddit API app and run the scraper with OAuth credentials.

## Reddit API Setup

1. Go to <https://www.reddit.com/prefs/apps>.
2. Click **create another app...**.
3. Choose **script**.
4. Set any name, description, and redirect URI. For local scraping, `http://localhost:8080` is fine.
5. Copy the client ID below the app name and the client secret.
6. Set them in your terminal:

```bash
export REDDIT_CLIENT_ID="your_client_id"
export REDDIT_CLIENT_SECRET="your_client_secret"
export REDDIT_USER_AGENT="macos:reddit-comment-scraper:v1.0 by /u/your_reddit_username"
```

## Usage

```bash
python3 reddit_scraper.py AskReddit
```

By default this writes up to 1000 comments to `reddit_AskReddit_comments.csv`.

You can also pass credentials directly:

```bash
python3 reddit_scraper.py AskReddit --client-id YOUR_ID --client-secret YOUR_SECRET
```

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

Note: Reddit usually exposes comment score/upvotes, but true downvote counts are often hidden or returned as `0`.
