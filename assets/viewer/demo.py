"""Check synthetic instrument readings against a specified threshold."""

import argparse
import csv
import json
import math
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--input", type=Path, required=True)
parser.add_argument("--threshold", type=float, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--verbose", action="store_true")
args = parser.parse_args()

with args.input.open(encoding="utf-8-sig", newline="") as source:
    reader = csv.DictReader(source)
    if not {"sample_id", "reading"}.issubset(reader.fieldnames or []):
        parser.error("CSV requires sample_id and reading columns")
    rows = []
    for line, item in enumerate(reader, start=2):
        reading = float(item["reading"])
        if not math.isfinite(reading):
            parser.error(f"Non-finite reading on line {line}")
        rows.append((line, item["sample_id"], reading, reading > args.threshold))

print(f"Checking {len(rows)} readings at threshold {args.threshold:g}", flush=True)
for line, sample, reading, flagged in rows:
    if args.verbose:
        print(f"line {line} | {sample} | {reading:g} | {'above limit' if flagged else 'ok'}", flush=True)
        time.sleep(0.22)

args.output.mkdir(parents=True, exist_ok=True)
with (args.output / "readings.csv").open("w", encoding="utf-8", newline="") as output:
    writer = csv.writer(output)
    writer.writerow(["source_line", "sample_id", "reading", "above_limit"])
    writer.writerows(rows)

summary = {"threshold": args.threshold, "rows": len(rows), "flagged": sum(row[3] for row in rows)}
(args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(f"Wrote readings.csv and summary.json ({summary['flagged']} flagged)", flush=True)
