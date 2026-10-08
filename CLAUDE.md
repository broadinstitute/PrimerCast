# PrimerCast Development Guide

## Project Structure

This is a Python package for ML-guided qPCR primer design. Key directories:

- `src/primercast/` - Main package source
  - `cli.py` - Single CLI entry point with subcommands
  - `commands/` - Individual subcommand implementations
  - `models/` - PyTorch ML model architectures
  - `external/` - Wrappers for external tools (ViennaRNA, bowtie2, MAFFT)
  - `utils/` - Shared utilities (sequence ops, encoding, params)
  - `data/` - Pre-trained ML models (bundled as package data)
- `workflows/` - Snakemake workflow templates
- `training/` - Historical model training code (not production)
- `tests/` - pytest test suite

## Development Setup

```bash
# Create conda environment with external tools
conda env create -f environment.yml
conda activate primercast

# Install package in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v
```

## CLI Usage

Single entry point with subcommands:
```bash
primercast generate --help
primercast evaluate --help
primercast pick-representatives --help
primercast prepare-input --help
primercast filter --help
primercast build-output --help
primercast select-multiplex --help
```

## Key Patterns

### External Tool Wrappers
External bioinformatics tools (RNAduplex, bowtie2, mafft) are assumed to be
in PATH via conda installation. Use `shutil.which()` to verify availability.

### ML Model Loading
Models are bundled as package data. Load using `importlib.resources`:
```python
from importlib.resources import files
model_path = files('primercast.data').joinpath('combined_classifier.pth')
```

### Filter Diagnostics
Filtering steps record a funnel (candidates left after each filter) with
`utils.diagnostics.StageDiagnostics`, written to `<output>.diagnostics.json`.
`finish()` exits with code 3 and a "NO CANDIDATES: ..." message when a required
step ends up empty; the GUI and `adapt` CLI show it via `explain_run(run_dir)`.
Off-target steps use `fatal=False` (`prepare-input` and `evaluate` are only fatal with
`--fail-if-empty`, passed for design on-target only). Evaluate mode never stops on an
empty step: it still writes a report (0 coverage, targets listed as unmapped), and
`run_warnings(run_dir)` surfaces zero on-target coverage plus rescue re-evaluations
(`<eval>.rescue.json`) as warnings in the GUI and CLI.

### Adding New Subcommands
1. Create module in `src/primercast/commands/`
2. Implement `register(subparsers)` function to add argparse subparser
3. Import and register in `cli.py`

## Testing

- Run all tests: `pytest tests/ -v`
- Run with coverage: `pytest tests/ -v --cov=primercast`
- Tests should not require external tools (mock them)

## Docker

Two images are built from the single `Dockerfile` via the `TORCH_VARIANT` build arg:

- **GHCR — `ghcr.io/broadinstitute/primercast`**: multi-arch (amd64+arm64),
  **GPU**-enabled (CUDA PyTorch). Use this for the CLI, training, and Terra/batch
  workflows:
  ```bash
  docker pull ghcr.io/broadinstitute/primercast:latest
  docker run --rm ghcr.io/broadinstitute/primercast:latest primercast --help
  ```
- **GAR — `us-central1-docker.pkg.dev/sabeti-adapt/primercast/primercast`**:
  amd64-only, **CPU**-only (slim, ~1.5–2 GB compressed). Runs the Streamlit web app on
  Cloud Run; it has no CUDA (Cloud Run has no GPU). Not for GPU/CLI use.

Build locally (GPU is the default):
```bash
docker build -t primercast:local .                          # GPU (GHCR-style)
docker build --build-arg TORCH_VARIANT=cpu -t primercast-cpu:local . # CPU (GAR/Cloud Run)
docker run --rm primercast:local primercast --help
```

CI (`.github/workflows/docker.yml`) builds both: GPU→GHCR (multi-arch manifest) and
CPU→GAR.

## Web app deployment (Cloud Run)

The Streamlit GUI (`gui/app.py`) is served publicly on Google Cloud Run in project
`sabeti-adapt`. Infrastructure is Terraform (`terraform/`, see `terraform/README.md`):
an Artifact Registry repo, a runtime service account, and `primercast` (prod) +
`primercast-staging` services. CI deploys the CPU/GAR image — every branch push
creates a per-branch staging revision; pushing a `v*` tag deploys production.

URLs:
- **Production**: `https://primercast.sabeti.broadinstitute.org`
- **Staging base**: `https://primercast-staging-soitfyremq-uc.a.run.app`
- **Per-branch preview**: `https://<branch>---primercast-staging-soitfyremq-uc.a.run.app`
  (e.g. branch `my-feature` → `https://my-feature---primercast-staging-soitfyremq-uc.a.run.app`)

Each pipeline run executes in an isolated scratch working directory
(`gui/run_isolation.py`) so concurrent users don't share a Snakefile / `.snakemake`
lock. Results are written to local disk, which is **ephemeral** on Cloud Run (lost on
redeploy / scale events) in this iteration; `PRIMERCAST_DATA_DIR` is the seam for adding
GCS persistence later.

## Snakemake Workflows

Workflows are in `workflows/`. They use the `primercast` CLI internally:
```bash
cd workflows
snakemake -s Snakefile.example --cores all
```

Dry-run validation:
```bash
snakemake -s Snakefile.example --dry-run
```

## Environment Variables

- `PRIMERCAST_DATA_DIR`: Root for GUI run outputs (`runs/`, `monitor/`). Defaults to the
  project root (and the image's writable `/app`). Point at a mounted volume to persist
  results, e.g. on Cloud Run (optional). Reference inputs (`target_seqs/`) always stay
  under the project root.
- `PRIMERCAST_REPORT_BUCKET` / `PRIMERCAST_REPORT_PREFIX`: GCS bucket + object prefix for the
  GUI's "Report a problem" bundles (`gui/error_report.py`; set by terraform for prod and
  by CI for staging). Unset (local dev) → bundles are saved under `DATA_DIR/error_reports/`
  and offered as a download.
- `PRIMERCAST_FONT_PATH`: Custom font directory for training plots (optional)
- `PRIMERCAST_TOOLPATH`: Custom tool installation path for training scripts (optional)
- `RNASTRUCTURE_DATAPATH`: Path to RNAstructure data tables (optional)
