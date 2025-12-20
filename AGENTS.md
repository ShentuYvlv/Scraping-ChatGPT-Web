# Repository Guidelines

## Project Structure & Module Organization

- `MCPfiles/`: Primary Python automation/scraper scripts (e.g., `deepseek_chat_scraper.py`, `doubao_chat_scraper.py`) plus browser artifacts like `*_cookies.json` / `*_storage.json` and templates (e.g., `Camoufox_template.py`).
- Repo root: dataset and prompt inputs/outputs such as `train_dataset.jsonl`, `test_dataset.jsonl`, `*_input_prompts.txt`, plus helpers like `generate_prompts.py`.
- Docs and notes: `README.md` (usage), `webhook.md` (network/SSE hook snippet), and `cursor-rules/` (process notes).

## Build, Test, and Development Commands

- Install dependencies (Python 3.10+ recommended):
  - `pip install camoufox mcp scrapy screeninfo`
  - `python -m camoufox fetch` (downloads the Camoufox runtime)
- Generate prompt lists from `*_dataset.jsonl`:
  - `python generate_prompts.py` → writes `train_input_prompts.txt` and `test_input_prompts.txt`
- Run scrapers from repo root:
  - `python MCPfiles/deepseek_chat_scraper.py`
  - `python MCPfiles/doubao_chat_scraper.py`
- Parallelization options (supported by the scrapers):
  - Manual shards: `--shard-index N --shard-count M`
  - Auto workers: `--spawn-workers M`
- Outputs are written under `output/` (e.g., `output/*_conversations_<task>.ndjson` and `.md`).

## Coding Style & Naming Conventions

- Python: 4-space indentation, `snake_case` for files/functions, and prefer f-strings.
- Keep paths repo-relative (assume scripts run from the repo root).
- Avoid committing generated artifacts unless explicitly needed (especially `output/` and local browser state files).

## Testing Guidelines

- No dedicated test runner is included. Validate changes with a small `*_input_prompts.txt` (a few lines) and a smoke run of the target scraper.
- Sanity-check output format by inspecting the produced `.ndjson` and `.md` in `output/`.

## Commit & Pull Request Guidelines

- Keep commits small and messages short/clear (repo history uses simple messages like `fix` / `Add ...`).
- PRs should include: what changed, how to run (`python MCPfiles/<script>.py` plus any flags), and a redacted sample output snippet or screenshot when relevant.

## Security & Configuration Tips

- Do not commit real cookies/storage/session data (`*_cookies.json`, `*_storage.json`) or any credentials. Use placeholders or document setup steps in the PR instead.
- Be mindful of target site Terms of Service and rate limits; avoid adding automation that would run unintentionally in CI.
