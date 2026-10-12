# DemonicScans-dl

Downloads chapters from DemonicScans and saves each one as a `.cbz` file.

## Requirements

- Python 3
- Google Chrome (Selenium finds or downloads a matching chromedriver on its own)

```sh
pip install requests selenium
```

## Usage

Download a single chapter (a `/title/` URL):

```sh
python scraper.py "https://example.com/title/manga/chapter-106"
```

Download all chapters (a `/manga/` URL):

```sh
python scraper.py "https://example.com/manga/manga-name"
```

Start at a specific chapter, replacing any CBZ files that already exist:

```sh
python scraper.py --overwrite --start-chapter "Chapter 106" "https://example.com/manga/manga-name"
```

Chapters are saved to `out/<Series Name>/<Chapter>.cbz`, and chapters that already exist are skipped.

### Options

| Option | What it does |
| --- | --- |
| `--start-chapter TEXT` | Start at the first chapter whose title contains `TEXT`, e.g. `"Chapter 106"` |
| `--reverse` | Download chapters in reverse order |
| `--overwrite` | Replace `.cbz` files that already exist |
| `--output DIR` | Put series folders in `DIR` instead of `out/` |
| `--log-dir DIR` | Put error logs in `DIR` instead of `out/logs/` |

Run `python scraper.py --help` for the full list.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Everything was saved or already existed |
| 1 | Bad arguments |
| 2 | Something failed (see the error log) |
| 130 | Interrupted |
