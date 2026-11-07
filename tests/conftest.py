"""Load the plugin directory as package `popquiz_plugin` (Hermes imports it as a package, so the relative
imports of __init__.py must work). No Hermes import here."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "popquiz_plugin"
ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]


def _load():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = mod
    spec.loader.exec_module(mod)
    return mod


_load()


@pytest.fixture
def plugin():
    return sys.modules[PACKAGE]


@pytest.fixture
def plugin_root() -> Path:
    return ROOT


class _RootIsNotATestPackage:
    """The plugin directory is a package (__init__.py uses relative imports): pytest would collect it as
    a Package and import __init__.py alone. Collect it as a plain directory instead (pattern from
    NousResearch/hermes-official-plugins, elevenlabs-sound-effects/tests/conftest.py)."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_collect_directory(self, path, parent):
        if Path(path).resolve() == ROOT:
            return pytest.Dir.from_parent(parent, path=path)
        return None


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("popquiz-root-as-dir"):
        config.pluginmanager.register(_RootIsNotATestPackage(), "popquiz-root-as-dir")
