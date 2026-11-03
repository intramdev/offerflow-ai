#!/usr/bin/env python3
"""Generate disposable, synthetic CSV upload-boundary fixtures. No network calls."""
import argparse
from pathlib import Path

HEADER = b"Supplier,Model,Price,Quantity,Currency,Notes\n"
ROWS = 1999

def make_valid(path, target):
    prefixes = [f"UI Upload Test,Product {i:04d},19.95,3,EUR,".encode() for i in range(ROWS)]
    padding = target - len(HEADER) - sum(map(len, prefixes)) - ROWS
    width, extra = divmod(padding, ROWS)
    if padding < 0 or width + 1 >= 131000:
        raise ValueError("Fixture field width is outside this generator's tested range")
    with path.open("wb") as stream:
        stream.write(HEADER)
        for index, prefix in enumerate(prefixes):
            stream.write(prefix + b"x" * (width + (index < extra)) + b"\n")
    assert path.stat().st_size == target

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, size in [("synthetic-upload-12mb.csv", 12_000_000),
                       ("synthetic-upload-exactly-200mb.csv", 200_000_000)]:
        path = args.output_dir / name
        if path.exists():
            raise FileExistsError(path)
        make_valid(path, size)
        print(path.name, path.stat().st_size)
    path = args.output_dir / "synthetic-upload-over-limit.csv"
    with path.open("xb") as stream:
        stream.truncate(200_000_001)
    print(path.name, path.stat().st_size)
    print("Upload/preview tests only: do not send the large fixture contents to a model.")

if __name__ == "__main__":
    main()
