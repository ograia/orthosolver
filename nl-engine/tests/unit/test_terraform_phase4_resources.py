from pathlib import Path


def test_terraform_contains_dashboards_and_alerts() -> None:
    tf = Path("infra/terraform/main.tf").read_text()
    assert 'resource "google_monitoring_dashboard" "orthos_costs"' in tf
    assert 'resource "google_monitoring_dashboard" "orthos_operational"' in tf
    assert 'resource "google_monitoring_alert_policy" "high_failure_rate"' in tf
    assert 'resource "google_monitoring_alert_policy" "queue_backlog"' in tf
    assert 'resource "google_monitoring_alert_policy" "stuck_runs"' in tf
    assert 'resource "google_monitoring_alert_policy" "timeout_spike"' in tf
    assert 'resource "google_monitoring_alert_policy" "cost_burn_anomaly"' in tf
