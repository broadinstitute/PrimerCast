#!/usr/bin/env python
"""primercast - ML-guided PCR primer design CLI."""

import argparse
import sys


def main():
    """Main entry point for the primercast CLI."""
    parser = argparse.ArgumentParser(
        prog="primercast",
        description="ML-guided PCR primer design with off-target minimization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  primercast generate --in targets.fa --out primers.fa --params params.txt --name myprimer
  primercast generate-probe --in targets.fa --out probes.fa --params params.txt --name myprobe
  primercast prepare-features --fa my_primers.fa --out primers.feat
  primercast evaluate --in input.csv --out output.csv --ref reference.fa --reftype on
  primercast export-report --on eval.on --off eval.off1 eval.off2 --out reports/ --names primer1 primer2
For more information on a specific command:
  primercast <command> --help
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
        description="Available subcommands",
        metavar="<command>",
    )

    # Import and register subcommands
    from primercast.commands import (
        generate,
        generate_probe,
        parse_probe_mapping,
        evaluate_probe,
        prepare_features,
        prepare_input,
        evaluate,
        rescue_evaluate,
        filter_primers,
        build_output,
        select_multiplex,
        export_report,
        quick_design,
    )

    generate.register(subparsers)
    generate_probe.register(subparsers)
    parse_probe_mapping.register(subparsers)
    evaluate_probe.register(subparsers)
    prepare_features.register(subparsers)
    prepare_input.register(subparsers)
    evaluate.register(subparsers)
    rescue_evaluate.register(subparsers)
    filter_primers.register(subparsers)
    build_output.register(subparsers)
    select_multiplex.register(subparsers)
    export_report.register(subparsers)
    quick_design.register(subparsers)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Execute the subcommand
    args.func(args)


if __name__ == "__main__":
    main()
