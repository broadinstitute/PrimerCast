"""Run ``gget virus`` so location/date filters reach NCBI for every virus.

For SARS-CoV-2 and influenza A, gget downloads NCBI's prebuilt package of
*every* genome and only then applies location and date filters locally. That
is many GB and exhausts memory on Cloud Run, whose disk is RAM. gget's normal
API path sends those filters to NCBI and downloads only matching sequences,
so we use it whenever such a filter is given (gget's private ``_skip_cache``).
Unfiltered queries keep the bulk package, which beats paging the API for
millions of records; so do ``--lineage`` queries, which only it supports.

Usage matches ``gget virus``::

    python -m primercast.external.gget_virus <taxon> [gget virus options]
"""

import functools
import inspect
import sys

# Command prefix that replaces ``gget virus``.
GGET_VIRUS = [sys.executable, "-m", "primercast.external.gget_virus"]

# Filters the bulk package applies only after downloading everything.
NARROWING_FLAGS = frozenset({
    "--geographic_location", "--submitter_country",
    "--min_release_date", "--max_release_date",
    "--min_collection_date", "--max_collection_date",
})


def use_api_path(args: list[str]) -> bool:
    """Whether to bypass gget's bulk-package download for these arguments."""
    flags = {a.split("=", 1)[0] for a in args if a.startswith("--")}
    return "--lineage" not in flags and bool(flags & NARROWING_FLAGS)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    from gget import main as gget_main

    virus = gget_main.virus
    if use_api_path(args) and "_skip_cache" in inspect.signature(virus).parameters:
        # gget's CLI passes _skip_cache=False explicitly, so override it per call.
        @functools.wraps(virus)
        def api_path_virus(*a, **kw):
            return virus(*a, **{**kw, "_skip_cache": True})
        gget_main.virus = api_path_virus
    sys.argv = ["gget", "virus", *args]
    gget_main.main()


if __name__ == "__main__":
    main()
