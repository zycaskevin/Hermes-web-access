"""Pytest configuration — load cdp-bridge module and register aiohttp fixtures."""
import importlib.util
import sys
from pathlib import Path

# Import cdp-bridge (hyphenated filename) as cdp_bridge
scripts_dir = Path(__file__).parent.parent / 'scripts'
sys.path.insert(0, str(scripts_dir))

spec = importlib.util.spec_from_file_location('cdp_bridge', scripts_dir / 'cdp-bridge.py')
cdp_bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdp_bridge)
sys.modules['cdp_bridge'] = cdp_bridge