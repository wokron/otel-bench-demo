from pathlib import Path

from demo_api.app import Target
from demo_api.config import read_config


def test_existing_full_and_catalog_have_separate_scopes_and_identity():
    config = read_config(str(Path(__file__).resolve().parents[1] / 'configs/otel-multi.yaml'))
    targets = {t.id: Target(t) for t in config.targets}
    catalog, full = targets["otel-demo"], targets["otel-full-existing"]
    assert catalog.config.id == 'otel-demo'
    assert catalog.discovery.kube.namespace == 'otel-demo'
    assert full.config.id == 'otel-full-existing'
    assert full.discovery.kube.namespace == 'default'
    assert full.telemetry['metrics'].config['service'] == 'prometheus'
    assert catalog.telemetry['metrics'].config['service'] == 'otel-demo-prometheus'
    assert full.discovery.service_id('checkout') != catalog.discovery.service_id('checkout')
    assert full.actuator.target == 'otel-full-existing'
    assert full.actuator.kube.namespace == 'default'
    assert full.telemetry['logs'].describe()['warnings']
    metrics = full.telemetry['metrics'].describe()['metrics']
    assert {m['unit'] for m in metrics} >= {'spans/s', 'ms', 'ratio', 'bytes'}
    assert all('services' in m for m in metrics)
