"""Digital terrestrial television transmitter and broadcast-antenna models.

``dvt`` is the planner technology identifier. Waveform presets are ``atsc1``,
``atsc3``, ``dvbt``, or ``baseline``.

A DVT transmitter is one broadcast station and one broadcast antenna system.
It is never represented as a cellular sector, serving sector, or sector group.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


DVTWaveform = Literal["atsc1", "atsc3", "dvbt", "baseline"]
DVTAntennaPatternType = Literal["omnidirectional", "parametric", "tabulated"]

DVT_MIN_CENTER_FREQUENCY_HZ = 40.0e6
DVT_MAX_CENTER_FREQUENCY_HZ = 1.0e9
DVT_MIN_CHANNEL_BANDWIDTH_HZ = 1.0e6
DVT_MAX_CHANNEL_BANDWIDTH_HZ = 10.0e6


class DVTGeometry(BaseModel):
    """Broadcast transmitter site geometry only."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    altitude: float = Field(..., description="Site elevation above mean sea level, metres")
    antenna_height: float = Field(..., alias="antennaHeight", ge=0.0)
    name: str


class DVTAzimuthPatternPoint(BaseModel):
    """One sample in a circular broadcast azimuth radiation pattern.

    Relative-field values are linear electric-field ratios. They are converted
    to power/ERP attenuation with ``20*log10(field)``. Attenuation values are dB.
    A common value can be supplied for both polarization components, or H/V
    values can be supplied separately for an elliptically polarized facility.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    azimuth_deg: float = Field(..., alias="azimuthDeg", ge=0.0, lt=360.0)
    relative_field: Optional[float] = Field(None, alias="relativeField", ge=0.0, le=1.0)
    attenuation_db: Optional[float] = Field(None, alias="attenuationDb", ge=0.0)
    relative_field_h: Optional[float] = Field(None, alias="relativeFieldH", ge=0.0, le=1.0)
    attenuation_db_h: Optional[float] = Field(None, alias="attenuationDbH", ge=0.0)
    relative_field_v: Optional[float] = Field(None, alias="relativeFieldV", ge=0.0, le=1.0)
    attenuation_db_v: Optional[float] = Field(None, alias="attenuationDbV", ge=0.0)

    @model_validator(mode="after")
    def validate_sample(self) -> "DVTAzimuthPatternPoint":
        pairs = (
            (self.relative_field, self.attenuation_db, "common"),
            (self.relative_field_h, self.attenuation_db_h, "H"),
            (self.relative_field_v, self.attenuation_db_v, "V"),
        )
        if not any(field is not None or atten is not None for field, atten, _ in pairs):
            raise ValueError("azimuth pattern point requires a relative-field or attenuation value")
        for field, atten, label in pairs:
            if field is not None and atten is not None:
                raise ValueError(f"azimuth pattern point cannot specify both {label} field and attenuation")
        return self

    @staticmethod
    def _field_from(field: Optional[float], attenuation_db: Optional[float]) -> Optional[float]:
        if field is not None:
            return float(field)
        if attenuation_db is not None:
            return 10.0 ** (-float(attenuation_db) / 20.0)
        return None

    def field_ratio(self, component: str = "common") -> float:
        common = self._field_from(self.relative_field, self.attenuation_db)
        horizontal = self._field_from(self.relative_field_h, self.attenuation_db_h)
        vertical = self._field_from(self.relative_field_v, self.attenuation_db_v)
        if component == "h":
            value = horizontal if horizontal is not None else common
        elif component == "v":
            value = vertical if vertical is not None else common
        else:
            value = common
        # A single-component filing is also a useful common pattern when the
        # other component is not independently tabulated.
        if value is None:
            value = horizontal if horizontal is not None else vertical
        return max(float(value if value is not None else 1.0), 0.0)


class DVTBroadcastAntenna(BaseModel):
    """One broadcast antenna radiation system, distinct from cellular sectors."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    pattern_type: DVTAntennaPatternType = Field("omnidirectional", alias="patternType")
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    rotation_deg: float = Field(
        0.0,
        alias="rotationDeg",
        ge=0.0,
        lt=360.0,
        description="Clockwise rotation applied to the filed/manufacturer pattern",
    )
    beam_tilt_deg: float = Field(
        0.0,
        alias="beamTiltDeg",
        ge=-30.0,
        le=30.0,
        description="Positive values point the broadcast beam downward",
    )
    vertical_beamwidth_deg: float = Field(8.0, alias="verticalBeamwidthDeg", gt=0.0, le=180.0)
    max_vertical_attenuation_db: float = Field(
        30.0, alias="maxVerticalAttenuationDb", ge=0.0, le=80.0
    )

    # Optional parametric approximation. These are broadcast-pattern terms,
    # not sector configuration or serving-cell geometry.
    main_azimuth_deg: float = Field(0.0, alias="mainAzimuthDeg", ge=0.0, lt=360.0)
    horizontal_beamwidth_deg: float = Field(
        360.0, alias="horizontalBeamwidthDeg", gt=0.0, le=360.0
    )
    max_horizontal_attenuation_db: float = Field(
        30.0, alias="maxHorizontalAttenuationDb", ge=0.0, le=80.0
    )
    front_to_back_attenuation_db: float = Field(
        25.0, alias="frontToBackAttenuationDb", ge=0.0, le=80.0
    )

    azimuth_pattern: list[DVTAzimuthPatternPoint] = Field(
        default_factory=list,
        alias="azimuthPattern",
        description="Circular relative-field or attenuation samples before rotation",
    )

    @model_validator(mode="after")
    def validate_pattern(self) -> "DVTBroadcastAntenna":
        if self.pattern_type == "tabulated":
            if len(self.azimuth_pattern) < 2:
                raise ValueError("tabulated broadcast pattern requires at least two azimuth points")
            azimuths = [round(float(point.azimuth_deg), 9) for point in self.azimuth_pattern]
            if len(set(azimuths)) != len(azimuths):
                raise ValueError("tabulated broadcast pattern azimuths must be unique")
        return self

    @staticmethod
    def _wrapped_delta_deg(a_deg: float, b_deg: float) -> float:
        return (float(a_deg) - float(b_deg) + 180.0) % 360.0 - 180.0

    def _tabulated_field_ratio(self, true_bearing_deg: float, component: str) -> float:
        sample_azimuth = (float(true_bearing_deg) - float(self.rotation_deg)) % 360.0
        points = sorted(
            (float(point.azimuth_deg), point.field_ratio(component))
            for point in self.azimuth_pattern
        )
        wrapped = [(points[-1][0] - 360.0, points[-1][1]), *points, (points[0][0] + 360.0, points[0][1])]
        for (az0, field0), (az1, field1) in zip(wrapped, wrapped[1:]):
            if az0 <= sample_azimuth <= az1:
                span = max(az1 - az0, 1.0e-12)
                fraction = (sample_azimuth - az0) / span
                return max(field0 + fraction * (field1 - field0), 0.0)
        return max(points[0][1], 0.0)

    def horizontal_attenuation_db(self, true_bearing_deg: float, component: str = "common") -> float:
        if self.pattern_type == "omnidirectional":
            return 0.0
        if self.pattern_type == "tabulated":
            field_ratio = self._tabulated_field_ratio(true_bearing_deg, component)
            return min(-20.0 * math.log10(max(field_ratio, 1.0e-6)), 120.0)

        boresight_true = (float(self.main_azimuth_deg) + float(self.rotation_deg)) % 360.0
        delta = abs(self._wrapped_delta_deg(true_bearing_deg, boresight_true))
        attenuation = min(
            12.0 * (delta / max(float(self.horizontal_beamwidth_deg), 1.0e-6)) ** 2,
            float(self.max_horizontal_attenuation_db),
        )
        if delta >= 90.0:
            attenuation = max(attenuation, float(self.front_to_back_attenuation_db))
        return attenuation

    def public_description(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class DVTPower(BaseModel):
    """Broadcast transmitter power-chain inputs.

    Supported source references:

    * ``erpKw``: one maximum ERP value for a common polarization pattern.
    * ``erpHKw``/``erpVKw``: maximum horizontal and vertical ERP components.
    * ``conductedPowerKw``: transmitter output before antenna/feed components.

    ERP inputs already include the antenna system and therefore cannot also
    apply feeder or antenna gain. Conducted power applies those components once.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    erp_kw: Optional[float] = Field(None, alias="erpKw", gt=0.0)
    erp_h_kw: Optional[float] = Field(None, alias="erpHKw", gt=0.0)
    erp_v_kw: Optional[float] = Field(None, alias="erpVKw", gt=0.0)
    conducted_power_kw: Optional[float] = Field(None, alias="conductedPowerKw", gt=0.0)
    tx_gain_db: float = Field(0.0, alias="txGainDb")
    feeder_loss_db: float = Field(0.0, alias="feederLossDb", ge=0.0)
    antenna_gain_dbi: float = Field(0.0, alias="antennaGainDbi")
    polarization: str = ""

    @model_validator(mode="after")
    def validate_power_reference(self) -> "DVTPower":
        scalar_erp = self.erp_kw is not None
        polarized_erp = self.erp_h_kw is not None or self.erp_v_kw is not None
        conducted = self.conducted_power_kw is not None
        if int(scalar_erp) + int(polarized_erp) + int(conducted) != 1:
            raise ValueError(
                "supply exactly one power basis: erpKw, erpHKw/erpVKw, or conductedPowerKw"
            )
        if (scalar_erp or polarized_erp) and any(
            abs(value) > 1.0e-12
            for value in (self.tx_gain_db, self.feeder_loss_db, self.antenna_gain_dbi)
        ):
            raise ValueError(
                "ERP already includes the antenna system; use conductedPowerKw when "
                "txGainDb, feederLossDb, or antennaGainDbi must be applied"
            )
        return self

    @staticmethod
    def _kw_to_dbm(power_kw: float) -> float:
        return 10.0 * math.log10(float(power_kw) * 1.0e6)

    @property
    def input_power_dbm(self) -> float:
        if self.conducted_power_kw is not None:
            return self._kw_to_dbm(self.conducted_power_kw)
        if self.erp_kw is not None:
            return self._kw_to_dbm(self.erp_kw)
        return self._kw_to_dbm(float(self.erp_h_kw or 0.0) + float(self.erp_v_kw or 0.0))

    @property
    def source_eirp_dbm(self) -> float:
        if self.erp_kw is not None:
            return erp_kw_to_eirp_dbm(float(self.erp_kw))
        if self.erp_h_kw is not None or self.erp_v_kw is not None:
            total_erp_kw = float(self.erp_h_kw or 0.0) + float(self.erp_v_kw or 0.0)
            return erp_kw_to_eirp_dbm(total_erp_kw)
        return (
            self.input_power_dbm
            + float(self.tx_gain_db)
            - float(self.feeder_loss_db)
            + float(self.antenna_gain_dbi)
        )

    @property
    def has_polarized_erp_components(self) -> bool:
        return self.erp_h_kw is not None or self.erp_v_kw is not None


class DVTStationIdentity(BaseModel):
    """Station metadata; excluded from propagation physics."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    call_sign: Optional[str] = Field(None, alias="callSign")
    virtual_channel: Optional[Union[str, int, float]] = Field(None, alias="virtualChannel")
    rf_channel: Optional[Union[str, int]] = Field(None, alias="rfChannel")
    physical_channel: Optional[Union[str, int]] = Field(None, alias="physicalChannel")
    facility_id: Optional[Union[str, int]] = Field(None, alias="facilityId")
    city: Optional[str] = None
    network: Optional[str] = None
    standard: Optional[str] = None
    band: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = Field(None, alias="sourceUrl")


class DVTTransmitter(BaseModel):
    """Complete per-transmitter DVT input model."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    fc: float = Field(..., gt=0.0, description="Center frequency in Hz")
    fs: float = Field(..., gt=0.0, description="Waveform-preset sample rate in Hz")
    bandwidth: float = Field(..., gt=0.0, description="Analog/channel bandwidth in Hz")
    waveform: DVTWaveform
    tx: DVTGeometry
    power: DVTPower
    antenna: DVTBroadcastAntenna = Field(default_factory=DVTBroadcastAntenna)
    station: DVTStationIdentity = Field(default_factory=DVTStationIdentity)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_tx_pattern(cls, value: Any) -> Any:
        """Accept old payloads while moving directionality out of ``tx``.

        The resulting validated model always exposes a separate ``antenna``
        object. This compatibility path does not reintroduce sectors.
        """

        if not isinstance(value, dict) or "antenna" in value:
            return value
        payload = dict(value)
        tx = payload.get("tx")
        if not isinstance(tx, dict):
            return payload
        tx_copy = dict(tx)
        legacy_names = {
            "azimuthDeg",
            "elevationDeg",
            "beamwidthHDeg",
            "beamwidthVDeg",
            "electricalTiltDeg",
            "mechanicalTiltDeg",
            "maxHorizontalAttenuationDb",
            "frontToBackAttenuationDb",
            "maxVerticalAttenuationDb",
        }
        if not any(name in tx_copy for name in legacy_names):
            return payload
        azimuth = float(tx_copy.pop("azimuthDeg", 0.0)) % 360.0
        elevation = float(tx_copy.pop("elevationDeg", 0.0))
        beamwidth_h = float(tx_copy.pop("beamwidthHDeg", 360.0))
        electrical_tilt = float(tx_copy.pop("electricalTiltDeg", 0.0))
        mechanical_tilt = float(tx_copy.pop("mechanicalTiltDeg", 0.0))
        payload["tx"] = tx_copy
        payload["antenna"] = {
            "patternType": "omnidirectional" if beamwidth_h >= 360.0 else "parametric",
            "mainAzimuthDeg": azimuth,
            "horizontalBeamwidthDeg": beamwidth_h,
            "verticalBeamwidthDeg": float(tx_copy.pop("beamwidthVDeg", tx.get("beamwidthVDeg", 8.0))),
            "beamTiltDeg": electrical_tilt + mechanical_tilt - elevation,
            "maxHorizontalAttenuationDb": float(
                tx_copy.pop("maxHorizontalAttenuationDb", tx.get("maxHorizontalAttenuationDb", 30.0))
            ),
            "frontToBackAttenuationDb": float(
                tx_copy.pop("frontToBackAttenuationDb", tx.get("frontToBackAttenuationDb", 25.0))
            ),
            "maxVerticalAttenuationDb": float(
                tx_copy.pop("maxVerticalAttenuationDb", tx.get("maxVerticalAttenuationDb", 30.0))
            ),
        }
        # Remove any fields popped after assigning tx_copy.
        payload["tx"] = {key: val for key, val in tx_copy.items() if key not in legacy_names}
        return payload

    @model_validator(mode="after")
    def validate_sampling(self) -> "DVTTransmitter":
        if not DVT_MIN_CENTER_FREQUENCY_HZ <= self.fc <= DVT_MAX_CENTER_FREQUENCY_HZ:
            raise ValueError(
                "DVT fc must be between 40 MHz and 1000 MHz; "
                f"received {self.fc / 1.0e6:g} MHz"
            )
        if not DVT_MIN_CHANNEL_BANDWIDTH_HZ <= self.bandwidth <= DVT_MAX_CHANNEL_BANDWIDTH_HZ:
            raise ValueError(
                "DVT bandwidth must be between 1 MHz and 10 MHz; "
                f"received {self.bandwidth / 1.0e6:g} MHz"
            )
        if self.fs < self.bandwidth:
            raise ValueError("fs must be greater than or equal to bandwidth")
        return self

    @property
    def frequency_mhz(self) -> float:
        return self.fc / 1.0e6

    @property
    def bandwidth_mhz(self) -> float:
        return self.bandwidth / 1.0e6

    def effective_source_eirp_dbm(self, true_bearing_deg: float) -> float:
        """Maximum source power after the broadcast azimuth pattern at a bearing."""

        if self.power.has_polarized_erp_components:
            components_mw: list[float] = []
            for component, erp_kw in (("h", self.power.erp_h_kw), ("v", self.power.erp_v_kw)):
                if erp_kw is None:
                    continue
                eirp_dbm = erp_kw_to_eirp_dbm(float(erp_kw))
                attenuation_db = self.antenna.horizontal_attenuation_db(true_bearing_deg, component)
                components_mw.append(10.0 ** ((eirp_dbm - attenuation_db) / 10.0))
            return 10.0 * math.log10(max(sum(components_mw), 1.0e-15))

        attenuation_db = self.antenna.horizontal_attenuation_db(true_bearing_deg, "common")
        return float(self.power.source_eirp_dbm) - attenuation_db


def erp_kw_to_erp_dbm(erp_kw: float) -> float:
    """Convert effective radiated power in kW to dBm (dipole-referenced ERP)."""

    if erp_kw <= 0.0:
        raise ValueError("erp_kw must be greater than zero")
    return 10.0 * math.log10(erp_kw * 1.0e6)


def erp_kw_to_eirp_dbm(erp_kw: float) -> float:
    """Convert dipole-referenced ERP in kW to isotropic EIRP in dBm."""

    return erp_kw_to_erp_dbm(erp_kw) + 2.15


def received_power_to_field_strength_dbuv_m(
    received_power_dbm: float,
    frequency_mhz: float,
    rx_gain_dbi: float = 0.0,
) -> float:
    """Convert received power to equivalent field strength."""

    return (
        float(received_power_dbm)
        - float(rx_gain_dbi)
        + 20.0 * math.log10(max(float(frequency_mhz), 1.0e-9))
        + 77.2
    )
