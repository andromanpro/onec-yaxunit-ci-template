"""CLI: JUnit XML (YAxUnit) -> Allure raw results directory.

Usage:
    py -3.14 junit-to-allure.py <input.xml> <output_dir>

See `junit_to_allure_impl.py` for logic.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from junit_to_allure_impl import convert


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <input.xml> <output_dir>", file=sys.stderr)
        return 2
    input_xml = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])
    if not input_xml.exists():
        print(f"input not found: {input_xml}", file=sys.stderr)
        return 1
    n = convert(input_xml, output_dir)
    print(f"wrote {n} result files to {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
