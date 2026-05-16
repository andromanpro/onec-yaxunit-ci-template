"""End-to-end: latest YAxUnit JUnit XML -> Allure raw -> Allure server.

Usage:
    py -3.14 upload-to-allure.py [--project your-project-id]
                                 [--url http://your-allure-host:5050]
                                 [--xml /path/to/specific.xml]
                                 [--reports-dir /path/to/ci-reports]

By default: scans ci-reports/ for newest *.xml, converts to Allure raw,
uploads to /allure-docker-service/send-results?project_id=... and triggers
/generate-report. Prints final report URL.
"""

import argparse
import io
import json
import mimetypes
import os
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

# Reuse converter
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
from junit_to_allure_impl import convert as _convert  # noqa: E402


def newest_xml(reports_dir: Path) -> Path | None:
    xmls = sorted(reports_dir.glob("*.xml"), key=lambda p: p.stat().st_mtime, reverse=True)
    return xmls[0] if xmls else None


def multipart_body(files: list[Path]) -> tuple[bytes, str]:
    boundary = f"----AllureUpload{uuid.uuid4().hex}"
    buf = io.BytesIO()
    for f in files:
        ctype, _ = mimetypes.guess_type(f.name)
        if ctype is None:
            ctype = "application/octet-stream"
        buf.write(f"--{boundary}\r\n".encode("ascii"))
        buf.write(
            f'Content-Disposition: form-data; name="files[]"; filename="{f.name}"\r\n'.encode("utf-8")
        )
        buf.write(f"Content-Type: {ctype}\r\n\r\n".encode("ascii"))
        buf.write(f.read_bytes())
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode("ascii"))
    return buf.getvalue(), boundary


def post_multipart(url: str, files: list[Path]) -> dict:
    body, boundary = multipart_body(files)
    req = urllib.request.Request(
        url,
        method="POST",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--project",
        default=os.environ.get("ALLURE_PROJECT_ID", "your-project-id"),
        help="Allure project id (default from env ALLURE_PROJECT_ID)",
    )
    ap.add_argument(
        "--url",
        default=os.environ.get("ALLURE_URL", "http://localhost:5050"),
        help="Allure server base URL (default from env ALLURE_URL)",
    )
    ap.add_argument("--xml", default=None, help="explicit XML path; default — newest in --reports-dir")
    ap.add_argument(
        "--reports-dir",
        default=str(SCRIPT_DIR / "ci-reports"),
        help="where to look for JUnit XML reports",
    )
    args = ap.parse_args()

    if args.xml:
        xml_path = Path(args.xml)
    else:
        reports_dir = Path(args.reports_dir)
        xml_path = newest_xml(reports_dir)
        if xml_path is None:
            print(f"no *.xml found in {reports_dir}", file=sys.stderr)
            return 1

    print(f"=== upload-to-allure ===")
    print(f"  xml:     {xml_path}")
    print(f"  server:  {args.url}")
    print(f"  project: {args.project}")

    with tempfile.TemporaryDirectory(prefix="allure-results-") as tmp:
        tmp_path = Path(tmp)
        n = _convert(xml_path, tmp_path)
        print(f"  converted {n} test results")

        # Collect all files: *-result.json, environment.properties, executor.json
        files = sorted(tmp_path.iterdir())
        print(f"  uploading {len(files)} files...")

        send_url = f"{args.url}/allure-docker-service/send-results?project_id={args.project}"
        resp = post_multipart(send_url, files)
        meta = resp.get("meta_data", {})
        data = resp.get("data", {})
        print(f"  send-results: {meta.get('message','?')}")
        print(f"    processed={data.get('processed_files_count','?')} failed={data.get('failed_files_count','?')}")
        if data.get("failed_files_count", 0) > 0:
            print(f"    FAILED: {data.get('failed_files', [])}", file=sys.stderr)
            return 2

    gen_url = f"{args.url}/allure-docker-service/generate-report?project_id={args.project}"
    gen = get_json(gen_url)
    report_url = gen.get("data", {}).get("report_url", "")
    print(f"  generate-report: {gen.get('meta_data',{}).get('message','?')}")
    print()
    print(f"REPORT: {report_url}")
    print(f"LATEST: {args.url}/allure-docker-service/projects/{args.project}/reports/latest/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
