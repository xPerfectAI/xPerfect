"""Owner-only worker configuration routes, using the existing authenticated principal."""

from fastapi import APIRouter, HTTPException, Request
from .worker_configuration import ConfigurationError, ConfigurationUpdate


def install_worker_configuration_routes(app, configuration, principal):
    router = APIRouter(prefix="/v1/workers")

    def call(fn, *args):
        try:
            return fn(*args)
        except ConfigurationError as exc:
            raise HTTPException(exc.status, {"code": exc.code}) from exc

    @router.get("/{worker_id}/configuration")
    def get(worker_id: str, request: Request):
        return call(configuration.get, *principal(request), worker_id)

    @router.put("/{worker_id}/configuration")
    def put(worker_id: str, payload: ConfigurationUpdate, request: Request):
        return call(configuration.put, *principal(request), worker_id, payload)

    @router.get("/{worker_id}/configuration/context/{source_id}")
    def read(
        worker_id: str,
        source_id: str,
        request: Request,
        offset: int = 0,
        max_chars: int = 12000,
    ):
        return call(
            configuration.read,
            *principal(request),
            worker_id,
            source_id,
            offset,
            max_chars,
        )

    app.include_router(router)
