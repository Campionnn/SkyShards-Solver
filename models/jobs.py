# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

"""Job queue API models."""

from pydantic import BaseModel, Field
from typing import Dict, Optional, Literal

from jobs import JobProgress


class JobSubmitRequest(BaseModel):
    """Request to submit a new job."""
    type: Literal["greenhouse", "greenhouse_expansion"]
    params: Dict


class JobSubmitResponse(BaseModel):
    """Response when a job is submitted."""
    job_id: str
    status: str
    message: str = Field(default='Job queued successfully')


class JobStatusResponse(BaseModel):
    """Response with job status and progress."""
    id: str
    status: str
    request_type: str
    request_params: Dict = Field(default_factory=dict)
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Progress (when running)
    progress: Optional[JobProgress] = None

    # Queue position (when queued)
    queue_position: Optional[int] = None

    # Result (when completed)
    result: Optional[Dict] = None
    error: Optional[str] = None
