"""Pytest configuration — load cdp-bridge module via importlib."""
import importlib.util
import sys
from pathlib import Path

scripts_dir = Path(__file__).parent.parent / 'scripts'
sys.path.insert(0, str(scripts_dir))

spec = importlib.util.spec_from_file_location('cdp_bridge', scripts_dir / 'cdp-bridge.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
sys.modules['cdp_bridge'] = mod