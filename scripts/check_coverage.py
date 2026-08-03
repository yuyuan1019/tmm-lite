"""Check coverage thresholds for specific modules.

Usage: python scripts/check_coverage.py coverage.json app/path/file.py=85 [app/other.py=80 ...]
Exits non-zero if any specified module is below its threshold.
"""
import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: check_coverage.py <coverage.json> <file=threshold> ...")
        sys.exit(2)

    coverage_path = Path(sys.argv[1])
    if not coverage_path.exists():
        print(f"Coverage file not found: {coverage_path}")
        sys.exit(1)

    with open(coverage_path) as f:
        data = json.load(f)

    files = data.get("files", {})
    # Normalize file paths to forward slashes for cross-platform matching
    normalized_files: dict[str, object] = {}
    for k, v in files.items():
        normalized_files[k.replace("\\", "/")] = v

    failed = False
    for arg in sys.argv[2:]:
        file_path, _, threshold_str = arg.partition("=")
        threshold = float(threshold_str)
        normalized_path = file_path.replace("\\", "/")

        if normalized_path not in normalized_files:
            print(f"MISSING: {file_path} not found in coverage report")
            failed = True
            continue

        pct = normalized_files[normalized_path]["summary"]["percent_covered"]  # type: ignore[index]
        if pct < threshold:
            print(f"FAIL: {file_path} = {pct:.1f}% (threshold: {threshold:.0f}%)")
            failed = True
        else:
            print(f"OK:   {file_path} = {pct:.1f}% (threshold: {threshold:.0f}%)")

    if failed:
        sys.exit(1)
    print("All coverage thresholds met.")


if __name__ == "__main__":
    main()
