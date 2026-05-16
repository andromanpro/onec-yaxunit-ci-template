"""Vanessa Automation BDD results → Allure raw → server.

Vanessa MCP (`vanessa-mcp.py call run_scenario`) возвращает markdown с per-scenario
статусами, не JUnit XML. Этот скрипт парсит markdown, формирует Allure raw JSON
напрямую и шлёт в `frankescobar/allure-docker-service` (тот же сервер что для YAxUnit).

Usage:
    py -3.14 vanessa-bdd-to-allure.py \\
        --project bdd-project \\
        --feature "F:/path/to/smoke.feature" \\
        [--url http://localhost:5050]

Pipeline:
    1. Загружает feature через Vanessa MCP (load_features)
    2. Запускает run_scenario через MCP
    3. Парсит markdown результата (имя + статус + длительность)
    4. Формирует Allure JSON per-scenario + environment.properties + executor.json
    5. POST /send-results → GET /generate-report
    6. Печатает URL отчёта
"""

import argparse
import hashlib
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent
# Helper лежит рядом в ci/ — шаблон самодостаточен.
# Переопределяется через env VANESSA_MCP_HELPER при нестандартной раскладке.
VANESSA_MCP_HELPER = Path(
    os.environ.get("VANESSA_MCP_HELPER", str(SCRIPT_DIR / "vanessa-mcp.py"))
)


def call_vanessa(tool: str, args: dict | None = None) -> dict:
    cmd = [sys.executable, str(VANESSA_MCP_HELPER), "call", tool]
    if args is not None:
        cmd.append(json.dumps(args, ensure_ascii=False))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"vanessa-mcp {tool} failed: {r.stderr or r.stdout}")
    return json.loads(r.stdout)


def extract_md_text(rpc_response: dict) -> str:
    return rpc_response["result"]["content"][0]["text"]


def parse_run_output(md: str) -> list[dict]:
    """Парсит markdown результат run_scenario.

    Формат строк (русский Vanessa output):
        Имя сценария: <name>
        Результат прохождения сценнария: Success|Failed|...

    Возвращает [{name, status}, ...] в порядке.
    """
    scenarios = []
    name = None
    for line in md.splitlines():
        m_name = re.match(r"\s*Имя сценария:\s*(.+?)\s*$", line)
        if m_name:
            name = m_name.group(1)
            continue
        m_st = re.match(r"\s*Результат прохожд[еe]ния сцен+а?рия:\s*(\S+)", line)
        if m_st and name is not None:
            status_raw = m_st.group(1).strip()
            scenarios.append({"name": name, "status_raw": status_raw})
            name = None
    return scenarios


def map_status(raw: str) -> str:
    raw_low = raw.lower()
    if raw_low in ("success", "passed", "ok"):
        return "passed"
    if raw_low in ("failed", "fail", "error"):
        return "failed"
    if raw_low in ("skipped", "skip"):
        return "skipped"
    return "broken"


def history_id(full_name: str) -> str:
    return hashlib.md5(full_name.encode("utf-8")).hexdigest()


def build_allure_results(scenarios: list[dict], feature_path: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    feature_name = feature_path.stem
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for idx, sc in enumerate(scenarios):
        name = sc["name"]
        full_name = f"{feature_name}.{name}"
        status = map_status(sc["status_raw"])

        result = {
            "uuid": str(uuid.uuid4()),
            "name": name,
            "fullName": full_name,
            "historyId": history_id(full_name),
            "status": status,
            "stage": "finished",
            "start": now_ms + idx * 1000,
            "stop": now_ms + idx * 1000 + 500,
            "labels": [
                {"name": "suite", "value": feature_name},
                {"name": "feature", "value": feature_name},
                {"name": "framework", "value": "Vanessa Automation"},
                {"name": "language", "value": "1c-bsl"},
                {"name": "testMethod", "value": name},
            ],
            "links": [],
            "parameters": [],
            "attachments": [],
        }
        if status != "passed":
            result["statusDetails"] = {
                "message": f"Vanessa reported: {sc['status_raw']}",
                "trace": "",
            }
        out_file = output_dir / f"{result['uuid']}-result.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # environment.properties
    env_lines = [
        f"Platform.1C={os.environ.get('ONEC_PLATFORM_VERSION', '8.3.27.1989')}",
        f"Vanessa.Framework=Vanessa Automation",
        f"Feature.source={feature_path.name}",
        f"Scenarios={len(scenarios)}",
    ]
    (output_dir / "environment.properties").write_text("\n".join(env_lines), encoding="utf-8")

    # executor.json
    executor = {
        "name": "Vanessa Automation (manual via MCP)",
        "type": "manual",
        "url": os.environ.get("EXECUTOR_URL", ""),
        "buildName": f"bdd-{feature_name}",
        "buildOrder": int(datetime.now().timestamp()),
    }
    (output_dir / "executor.json").write_text(
        json.dumps(executor, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return len(scenarios)


def multipart_body(files: list[Path]) -> tuple[bytes, str]:
    boundary = f"----VanessaAllureUpload{uuid.uuid4().hex}"
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
        url, method="POST", data=body,
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
    ap.add_argument("--project", default=os.environ.get("ALLURE_PROJECT_ID", "bdd-project"))
    ap.add_argument("--url", default=os.environ.get("ALLURE_URL", "http://localhost:5050"))
    ap.add_argument("--feature", required=True, help="Path to .feature to load+run")
    ap.add_argument("--skip-load", action="store_true",
                    help="Skip load_features (если уже загружен — сразу run_scenario)")
    args = ap.parse_args()

    feature_path = Path(args.feature)
    if not feature_path.exists():
        print(f"feature not found: {feature_path}", file=sys.stderr)
        return 1

    print(f"=== vanessa-bdd-to-allure ===")
    print(f"  feature: {feature_path}")
    print(f"  server:  {args.url}")
    print(f"  project: {args.project}")

    # 1. Load (если не пропущено)
    if not args.skip_load:
        print("  -> load_features")
        load_resp = call_vanessa("load_features", {"path": str(feature_path).replace("\\", "/")})
        load_md = extract_md_text(load_resp)
        if "Загружено: да" not in load_md and "loaded" not in load_md.lower():
            print(f"    WARN: load output не подтверждает успех:\n{load_md}", file=sys.stderr)

    # 2. Run
    print("  -> run_scenario")
    run_resp = call_vanessa("run_scenario", {})
    run_md = extract_md_text(run_resp)
    print("\n--- run output ---")
    print(run_md)
    print("---")

    # 3. Parse
    scenarios = parse_run_output(run_md)
    if not scenarios:
        print("  FAIL: не удалось распарсить ни одного сценария из вывода", file=sys.stderr)
        return 2
    passed = sum(1 for s in scenarios if map_status(s["status_raw"]) == "passed")
    failed = len(scenarios) - passed
    print(f"  parsed {len(scenarios)} scenarios: passed={passed} failed={failed}")

    # 4. Build Allure raw + upload
    with tempfile.TemporaryDirectory(prefix="vanessa-allure-") as tmp:
        tmp_path = Path(tmp)
        n = build_allure_results(scenarios, feature_path, tmp_path)
        files = sorted(tmp_path.iterdir())
        print(f"  uploading {len(files)} files...")
        send_url = f"{args.url}/allure-docker-service/send-results?project_id={args.project}"
        resp = post_multipart(send_url, files)
        meta = resp.get("meta_data", {})
        data = resp.get("data", {})
        print(f"  send-results: {meta.get('message','?')}")
        if data.get("failed_files_count", 0) > 0:
            print(f"    FAILED: {data.get('failed_files', [])}", file=sys.stderr)
            return 3

    gen_url = f"{args.url}/allure-docker-service/generate-report?project_id={args.project}"
    gen = get_json(gen_url)
    report_url = gen.get("data", {}).get("report_url", "")
    print(f"  generate-report: {gen.get('meta_data',{}).get('message','?')}")
    print()
    print(f"REPORT: {report_url}")
    print(f"LATEST: {args.url}/allure-docker-service/projects/{args.project}/reports/latest/index.html")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
