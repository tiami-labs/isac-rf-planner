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
    assert 'id="dvt-fc-mhz" class="rf-input" type="number" min="40" max="1000"' in html
    assert 'id="dvt-bandwidth-mhz"' in html
    assert 'id="dvt-bandwidth-mhz" class="rf-input" type="number" min="1" max="10"' in html
    assert 'Broadcast antenna radiation pattern' in html
    assert 'This is not cellular sector configuration.' in html
    assert 'id="dvt-pattern-type"' in html
    assert 'id="dvt-pattern-rotation-deg"' in html
    assert 'id="dvt-azimuth-pattern"' in html
    assert 'id="dvt-erp-h-kw"' in html
    assert 'id="dvt-erp-v-kw"' in html
    assert 'id="dvt-noise-figure-db"' in html
    assert 'id="dvt-rx-antenna-gain-dbi"' in html
    assert 'id="dvt-termination-power-dbm"' in html
    assert 'id="dvt-coverage-radius-m"' in html
    assert 'Coverage radius (m)' in html
    assert 'id="dvt-dr-m"' in html
    # NR sector configuration still exists, but is a separate panel hidden for DVT.
    assert 'id="sector-configuration-section"' in html
    assert 'id="coverage-layer-rsrp"' in html
    assert '<script src="/waveform_ui.js"></script>' in html


def test_waveform_ui_builds_explicit_atsc3_broadcast_pattern_request():
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
          'dvt-pattern-type': 'tabulated',
          'dvt-pattern-rotation-deg': '345',
          'dvt-antenna-manufacturer': 'Dielectric',
          'dvt-antenna-model': 'TFU test fixture',
          'dvt-beam-tilt-deg': '1.5',
          'dvt-vertical-beamwidth-deg': '8',
          'dvt-max-vertical-atten-db': '30',
          'dvt-azimuth-pattern': '0,1,0.5\\n90,0.8,0.4\\n180,0.1,0.1\\n270,0.2,0.2',
          'dvt-noise-figure-db': '6.5',
          'dvt-rx-antenna-gain-dbi': '8',
          'dvt-termination-power-dbm': '-125',
          'dvt-power-mode': 'erp',
          'dvt-erp-h-kw': '1000',
          'dvt-erp-v-kw': '250',
          'dvt-coverage-radius-m': '20000',
          'dvt-dr-m': '20',
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
          sectors: [{{sector_id:'must-not-be-used'}}],
        }});
        if (out.technology !== 'dvt') throw new Error('technology');
        if (out.waveform !== 'atsc3') throw new Error('top-level waveform');
        if (out.dvt.waveform !== 'atsc3') throw new Error('nested waveform');
        if (out.dvt.fc !== 587000000) throw new Error('fc');
        if (out.dvt.fs !== 10000000) throw new Error('fs');
        if (out.dvt.bandwidth !== 6000000) throw new Error('bandwidth');
        if (out.dvt.power.erpHKw !== 1000 || out.dvt.power.erpVKw !== 250) throw new Error('polarized ERP');
        if (out.dvt.antenna.patternType !== 'tabulated') throw new Error('pattern type');
        if (out.dvt.antenna.rotationDeg !== 345) throw new Error('pattern rotation');
        if (out.dvt.antenna.beamTiltDeg !== 1.5) throw new Error('beam tilt');
        if (out.dvt.antenna.azimuthPattern.length !== 4) throw new Error('pattern samples');
        if (Object.prototype.hasOwnProperty.call(out.dvt.tx, 'azimuthDeg')) throw new Error('pattern leaked into site geometry');
        if (Object.prototype.hasOwnProperty.call(out, 'sectors')) throw new Error('DVT sectors emitted');
        if (out.max_range_m !== 20000 || out.step_m !== 20 || out.dtheta_deg !== 0.25) throw new Error('grid');
        if (out.noise_figure_db !== 6.5) throw new Error('noise figure');
        if (out.ue_antenna_gain_dbi !== 8) throw new Error('rx gain');
        if (out.termination_power_dbm !== -125) throw new Error('termination power');
        const body = Object.assign({{
          freq_mhz: 3500, tx_power_dbm: 43, subcarrier_spacing_khz: 30,
          num_resource_blocks: 100, channel_bandwidth_mhz: 40,
          mimo_mode: 'MIMO', sectors: [{{sector_id:'x'}}],
          path_loss_model: '3gpp_38901', propagation_scenario: 'umi_street_canyon',
          termination_rsrp_dbm: -140
        }}, out);
        window.RFWaveformUI.sanitizePlanBody(body);
        for (const key of ['freq_mhz','tx_power_dbm','subcarrier_spacing_khz','num_resource_blocks','channel_bandwidth_mhz','mimo_mode','path_loss_model','propagation_scenario','termination_rsrp_dbm','sectors']) {{
          if (Object.prototype.hasOwnProperty.call(body, key)) throw new Error(`NR key leaked: ${{key}}`);
        }}
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
        element('dvt-pattern-type').value = 'tabulated';
        vm.runInThisContext(fs.readFileSync({str(script_path)!r}, 'utf8'));
        window.RFWaveformUI.syncUi({{}});
        const hiddenIds = [
          'tx-power-dbm', 'scs-khz', 'num-rb', 'termination-rsrp-dbm',
          'mimo-mode', 'link-adapt', 'path-loss-model', 'propagation-scenario',
          'advanced-rf-params', 'sector-configuration-section', 'sector-overlay-control',
          'mesh-profile-params-section'
        ];
        for (const id of hiddenIds) {{
          if (element(id).style.display !== 'none') throw new Error(`visible in DVT: ${{id}}`);
        }}
        if (element('dvt-transmitter-fields').style.display === 'none') throw new Error('DVT fields hidden');
        if (element('dvt-tabulated-pattern-fields').style.display === 'none') throw new Error('tabulated pattern fields hidden');
        if (element('dvt-parametric-pattern-fields').style.display !== 'none') throw new Error('parametric pattern fields visible');
        if (element('coverage-display-layer').value !== 'received_power') throw new Error('DVT display layer');
        if (!element('coverage-layer-rsrp').disabled) throw new Error('RSRP option not disabled');
        if (element('advanced-rf-summary').textContent !== 'DVT propagation calibration') throw new Error('summary');
        if (!element('waveform-summary').textContent.includes('cellular sectors are not used')) throw new Error('distinction summary');

        element('waveform-profile').value = '5g_nr';
        window.RFWaveformUI.syncUi({{}});
        for (const id of hiddenIds) {{
          if (element(id).style.display === 'none') throw new Error(`still hidden in NR: ${{id}}`);
        }}
        if (element('dvt-transmitter-fields').style.display !== 'none') throw new Error('DVT fields visible in NR');
        """
    )
    subprocess.run([node, "-e", js], check=True, cwd=ROOT)


def test_waveform_script_is_served_by_an_explicit_no_store_route():
    rest_source = (ROOT / "src" / "agentic_rf_planner" / "api" / "rest.py").read_text(encoding="utf-8")

    assert '@app.get("/waveform_ui.js")' in rest_source
    route_start = rest_source.index('@app.get("/waveform_ui.js")')
    route_end = rest_source.index('@app.get("/app.js")', route_start)
    route_source = rest_source[route_start:route_end]
    assert 'static_dir / "waveform_ui.js"' in route_source
    assert '"Cache-Control": "no-store"' in route_source


def test_dvt_hides_the_entire_legacy_nr_advanced_panel():
    js = (STATIC / "waveform_ui.js").read_text(encoding="utf-8")
    assert 'setVisible(byId("advanced-rf-params"), !dvt);' in js
    assert 'setVisible(byId("sector-configuration-section"), !dvt);' in js


def test_waveform_script_http_route_returns_javascript():
    from fastapi.testclient import TestClient
    from agentic_rf_planner.api.rest import app

    response = TestClient(app).get("/waveform_ui.js")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    assert "RFWaveformUI" in response.text


def test_waveform_ui_builds_waveform_agnostic_channel_analysis_for_nr_and_dvt():
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
          'waveform-profile': '5g_nr',
          'dvt-fc-mhz': '587', 'dvt-fs-mhz': '10', 'dvt-bandwidth-mhz': '6',
          'dvt-site-altitude-m': '5', 'dvt-tx-name': 'KTX', 'dvt-polarization': 'DA (E)',
          'dvt-pattern-type': 'omnidirectional', 'dvt-pattern-rotation-deg': '0',
          'dvt-antenna-manufacturer': '', 'dvt-antenna-model': '',
          'dvt-beam-tilt-deg': '1.5', 'dvt-vertical-beamwidth-deg': '8',
          'dvt-max-vertical-atten-db': '30', 'dvt-power-mode': 'erp',
          'dvt-erp-h-kw': '1000', 'dvt-erp-v-kw': '250',
          'dvt-noise-figure-db': '7', 'dvt-rx-antenna-gain-dbi': '0',
          'dvt-termination-power-dbm': '-140', 'dvt-coverage-radius-m': '20000',
          'dvt-dr-m': '20', 'dvt-dtheta-deg': '0.25',
          'coverage-display-layer': 'bistatic_margin',
          'channel-rx-location': '38.4, -121.5',
          'channel-return-path-model': 'environmental_reciprocal_grid',
          'channel-rx-altitude-m': '10', 'channel-rx-height-m': '15',
          'channel-direct-gain-dbi': '8', 'channel-echo-gain-dbi': '12',
          'channel-rx-feeder-loss-db': '1', 'channel-rx-noise-figure-db': '5',
          'channel-target-height-m': '1000', 'channel-bistatic-rcs-m2': '10',
          'channel-target-speed-mps': '120', 'channel-target-heading-deg': '45',
          'channel-target-climb-mps': '3',
          'channel-direct-excess-loss-db': '2', 'channel-return-excess-loss-db': '4',
          'channel-processing-bandwidth-mhz': '0', 'channel-integration-s': '1',
          'channel-prf-hz': '1000', 'channel-clutter-notch-hz': '2', 'channel-min-doppler-hz': '1',
          'channel-processing-loss-db': '3', 'channel-system-loss-db': '3',
          'channel-required-snr-db': '10', 'channel-direct-cancellation-db': '60',
        }};
        function element(id) {{
          return {{
            get value() {{ return values[id] ?? ''; }},
            set value(v) {{ values[id] = String(v); }},
            checked: id === 'channel-analysis-enabled',
            hidden: false, disabled: false, style: {{}}, dataset: {{}}, options: [], selectedIndex: -1,
            addEventListener() {{}}, closest() {{ return this; }},
          }};
        }}
        global.document = {{ getElementById: (id) => element(id) }};
        vm.runInThisContext(fs.readFileSync({str(script_path)!r}, 'utf8'));

        const nr = window.RFWaveformUI.buildPlanFields({{lat:38.271667, lon:-121.506111, txHeightM:320}});
        if (nr.technology !== '5g_nr' || !nr.channel_analysis) throw new Error('NR channel analysis missing');
        if (nr.channel_analysis.receiver.directAntennaGainDbi !== 8) throw new Error('direct gain');
        if (nr.channel_analysis.motion.speedMps !== 120) throw new Error('motion');
        if (nr.channel_analysis.processing.pulseRepetitionFrequencyHz !== 1000) throw new Error('PRF');

        values['waveform-profile'] = 'atsc1';
        const dvt = window.RFWaveformUI.buildPlanFields({{lat:38.271667, lon:-121.506111, txHeightM:320}});
        if (dvt.technology !== 'dvt' || !dvt.channel_analysis) throw new Error('DVT channel analysis missing');
        if (dvt.channel_analysis.target.heightMagl !== 1000) throw new Error('target height');
        if (dvt.channel_analysis.target.bistaticRcsM2 !== 10) throw new Error('RCS');
        if (dvt.channel_analysis.returnPathModel !== 'environmental_reciprocal_grid') throw new Error('return path model');
        if (dvt.channel_analysis.receiver.latitude !== 38.4 || dvt.channel_analysis.receiver.longitude !== -121.5) throw new Error('combined receiver location');
        if (dvt.coverage_display_layer !== 'bistatic_margin') throw new Error('display layer');
        if ('passive_radar' in dvt || 'passive_radar' in nr) throw new Error('legacy key leaked');
        """
    )
    subprocess.run([node, "-e", js], check=True, cwd=ROOT)



def test_ui_export_collector_includes_every_heatmap_settings_response_and_npz():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script_path = STATIC / "export_utils.js"
    js = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        global.window = {{}};
        global.atob = (value) => Buffer.from(value, 'base64').toString('binary');
        global.fetch = async (url) => ({{
          ok: true,
          status: 200,
          statusText: 'OK',
          blob: async () => new Blob([Buffer.from('npz-bytes')], {{type:'application/octet-stream'}}),
        }});
        vm.runInThisContext(fs.readFileSync({str(script_path)!r}, 'utf8'));
        const png = 'data:image/png;base64,' + Buffer.from('png').toString('base64');
        const out = {{
          rf_config_used: {{technology:'dvt', waveform:'atsc1', channel_analysis:{{receiver:{{latitude:1,longitude:2}}}}}},
          channel_analysis: {{model:'waveform_agnostic_bistatic_channel'}},
          geometry_source: {{type:'automatic_osm_aoi_cache'}},
          heatmap: {{png_b64:png, layer:'received_power_dbm', units:'dBm'}},
          heatmap_bistatic_echo: {{png_b64:png, layer:'echo_power_dbm', units:'dBm'}},
          heatmap_bistatic_total_path_loss: {{png_b64:png, layer:'total_bistatic_path_loss_db', units:'dB'}},
          heatmap_isac_quality: {{png_b64:png, layer:'bistatic_isac_quality_code', units:'class'}},
          channel_analysis_product: {{product_id:'abc', download_url:'/api/channel-analysis/products/abc'}},
        }};
        (async () => {{
          const artifacts = await window.RFExportUtils.collectPlanExportArtifacts([{{lat:1,lon:2,out}}]);
          const names = Object.keys(artifacts.files);
          const required = [
            'heatmaps/TX1/primary_coverage.png',
            'heatmaps/TX1/bistatic_echo_power.png',
            'heatmaps/TX1/total_bistatic_path_loss.png',
            'heatmaps/TX1/isac_target_quality.png',
            'plans/TX1/complete_plan_response.json',
            'plans/TX1/complete_settings.json',
            'channel/TX1_complete_rf_channel_grid.npz',
            'export_manifest.json',
          ];
          for (const name of required) if (!names.includes(name)) throw new Error('missing '+name);
          if (artifacts.manifest.plans[0].heatmaps.length !== 4) throw new Error('heatmap manifest incomplete');
          const settings = JSON.parse(await artifacts.files['plans/TX1/complete_settings.json'].text());
          if (settings.rf_config_used.technology !== 'dvt') throw new Error('settings missing');
          if (!settings.channel_analysis) throw new Error('channel summary missing');
        }})().catch((error) => {{ console.error(error); process.exit(1); }});
        """
    )
    subprocess.run([node, "-e", js], check=True, cwd=ROOT)
