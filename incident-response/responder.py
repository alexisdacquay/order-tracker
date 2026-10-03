import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

HERE = Path(__file__).resolve().parent
REPO_DIR = HERE.parent
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", HERE / "incidents"))
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
SERVICE_NAME = os.getenv("SERVICE_NAME", "order-tracker")
LOOKBACK_SECONDS = 15 * 60
AGENT_TIMEOUT_SECONDS = 30 * 60

# Tools are removed with --tools, not just left unapproved: user-level settings can approve
# tools on their own, so only removal guarantees a test alert cannot change anything.
READ_ONLY_ARGS = [
    "--tools", "Read,Glob,Grep",
    "--disallowedTools", "Edit", "Write", "Bash", "NotebookEdit",
    "--permission-mode", "default",
]
FIX_ARGS = [
    "--tools", "Read,Edit,Write,Glob,Grep,Bash",
    "--permission-mode", "acceptEdits",
    "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(uv run pytest:*)", "Bash(docker compose:*)", "Bash(curl:*)",
    "Bash(git diff:*)", "Bash(git status:*)",
    "--disallowedTools",
    "Bash(git checkout:*)", "Bash(git restore:*)", "Bash(git reset:*)", "Bash(git stash:*)",
    "Bash(git clean:*)", "Bash(git commit:*)", "Bash(git push:*)", "Bash(docker compose down:*)",
]

log = logging.getLogger("responder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Incident responder")
agent_lock = threading.Lock()


@app.get("/health")
def health():
    return {"status": "ok", "incidents_dir": str(INCIDENTS_DIR)}


@app.post("/alerts")
async def receive_alerts(request: Request):
    payload = await request.json()
    firing = [a for a in payload.get("alerts", []) if a.get("status", "firing") == "firing"]
    if not firing:
        return {"status": "ignored", "reason": "no firing alerts"}

    mode = "test" if is_test(firing) else "fix"
    incident_dir = create_incident(payload, firing)
    threading.Thread(target=investigate, args=(incident_dir, firing, mode), daemon=True).start()
    return {"status": "accepted", "mode": mode, "incident": str(incident_dir)}


def is_test(firing):
    """A test notification, or an alert that names no endpoint, gets a look-only agent."""
    labels = [a.get("labels", {}) for a in firing]
    return any(l.get("test") == "true" for l in labels) or not any(l.get("http_route") for l in labels)


def create_incident(payload, firing):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", firing[0].get("labels", {}).get("alertname", "alert"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    incident_dir = INCIDENTS_DIR / f"{stamp}-{name}"
    suffix = 2
    while incident_dir.exists():
        incident_dir = INCIDENTS_DIR / f"{stamp}-{name}-{suffix}"
        suffix += 1
    incident_dir.mkdir(parents=True)
    (incident_dir / "alert.json").write_text(json.dumps(payload, indent=2) + "\n")
    log.info("incident %s created", incident_dir.name)
    return incident_dir


def investigate(incident_dir, firing, mode):
    (incident_dir / "evidence.md").write_text(collect_evidence(firing, mode))
    with agent_lock:  # one agent at a time; later alerts wait their turn instead of being dropped
        run_agent(incident_dir, mode)


def collect_evidence(firing, mode):
    end = time.time()
    start = end - LOOKBACK_SECONDS
    sections = ["# Evidence", ""]
    for alert in firing:
        labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
        sections += [
            f"## Alert: {labels.get('alertname', 'unknown')}",
            f"- Endpoint: {labels.get('http_route') or annotations.get('endpoint') or 'none'}",
            f"- Started: {alert.get('startsAt', 'unknown')}",
            f"- Summary: {annotations.get('summary', '')}",
            f"- Description: {annotations.get('description', '')}",
            f"- Time window: {annotations.get('time_window', 'unknown')}",
            f"- Dashboard: {alert.get('dashboardURL') or annotations.get('dashboard_url', '')}",
            "",
        ]
    sections += section("## Error traces (Tempo)", lambda: error_traces(firing, start, end) if mode == "fix"
                        else "(test notification: no traces collected)")
    sections += section(f"## Logs (Loki, last {LOOKBACK_SECONDS // 60} minutes)", lambda: fenced(loki_logs(start, end)))
    if mode == "fix":
        sections += section("## Application console output (last 100 lines)", lambda: fenced(console_output()))
    return "\n".join(sections)


def section(title, build):
    """Each source is collected on its own, so one failing source does not lose the others."""
    try:
        body = build()
    except Exception as exc:
        log.exception("evidence for %s failed", title)
        body = f"(could not collect: {exc})"
    return [title, "", body, ""]


def fenced(text):
    return f"```\n{text}\n```"


def grafana_get(path, params):
    response = httpx.get(f"{GRAFANA_URL}/api/datasources/proxy/uid/{path}", params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def loki_logs(start, end):
    data = grafana_get("loki/loki/api/v1/query_range", {
        "query": f'{{service_name="{SERVICE_NAME}"}}',
        "start": int(start * 1e9), "end": int(end * 1e9), "limit": 200, "direction": "backward",
    })
    lines = []
    for stream in data["data"]["result"]:
        labels = stream["stream"]
        for ts, line in stream["values"]:
            when = datetime.fromtimestamp(int(ts) / 1e9, timezone.utc).isoformat(timespec="milliseconds")
            level = labels.get("severity_text") or labels.get("detected_level", "")
            lines.append(f"{when} {level} {line} order_id={labels.get('order_id', '-')} trace_id={labels.get('trace_id', '-')}")
    return "\n".join(sorted(lines)) or "(no logs)"


def error_traces(firing, start, end):
    routes = sorted({a.get("labels", {}).get("http_route") for a in firing} - {None})
    sections = []
    for route in routes:
        query = f'{{ resource.service.name = "{SERVICE_NAME}" && status = error && span.http.route = "{route}" }}'
        found = grafana_get("tempo/api/search", {"q": query, "start": int(start), "end": int(end), "limit": 5})
        traces = found.get("traces", [])
        sections += [describe_trace(t["traceID"]) for t in traces] or [f"(no error traces for {route})"]
    return "\n\n".join(sections)


def describe_trace(trace_id):
    data = grafana_get(f"tempo/api/traces/{trace_id}", {})
    lines = [f"### Trace {trace_id}"]
    for resource_spans in data.get("batches") or data.get("resourceSpans") or []:
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                attrs = {a["key"]: next(iter(a["value"].values()), "") for a in span.get("attributes", [])}
                status = span.get("status", {}).get("code", "")
                lines.append(f"- span `{span['name']}` status={status} "
                             + " ".join(f"{k}={v}" for k, v in attrs.items()
                                        if k in ("http.route", "http.response.status_code", "order.id")))
                for event in span.get("events", []):
                    if event.get("name") != "exception":
                        continue
                    ev = {a["key"]: next(iter(a["value"].values()), "") for a in event.get("attributes", [])}
                    lines += [f"  - exception {ev.get('exception.type')}: {ev.get('exception.message')}",
                              "```", ev.get("exception.stacktrace", "").rstrip(), "```"]
    return "\n".join(lines)


def console_output():
    result = subprocess.run(
        ["docker", "compose", "logs", "app", "--no-log-prefix", "--since", f"{LOOKBACK_SECONDS}s"],
        cwd=REPO_DIR, capture_output=True, text=True, timeout=30)
    lines = [line for line in (result.stdout + result.stderr).splitlines() if "GET /healthz" not in line]
    return "\n".join(lines[-100:]) or "(no output)"


def agent_command(incident_dir, mode):
    where = incident_dir.relative_to(REPO_DIR) if incident_dir.is_relative_to(REPO_DIR) else incident_dir
    if mode == "test":
        prompt = f"""The incident responder received a test notification. The alert and the evidence are in {where}/ (alert.json, evidence.md).

Read them and confirm that the responder delivered the alert and evidence correctly. This is a test: do not investigate or change anything, even if the evidence shows other problems. End your answer with one line that starts with "RESULT:" and states the outcome."""
        args = READ_ONLY_ARGS + ["--add-dir", str(incident_dir)]
    else:
        prompt = f"""An alert fired for the Order Tracker service. The incident responder saved the alert and the evidence in {where}/ (alert.json, evidence.md).

1. Read the alert and the evidence, and find the root cause in this repository.
2. Fix it with the smallest correct change. Add a regression test.
3. Run the tests with `uv run pytest`.
4. Rebuild and restart the app with `docker compose up --build -d --wait app`, then repeat the failing request and confirm it no longer returns a 5xx.
5. Write your findings to {where}/report.md: what happened, root cause, fix, verification.

Do not commit or push. End your answer with one line that starts with "RESULT:" and states the outcome."""
        args = FIX_ARGS + ["--add-dir", str(incident_dir)]
    return ["claude", "-p", prompt, "--output-format", "text", "--strict-mcp-config", *args]


def run_agent(incident_dir, mode):
    # Started from inside another Claude Code session, the CLI refuses to nest; drop those markers.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    log.info("incident %s: starting agent (%s mode)", incident_dir.name, mode)
    try:
        result = subprocess.run(agent_command(incident_dir, mode), cwd=REPO_DIR, env=env,
                                capture_output=True, text=True, timeout=AGENT_TIMEOUT_SECONDS)
        output = result.stdout.strip() or f"(no output, exit code {result.returncode})\n{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        output = f"Agent stopped after {AGENT_TIMEOUT_SECONDS // 60} minutes without finishing."
    (incident_dir / "agent-response.md").write_text(output + "\n")
    log.info("incident %s: agent finished\n%s", incident_dir.name, output)
