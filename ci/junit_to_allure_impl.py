"""JUnit XML (YAxUnit kernel output) -> Allure raw results.

Pure logic, no CLI. Used by `junit-to-allure.py` (CLI) and `upload-to-allure.py`.

Output schema reference: https://allurereport.org/docs/how-it-works-test-result-file/

Environment variables (для тиражируемости — generic CI template):
    ONEC_PLATFORM_VERSION   платформа 1С (default: "8.3.27.1989")
    YAXUNIT_VERSION         версия YAxUnit kernel (default: "25.12")
    EXECUTOR_BUILD_NAME     buildName в executor.json (default: "yaxunit-smoke")
    EXECUTOR_URL            URL в executor.json (default: пусто)
"""

import hashlib
import json
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def iso_to_unix_ms(iso_str: str) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def history_id(full_name: str) -> str:
    return hashlib.md5(full_name.encode("utf-8")).hexdigest()


def testcase_status(tc: ET.Element) -> tuple[str, dict | None]:
    if tc.find("failure") is not None:
        f = tc.find("failure")
        return "failed", {
            "message": f.get("message", "") or "",
            "trace": (f.text or "").strip(),
        }
    if tc.find("error") is not None:
        e = tc.find("error")
        return "broken", {
            "message": e.get("message", "") or "",
            "trace": (e.text or "").strip(),
        }
    if tc.find("skipped") is not None:
        s = tc.find("skipped")
        return "skipped", {
            "message": s.get("message", "") or "Skipped",
            "trace": (s.text or "").strip(),
        }
    return "passed", None


def convert(input_xml: Path, output_dir: Path) -> int:
    tree = ET.parse(input_xml)
    root = tree.getroot()
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]

    output_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for suite in suites:
        suite_name = suite.get("name", "")
        suite_classname = suite.get("classname", "")
        suite_context = suite.get("context", "")
        suite_timestamp = suite.get("timestamp", "")
        suite_start_ms = iso_to_unix_ms(suite_timestamp)

        cursor_ms = suite_start_ms

        for tc in suite.findall("testcase"):
            name = tc.get("name", "")
            classname = tc.get("classname", "")
            time_sec = float(tc.get("time", "0") or 0)
            duration_ms = int(time_sec * 1000)
            # fullName/historyId должны быть уникальны на тест, иначе Allure
            # склеит историю разных тестов. suite_name (имя модуля) +
            # name (имя теста) — гарантированно уникальная пара в прогоне.
            full_name = f"{suite_name}.{name}"
            status, details = testcase_status(tc)

            start_ms = cursor_ms
            stop_ms = start_ms + duration_ms
            cursor_ms = stop_ms

            labels = [
                {"name": "suite", "value": suite_name},
                {"name": "feature", "value": suite_name},
                {"name": "framework", "value": "YAxUnit"},
                {"name": "language", "value": "1c-bsl"},
                {"name": "package", "value": suite_classname},
                {"name": "testClass", "value": suite_classname},
                {"name": "testMethod", "value": name},
            ]
            if suite_context:
                labels.append({"name": "subSuite", "value": suite_context})

            result = {
                "uuid": str(uuid.uuid4()),
                "name": name,
                "fullName": full_name,
                "historyId": history_id(full_name),
                "status": status,
                "stage": "finished",
                "start": start_ms,
                "stop": stop_ms,
                "labels": labels,
                "links": [],
                "parameters": [],
                "attachments": [],
            }
            if details is not None:
                result["statusDetails"] = details

            out_file = output_dir / f"{result['uuid']}-result.json"
            out_file.write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written += 1

    env_file = output_dir / "environment.properties"
    env_lines = [
        f"YAxUnit.version={os.environ.get('YAXUNIT_VERSION', '25.12')}",
        f"Platform.1C={os.environ.get('ONEC_PLATFORM_VERSION', '8.3.27.1989')}",
        f"Report.source={input_xml.name}",
        f"Suites={len(suites)}",
        f"Tests={written}",
    ]
    env_file.write_text("\n".join(env_lines), encoding="utf-8")

    executor = {
        "name": "Gitea Actions (Windows runner)",
        "type": "gitea",
        "url": os.environ.get("EXECUTOR_URL", ""),
        "buildName": os.environ.get("EXECUTOR_BUILD_NAME", "yaxunit-smoke"),
        "buildOrder": int(datetime.now().timestamp()),
    }
    (output_dir / "executor.json").write_text(
        json.dumps(executor, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return written
