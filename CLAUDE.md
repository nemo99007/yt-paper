# yt-paper — notes for Claude Code

Owner: Nemo (YouTube "Nemo Reading Archive", https://www.youtube.com/@99007nemo). Talks in Korean;
answer in Korean. Wants clarifying questions when something is ambiguous, but results, not
option lists, when the next step is obvious.

## What this is
Paper-reading archive. Each paper the owner has read (and usually recorded a YouTube reading video for)
gets one canonical page with a representative figure and an English summary paired paragraph-by-paragraph
with a Korean translation. Published with MkDocs Material to GitHub Pages.

- Repo: git@github.com:nemo99007/yt-paper.git (branch main) · Site: https://nemo99007.github.io/yt-paper/
- Deploy: push to main → `.github/workflows/deploy.yml` → gh-pages. Phone adds via the
  "Add resource (mobile)" workflow (needs repo secret ANTHROPIC_API_KEY).

## Layout decisions (agreed with the owner)
- Hierarchy: Home → collection (DVCon / DATE / IEEE / arXiv / Others) → region → year; each collection
  also has By Year and By Topic pages. Papers live once in `docs/papers/`; hubs only link to them.
- Site language English. Paper page order: breadcrumb, title, representative figure, Original paper button,
  meta table, Summary (EN|KO), Key Insights (EN|KO per bullet), Detailed Summary (EN|KO per paragraph/bullet,
  mermaid shown once full-width), Videos.
- Only summaries are published — never full paper text or full translations (copyright).
- Date shown for a paper = first YouTube upload of its videos (`yt-meta` syncs upload dates).
- A paper read twice: link the latest video; its note links the previous one
  (`[Previous reading (YYYY-MM-DD)](https://youtu.be/ID)` in the CSV timestamp column).

## Data
- `data/reading_list.csv` — the read list (source URL, local_pdf, youtube, title, type, timestamp,
  venue, region, year, optional figure number). `python -m src.cli import` is idempotent.
- `data/index_dump.json` — committed DB dump; `data/archive.db` is rebuilt from it when missing.
- DVCon PDFs: `~/works/dvcon/papers/{year}/{location}/` with `index.csv` (read-only source; a separate
  project — do not move it into this repo).
- `secrets/` (gitignored): Google OAuth client_secret*.json + youtube_token.json (scope youtube.force-ssl).

## Commands
    python -m src.cli import [--refresh] [--limit N] [--push]   # batch from reading_list.csv
    python -m src.cli add --source URL|PDF [--youtube URL] [--push]
    python -m src.cli link --ref-id ID --youtube URL
    python -m src.cli rebuild [--push]
    python -m src.cli yt-auth | yt-meta | yt-desc [--video ID] [--apply]
    mkdocs build --strict

Summaries: `LLM_BACKEND=auto` uses `claude -p` (owner's subscription, model from CLI_MODEL in .env —
owner chose **haiku**) when no ANTHROPIC_API_KEY is set. If the CLI login expires the batch stops;
owner runs /login, then re-run (idempotent).

## Open items (as of 2026-10-18)
1. YouTube descriptions: `yt-desc` previews 101 videos; owner approved "structure first, then apply".
   Apply 2 first (ZOCbWSYhnJM, l5LVIft6xNs), let the owner check, then the rest. End screens/cards are not
   available in the API — description links + playlists instead.
2. Find non-DVCon read papers (DATE, IEEE, arXiv, classics like Tomasulo / Alpha 21264 / MIPS R10000)
   by extracting source links from the channel's video descriptions; add to reading_list.csv.
3. DVCon official presentation videos: link them on paper pages where they exist.
4. Unmatched video: enlBLibl7wM "[DVCON2026] New RTL Modeling Constructs in Verilog" (no PDF in the
   DVCon download).
5. Figure crops are heuristic (caption-based); override with the `figure` column in the CSV.
