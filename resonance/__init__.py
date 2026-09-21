"""Configuration-driven sector/theme resonance aggregation layer."""

from .config import ResonanceConfig, ThemeConfig, load_resonance_config
from .engine import ResonanceEngine
from .models import Evidence, ResonanceEvent, SignalCluster, SignalObservation

__all__ = [
    "Evidence",
    "ResonanceConfig",
    "ResonanceEngine",
    "ResonanceEvent",
    "SignalCluster",
    "SignalObservation",
    "ThemeConfig",
    "load_resonance_config",
]
