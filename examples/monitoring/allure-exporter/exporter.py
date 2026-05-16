"""Allure projects exporter for Prometheus.

Опрашивает widget summary каждого проекта на Allure server, форматирует как
Prometheus-метрики. Один экспортер собирает метрики для всех проектов сразу.

Endpoint: GET :9091/metrics

Метрики:
  - allure_exporter_up           — 1/0, отвечает ли Allure server
  - allure_tests{project,status} — число тестов по статусам (passed/failed/broken/skipped/unknown/total)
  - allure_last_run_duration_ms  — продолжительность последнего прогона в мс
  - allure_last_run_timestamp    — unix-ms времени окончания последнего прогона
"""
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

ALLURE_URL = os.environ.get("ALLURE_URL", "http://allure:5050")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9091"))


def fetch_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def collect() -> str:
    lines = [
        "# HELP allure_exporter_up 1 if Allure server is responding",
        "# TYPE allure_exporter_up gauge",
    ]

    projects_resp = fetch_json(f"{ALLURE_URL}/allure-docker-service/projects")
    if not projects_resp:
        lines.append("allure_exporter_up 0")
        return "\n".join(lines) + "\n"

    lines.append("allure_exporter_up 1")
    lines.append("")
    lines.append("# HELP allure_tests Test count by status in latest report per project")
    lines.append("# TYPE allure_tests gauge")
    lines.append("# HELP allure_last_run_duration_ms Duration of latest run (ms)")
    lines.append("# TYPE allure_last_run_duration_ms gauge")
    lines.append("# HELP allure_last_run_timestamp Unix-ms of latest run stop time")
    lines.append("# TYPE allure_last_run_timestamp gauge")

    projects = list(projects_resp.get("data", {}).get("projects", {}).keys())
    for p in projects:
        summary = fetch_json(
            f"{ALLURE_URL}/allure-docker-service/projects/{p}/reports/latest/widgets/summary.json"
        )
        if not summary:
            continue
        stat = summary.get("statistic", {})
        time_data = summary.get("time", {})
        for status in ("passed", "failed", "broken", "skipped", "unknown"):
            v = stat.get(status, 0)
            lines.append(f'allure_tests{{project="{p}",status="{status}"}} {v}')
        lines.append(f'allure_tests{{project="{p}",status="total"}} {stat.get("total", 0)}')
        if "duration" in time_data:
            lines.append(f'allure_last_run_duration_ms{{project="{p}"}} {time_data["duration"]}')
        if "stop" in time_data:
            lines.append(f'allure_last_run_timestamp{{project="{p}"}} {time_data["stop"]}')

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = collect().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()
