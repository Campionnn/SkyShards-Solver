# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

"""Pydantic models for the SkyShards API."""

from .crops import CropDefinition, MutationRequirement, MutationDefinition
from .solver import (
    LockedPlacement,
    MutationGoal,
    GenericSolveRequest,
    CropPlacement,
    MutationResult,
    GenericSolveResponse,
)
from .jobs import JobSubmitRequest, JobSubmitResponse, JobStatusResponse

__all__ = [
    # Crops
    "CropDefinition",
    "MutationRequirement",
    "MutationDefinition",
    # Solver
    "LockedPlacement",
    "MutationGoal",
    "GenericSolveRequest",
    "CropPlacement",
    "MutationResult",
    "GenericSolveResponse",
    "JobSubmitRequest",
    "JobSubmitResponse",
    "JobStatusResponse",
]
