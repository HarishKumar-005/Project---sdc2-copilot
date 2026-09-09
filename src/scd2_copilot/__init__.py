"""SCD2 Copilot — AI-assisted Slowly Changing Dimension Type 2 builder."""

__version__ = "0.1.0"

from .exceptions import DuplicateBusinessKeyError, InvalidTemporalValueError, SCD2Error
from .models import DeletePolicy, SnapshotMode

__all__ = [
    "__version__",
    "DuplicateBusinessKeyError",
    "InvalidTemporalValueError",
    "SCD2Error",
    "SnapshotMode",
    "DeletePolicy",
    "run_pipeline",
    "DeploymentParameters",
    "apply_deployment",
    "build_deployment",
    "serve_deployment",
    "trigger_pipeline_run",
]


def __getattr__(name: str):
    if name in {
        "apply_deployment",
        "build_deployment",
        "serve_deployment",
        "trigger_pipeline_run",
        "DeploymentParameters",
    }:
        from . import deployment
        return getattr(deployment, name)
    if name == "run_pipeline":
        from . import workflow
        return getattr(workflow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


