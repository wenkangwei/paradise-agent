"""Plugin loader — discovers and loads plugins from multiple sources.

Sources (priority, later overrides earlier on collision):
  1. Bundled: paradise/plugins/bundled/
  2. User: ~/.paradise/plugins/
  3. Project: ./plugins/
  4. Pip: paradise.plugins entry-point group
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Type

from paradise.plugins.base import ParadisePlugin, PluginContext
from paradise.plugins.hooks import HookExecutor
from paradise.exceptions import PluginLoadError

logger = logging.getLogger(__name__)

_PARADISE_HOME = Path.home() / ".paradise"


class PluginLoader:
    """Discovers, loads, and manages plugins."""

    def __init__(self):
        self._plugins: dict[str, ParadisePlugin] = {}
        self._hook_executor = HookExecutor()

    @property
    def hook_executor(self) -> HookExecutor:
        return self._hook_executor

    @property
    def plugins(self) -> dict[str, ParadisePlugin]:
        return dict(self._plugins)

    def discover_and_load(
        self,
        ctx: PluginContext,
        disabled: list[str] | None = None,
    ) -> dict[str, ParadisePlugin]:
        """Discover plugins from all sources and load them."""
        disabled = disabled or []

        # Source 1: Bundled
        bundled_dir = Path(__file__).parent / "bundled"
        self._load_from_directory(bundled_dir, ctx, disabled)

        # Source 2: User plugins
        user_dir = _PARADISE_HOME / "plugins"
        self._load_from_directory(user_dir, ctx, disabled)

        # Source 3: Project plugins
        project_dir = Path.cwd() / "plugins"
        self._load_from_directory(project_dir, ctx, disabled)

        # Source 4: Pip entry-points
        self._load_from_entry_points(ctx, disabled)

        logger.info("[Plugins] Loaded %d plugins: %s",
                     len(self._plugins), list(self._plugins.keys()))
        return dict(self._plugins)

    def _load_from_directory(
        self,
        directory: Path,
        ctx: PluginContext,
        disabled: list[str],
    ) -> None:
        """Load plugins from a directory."""
        if not directory.exists():
            return

        for plugin_dir in sorted(directory.iterdir()):
            if not plugin_dir.is_dir():
                continue
            if plugin_dir.name.startswith("_"):
                continue

            # Check for plugin.yaml or plugin.json
            yaml_path = plugin_dir / "plugin.yaml"
            json_path = plugin_dir / "plugin.json"
            init_path = plugin_dir / "__init__.py"

            if not init_path.exists():
                continue

            plugin_name = plugin_dir.name
            if plugin_name in disabled:
                logger.debug("[Plugins] Skipping disabled plugin: %s", plugin_name)
                continue

            # Load metadata
            meta = {}
            if yaml_path.exists():
                try:
                    import yaml
                    meta = yaml.safe_load(yaml_path.read_text()) or {}
                except Exception:
                    pass
            elif json_path.exists():
                try:
                    meta = json.loads(json_path.read_text())
                except Exception:
                    pass

            # Load plugin class
            try:
                plugin = self._load_plugin_from_path(plugin_dir, plugin_name)
                if plugin:
                    self._register_plugin(plugin, ctx)
            except Exception as e:
                logger.warning("[Plugins] Failed to load %s: %s", plugin_name, e)

    def _load_plugin_from_path(self, plugin_dir: Path, name: str) -> ParadisePlugin | None:
        """Load a ParadisePlugin from a directory."""
        init_path = plugin_dir / "__init__.py"
        if not init_path.exists():
            return None

        # Add to sys.path if needed
        parent = str(plugin_dir.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)

        try:
            module = importlib.import_module(name)
            # Find ParadisePlugin subclass
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type)
                        and issubclass(attr, ParadisePlugin)
                        and attr is not ParadisePlugin):
                    return attr()
        except Exception as e:
            raise PluginLoadError(f"Import error: {e}", plugin_name=name)

        return None

    def _load_from_entry_points(self, ctx: PluginContext, disabled: list[str]) -> None:
        """Load plugins from pip entry-points."""
        try:
            if sys.version_info >= (3, 12):
                from importlib.metadata import entry_points
                eps = entry_points(group="paradise.plugins")
            else:
                from importlib.metadata import entry_points
                eps = entry_points().get("paradise.plugins", [])

            for ep in eps:
                if ep.name in disabled:
                    continue
                try:
                    cls = ep.load()
                    if isinstance(cls, type) and issubclass(cls, ParadisePlugin):
                        self._register_plugin(cls(), ctx)
                except Exception as e:
                    logger.warning("[Plugins] Failed to load entry-point %s: %s", ep.name, e)
        except Exception:
            pass

    def _register_plugin(self, plugin: ParadisePlugin, ctx: PluginContext) -> None:
        """Register a plugin instance."""
        try:
            plugin.on_register(ctx)
            self._plugins[plugin.name] = plugin
            logger.info("[Plugins] Registered: %s v%s", plugin.name, plugin.version)
        except Exception as e:
            raise PluginLoadError(f"Registration failed: {e}", plugin_name=plugin.name)

    def unregister_all(self) -> None:
        """Unregister all plugins."""
        for plugin in self._plugins.values():
            try:
                plugin.on_unregister()
            except Exception as e:
                logger.warning("[Plugins] Unregister error for %s: %s", plugin.name, e)
        self._plugins.clear()
        self._hook_executor.clear()
