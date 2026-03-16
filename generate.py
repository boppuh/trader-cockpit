#!/usr/bin/env python3
"""Main entry point: collect data → render HTML → write to output directory.

Usage:
    python generate.py              # Full pipeline (collect + render)
    python generate.py --from-json data.json   # Render from existing JSON
    python generate.py --output-dir ./output   # Override output directory
"""

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

import config
from collect_data import collect_all
from render import render


def setup_logging():
    """Configure logging to file and stderr."""
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main():
    parser = argparse.ArgumentParser(description="Generate Trader's Cockpit dashboard")
    parser.add_argument(
        "--from-json",
        type=str,
        default=None,
        help="Path to a JSON file to render instead of collecting live data",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override the output directory",
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("cockpit.generate")

    output_dir = Path(args.output_dir) if args.output_dir else config.OUTPUT_DIR

    # Collect or load data
    if args.from_json:
        logger.info("Loading data from %s", args.from_json)
        with open(args.from_json) as f:
            data = json.load(f)
        # Inject template-level fields that collectors normally provide
        data.setdefault("ticker_names", config.TICKER_NAMES)
        data.setdefault("gainers", [])
        data.setdefault("losers", [])
        data.setdefault("gamma_squeeze_entries", [])
        data.setdefault("gamma_squeeze_note", "")
    else:
        logger.info("Collecting live data...")
        data = collect_all()

    # Render HTML
    logger.info("Rendering template...")
    html = render(data)

    # Write output
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / "index.html"
    html_path.write_text(html)
    logger.info("Wrote HTML to %s", html_path)

    # Copy static files
    css_src = config.STATIC_DIR / "style.css"
    css_dst = output_dir / "style.css"
    if css_src.exists():
        shutil.copy2(css_src, css_dst)
        logger.info("Copied style.css to %s", css_dst)

    # Also save the data as JSON for debugging
    data_path = output_dir / "data.json"
    data_path.write_text(json.dumps(data, indent=2, default=str))
    logger.info("Wrote data.json to %s", data_path)

    logger.info("Generation complete. Dashboard at %s", html_path)
    print(f"Dashboard generated: {html_path}")


if __name__ == "__main__":
    main()
