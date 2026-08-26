from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "agentic_rf_planner" / "ui" / "static"


def test_export_utils_collects_every_layer_png_numeric_npz_and_settings():
    script = f"""
      global.window = global;
      const fs = require('fs');
      eval(fs.readFileSync({json.dumps(str(STATIC / 'export_utils.js'))}, 'utf8'));
      global.fetch = async () => ({{ ok: true, status: 200, blob: async () => new Blob(['NPZ']) }});
      const png = 'data:image/png;base64,iVBORw0KGgo=';
      (async () => {{
        const assets = await global.RFExportUtils.collectAnalysisExportAssets([{{out:{{
          rf_config_used: {{technology:'dvt', waveform:'atsc1', max_range_m:20000}},
          channel_analysis: {{technology:'dvt', waveform:'atsc1', receiver:{{latitude:1}}, target:{{}}, motion:{{}}, processing:{{}}}},
          channel_analysis_product: {{product_id:'abc', download_url:'/product'}},
          coverage_layers: {{
            bistatic_echo: {{id:'bistatic_echo', label:'Echo', units:'dBm', numeric_units:'dBm', numeric_member:'echo_power_dbm', heatmap:{{png_b64:png,vmin:-200,vmax:-80}}}},
            bistatic_delay: {{id:'bistatic_delay', label:'Delay', units:'µs', numeric_units:'s', numeric_to_display_scale:1e6, numeric_member:'excess_delay_s', heatmap:{{png_b64:png,vmin:0,vmax:50}}}}
          }}
        }}}}]);
        const names = assets.files.map(f => f.name).sort();
        console.log(JSON.stringify({{names, plans:assets.plans}}));
      }})().catch(err => {{ console.error(err); process.exit(1); }});
    """
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    payload = json.loads(completed.stdout.strip())
    names = set(payload["names"])
    assert "coverage_layers/TX1/bistatic_echo.png" in names
    assert "coverage_layers/TX1/bistatic_delay.png" in names
    assert "coverage_layers/TX1/manifest.json" in names
    assert "coverage_layers/TX1/layer_statistics.csv" in names
    assert "numeric/TX1/channel_analysis_abc.npz" in names
    assert "channel_analysis/TX1/effective_settings.json" in names
    assert "channel_analysis/TX1/summary.json" in names
    assert "export_manifest.json" in names
    assert payload["plans"][0]["layer_count"] == 2


def test_ui_uses_heatmaps_as_primary_result_and_supports_exact_target_probe():
    index = (STATIC / "index.html").read_text()
    app = (STATIC / "app.js").read_text()
    planner_3d = (STATIC / "planner_3d.js").read_text()
    assert "The selected coverage layer is the spatial result" in index
    assert "coverage_layers?.[layer]?.heatmap" in app
    assert "/api/channel-analysis/products/${encodeURIComponent(product.product_id)}/probe" in app
    assert "channelProbeMode3d && lastChannelAnalysis3d" in planner_3d
    assert "coverage_layers?.[covLayer]?.heatmap" in planner_3d
