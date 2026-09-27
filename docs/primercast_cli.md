# `primercast` CLI Reference

Low-level subcommands used internally by the Snakemake workflow. These are not intended to be called directly by users — use the `adapt` CLI instead.

| Command | Description |
|---------|-------------|
| `primercast generate` | Generate primer candidates from target sequences |
| `primercast prepare-features` | Compute features (Tm, GC%, dG) for existing primers |
| `primercast prepare-input` | Prepare input data for ML evaluation |
| `primercast evaluate` | Run ML model to score primer candidates |
| `primercast filter` | Filter primers based on evaluation scores |
| `primercast build-output` | Build final output CSV with scores |
| `primercast select-multiplex` | Select best multiplex primer set |
| `primercast export-report` | Export evaluation results to Excel reports |
| `primercast generate-probe` | Generate probe candidates for TaqMan assays |
| `primercast parse-probe-mapping` | Parse probe SAM alignments into mapping table |
| `primercast quick-design` | Batch primer generation with early stopping for multi-sequence targets |

For usage details on any subcommand:

```bash
primercast <subcommand> --help
```
