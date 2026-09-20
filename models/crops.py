# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

"""Crop and mutation definition models."""

from pydantic import BaseModel, Field
from typing import List, Dict, Optional


class CropDefinition(BaseModel):
    """Definition of a crop type."""
    name: str
    size: int = Field(1, ge=1, le=3)
    priority: int = Field(0, ge=0, le=100)
    ground: str = 'farmland'
    growth_stages: Optional[int] = None
    positive_buffs: List[str] = Field(default_factory=list)
    negative_buffs: List[str] = Field(default_factory=list)
    sell_price: Optional[float] = None


class MutationRequirement(BaseModel):
    """Requirement for a specific crop in a mutation."""
    crop: str
    count: int = Field(..., ge=1)


class MutationDefinition(BaseModel):
    """Definition of a mutation type."""
    name: str
    size: int = Field(1, ge=1, le=3)
    requirements: List[MutationRequirement] = Field(default_factory=list)
    requires_zero_adjacent: bool = False
    ground: str = 'farmland'
    rarity: str = 'common'
    growth_stages: int = 0
    decay: float = 0
    positive_buffs: List[str] = Field(default_factory=list)
    negative_buffs: List[str] = Field(default_factory=list)
    drops: Dict[str, int] = Field(default_factory=dict)
    special: Optional[str] = None
    harvest_info: Optional[str] = None
    growing_info: Optional[str] = None
    mutation_chance: float = 0.1
    spawn_weight: int = Field(0, ge=0)
