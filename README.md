# homepage

A personal browser home page: shortcuts and search, plus two live sections —
world/tech/sport headlines and Formula 1.

## How it works

The page never makes a network request of its own. Every section reads a plain
JavaScript file that a fetcher already wrote to `data/`:

| File | Source | Refresh |
|---|---|---|
| `data/news.js` | RSS feeds listed in `tools/feeds.conf` | 30 min |
| `data/f1.js` | [Jolpica-F1](https://api.jolpi.ca) (Ergast successor) + F1 RSS | 1 hour |

That design exists because the page was written to be opened as a `file://`
document, where Safari blocks `fetch`/XHR but still loads sibling `<script>`
tags. It works unchanged over HTTP, so the same file serves locally and hosted.

News photos are downloaded into `data/img/` rather than hotlinked: remote images
do load on a `file://` page, but some CDNs refuse them (the Guardian's
`i.guim.co.uk` serves fine to a script and rejects a `file://` origin). Caching
keeps the page self-contained and working offline. Each section owns a
subdirectory and prunes only its own, so the fetchers never delete each other's
files.

## Running it locally

```sh
python3 tools/fetch_icons.py
python3 tools/fetch_news.py
python3 tools/fetch_f1.py
open index.html
```

No dependencies — the standard library only.

## Hosting

`.github/workflows/refresh.yml` runs the fetchers on a schedule and force-pushes
a single-commit `gh-pages` branch containing `index.html` and `data/`. Because
that branch is rewritten rather than appended to, the repository stays flat
instead of growing by the size of the image cache on every refresh.

`data/` is gitignored on `main`: `main` holds source, `gh-pages` holds output.

## Customising

- **Feeds** — edit `tools/feeds.conf` (`Category | Name | URL`). Re-read on every run.
- **Team colours** — the `TEAM` map near the F1 code in `index.html`.
- **Shortcuts and name** — stored in your browser's `localStorage`, never
  written to these files.
- **Default tiles** — edit `tools/shortcuts.conf`; `fetch_icons.py` caches each
  site's logo.
