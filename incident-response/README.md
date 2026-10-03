# Incident responder

Receives Grafana alerts at `POST /alerts` on port 8001. For each firing alert it saves, in `incidents/<time>-<alert>/`:

- `alert.json`: the alert as received
- `evidence.md`: affected endpoint, error traces with stack traces (Tempo), logs (Loki), app console output
- `agent-response.md`: the answer of the coding agent it starts in headless mode (`claude -p`)

A test notification (label `test="true"`) or an alert without an endpoint gets a look-only agent (read, search). A real alert gets an agent that can fix the code, run the tests and restart the app. It does not commit.

## Start

From the repository root, with the Docker Compose stack running:

```bash
uv run --directory incident-response uvicorn responder:app --host 127.0.0.1 --port 8001
```

Grafana, running in Docker, reaches it at `http://host.docker.internal:8001/alerts`.

## Test

```bash
uv run --directory incident-response pytest
```
