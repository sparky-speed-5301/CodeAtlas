"""Compatibility import surface for Work Package 2 providers."""

from codeatlas.providers.repair import (
    DeterministicRepairProvider,
    LiveRepairProvider,
    RealRepairProvider,
    StructuredRepairProvider,
)
from codeatlas.providers.repair_config import RepairProviderConfig, load_repair_provider_config
from codeatlas.providers.repair_models import (
    RepairCandidate,
    RepairFinding,
    RepairLanguage,
    RepairLanguageCapabilities,
    RepairOutput,
    RepairPolicy,
    RepairRequest,
    RepairResponse,
)

__all__ = [
    "DeterministicRepairProvider",
    "LiveRepairProvider",
    "RealRepairProvider",
    "RepairCandidate",
    "RepairFinding",
    "RepairLanguage",
    "RepairLanguageCapabilities",
    "RepairOutput",
    "RepairPolicy",
    "RepairProviderConfig",
    "RepairRequest",
    "RepairResponse",
    "StructuredRepairProvider",
    "load_repair_provider_config",
]
