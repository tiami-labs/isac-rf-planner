"""Compatibility imports for the former DVT-only passive-radar module.

New code must import :mod:`isac_rf_planner.rf.channel_analysis`. The channel
model is waveform agnostic; these names remain only so existing Python clients
can migrate without an immediate import failure.
"""

from .channel_analysis import (  # noqa: F401
    SPEED_OF_LIGHT_M_S,
    ChannelAnalysisConfig as PassiveRadarConfig,
    ChannelProcessing as PassiveRadarProcessing,
    ChannelReceiver as PassiveRadarReceiver,
    ChannelTarget as PassiveRadarTarget,
    bistatic_echo_power_dbm,
    coherent_processing_gain_db,
    free_space_path_loss_db,
    thermal_noise_power_dbm,
)

__all__ = [
    "SPEED_OF_LIGHT_M_S",
    "PassiveRadarConfig",
    "PassiveRadarProcessing",
    "PassiveRadarReceiver",
    "PassiveRadarTarget",
    "bistatic_echo_power_dbm",
    "coherent_processing_gain_db",
    "free_space_path_loss_db",
    "thermal_noise_power_dbm",
]
