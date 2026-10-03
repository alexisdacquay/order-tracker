import json

import pytest
from fastapi.testclient import TestClient

import responder

TEST_ALERT = {"alerts": [{"status": "firing",
                          "labels": {"alertname": "ResponderTest", "test": "true"},
                          "annotations": {"summary": "Test notification; no incident to fix"}}]}
GRAFANA_ALERT = {"alerts": [{"status": "firing",
                             "labels": {"alertname": "Server errors (5xx)", "grafana_folder": "Order Tracker",
                                        "http_route": "/api/orders/{order_id}", "service": "order-tracker",
                                        "severity": "critical"},
                             "annotations": {"summary": "Order Tracker is returning server errors (5xx)",
                                             "endpoint": "/api/orders/{order_id}", "time_window": "5m"}}]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "INCIDENTS_DIR", tmp_path)
    started = []
    monkeypatch.setattr(responder, "investigate", lambda d, f, mode: started.append(mode))
    monkeypatch.setattr(responder.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    yield TestClient(responder.app), tmp_path, started


def test_test_alert_is_saved_and_gets_a_look_only_agent(client):
    http, incidents, started = client
    response = http.post("/alerts", json=TEST_ALERT)
    assert response.status_code == 200 and response.json()["mode"] == "test"
    saved = next(incidents.iterdir()) / "alert.json"
    assert json.loads(saved.read_text()) == TEST_ALERT
    assert started == ["test"]


def test_grafana_alert_gets_the_fixing_agent(client):
    http, _, started = client
    assert http.post("/alerts", json=GRAFANA_ALERT).json()["mode"] == "fix"
    assert started == ["fix"]


def test_resolved_alerts_are_ignored(client):
    http, incidents, started = client
    resolved = {"alerts": [dict(GRAFANA_ALERT["alerts"][0], status="resolved")]}
    assert http.post("/alerts", json=resolved).json()["status"] == "ignored"
    assert started == [] and not any(incidents.iterdir())


def test_look_only_agent_has_no_editing_tools(tmp_path):
    command = responder.agent_command(tmp_path, "test")
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"


def test_same_second_alerts_get_separate_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(responder, "INCIDENTS_DIR", tmp_path)
    first = responder.create_incident(TEST_ALERT, TEST_ALERT["alerts"])
    second = responder.create_incident(TEST_ALERT, TEST_ALERT["alerts"])
    assert first != second
