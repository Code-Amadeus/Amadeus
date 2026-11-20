"""Browser acceptance entry point and shared VN page harness.

Native launch, microphone, capture and model output are substituted; events and
history come from the production VN runtime. Real-game testing has its own probe.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body><div id="root" style="height:100vh;display:flex"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
</script><script type="module" src="/tests/fixtures/vnPage.tsx"></script></body></html>"""

if __name__ == "__main__":
    import argparse
    import asyncio
    from tools.probes.vn_product_ui import run
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    asyncio.run(run(parser.parse_args().url))
