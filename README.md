# dsdl — Inner Speech dataset & code downloader

`dsdl` downloads everything listed in the
[Inner Speech – Datasets](https://docs.google.com/spreadsheets/d/1foTcOWLRAXgwGOOQB3PZ5DWY6EKg6bPDzm8fFxoA3Wk/edit) sheet.
It has two sync tasks:

| task | sheet | sources |
|---|---|---|
| `datasets` | **Datasets** tab (ID column) | OpenNeuro, OSF, Zenodo, Figshare, Hugging Face, GIN (git-annex) |
| `code` | **Code Examples** tab + the Datasets tab's *Code* column | GitHub (shallow clone + LFS), plus Zenodo/OSF/Figshare code archives |

It shows a progress bar per item, a live overall ETA, and the size of every item before it starts.
API keys are optional. Everything is set in `config.yaml`, including the output folder, which can be an external drive.
Reruns pick up where the last run stopped, and if everything is already downloaded a rerun finishes in about a second.

## Setup

```bash
cd DatasetDownloader
python3 -m venv .venv
.venv/bin/pip install -e .
```

Then copy `config.example.yaml` to `config.yaml` and set at least `output_dir`:

```yaml
general:
  output_dir: /Volumes/MyDrive/InnerSpeech
```

If that drive isn't mounted, dsdl refuses to run. It never falls back to writing on the internal disk.

## Usage

```bash
.venv/bin/dsdl plan datasets       # list remote files: sizes per item, totals, free space, time estimate
.venv/bin/dsdl sync datasets       # task 1: download / resume all datasets
.venv/bin/dsdl sync code           # task 2: clone / update all code repos
.venv/bin/dsdl sync all            # both
.venv/bin/dsdl status              # what's done (works offline, from the state DB)
.venv/bin/dsdl verify --checksums  # re-check files on disk (sizes; --checksums rehashes)
```

Useful flags:

- `--only openneuro:ds004196 ds004510 'osf:*'`: restrict to some items. Keys come from `plan` and `status`.
- `--exclude hf:datasets/FOMO-MRI/FOMO300K`: skip items. Add them to `download: exclude:` to make that permanent.
- `--check-updates`: re-check finished items against upstream (new OpenNeuro snapshot, new HF commit, new git commits).
- `--retry-failed`: retry items marked *unavailable* (gated, private, not found). These are also retried automatically when you add or change a token for that source.
- `--offline`: use the cached copy of the sheet.

`Ctrl-C` is safe at any point. Partial files (`*.part`) and the state are kept, and the next run resumes them with HTTP range requests.

## How reruns stay fast

- The state is a SQLite DB at `<output_dir>/.dsdl/state.sqlite`, so it travels with the drive. It records every item and every file in it, with size, checksum and done flag.
- A rerun fetches the sheet (one request; it falls back to the cached copy if offline) and skips every *complete* item without touching the network. With nothing left to do you get one summary line, e.g. `✔ datasets: 86/94 complete (9.4 TB on disk) · 3 unavailable · 6 manual — nothing to do`.
- Remote file lists are fetched once and cached, so resuming a half-done dataset doesn't list it again.
- New rows added to the sheet are picked up automatically on the next run.
- If a finished item's folder is deleted, it is downloaded again.

## Output layout

```
<output_dir>/
  datasets/openneuro/ds004196/…          datasets/osf/agre8/…        datasets/zenodo/8208839/…
  datasets/figshare/23633751/…           datasets/huggingface/datasets/FOMO-MRI/FOMO300K/…
  datasets/gin/gallantlab/shortclips/…   datasets/INDEX.csv          (sheet row → key → folder)
  code/github/HuthLab/semantic-decoding/ code/zenodo/15682059/…      code/INDEX.csv
  MANUAL_DOWNLOADS.md                    (entries needing registration / no link — regenerated each run)
  .dsdl/                                 (state DB, cached sheet, logs)
```

## API keys (all optional)

Put them under `auth:` in `config.yaml`, or set the environment variables:

| key | env | what it gives you |
|---|---|---|
| `github_token` | `GITHUB_TOKEN` | repo sizes in `plan`/progress (without one: 60 API requests/hour, so sizes show as `?`) |
| `hf_token` | `HF_TOKEN` | gated repos (e.g. `karavela/cephalonauts_one`; also accept the terms on the website) and much higher rate limits, which matter for FOMO300K (~300k files) |
| `osf_token` | `OSF_TOKEN` | private projects you have access to, higher rate limits |
| `zenodo_token` | `ZENODO_TOKEN` | higher rate limits (Zenodo throttles anonymous downloads to under 1 MB/s) |
| `figshare_token` | `FIGSHARE_TOKEN` | private articles |

OSF view-only links go under `overrides:` as `"osf:czb26": {view_only: <key>}`.

## What you should know

- **Size** (measured from the full file listings, Oct 2026): about **19 TB** in total.
  - OpenNeuro: ~15 TB, of which `derivatives/` (mostly fMRIPrep outputs) are 8.7 TB. Set `openneuro: include_derivatives: false` to get raw only (~6.3 TB), which brings the total to about **10.4 TB**.
  - Hugging Face: ~3.6 TB. FOMO300K (2.3 TB) and OpenMind (1.0 TB) are the bulk.
  - Figshare 0.25 TB, OSF 0.14 TB, GIN 0.11 TB, Zenodo 0.04 TB.

  Items download smallest first. An item that wouldn't leave `min_free_space_gb` free is skipped with a message, and the run continues.
- **First run** lists every remote file before downloading, which takes about 20 minutes for the Datasets tab (some datasets have 100k–400k files). The listings are cached, so later runs don't repeat this.
- **OpenNeuro** always downloads the latest snapshot, one copy per dataset, even if the sheet links an older version. Set `openneuro: version: sheet` to pin to the linked version.
- **git-lfs** keeps a second copy of every LFS file in `.git/lfs`, so LFS-heavy repos take about twice their size on disk.
- **GIN** repos need `git-annex` (`brew install git-annex`). On exFAT drives git-annex switches to an "adjusted unlocked" branch automatically, which is fine.
- **Manual downloads:** HCP, CNeuroMod, Synapse (BrainIAC), PURR, ScienceDB, SWOW, GitHub organisation pages and paper-only rows are listed in `MANUAL_DOWNLOADS.md` with the reason and link.
- On macOS the sync keeps the machine awake (`caffeinate`). Set `prevent_sleep: false` to disable this.
- Logs: `<output_dir>/.dsdl/logs/`.

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```