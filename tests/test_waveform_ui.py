"""Static and executable checks for the waveform selector UI."""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "agentic_rf_planner" / "ui" / "static"


@pytest.mark.parametrize("filename", ["index.html", "index_3d.html"])
def test_waveform_selector_is_present_on_both_planners(filename):
    html = (STATIC / filename).read_text(encoding="utf-8")

    assert 'id="waveform-profile"' in html
    assert 'value="5g_nr"' in html
    assert 'value="atsc1"' in html
    assert 'value="atsc3"' in html
    assert 'value="dvbt"' in html
    assert 'value="baseline"' in html
    assert 'id="dvt-transmitter-fields"' in html
    assert 'id="dvt-fc-mhz"' in html
    assert 'id="dvt-bandwidth-mhz"' in html
    assert 'id="dvt-noise-figure-db"' in html
    assert 'id="dvt-rx-antenna-gain-dbi"' in html
    assert 'id="dvt-termination-power-dbm"' in html
    assert 'id="sector-configuration-section"' in html
    assert 'id="coverage-layer-rsrp"' in html
    assert '<script src="/waveform_ui.js"></script>' in html


def test_waveform_ui_builds_explicit_atsc3_request():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script_path = STATIC / "waveform_ui.js"
    js = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        global.window = {{}};
        const values = {{
          'waveform-profile': 'atsc3',
          'dvt-fc-mhz': '587',
          'dvt-fs-mhz': '10',
          'dvt-bandwidth-mhz': '6',
          'tx-height-m': '320',
          'dvt-site-altitude-m': '18',
          'dvt-tx-name': 'KTEST',
          'dvt-polarization': 'DA (E)',
          'dvt-azimuth-deg': '90',
          'dvt-elevation-deg': '-1',
          'dvt-beamwidth-h-deg': '65',
          'dvt-beamwidth-v-deg': '8',
          'dvt-electrical-tilt-deg': '0',
          'dvt-mechanical-tilt-deg': '0',
          'dvt-max-horizontal-atten-db': '30',
          'dvt-front-to-back-atten-db': '30',
          'dvt-max-vertical-atten-db': '30',
          'dvt-noise-figure-db': '6.5',
          'dvt-rx-antenna-gain-dbi': '8',
          'dvt-termination-power-dbm': '-125',
          'dvt-power-mode': 'conducted',
          'dvt-conducted-power-kw': '10',
          'dvt-tx-gain-db': '1',
          'dvt-feeder-loss-db': '1.5',
          'dvt-antenna-gain-dbi': '12',
          'max-range': '20000',
          'dr-m': '20',
          'dvt-dtheta-deg': '0.25',
          'dvt-call-sign': 'KTEST',
          'dvt-station-standard': 'ATSC 3.0',
        }};
        function element(id) {{
          return {{
            value: values[id] ?? '',
            hidden: false,
            style: {{}},
            dataset: {{}},
            options: [],
            addEventListener() {{}},
            closest() {{ return this; }},
          }};
        }}
        global.document = {{ getElementById: (id) => element(id) }};
        vm.runInThisContext(fs.readFileSync({str(script_path)!r}, 'utf8'));
        const out = window.RFWaveformUI.buildPlanFields({{
          lat: 37.7749,
          lon: -122.4194,
          txHeightM: 320,
          sectors: [],
        }});
        if (out.technology !== 'dvt') throw new Error('technology');
        if (out.waveform !== 'atsc3') throw new Error('top-level waveform');
        if (out.dvt.waveform !== 'atsc3') throw new Error('nested waveform');
        if (out.dvt.fc !== 587000000) throw new Error('fc');
        if (out.dvt.fs !== 10000000) throw new Error('fs');
        if (out.dvt.bandwidth !== 6000000) throw new Error('bandwidth');
        if (out.dvt.power.feederLossDb !== 1.5) throw new Error('feeder');
        if (out.dvt.power.antennaGainDbi !== 12) throw new Error('gain');
        if (out.max_range_m !== 20000 || out.step_m !== 20 || out.dtheta_deg !== 0.25) throw new Error('grid');
        if (out.noise_figure_db !== 6.5) throw new Error('noise figure');
        if (out.ue_antenna_gain_dbi !== 8) throw new Error('rx gain');
        if (out.termination_rsrp_dbm !== -125) throw new Error('termination power');
        const body = Object.assign({{
          freq_mhz: 3500, tx_power_dbm: 43, subcarrier_spacing_khz: 30,
          num_resource_blocks: 100, mimo_mode: 'MIMO', sectors: [{{sector_id:'x'}}],
          path_loss_model: '3gpp_38901', propagation_scenario: 'umi_street_canyon'
        }}, out);
        window.RFWaveformUI.sanitizePlanBody(body);
        for (const key of ['freq_mhz','tx_power_dbm','subcarrier_spacing_khz','num_resource_blocks','mimo_mode','path_loss_model','propagation_scenario']) {{
          if (Object.prototype.hasOwnProperty.call(body, key)) throw new Error(`NR key leaked: ${{key}}`);
        }}
        if (body.sectors !== null) throw new Error('DVT sectors must be null');
        """
    )
    subprocess.run([node, "-e", js], check=True, cwd=ROOT)


def test_dvt_sync_hides_nr_only_controls_and_restores_nr():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    script_path = STATIC / "waveform_ui.js"
    js = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        global.window = {{}};
        const elements = new Map();
        function element(id) {{
          if (!elements.has(id)) {{
            elements.set(id, {{
              id,
              value: '',
              hidden: false,
              disabled: false,
              checked: true,
              textContent: '',
              style: {{}},
              dataset: {{}},
              options: [],
              addEventListener() {{}},
              closest() {{ return this; }},
            }});
          }}
          return elements.get(id);
        }}
        global.document = {{ getElementById: (id) => element(id) }};
        element('waveform-profile').value = 'atsc3';
        element('coverage-display-layer').value = 'rsrp';
        element('dvt-power-mode').value = 'erp';
        vm.runInThisContext(fs.readFileSync({str(script_path)!r}, 'utf8'));
        window.RFWaveformUI.syncUi({{}});
        const hiddenIds = [
          'tx-power-dbm', 'scs-khz', 'num-rb', 'termination-rsrp-dbm',
          'mimo-mode', 'link-adapt', 'path-loss-model', 'propagation-scenario',
          'sector-configuration-section', 'sector-overlay-control'
        ];
        for (const id of hiddenIds) {{
          if (element(id).style.display !== 'none') throw new Error(`visible in DVT: ${{id}}`);
        }}
        if (element('dvt-transmitter-fields').style.display === 'none') throw new Error('DVT fields hidden');
        if (element('coverage-display-layer').value !== 'field_strength') throw new Error('DVT display layer');
        if (!element('coverage-layer-rsrp').disabled) throw new Error('RSRP option not disabled');
        if (element('advanced-rf-summary').textContent !== 'DVT propagation calibration') throw new Error('summary');

        element('waveform-profile').value = '5g_nr';
        window.RFWaveformUI.syncUi({{}});
        for (const id of hiddenIds) {{
          if (element(id).style.display === 'none') throw new Error(`still hidden in NR: ${{id}}`);
        }}
        if (element('dvt-transmitter-fields').style.display !== 'none') throw new Error('DVT fields visible in NR');
        """
    )
    subprocess.run([node, "-e", js], check=True, cwd=ROOT)
