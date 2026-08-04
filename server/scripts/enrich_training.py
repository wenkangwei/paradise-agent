#!/usr/bin/env python3
"""Offline training data enrichment — joins session dialogues, behavior events,
and user profiles into a labeled training dataset.

Usage:
    cd server
    python scripts/enrich_training.py [--month 2026-08] [--output custom_path.jsonl]

Output: logs/training/enriched_YYYY-MM.jsonl

Each entry contains:
    {
      "session_id": "...",
      "user_id": "...",
      "user_profile": {...},
      "messages": [...],
      "labels": {"msg_id": "chosen"|"rejected"},
      "context": {...}
    }
"""

import argparse
import sys
from pathlib import Path

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from data_store import training_joiner


def main():
    parser = argparse.ArgumentParser(description="Enrich training data with feedback labels")
    parser.add_argument(
        "--month",
        default=None,
        help="Year-month to process (YYYY-MM). Defaults to current month.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output file path. Defaults to logs/training/enriched_YYYY-MM.jsonl",
    )
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else None
    result = training_joiner.build_training_dataset(
        year_month=args.month,
        output_path=output_path,
    )

    print(f"\nTraining dataset built:")
    print(f"  Sessions:      {result['sessions']}")
    print(f"  Labeled msgs:  {result['labeled_messages']}")
    print(f"  Output:        {result['output_path']}")


if __name__ == "__main__":
    main()
