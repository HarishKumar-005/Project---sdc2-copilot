"""Production-grade Prefect 3 Deployment and Execution Infrastructure.

Provides:
- Deployment configuration and RunnerDeployment builder for `scd2_pipeline`
- Explicit, JSON-safe parameter schemas (no raw DataFrames or secrets)
- Deployment-level concurrency control (limit=1, collision_strategy=ENQUEUE)
- Local runner process serving (`serve_deployment`)
- Programmatic triggering via Prefect client (`trigger_pipeline_run`)
- Process work pool provisioning helper (`ensure_process_work_pool`)
- CLI entrypoint for local deployment operations
"""

from __future__ import annotations

import argparse
from datetime import date
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Optional, Union
from uuid import UUID

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── CRITICAL: Set PREFECT_API_URL BEFORE importing Prefect ──────────────
# Prefect reads PREFECT_API_URL during initialization. If unset at import
# time, each process starts its own ephemeral server on a random port.
# This causes the parent (serve) and child (flow subprocess) to use
# independent servers, leading to "concurrency slot lost" failures.
# Setting it here ensures ALL Prefect modules see a consistent API URL.
os.environ.setdefault("PREFECT_API_URL", "http://127.0.0.1:4200/api")
from prefect import serve
from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate
from prefect.client.schemas.filters import (
    FlowRunFilter,
    FlowRunFilterDeploymentId,
    FlowRunFilterState,
    FlowRunFilterStateType,
)
from prefect.client.schemas.objects import (
    ConcurrencyLimitConfig,
    ConcurrencyLimitStrategy,
    FlowRun,
    StateType,
)
from prefect.client.schemas.schedules import CronSchedule
from prefect.deployments import run_deployment
from prefect.deployments.runner import RunnerDeployment
from prefect.states import Cancelled, Crashed

from .config import get_settings
from .workflow import run_pipeline

# Ensure robust SQLite busy timeout for local ephemeral execution
os.environ.setdefault("PREFECT_SERVER_DATABASE_TIMEOUT", "30.0")

logger = logging.getLogger("scd2_copilot.deployment")

# Module-level reference to managed Prefect server subprocess (if started by us).
_managed_server_process: Optional[subprocess.Popen] = None


def configure_prefect_api_url(api_url: Optional[str] = None) -> str:
    """Set PREFECT_API_URL in the current process environment.

    This ensures all Prefect clients in this process and any spawned subprocesses
    connect to the same Prefect API server — eliminating the ephemeral-server
    isolation bug where parent and child processes start independent servers.

    Args:
        api_url: The Prefect API URL. If None, reads from Settings.

    Returns:
        The configured API URL.
    """
    if api_url is None:
        settings = get_settings()
        api_url = settings.prefect_api_url
    os.environ["PREFECT_API_URL"] = api_url
    logger.debug("PREFECT_API_URL set to %s", api_url)
    return api_url


def _is_server_reachable(api_url: str, timeout: float = 2.0) -> bool:
    """Check if a Prefect server is reachable at the given URL."""
    import urllib.request
    import urllib.error

    health_url = api_url.rstrip("/").removesuffix("/api") + "/api/health"
    try:
        req = urllib.request.Request(health_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_prefect_server(
    api_url: Optional[str] = None,
    startup_timeout: float = 30.0,
    host: str = "127.0.0.1",
    port: int = 4200,
) -> str:
    """Ensure a local Prefect API server is running and accessible.

    If PREFECT_API_URL is already set and reachable, does nothing.
    Otherwise, starts a managed local Prefect server subprocess.

    The managed server subprocess is automatically terminated when
    the parent process exits via atexit handler.

    Args:
        api_url: Override API URL. Defaults to Settings.prefect_api_url.
        startup_timeout: Max seconds to wait for server startup.
        host: Host to bind the local server to.
        port: Port to bind the local server to.

    Returns:
        The resolved PREFECT_API_URL.

    Raises:
        RuntimeError: If the server cannot be started or reached within timeout.
    """
    global _managed_server_process

    # Resolve target URL
    if api_url is None:
        settings = get_settings()
        api_url = settings.prefect_api_url

    # Check if already reachable (user may have started `prefect server start` manually)
    if _is_server_reachable(api_url):
        configure_prefect_api_url(api_url)
        logger.info("Prefect API server already reachable at %s", api_url)
        return api_url

    # Check if we already have a managed server process running
    if _managed_server_process is not None and _managed_server_process.poll() is None:
        # Process alive — verify it's actually serving
        if _is_server_reachable(api_url):
            configure_prefect_api_url(api_url)
            return api_url
        else:
            logger.warning("Managed server process alive but not reachable; restarting...")
            _managed_server_process.terminate()
            _managed_server_process.wait(timeout=5)
            _managed_server_process = None

    # Start a local Prefect server as a managed subprocess
    logger.info("Starting managed Prefect server on %s:%d...", host, port)

    # Set the env var BEFORE starting the server so it's inherited
    configure_prefect_api_url(api_url)

    server_cmd = [
        sys.executable, "-m", "prefect", "server", "start",
        "--host", host,
        "--port", str(port),
    ]

    # Inherit current env (which now includes PREFECT_API_URL)
    server_env = {**os.environ}
    # Suppress the server's own analytics in local mode
    server_env.setdefault("PREFECT_SERVER_ANALYTICS_ENABLED", "False")

    creation_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        creation_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    _managed_server_process = subprocess.Popen(
        server_cmd,
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **creation_kwargs,
    )

    # Register atexit handler to clean up the server process
    import atexit

    def _shutdown_managed_server() -> None:
        global _managed_server_process
        if _managed_server_process is not None and _managed_server_process.poll() is None:
            logger.info("Shutting down managed Prefect server (PID %d)...", _managed_server_process.pid)
            _managed_server_process.terminate()
            try:
                _managed_server_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _managed_server_process.kill()
            _managed_server_process = None

    atexit.register(_shutdown_managed_server)

    # Wait for the server to become healthy
    deadline = time.monotonic() + startup_timeout
    poll_interval = 0.5
    while time.monotonic() < deadline:
        if _managed_server_process.poll() is not None:
            raise RuntimeError(
                f"Prefect server process exited unexpectedly with code {_managed_server_process.returncode}"
            )
        if _is_server_reachable(api_url, timeout=2.0):
            logger.info(
                "Prefect server started successfully (PID %d) at %s",
                _managed_server_process.pid,
                api_url,
            )
            return api_url
        time.sleep(poll_interval)

    # Timed out
    _managed_server_process.terminate()
    _managed_server_process = None
    raise RuntimeError(
        f"Prefect server failed to become reachable at {api_url} within {startup_timeout}s"
    )

# ── Canonical Deployment Constants ────────────────────────
DEPLOYMENT_NAME: str = "local-processing"
FLOW_NAME: str = "scd2_pipeline"
FULL_DEPLOYMENT_NAME: str = f"{FLOW_NAME}/{DEPLOYMENT_NAME}"
WORK_POOL_NAME: str = "local-process-pool"
DEFAULT_VERSION: str = "3.3.0"
DEFAULT_TAGS: list[str] = ["scd2", "batch", "process", "local"]
CONCURRENCY_LIMIT: int = 1
COLLISION_STRATEGY: ConcurrencyLimitStrategy = ConcurrencyLimitStrategy.ENQUEUE
DEFAULT_DESCRIPTION: str = (
    "Production-grade local processing deployment for the SCD2 Copilot pipeline. "
    "Executes deterministic SCD2 change detection, transformation, invariant validation, "
    "and structured AI explanations with concurrency protection."
)


# ── Deployment Parameter Schema Contract ──────────────────
class DeploymentParameters(BaseModel):
    """Explicit, JSON/OpenAPI-compatible deployment parameter contract.

    Guarantees:
    - Only file references / paths or dataset URIs are accepted (no DataFrames).
    - When source/target are omitted, pipeline resolves to configured defaults.
    - No API keys or environment secrets are exposed or accepted.
    - Dates are ISO string or date instances.
    - Enums are validated case-insensitively.
    """

    model_config = ConfigDict(extra="forbid")

    source: Optional[str] = Field(
        default=None,
        description="Path or URI to today's source CSV dataset (defaults to configured path).",
        examples=["data/source.csv", "/data/snapshots/source_2026-09-07.csv"],
    )
    target: Optional[str] = Field(
        default=None,
        description="Path or URI to target SCD2 CSV dataset (defaults to configured path).",
        examples=["data/target_scd2.csv", "/data/snapshots/target_scd2.csv"],
    )
    processing_date: Optional[str] = Field(
        default=None,
        description="Processing date in ISO format YYYY-MM-DD (defaults to config / today).",
        examples=["2026-09-07"],
    )
    business_key_override: Optional[list[str]] = Field(
        default=None,
        description="Explicit list of business key columns (bypasses auto-detection).",
        examples=[["customer_id"], ["region", "store_id"]],
    )
    tracked_columns_override: Optional[list[str]] = Field(
        default=None,
        description="Explicit list of tracked attribute columns (bypasses auto-detection).",
        examples=[["tier", "credit_limit"]],
    )
    snapshot_mode: Optional[str] = Field(
        default="full",
        description="Snapshot interpretation mode: 'full' or 'incremental'.",
        examples=["full", "incremental"],
    )
    delete_policy: Optional[str] = Field(
        default="soft_delete",
        description="Delete policy when active keys are absent: 'soft_delete' or 'ignore'.",
        examples=["soft_delete", "ignore"],
    )
    llm_provider: Optional[str] = Field(
        default=None,
        description="Optional LLM provider override ('gemini', 'groq', 'template').",
        examples=["gemini", "groq", "template"],
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Optional custom run identifier for persistent artifacts.",
        examples=["run_20260907_120000_abc123"],
    )
    persist_artifacts: Optional[bool] = Field(
        default=True,
        description="Whether to persist execution outputs to the runs directory.",
    )
    reuse_existing: Optional[bool] = Field(
        default=True,
        description="Whether to reuse completed run results for identical fingerprints.",
    )
    force_recompute: bool = Field(
        default=False,
        description="Force full pipeline recomputation even if an existing matching run is found.",
    )

    @field_validator("source", "target")
    @classmethod
    def _validate_path_reference(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("Dataset path reference must not be empty.")
        if s.startswith("{") or s.startswith("["):
            raise ValueError("Raw serialized datasets are not allowed as deployment parameters. Use file paths.")
        return s

    @field_validator("processing_date")
    @classmethod
    def _validate_processing_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            return None
        # Validate ISO format
        try:
            date.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"processing_date must be in ISO format YYYY-MM-DD, got: '{v}'") from exc
        return s

    @field_validator("snapshot_mode")
    @classmethod
    def _validate_snapshot_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return "full"
        s = v.strip().lower()
        if s not in {"full", "incremental"}:
            raise ValueError(f"snapshot_mode must be 'full' or 'incremental', got: '{v}'")
        return s

    @field_validator("delete_policy")
    @classmethod
    def _validate_delete_policy(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return "soft_delete"
        s = v.strip().lower()
        if s not in {"soft_delete", "ignore"}:
            raise ValueError(f"delete_policy must be 'soft_delete' or 'ignore', got: '{v}'")
        return s

    @field_validator("llm_provider")
    @classmethod
    def _validate_llm_provider(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip().lower()
        if s not in {"gemini", "groq", "template"}:
            raise ValueError(f"llm_provider must be 'gemini', 'groq', or 'template', got: '{v}'")
        return s


# ── Deployment Construction & Registration ────────────────
def build_deployment(
    name: str = DEPLOYMENT_NAME,
    version: str = DEFAULT_VERSION,
    tags: Optional[list[str]] = None,
    concurrency_limit: int = CONCURRENCY_LIMIT,
    collision_strategy: ConcurrencyLimitStrategy = COLLISION_STRATEGY,
    parameters: Optional[dict[str, Any]] = None,
    description: Optional[str] = None,
    cron: Optional[str] = None,
    timezone: Optional[str] = None,
    paused: Optional[bool] = None,
) -> RunnerDeployment:
    """Construct a production-grade RunnerDeployment for the canonical SCD2 flow.

    Configures:
    - Deployment name & tags
    - Global concurrency limit = 1 with ENQUEUE collision strategy
    - Enforced parameter schema
    - Version metadata
    - Configurable schedule (CronSchedule with explicit timezone)
    """
    settings = get_settings()
    effective_tags = list(tags) if tags is not None else list(DEFAULT_TAGS)
    effective_description = description or DEFAULT_DESCRIPTION

    default_parameters: dict[str, Any] = {
        "snapshot_mode": "full",
        "delete_policy": "soft_delete",
    }
    if parameters:
        default_parameters.update(parameters)

    concurrency_config = ConcurrencyLimitConfig(
        limit=concurrency_limit,
        collision_strategy=collision_strategy,
    )

    effective_cron = cron
    effective_tz = timezone or settings.pipeline_schedule_timezone
    if effective_cron is None and settings.pipeline_schedule_active and settings.pipeline_schedule_cron:
        effective_cron = settings.pipeline_schedule_cron

    schedule_obj = None
    if effective_cron:
        schedule_obj = CronSchedule(cron=effective_cron, timezone=effective_tz)

    effective_paused = paused if paused is not None else (not settings.pipeline_schedule_active)

    deployment = run_pipeline.to_deployment(
        name=name,
        version=version,
        tags=effective_tags,
        description=effective_description,
        concurrency_limit=concurrency_config,
        parameters=default_parameters,
        schedule=schedule_obj,
        paused=effective_paused,
        enforce_parameter_schema=True,
    )
    return deployment


def apply_deployment(
    deployment: Optional[RunnerDeployment] = None,
    cron: Optional[str] = None,
    timezone: Optional[str] = None,
    paused: Optional[bool] = None,
) -> UUID:
    """Register and persist the deployment to the Prefect API/database.

    Returns:
        UUID of the registered deployment.
    """
    if deployment is None:
        deployment = build_deployment(cron=cron, timezone=timezone, paused=paused)

    deployment_id = deployment.apply()
    logger.info("Successfully applied deployment '%s' (ID: %s)", deployment.full_name, deployment_id)
    return deployment_id


def reconcile_deployment_concurrency(
    deployment_name: str = FULL_DEPLOYMENT_NAME,
    cancel_stale_scheduled: bool = False,
) -> dict[str, int]:
    """Reconcile orphaned or stuck flow runs for a deployment to prevent concurrency slot starvation.

    Inspects active/pending flow runs for the given deployment:
    1. Runs in 'Submitting' or 'Running' state from prior dead sessions are marked
       as Crashed, immediately releasing leaked concurrency slots back to 0.
    2. If cancel_stale_scheduled is True, runs in 'AwaitingConcurrencySlot' or
       'Scheduled' states are marked Cancelled.
    3. Runs in 'Scheduled' / 'AwaitingConcurrencySlot' pointing to non-existent source
       files are cancelled to prevent failure loops.

    Returns:
        Summary dict of reconciled runs: {"crashed": N, "cancelled": M}.
    """
    reconciled = {"crashed": 0, "cancelled": 0}
    try:
        with get_client(sync_client=True) as client:
            try:
                dep = client.read_deployment_by_name(name=deployment_name)
            except Exception:
                return reconciled

            flt = FlowRunFilter(
                deployment_id=FlowRunFilterDeploymentId(any_=[dep.id]),
                state=FlowRunFilterState(
                    type=FlowRunFilterStateType(any_=[
                        StateType.PENDING,
                        StateType.RUNNING,
                        StateType.SCHEDULED,
                    ])
                ),
            )
            runs = client.read_flow_runs(flow_run_filter=flt, limit=200)
            for r in runs:
                state_type = r.state.type if r.state else None
                state_name = r.state_name

                # 1. Stuck in Submitting, Pending, or Running from a prior session
                if state_type in (StateType.RUNNING, StateType.PENDING) or state_name in ("Submitting", "Pending"):
                    logger.warning(
                        "Reconciling orphaned flow run '%s' (%s, was %s) - releasing concurrency slot",
                        r.name, r.id, state_name,
                    )
                    client.set_flow_run_state(
                        flow_run_id=r.id,
                        state=Crashed(message="Reconciled orphaned run from prior interrupted session"),
                    )
                    reconciled["crashed"] += 1

                # 2. Explicit clear of scheduled backlog
                elif cancel_stale_scheduled:
                    logger.info("Cancelling queued flow run '%s' (%s) via queue clear", r.name, r.id)
                    client.set_flow_run_state(
                        flow_run_id=r.id,
                        state=Cancelled(message="Cancelled via deployment queue reconciliation"),
                    )
                    reconciled["cancelled"] += 1

                # 3. Scheduled runs whose source file does not exist on disk
                elif r.parameters and r.parameters.get("source"):
                    src_file = r.parameters.get("source")
                    if not Path(src_file).exists():
                        logger.warning(
                            "Cancelling invalid scheduled flow run '%s' (%s): source '%s' not found",
                            r.name, r.id, src_file,
                        )
                        client.set_flow_run_state(
                            flow_run_id=r.id,
                            state=Cancelled(message=f"Source file not found: {src_file}"),
                        )
                        reconciled["cancelled"] += 1

    except Exception as exc:
        logger.error("Error during deployment concurrency reconciliation: %s", exc)

    if reconciled["crashed"] > 0 or reconciled["cancelled"] > 0:
        logger.info(
            "Deployment '%s' reconciliation complete: %d crashed/unlocked, %d cancelled",
            deployment_name, reconciled["crashed"], reconciled["cancelled"],
        )
    return reconciled


def serve_deployment(
    deployment: Optional[RunnerDeployment] = None,
    limit: int = CONCURRENCY_LIMIT,
    reconcile_runs: bool = True,
) -> None:
    """Start a local runner process serving the SCD2 deployment.

    Ensures a dedicated Prefect API server is running so that the runner
    and all spawned flow subprocesses connect to the same API — preventing
    the ephemeral-server isolation bug where each process starts its own
    independent server on a random port.

    Monitors for scheduled or triggered runs and executes them in local processes.
    Automatically reconciles orphaned flow runs on startup to guarantee clean concurrency.
    """
    # CRITICAL: Ensure all processes share ONE Prefect API server.
    # Without this, serve() starts an ephemeral server on a random port,
    # but spawned flow subprocesses start ANOTHER ephemeral server on a
    # different port, causing "concurrency slot lost" failures because
    # the concurrency lease exists on the parent's server, not the child's.
    api_url = ensure_prefect_server()
    logger.info("All processes will use Prefect API at %s", api_url)

    if deployment is None:
        deployment = build_deployment()

    if reconcile_runs:
        rec = reconcile_deployment_concurrency(deployment.full_name)
        if rec["crashed"] > 0 or rec["cancelled"] > 0:
            logger.info("Self-healed deployment concurrency: %s", rec)

    logger.info("Serving deployment '%s' with concurrency limit %d...", deployment.full_name, limit)
    serve(deployment, limit=limit)


# ── Schedule Lifecycle & Operational Management ────────────
def pause_deployment_schedule(deployment_name: str = FULL_DEPLOYMENT_NAME) -> None:
    """Pause the schedule for the specified deployment."""
    with get_client(sync_client=True) as client:
        dep = client.read_deployment_by_name(name=deployment_name)
        client.pause_deployment(deployment_id=dep.id)
        logger.info("Paused deployment schedule for '%s' (ID: %s)", deployment_name, dep.id)


def resume_deployment_schedule(deployment_name: str = FULL_DEPLOYMENT_NAME) -> None:
    """Resume the schedule for the specified deployment."""
    with get_client(sync_client=True) as client:
        dep = client.read_deployment_by_name(name=deployment_name)
        client.resume_deployment(deployment_id=dep.id)
        logger.info("Resumed deployment schedule for '%s' (ID: %s)", deployment_name, dep.id)


def get_deployment_schedule_info(deployment_name: str = FULL_DEPLOYMENT_NAME) -> dict[str, Any]:
    """Retrieve operational schedule, pause state, and concurrency metadata for a deployment."""
    with get_client(sync_client=True) as client:
        dep = client.read_deployment_by_name(name=deployment_name)
        schedule_info: list[dict[str, Any]] = []
        for s in getattr(dep, "schedules", []):
            sched_obj = getattr(s, "schedule", None)
            cron_expr = getattr(sched_obj, "cron", None)
            tz = getattr(sched_obj, "timezone", None)
            schedule_info.append({
                "id": str(getattr(s, "id", "")),
                "active": getattr(s, "active", True),
                "cron": cron_expr,
                "timezone": tz,
            })

        concurrency_limit = None
        if getattr(dep, "global_concurrency_limit", None):
            concurrency_limit = getattr(dep.global_concurrency_limit, "limit", None)
        elif getattr(dep, "concurrency_options", None):
            concurrency_limit = getattr(dep.concurrency_options, "limit", None)

        collision_strategy = None
        if getattr(dep, "concurrency_options", None):
            strat = getattr(dep.concurrency_options, "collision_strategy", None)
            collision_strategy = getattr(strat, "value", str(strat)) if strat is not None else None

        return {
            "deployment_id": str(dep.id),
            "deployment_name": dep.name,
            "paused": dep.paused,
            "schedules": schedule_info,
            "concurrency_limit": concurrency_limit,
            "collision_strategy": collision_strategy,
        }


# ── Programmatic Triggering ────────────────────────────────
def trigger_pipeline_run(
    parameters: Union[dict[str, Any], DeploymentParameters],
    deployment_name: str = FULL_DEPLOYMENT_NAME,
    timeout: Optional[float] = None,
    poll_interval: float = 2.0,
    as_subflow: bool = False,
    idempotency_key: Optional[str] = None,
) -> FlowRun:
    """Programmatically trigger a run of the SCD2 deployment.

    Args:
        parameters: Validated parameters dict or DeploymentParameters instance.
        deployment_name: Name in format 'flow_name/deployment_name'.
        timeout: Wait timeout in seconds. Set to 0 to return immediately without waiting.
                 Set to None (or positive float) to block until flow run completion.
        poll_interval: Polling frequency in seconds when waiting for completion.
        as_subflow: Whether to link as a subflow if triggered within an active flow.
        idempotency_key: Optional key to prevent duplicate runs.

    Returns:
        FlowRun metadata representing the created/completed run.
    """
    if isinstance(parameters, DeploymentParameters):
        param_dict = parameters.model_dump(exclude_none=True)
    elif isinstance(parameters, dict):
        validated = DeploymentParameters.model_validate(parameters)
        param_dict = validated.model_dump(exclude_none=True)
    else:
        raise TypeError(f"parameters must be a dict or DeploymentParameters, got: {type(parameters)}")

    try:
        flow_run = run_deployment(
            name=deployment_name,
            parameters=param_dict,
            timeout=timeout,
            poll_interval=poll_interval,
            as_subflow=as_subflow,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        err_msg = str(exc).lower()
        if "not found" in err_msg or "deployment" in err_msg:
            logger.info("Deployment '%s' not found in database; registering via apply_deployment()...", deployment_name)
            apply_deployment()
            flow_run = run_deployment(
                name=deployment_name,
                parameters=param_dict,
                timeout=timeout,
                poll_interval=poll_interval,
                as_subflow=as_subflow,
                idempotency_key=idempotency_key,
            )
        else:
            raise

    logger.info("Triggered flow run '%s' (ID: %s, State: %s)", flow_run.name, flow_run.id, flow_run.state.name)
    return flow_run


# ── Deployment Run Result & Status Retrieval ──────────────
def load_deployment_run_artifacts(
    flow_run_id: Union[str, UUID],
    base_dir: Optional[Union[str, Path]] = None,
) -> tuple[str, Optional[str], Optional[Any]]:
    """Retrieve persisted M3.5 artifacts for a Prefect deployment flow run.

    Queries the Prefect client for the flow run's latest state.
    If Completed, resolves and loads the persisted run artifacts without
    re-running the SCD2 engine.

    Resolution strategy:
    1. Direct match by metadata.flow_run_id == flow_run_id.
    2. Idempotency match: computes execution fingerprint from flow parameters
       and queries find_run_by_fingerprint (handles M3.6 reused runs).
    3. Direct match by parameters["run_id"] if specified.
    4. Most recent run fallback.

    Args:
        flow_run_id: The Prefect flow run UUID or string.
        base_dir: Optional runs directory override.

    Returns:
        tuple of (state_name, error_or_state_message, Optional[PersistedRun])
    """
    from .artifacts import (
        compute_execution_fingerprint,
        find_run_by_fingerprint,
        list_runs,
        read_run_artifacts,
        run_exists,
    )

    fid_str = str(flow_run_id)
    with get_client(sync_client=True) as client:
        flow_run = client.read_flow_run(flow_run_id)

    state = flow_run.state
    state_name = state.name if state else "Unknown"
    state_msg = state.message if state else None

    if not (state and state.is_completed()):
        return (state_name, state_msg, None)

    # 1. Direct search by flow_run_id
    runs = list_runs(base_dir=base_dir)
    match = next((r for r in runs if r.flow_run_id and str(r.flow_run_id) == fid_str), None)
    if match:
        persisted = read_run_artifacts(match.run_id, base_dir=base_dir)
        return (state_name, None, persisted)

    # 2. Idempotency resolution from parameters (M3.6 reused runs)
    params = flow_run.parameters or {}
    src = params.get("source")
    tgt = params.get("target")
    if src and tgt:
        try:
            fp = compute_execution_fingerprint(
                source=src,
                target=tgt,
                processing_date=params.get("processing_date"),
                snapshot_mode=params.get("snapshot_mode"),
                delete_policy=params.get("delete_policy"),
                business_key=params.get("business_key_override"),
                tracked_columns=params.get("tracked_columns_override"),
            )
            found_meta = find_run_by_fingerprint(fp, base_dir=base_dir)
            if found_meta:
                persisted = read_run_artifacts(found_meta.run_id, base_dir=base_dir)
                return (state_name, None, persisted)
        except Exception as exc:
            logger.debug("Could not resolve execution fingerprint for flow run %s: %s", fid_str, exc)

    # 3. Direct match by parameters["run_id"]
    if params.get("run_id") and run_exists(params["run_id"], base_dir=base_dir):
        persisted = read_run_artifacts(params["run_id"], base_dir=base_dir)
        return (state_name, None, persisted)

    # 4. Fallback to latest persisted run if available
    if runs:
        persisted = read_run_artifacts(runs[0].run_id, base_dir=base_dir)
        return (state_name, None, persisted)

    return (state_name, "Run completed in Prefect, but no persisted artifacts were found.", None)


def poll_deployment_run_state(
    flow_run_id: Union[str, UUID],
    timeout: float = 120.0,
    poll_interval: float = 1.5,
    status_callback: Optional[Callable[[str, Optional[str]], None]] = None,
    base_dir: Optional[Union[str, Path]] = None,
) -> tuple[str, Optional[str], Optional[Any]]:
    """Poll a Prefect deployment flow run until it reaches a terminal state.

    Args:
        flow_run_id: The Prefect flow run UUID or string.
        timeout: Maximum seconds to poll before returning non-terminal state.
        poll_interval: Seconds to wait between polls.
        status_callback: Optional callback invoked on state transitions (state_name, message).
        base_dir: Optional runs directory override.

    Returns:
        tuple of (terminal_state_name, error_message, Optional[PersistedRun])
    """
    deadline = time.monotonic() + timeout
    last_reported_state: Optional[str] = None

    while True:
        try:
            with get_client(sync_client=True) as client:
                flow_run = client.read_flow_run(flow_run_id)

            state = flow_run.state
            state_name = state.name if state else "Unknown"
            state_msg = state.message if state else None

            if status_callback and state_name != last_reported_state:
                status_callback(state_name, state_msg)
                last_reported_state = state_name

            if state and state.is_final():
                if state.is_completed():
                    return load_deployment_run_artifacts(flow_run_id, base_dir=base_dir)
                else:
                    return (state_name, state_msg or f"Flow run ended in state {state_name}", None)

        except Exception as exc:
            logger.warning("Transient error polling flow run %s: %s", flow_run_id, exc)

        if time.monotonic() >= deadline:
            break

        time.sleep(poll_interval)

    # Timed out while still non-terminal
    return (
        last_reported_state or "Scheduled",
        f"Polling timed out after {timeout:.0f}s before flow run reached a terminal state.",
        None,
    )


# ── Dataset Staging Helper ─────────────────────────────────
def stage_dataset(
    data: Union[pl.DataFrame, bytes, str],
    name: str,
    staging_dir: Union[str, Path] = "data/staging",
) -> str:
    """Stage an in-memory DataFrame, bytes, or string to disk for deployment reference.

    Returns:
        Resolved absolute string path to the staged file.
    """
    stg = Path(staging_dir)
    stg.mkdir(parents=True, exist_ok=True)
    target_path = stg / f"{name}.csv"
    if isinstance(data, pl.DataFrame):
        data.write_csv(target_path)
    elif isinstance(data, bytes):
        target_path.write_bytes(data)
    elif isinstance(data, str):
        target_path.write_text(data, encoding="utf-8")
    else:
        raise TypeError(f"Unsupported data type for staging: {type(data)}")
    return str(target_path.resolve())


# ── Work Pool Provisioning Helper ─────────────────────────
async def ensure_process_work_pool(pool_name: str = WORK_POOL_NAME) -> Any:
    """Ensure a process work pool exists in the Prefect database."""
    async with get_client() as client:
        try:
            pool = await client.read_work_pool(pool_name)
            logger.info("Found existing work pool: '%s' (type: %s)", pool.name, pool.type)
            return pool
        except Exception:
            pool = await client.create_work_pool(
                work_pool=WorkPoolCreate(name=pool_name, type="process")
            )
            logger.info("Created process work pool: '%s'", pool.name)
            return pool


# ── CLI Interface ─────────────────────────────────────────
def parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prefect 3 Deployment & Execution CLI for SCD2 Copilot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Build and register the 'scd2_pipeline/local-processing' deployment.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start local runner process to serve the deployment and execute runs.",
    )
    parser.add_argument(
        "--create-pool",
        action="store_true",
        help="Create or verify the 'local-process-pool' work pool.",
    )
    parser.add_argument(
        "--pause",
        action="store_true",
        help="Pause scheduled execution for the deployment.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume scheduled execution for the deployment.",
    )
    parser.add_argument(
        "--inspect-schedule",
        action="store_true",
        help="Inspect current schedule status, cron expression, timezone, and concurrency limits.",
    )
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="Trigger a flow run of the deployment with the specified arguments.",
    )
    parser.add_argument(
        "--cron",
        type=str,
        default=None,
        help="Cron expression for deployment schedule (e.g. '0 2 * * *').",
    )
    parser.add_argument(
        "--timezone",
        type=str,
        default=None,
        help="Timezone for deployment schedule (e.g. 'UTC').",
    )
    parser.add_argument("--source", type=str, default=None, help="Path to source CSV file (defaults to config default).")
    parser.add_argument("--target", type=str, default=None, help="Path to target SCD2 CSV file (defaults to config default).")
    parser.add_argument("--date", type=str, default=None, help="Processing date (YYYY-MM-DD).")
    parser.add_argument("--business-keys", nargs="+", default=None, help="Business key columns.")
    parser.add_argument("--tracked-columns", nargs="+", default=None, help="Tracked attribute columns.")
    parser.add_argument(
        "--snapshot-mode",
        choices=["full", "incremental"],
        default="full",
        help="Snapshot mode.",
    )
    parser.add_argument(
        "--delete-policy",
        choices=["soft_delete", "ignore"],
        default="soft_delete",
        help="Delete policy.",
    )
    parser.add_argument(
        "--provider",
        choices=["gemini", "groq", "template"],
        default=None,
        help="LLM provider override.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="Trigger wait timeout in seconds (0 = return immediately after scheduling).",
    )
    parser.add_argument(
        "--list-runs",
        action="store_true",
        help="List all persisted pipeline runs from disk with metadata summary.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Optional custom run ID when triggering the pipeline.",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Disable persisting run artifacts to disk.",
    )
    parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="Disable reusing existing completed runs for identical fingerprints.",
    )
    parser.add_argument(
        "--force-recompute",
        action="store_true",
        help="Force recomputation of the pipeline even if an identical run exists.",
    )
    parser.add_argument(
        "--clear-queue",
        action="store_true",
        help="Cancel all queued and scheduled flow runs for the deployment.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Reconcile orphaned flow runs and release leaked concurrency slots without serving.",
    )
    return parser.parse_args(args)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    # --list-runs is purely local disk I/O; skip Prefect API configuration.
    if args.list_runs:
        from .artifacts import list_runs
        print("Persisted Pipeline Runs:", flush=True)
        runs = list_runs()
        if not runs:
            print("  No persisted runs found.", flush=True)
        else:
            for r in runs:
                ts = r.started_at or r.created_at
                fp_short = r.execution_fingerprint[:12] if r.execution_fingerprint else ""
                dedup = r.deduplication_status or ("reused" if r.is_reused else "new")
                print(
                    f"  - [{r.run_id}] {ts} | status: {dedup} | fp: {fp_short} | trigger: {r.trigger_type} | "
                    f"rows: {r.row_counts.get('output', '')} | ai: {r.ai_status} | valid: {r.validation_summary.get('pass', 0)} pass",
                    flush=True,
                )
        return 0

    # All remaining CLI commands interact with the Prefect API.
    # Ensure PREFECT_API_URL is set so this process connects to the
    # dedicated local server rather than starting an ephemeral one.
    configure_prefect_api_url()

    if args.clear_queue:
        print(f"Clearing scheduled queue for '{FULL_DEPLOYMENT_NAME}'...", flush=True)
        rec = reconcile_deployment_concurrency(FULL_DEPLOYMENT_NAME, cancel_stale_scheduled=True)
        print(f"Queue cleared: {rec['cancelled']} cancelled, {rec['crashed']} unlocked.", flush=True)
        return 0

    if args.reconcile:
        print(f"Reconciling concurrency slots for '{FULL_DEPLOYMENT_NAME}'...", flush=True)
        rec = reconcile_deployment_concurrency(FULL_DEPLOYMENT_NAME, cancel_stale_scheduled=False)
        print(f"Reconciliation complete: {rec['crashed']} unlocked, {rec['cancelled']} cleaned.", flush=True)
        return 0

    if args.pause:
        print(f"Pausing schedule for '{FULL_DEPLOYMENT_NAME}'...", flush=True)
        pause_deployment_schedule()
        print("Schedule paused successfully.", flush=True)
        return 0

    if args.resume:
        print(f"Resuming schedule for '{FULL_DEPLOYMENT_NAME}'...", flush=True)
        resume_deployment_schedule()
        print("Schedule resumed successfully.", flush=True)
        return 0

    if args.inspect_schedule:
        import json
        print(f"Inspecting schedule for '{FULL_DEPLOYMENT_NAME}'...", flush=True)
        info = get_deployment_schedule_info()
        print(json.dumps(info, indent=2), flush=True)
        return 0

    if args.apply:
        print(f"Registering deployment '{FULL_DEPLOYMENT_NAME}'...", flush=True)
        dep_id = apply_deployment(cron=args.cron, timezone=args.timezone)
        print(f"Deployment registered successfully. ID: {dep_id}", flush=True)
        return 0

    if args.serve:
        print(f"Starting runner serving '{FULL_DEPLOYMENT_NAME}' (concurrency limit = {CONCURRENCY_LIMIT})...", flush=True)
        serve_deployment()
        return 0

    if args.create_pool:
        import asyncio
        print(f"Ensuring process work pool '{WORK_POOL_NAME}' exists...", flush=True)
        pool = asyncio.run(ensure_process_work_pool())
        print(f"Work pool '{pool.name}' is ready.", flush=True)
        return 0

    if args.trigger:
        params = DeploymentParameters(
            source=args.source,
            target=args.target,
            processing_date=args.date,
            business_key_override=args.business_keys,
            tracked_columns_override=args.tracked_columns,
            snapshot_mode=args.snapshot_mode,
            delete_policy=args.delete_policy,
            llm_provider=args.provider,
            run_id=args.run_id,
            persist_artifacts=not args.no_persist,
            reuse_existing=not args.no_reuse,
            force_recompute=args.force_recompute,
        )
        print(f"Triggering deployment '{FULL_DEPLOYMENT_NAME}' with parameters:", flush=True)
        print(params.model_dump_json(indent=2), flush=True)
        flow_run = trigger_pipeline_run(params, timeout=args.timeout)
        print(f"Flow run triggered successfully!", flush=True)
        print(f"  Flow Run Name: {flow_run.name}", flush=True)
        print(f"  Flow Run ID:   {flow_run.id}", flush=True)
        print(f"  Current State: {flow_run.state.name}", flush=True)
        return 0

    # If no flags passed, print help
    parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
