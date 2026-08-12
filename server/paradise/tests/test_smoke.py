"""Phase 0 smoke tests — prove branch boundary & config flag.

Run:
    cd server && python -m pytest paradise/tests/test_smoke.py -v

These run on BOTH dev and prod branches (pure stdlib, no prod deps).
"""
from __future__ import annotations

import sys
from pathlib import Path


def test_paradise_importable():
    """ paradise package must import cleanly on both branches. """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import paradise
    assert hasattr(paradise, "config")


def test_config_mode_defaults_to_dev():
    """ParadiseConfig.mode must default to 'dev' so main branch is unaffected."""
    from paradise.config import ParadiseConfig
    cfg = ParadiseConfig()
    assert cfg.mode == "dev", f"Expected 'dev', got '{cfg.mode}'"


def test_prod_config_has_mode_flag():
    """config.prod.yaml must set mode: prod (Phase 0 boundary)."""
    prod_yaml = Path(__file__).resolve().parent.parent / "config.prod.yaml"
    assert prod_yaml.exists(), f"Missing {prod_yaml}"
    text = prod_yaml.read_text(encoding="utf-8")
    assert "mode: prod" in text, "config.prod.yaml must contain 'mode: prod'"


def test_factory_module_importable():
    """paradise.factory must be importable without prod deps installed
    (stubs raise NotImplementedError only when called, not at import)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from paradise import factory
    assert hasattr(factory, "load_prod_config")
    assert hasattr(factory, "build_graph")
    assert hasattr(factory, "build_transport")
    assert hasattr(factory, "build_memory_provider")
