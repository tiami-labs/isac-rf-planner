# Exemplar outputs

This directory collects representative exports from ISAC RF Planner. The figures illustrate construction of a geometry-consistent RF digital twin, multi-waveform illumination of that twin (5G NR and DVT/broadcast), and joint ISAC evaluation of communications coverage and bistatic sensing metrics on a shared world model.

Unless otherwise noted, coverage heatmaps use a blue-to-red false-color scale for received level, mapped approximately from -140 dBm to -60 dBm. Product-specific maps (detectability, delay, SNR) use scales defined by their respective estimators.

Request schemas and analysis workflows: [`examples/`](../../examples/), [`DVT_SUPPORT.md`](../../DVT_SUPPORT.md), [`ISAC_CHANNEL_ANALYSIS.md`](../../ISAC_CHANNEL_ANALYSIS.md).

---

## 1. RF digital twin

A single scene is represented at three levels of presentation.

**Structural twin** (built environment and transmitter markers; no RF field):

![Structural digital twin](nr-5g-sensing/digital_twin_geometry.jpg)

**Twin with predicted RF field** (propagation overlay on the same geometry):

![Digital twin with RF field](nr-5g-sensing/digital_twin_with_rf.jpg)

**Extended area of interest** (identical twin parameters, larger geospatial extent):

![Digital twin, extended AOI](nr-5g-sensing/digital_twin_wide_aoi.jpg)

---

## 2. 5G NR illumination of the twin

### Sectorized cellular site

Three co-sited sectors at a common midband carrier. Fixed twin and path-loss configuration; varying azimuth yields distinct canyon guidance and shadowed regions.

![Sector azimuth approximately 70 deg](nr-5g-sensing/nr_sector_az070.png)

![Sector azimuth approximately 200 deg](nr-5g-sensing/nr_sector_az200.png)

![Sector azimuth approximately 310 deg](nr-5g-sensing/nr_sector_az310.png)

### Multi-site configuration

An additional site on the same twin, with independent pointing and coverage footprint:

![Neighbor-site sector coverage](nr-5g-sensing/nr_neighbor_site_sector.png)

### Site-aggregated field

Composite coverage after aggregation of sectors belonging to one transmitter site:

![Site-aggregated coverage overlay](nr-5g-sensing/nr_site_aggregated_overlay.png)

### Omnidirectional NR pattern

Identical NR planning stack with an omnidirectional antenna pattern rather than a three-sector partition:

![Omnidirectional NR coverage](nr-5g-sensing/nr_omni_coverage.png)

### Plan record

[`nr-5g-sensing/plan_record.json`](nr-5g-sensing/plan_record.json) stores the machine-readable plan parameters associated with the NR figures: carrier frequency, path-loss model, ray mode, and per-sector azimuth, beamwidth, power, and PCI. It is required for reproducibility and for quantitative audit of any heatmap derived from the twin.

---

## 3. DVT / broadcast illumination and joint ISAC products

Terrestrial broadcast (DVT) illumination uses a single station and radiation system (ERP- or conducted-power based), distinct from cellular sectorization, on a large-extent twin.

**Predicted broadcast coverage on the twin:**

![Broadcast twin with RF field](dvt-sensing/broadcast_twin_with_rf.jpg)

![Broadcast coverage field](dvt-sensing/broadcast_coverage.png)

### Joint ISAC products

The following rasters are computed on the same twin and illuminator. They are sensing observables, not redundant copies of the communications coverage map.

| Product | Definition |
| --- | --- |
| Primary coverage | Forward illuminator field used as the sensing baseline |
| ISAC target quality | Scalar quality metric over candidate target locations |
| Bistatic detectability | Spatial map of target detectability for the TX-target-RX geometry |
| Bistatic post-processing SNR | Sensing SNR after the configured processing chain |
| Bistatic excess delay | Excess propagation delay relative to the geometric baseline path |

![Primary coverage](dvt-sensing/isac-products/primary_coverage.png)

![ISAC target quality](dvt-sensing/isac-products/isac_target_quality.png)

![Bistatic detectability](dvt-sensing/isac-products/bistatic_detectability.png)

![Bistatic post-processing SNR](dvt-sensing/isac-products/bistatic_postprocessing_snr.png)

![Bistatic excess delay](dvt-sensing/isac-products/bistatic_excess_delay.png)

### Sensing configuration record

[`dvt-sensing/sensing_config.json`](dvt-sensing/sensing_config.json) records illuminator power and antenna parameters together with the analysis receiver, target (height, bistatic RCS), and motion/processing assumptions used to generate the ISAC products. Interpretation of detectability, quality, and delay maps requires this configuration; the rasters alone are insufficient for scientific or engineering review.

---

## Reproduction

Issue plan and channel-analysis requests via the HTTP API or UI using the payloads under [`examples/`](../../examples/). Enable bistatic channel analysis to emit ISAC products. Export twin views, coverage heatmaps, and the associated plan/sensing records. See [`DVT_SUPPORT.md`](../../DVT_SUPPORT.md) and [`ISAC_CHANNEL_ANALYSIS.md`](../../ISAC_CHANNEL_ANALYSIS.md).
