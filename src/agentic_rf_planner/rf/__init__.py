"""RF simulation modules with lazy exports to avoid schema import cycles."""

__all__ = [
    "MaterialRFProps",
    "DEFAULT_MATERIAL_DB",
    "DVTGeometry",
    "DVTBroadcastAntenna",
    "DVTAzimuthPatternPoint",
    "DVTPower",
    "DVTStationIdentity",
    "DVTTransmitter",
    "compute_attenuation_grid",
]


def __getattr__(name: str):
    if name in {"MaterialRFProps", "DEFAULT_MATERIAL_DB"}:
        from .material_models import DEFAULT_MATERIAL_DB, MaterialRFProps

        return {"MaterialRFProps": MaterialRFProps, "DEFAULT_MATERIAL_DB": DEFAULT_MATERIAL_DB}[name]
    if name in {"DVTGeometry", "DVTBroadcastAntenna", "DVTAzimuthPatternPoint", "DVTPower", "DVTStationIdentity", "DVTTransmitter"}:
        from .dvt import (DVTGeometry, DVTBroadcastAntenna, DVTAzimuthPatternPoint, DVTPower, DVTStationIdentity, DVTTransmitter)

        return {
            "DVTGeometry": DVTGeometry,
            "DVTBroadcastAntenna": DVTBroadcastAntenna,
            "DVTAzimuthPatternPoint": DVTAzimuthPatternPoint,
            "DVTPower": DVTPower,
            "DVTStationIdentity": DVTStationIdentity,
            "DVTTransmitter": DVTTransmitter,
        }[name]
    if name == "compute_attenuation_grid":
        from .attenuation_models import compute_attenuation_grid

        return compute_attenuation_grid
    raise AttributeError(name)
