"""Digital television transmitter models and link-budget helpers.

"DVT" is used as the planner technology identifier. Individual waveform presets
are represented by ``atsc1``, ``atsc3``, ``dvbt``, or ``baseline``.
"""

from __future__ import annotations

import math
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


DVTWaveform = Literal["atsc1", "atsc3", "dvbt", "baseline"]


class DVTGeometry(BaseModel):
    """Transmitter site and antenna geometry."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    altitude: float = Field(..., description="Site elevation above mean sea level, metres")
    antenna_height: float = Field(..., alias="antennaHeight", ge=0.0)
    name: str

    # Directional-pattern parameters.  Defaults describe an omnidirectional
    # horizontal pattern while retaining the existing planner's vertical model.
    azimuth_deg: float = Field(0.0, alias="azimuthDeg", ge=0.0, le=360.0)
    elevation_deg: float = Field(
        0.0,
        alias="elevationDeg",
        ge=-90.0,
        le=90.0,
        description="Antenna boresight elevation; positive is above the horizon",
    )
    beamwidth_h_deg: float = Field(360.0, alias="beamwidthHDeg", gt=0.0, le=360.0)
    beamwidth_v_deg: float = Field(8.0, alias="beamwidthVDeg", gt=0.0, le=180.0)
    electrical_tilt_deg: float = Field(0.0, alias="electricalTiltDeg", ge=-30.0, le=30.0)
    mechanical_tilt_deg: float = Field(0.0, alias="mechanicalTiltDeg", ge=-30.0, le=30.0)
    max_horizontal_attenuation_db: float = Field(
        30.0, alias="maxHorizontalAttenuationDb", ge=0.0, le=60.0
    )
    front_to_back_attenuation_db: float = Field(
        25.0, alias="frontToBackAttenuationDb", ge=0.0, le=60.0
    )
    max_vertical_attenuation_db: float = Field(
        30.0, alias="maxVerticalAttenuationDb", ge=0.0, le=60.0
    )

    @property
    def effective_down_tilt_deg(self) -> float:
        """Convert boresight elevation to the planner's positive-down convention."""

        return self.electrical_tilt_deg + self.mechanical_tilt_deg - self.elevation_deg


class DVTPower(BaseModel):
    """Transmitter power-chain inputs.

    Two unambiguous source-power forms are supported:

    * ``erpKw``: effective radiated power. Antenna gain and feeder effects are
      already contained in this value, so they are not applied a second time.
    * ``conductedPowerKw``: transmitter output before the antenna system. The
      planner applies ``txGainDb + antennaGainDbi - feederLossDb``.

    ``referenceSignalOffsetDb`` is intentionally absent here because it is a
    waveform/reference-channel allocation term, not a physical RF-chain term.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    erp_kw: Optional[float] = Field(None, alias="erpKw", gt=0.0)
    conducted_power_kw: Optional[float] = Field(None, alias="conductedPowerKw", gt=0.0)
    tx_gain_db: float = Field(0.0, alias="txGainDb")
    feeder_loss_db: float = Field(0.0, alias="feederLossDb", ge=0.0)
    antenna_gain_dbi: float = Field(0.0, alias="antennaGainDbi")
    polarization: str = ""

    @model_validator(mode="after")
    def validate_power_reference(self) -> "DVTPower":
        supplied = int(self.erp_kw is not None) + int(self.conducted_power_kw is not None)
        if supplied != 1:
            raise ValueError("supply exactly one of erpKw or conductedPowerKw")
        if self.erp_kw is not None and any(
            abs(value) > 1.0e-12
            for value in (self.tx_gain_db, self.feeder_loss_db, self.antenna_gain_dbi)
        ):
            raise ValueError(
                "erpKw already includes the antenna system; use conductedPowerKw "
                "when txGainDb, feederLossDb, or antennaGainDbi must be applied"
            )
        return self

    @property
    def input_power_dbm(self) -> float:
        power_kw = self.erp_kw if self.erp_kw is not None else self.conducted_power_kw
        assert power_kw is not None
        return 10.0 * math.log10(float(power_kw) * 1.0e6)

    @property
    def source_eirp_dbm(self) -> float:
        if self.erp_kw is not None:
            return erp_kw_to_eirp_dbm(float(self.erp_kw))
        return (
            self.input_power_dbm
            + float(self.tx_gain_db)
            - float(self.feeder_loss_db)
            + float(self.antenna_gain_dbi)
        )


class DVTStationIdentity(BaseModel):
    """Station metadata; intentionally excluded from propagation physics."""

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
    station: DVTStationIdentity = Field(default_factory=DVTStationIdentity)

    @model_validator(mode="after")
    def validate_sampling(self) -> "DVTTransmitter":
        if self.fs < self.bandwidth:
            raise ValueError("fs must be greater than or equal to bandwidth")
        return self

    @property
    def frequency_mhz(self) -> float:
        return self.fc / 1.0e6

    @property
    def bandwidth_mhz(self) -> float:
        return self.bandwidth / 1.0e6


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
    """Convert received power to equivalent field strength.

    Uses the isotropic receiving-antenna relationship:
    E[dBµV/m] = P_r[dBm] - G_r[dBi] + 20 log10(f_MHz) + 77.2.
    """

    return (
        float(received_power_dbm)
        - float(rx_gain_dbi)
        + 20.0 * math.log10(max(float(frequency_mhz), 1.0e-9))
        + 77.2
    )
