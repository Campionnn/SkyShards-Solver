# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import inspect
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

import config
import contribute
import engine
from gamedata import DEFAULT_DATA
from jobs import JobStatus, get_job_manager, init_job_manager
from models import (
    GenericSolveRequest,
    GenericSolveResponse,
    JobStatusResponse,
    JobSubmitRequest,
    JobSubmitResponse,
)
from worker import init_worker, stop_worker

NAME = "skyshards-local-solver"
PORT = config.PORT
HOST = "127.0.0.1"

engine.SOLVER_THREADS = config.SOLVER_THREADS

ORIGIN_PATTERN = r"https://([a-z0-9-]+\.)?skyshards\.com"
DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def read_version() -> str:
    try:
        with open(os.path.join(HERE, "VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "dev"


VERSION = read_version()


def allowed_origins():
    return DEFAULT_ORIGINS + config.EXTRA_ORIGINS


def solve_job(request_params: Dict, job_id: str, job_manager) -> Dict:
    """Worker entry point: the shared engine, honouring the website's time limit."""
    if request_params.get("_request_type", "greenhouse") != "greenhouse":
        raise ValueError("only greenhouse solves run in the local solver")
    result = engine.run_job(request_params, job_id, job_manager, allow_client_time_limit=True)
    if isinstance(result, dict):
        result.pop("_polish_eligible", None)
        params = {k: v for k, v in request_params.items() if not k.startswith("_")}
        contribute.offer(params, result)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_mgr = init_job_manager(db_path=os.path.join(HERE, "local_jobs.db"), expiration_hours=6.0)
    job_mgr.mark_interrupted_as_failed()
    init_worker(job_mgr, solve_job, num_workers=1)
    print_banner()
    yield
    stop_worker()


app = FastAPI(title="SkyShards local solver", version=VERSION, lifespan=lifespan)

_cors_kwargs = dict(allow_origins=allowed_origins(), allow_origin_regex=ORIGIN_PATTERN,
                    allow_methods=["*"], allow_headers=["*"])
if "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters:
    _cors_kwargs["allow_private_network"] = True
app.add_middleware(CORSMiddleware, **_cors_kwargs)


@app.middleware("http")
async def private_network_access(request: Request, call_next):
    response = await call_next(request)
    if request.method == "OPTIONS":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


@app.get("/local/health")
async def health():
    import ortools
    return {
        "ok": True,
        "name": NAME,
        "version": VERSION,
        "ortools": getattr(ortools, "__version__", "unknown"),
        "contribute": contribute.ENABLED,
        "solver_threads": config.SOLVER_THREADS,
    }


@app.get("/greenhouse/defaults")
async def get_defaults():
    return DEFAULT_DATA


@app.post("/greenhouse/jobs", response_model=JobSubmitResponse)
async def submit_job(request: JobSubmitRequest):
    if request.type != "greenhouse":
        raise HTTPException(
            status_code=400,
            detail=f"Job type '{request.type}' is not available in the local solver; use api.skyshards.com",
        )
    job_mgr = get_job_manager()
    params = {**request.params, "_request_type": request.type}
    job_id = job_mgr.create_job(request_type=request.type, request_params=params)
    return JobSubmitResponse(job_id=job_id, status=JobStatus.QUEUED.value, message="Job queued successfully")


@app.get("/greenhouse/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    job_mgr = get_job_manager()
    job = job_mgr.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    queue_position = job_mgr.get_queue_position(job_id) if job.status == JobStatus.QUEUED else None
    visible_params = {k: v for k, v in job.request_params.items() if not k.startswith("_")}
    return JobStatusResponse(
        id=job.id,
        status=job.status.value,
        request_type=job.request_type,
        request_params=visible_params,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        progress=job.progress,
        queue_position=queue_position,
        result=job.result,
        error=job.error,
    )


@app.delete("/greenhouse/jobs/{job_id}")
async def cancel_job(job_id: str):
    job_mgr = get_job_manager()
    job = job_mgr.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status_code=400, detail=f"Cannot cancel job with status: {job.status.value}")
    if not job_mgr.cancel_job(job_id):
        raise HTTPException(status_code=500, detail="Failed to cancel job")
    return {"message": "Job cancelled", "job_id": job_id}


@app.post("/greenhouse/solver", response_model=GenericSolveResponse)
def solve_direct(payload: Dict = Body(...), maximize_only: bool = False, time_limit: Optional[float] = None):
    """Same body handling as the public endpoint (flat or {"req": ...})."""
    if isinstance(payload.get("req"), dict) and "cells" not in payload:
        payload = payload["req"]
    payload.pop("unique_crops", None)
    try:
        req = GenericSolveRequest(**payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=f"Invalid solve request: {e}")
    result = engine.solve_generic(
        req, maximize_only=maximize_only, time_limit=engine.clamp_client_time_limit(time_limit)
    )
    if isinstance(result, dict):
        result.pop("_polish_eligible", None)
        if not maximize_only:
            contribute.offer(req.model_dump(mode="json"), result)
    return result


def print_banner():
    print("")
    print("=" * 62)
    print(f"  SkyShards local solver v{VERSION}")
    print(f"  Listening on http://{HOST}:{PORT}")
    print("")
    print("  Keep this window open. Then open https://greenhouse.skyshards.com,")
    print("  and turn on 'Solve locally' in the calculator.")
    if contribute.ENABLED:
        print("")
        print("  Layouts that beat the public server's cached solution are")
        print("  uploaded so everyone benefits. Set \"contribute\": false in")
        print("  config.json to opt out.")
    print("=" * 62)
    print("", flush=True)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
