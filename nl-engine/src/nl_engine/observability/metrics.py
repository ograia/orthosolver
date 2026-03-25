from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from nl_engine.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class MetricPoint:
    metric_type: str
    value: float
    labels: dict[str, str]
    timestamp: datetime


class MetricsExporter:
    """Best-effort custom metric export.

    In local/dev environments this logs metric points. In production it can be
    switched to Cloud Monitoring by setting `ENABLE_CLOUD_MONITORING=true` and
    providing runtime credentials.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = settings.enable_cloud_monitoring
        self.project_id = settings.gcp_project_id

    def emit(self, metric_type: str, value: float, labels: dict[str, str] | None = None) -> None:
        point = MetricPoint(
            metric_type=metric_type,
            value=value,
            labels=labels or {},
            timestamp=datetime.now(UTC),
        )
        if not self.enabled:
            logger.info("metric %s value=%s labels=%s", point.metric_type, point.value, point.labels)
            return

        # Placeholder exporter behavior: keep interface stable even when Cloud
        # Monitoring client library is not available in local environments.
        try:  # pragma: no cover - optional runtime integration
            from google.cloud import monitoring_v3
            from google.protobuf import timestamp_pb2
        except ModuleNotFoundError:
            logger.warning("enable_cloud_monitoring=true but google-cloud-monitoring is not installed")
            return

        if not self.project_id:
            logger.warning("enable_cloud_monitoring=true but gcp_project_id is not set")
            return

        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{self.project_id}"
        series = monitoring_v3.TimeSeries()
        series.metric.type = f"custom.googleapis.com/orthos/{metric_type}"
        for key, val in point.labels.items():
            series.metric.labels[key] = val
        series.resource.type = "global"
        series.resource.labels["project_id"] = self.project_id

        interval = monitoring_v3.TimeInterval()
        end_time = timestamp_pb2.Timestamp()
        end_time.FromDatetime(point.timestamp)
        interval.end_time = end_time

        metric_value = monitoring_v3.TypedValue()
        metric_value.double_value = float(point.value)
        series.points.append(monitoring_v3.Point(interval=interval, value=metric_value))
        client.create_time_series(name=project_name, time_series=[series])
