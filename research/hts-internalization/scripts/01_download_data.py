#!/usr/bin/env python3
"""Download datasets: JARVIS-SC (Tc labels) and, with an MP API key, the MP
entries snapshot for hull construction.

  python scripts/01_download_data.py [--mp-api-key KEY]
"""
from __future__ import annotations

import argparse

from htsgen.config import PATHS
from htsgen.data.datasets import fetch_jarvis_supercon, fetch_mp_entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp-api-key", default=None,
                    help="Materials Project API key (free at materialsproject.org)")
    args = ap.parse_args()

    PATHS.ensure()
    fetch_jarvis_supercon()
    if args.mp_api_key:
        fetch_mp_entries(args.mp_api_key)
    else:
        print("No --mp-api-key: skipping MP hull snapshot "
              "(tier-2 E_hull will be unavailable until you fetch it).")


if __name__ == "__main__":
    main()
