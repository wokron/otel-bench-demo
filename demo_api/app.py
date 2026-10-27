import contextlib
import fcntl
import hmac
import os
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from demo_api.config import read_config
from demo_api.experiments import Experiments
from demo_api.models import APIError, ActionRequest, Query
from demo_api.plugins import load_provider


class Target:
    def __init__(self, config):
        self.config = config
        context = {"target_id": config.id}
        self.discovery = load_provider(config.discovery.plugin, config.discovery.config, context)
        context["discovery"] = self.discovery
        self.telemetry = {signal: load_provider(p.plugin, p.config, context) for signal, p in config.telemetry.items()}
        self.actuator = load_provider(config.actions.plugin, config.actions.config, context) if config.actions else None

    def capabilities(self):
        return {"target_id": self.config.id,
                "entity_kinds": ["service", "pod"], "relation_kinds": ["contains"],
                "telemetry": {signal: self.telemetry[signal].describe() if signal in self.telemetry
                              else {"configured": False, "reason": "No provider configured"}
                              for signal in ("metrics", "logs", "traces")},
                "actions": self.actuator.describe() if self.actuator else [],
                "limits": {"window_seconds": 3600, "query_limit": 1000, "active_experiments": 1},
                "availability": "configured capabilities; query errors report live backend availability"}


def create_app(config_path=None):
    config = read_config(config_path or os.environ.get("DEMO_API_CONFIG", "configs/otel-catalog.yaml"))
    targets = {t.id: Target(t) for t in config.targets}
    token = os.environ.get("DEMO_API_TOKEN")

    def auth(authorization: str | None = Header(default=None)):
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise APIError("unauthorized", "Valid Bearer token required", 401)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        Path(config.state_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config.state_path + ".lock", "a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("State database is already in use; run exactly one API worker") from exc
            store = Experiments(config.state_path, targets)
            app.state.experiments = store
            stop = threading.Event()
            def reconcile():
                while not stop.is_set():
                    store.reconcile()
                    stop.wait(1)
            worker = threading.Thread(target=reconcile, name="experiment-reconciler", daemon=True)
            worker.start()
            try:
                yield
            finally:
                stop.set()
                # Backend calls have bounded timeouts; wait before closing the shared database.
                worker.join()
                store.db.close()

    app = FastAPI(title="Microservice Demo API", version="1.0.0", lifespan=lifespan,
                  dependencies=[Depends(auth)])

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(ValidationError)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": str(exc)}})

    def target(target_id):
        if target_id not in targets:
            raise APIError("target_not_found", f"Unknown target {target_id!r}", 404)
        return targets[target_id]

    @app.get("/healthz")
    def health():
        return {"status": "ok", "spec_version": "demo-api/v1"}

    @app.get("/v1/targets")
    def list_targets():
        return {"targets": [{"id": t.config.id, "description": t.config.description} for t in targets.values()]}

    @app.get("/v1/targets/{target_id}/capabilities")
    def capabilities(target_id: str):
        return target(target_id).capabilities()

    @app.get("/v1/targets/{target_id}/entities")
    def entities(target_id: str):
        return {"entities": target(target_id).discovery.entities()}

    @app.get("/v1/targets/{target_id}/relations")
    def relations(target_id: str):
        return {"relations": target(target_id).discovery.relations(),
                "warnings": ["Structural membership only; not a service call/dependency graph."]}

    @app.post("/v1/targets/{target_id}/query")
    def query(target_id: str, body: Query):
        t = target(target_id)
        t.discovery.resolve(body.service)
        provider = t.telemetry.get(body.signal)
        if provider is None:
            raise APIError("unsupported_signal", f"No {body.signal} provider configured", 422)
        return {"target_id": target_id, "service": body.service,
                "window": {"start": body.start, "end": body.end}, "data": provider.query(body)}

    @app.post("/v1/targets/{target_id}/experiments")
    def create_experiment(target_id: str, body: ActionRequest, request: Request,
                          idempotency_key: str | None = Header(default=None)):
        target(target_id)
        return request.app.state.experiments.create(target_id, body, idempotency_key)

    @app.get("/v1/targets/{target_id}/experiments")
    def experiments(target_id: str, request: Request):
        target(target_id)
        return {"experiments": request.app.state.experiments.list(target_id)}

    @app.get("/v1/targets/{target_id}/experiments/{experiment_id}")
    def experiment(target_id: str, experiment_id: str, request: Request):
        target(target_id)
        return request.app.state.experiments.refresh(target_id, experiment_id)

    @app.delete("/v1/targets/{target_id}/experiments/{experiment_id}")
    def cancel(target_id: str, experiment_id: str, request: Request):
        target(target_id)
        return request.app.state.experiments.cancel(target_id, experiment_id)

    return app
