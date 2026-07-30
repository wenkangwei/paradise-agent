"""Paradise TUI -- Terminal User Interface for the Paradise Agent Framework.

A rich, interactive REPL backed by prompt_toolkit (input) and rich (output).

Usage::

    python -m paradise.tui
    python -m paradise          # same thing via __main__.py

Config is loaded from ``~/.paradise/config.yaml`` or environment variables
(``PARADISE_API_URL``, ``PARADISE_API_KEY``, ``PARADISE_MODEL``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Logging -- quiet by default; enable with PARADISE_DEBUG=1
# ---------------------------------------------------------------------------
logger = logging.getLogger("paradise.tui")
if os.getenv("PARADISE_DEBUG"):
    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_PARADISE_HOME = Path.home() / ".paradise"
_HISTORY_FILE = _PARADISE_HOME / "history"
_CONFIG_FILE = _PARADISE_HOME / "config.yaml"

_AGENT_NAME = "小橘"      # Default agent persona
_AGENT_AVATAR = "\U0001f431"  # cat emoji
_USER_AVATAR = "\U0001f43e"   # paw prints

_STREAM_CHAR_DELAY = 0.008   # seconds between characters in streaming output

# ---------------------------------------------------------------------------
# Slash commands (name -> description)
# ---------------------------------------------------------------------------
SLASH_COMMANDS = {
    "/help":      "Show available commands",
    "/status":    "Show agent emotion state",
    "/feed":      "Feed the agent (optional amount, default 1)",
    "/reflect":   "Trigger idle reflection manually",
    "/heartbeat": "Trigger a heartbeat tick",
    "/config":    "Show current configuration",
    "/plugins":   "List loaded plugins",
    "/clear":     "Clear the screen",
    "/quit":      "Exit Paradise TUI",
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, Any]:
    """Load config from ~/.paradise/config.yaml, falling back to env vars."""
    config: dict[str, Any] = {}

    # Try YAML first
    if _CONFIG_FILE.exists():
        try:
            import yaml
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            logger.debug("Loaded config from %s", _CONFIG_FILE)
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", _CONFIG_FILE, exc)

    # Env var overrides / fallbacks
    env_url = os.getenv("PARADISE_API_URL")
    env_key = os.getenv("PARADISE_API_KEY")
    env_model = os.getenv("PARADISE_MODEL")
    env_provider = os.getenv("PARADISE_PROVIDER")

    if env_url or env_key or env_model:
        llm = config.setdefault("llm", {})
        if env_url:
            llm["api_url"] = env_url
        if env_key:
            llm["api_key"] = env_key
        if env_model:
            llm["model"] = env_model
        if env_provider:
            llm["provider"] = env_provider

    return config


def _load_user_profile() -> dict[str, Any]:
    """Load user profile from data/user.json."""
    from paradise.memory.workspace import DATA_DIR
    user_path = Path(DATA_DIR) / "user.json"
    if user_path.exists():
        try:
            return json.loads(user_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _load_global_settings() -> dict[str, Any]:
    """Load global settings from data/settings.json."""
    from paradise.memory.workspace import DATA_DIR
    settings_path = Path(DATA_DIR) / "settings.json"
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _ensure_history_dir() -> None:
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Rich helpers
# ---------------------------------------------------------------------------

def _print_banner() -> None:
    """Print the startup banner."""
    from rich.panel import Panel
    from rich.text import Text
    from rich import box

    banner_text = Text()
    banner_text.append("  _                      _       \n", style="bold cyan")
    banner_text.append(" | |                    | |      \n", style="bold cyan")
    banner_text.append(" | |__   ___  __ _  __ _| |_ ___ \n", style="bold cyan")
    banner_text.append(" | '_ \\ / _ \\/ _` |/ _` | __/ __|\n", style="bold cyan")
    banner_text.append(" | |_) |  __/ (_| | (_| | |_\\__ \\\n", style="bold cyan")
    banner_text.append(" |_.__/ \\___|\\__, |\\__,_|\\__|___/\n", style="bold cyan")
    banner_text.append("              __/ |              \n", style="dim cyan")
    banner_text.append("             |___/               \n", style="dim cyan")

    subtitle = Text()
    subtitle.append(f"  {_AGENT_AVATAR}  Paradise Agent TUI", style="bold white")
    subtitle.append("  |  ", style="dim")
    subtitle.append("Type ", style="dim")
    subtitle.append("/help", style="bold yellow")
    subtitle.append(" for commands  |  ", style="dim")
    subtitle.append("/quit", style="bold yellow")
    subtitle.append(" to exit\n", style="dim")

    from rich.console import Console
    console = Console()
    console.print()
    console.print(Panel(
        banner_text,
        subtitle=subtitle,
        box=box.HEAVY,
        border_style="cyan",
        padding=(0, 2),
    ))
    console.print()


def _render_emotion_bar(state: dict[str, Any]) -> str:
    """Render a compact emotion status bar."""
    mood = state.get("mood", "happy")
    energy = state.get("energy", 1.0)
    hunger = state.get("hunger", 0.0)
    affection = state.get("affection", 0.5)
    boredom = state.get("boredom", 0.0)

    # Mood emoji
    mood_emojis = {
        "happy": "\U0001f60a", "excited": "\U0001f929", "playful": "\U0001f63a",
        "bored": "\U0001f611", "hungry": "\U0001f37d\ufe0f", "sad": "\U0001f622",
        "anxious": "\U0001f630", "contemplative": "\U0001f914",
        "tired": "\U0001f634", "loving": "\U0001f970",
    }
    mood_emoji = mood_emojis.get(mood, "\U0001f43e")

    # Build bar segments
    def _bar(value: float, char: str = "\u2588", width: int = 5) -> str:
        filled = int(value * width)
        return char * filled + "\u2591" * (width - filled)

    parts = [
        f"{mood_emoji} {mood}",
        f"\u26a1 energy: {_bar(energy)} {energy:.1f}",
        f"\U0001f37d\ufe0f hunger: {_bar(hunger)} {hunger:.1f}",
        f"\u2764\ufe0f affection: {_bar(affection)} {affection:.1f}",
        f"\U0001f4ad boredom: {_bar(boredom)} {boredom:.1f}",
    ]
    return "  ".join(parts)


async def _stream_text(console: "Console", text: str, style: str = "") -> None:
    """Stream text character-by-character to the console.

    Does NOT append a trailing newline -- the caller controls line breaks.
    """
    for ch in text:
        console.print(ch, end="", style=style, highlight=False)
        await asyncio.sleep(_STREAM_CHAR_DELAY)

# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------

async def _cmd_help(console: "Console") -> None:
    from rich.table import Table
    from rich import box

    table = Table(title="Available Commands", box=box.SIMPLE, show_lines=False)
    table.add_column("Command", style="bold yellow")
    table.add_column("Description", style="white")

    for cmd, desc in SLASH_COMMANDS.items():
        table.add_row(cmd, desc)

    console.print(table)
    console.print()


async def _cmd_status(console: "Console", agent: Any) -> None:
    from rich.panel import Panel

    state = agent.get_state()
    bar = _render_emotion_bar(state)
    console.print(Panel(bar, title=f"{_AGENT_AVATAR} {_AGENT_NAME} Status", border_style="green"))
    console.print()


async def _cmd_feed(console: "Console", agent: Any, amount_str: str = "1") -> None:
    try:
        amount = int(amount_str)
    except ValueError:
        amount = 1

    state = await agent.feed(amount)
    hunger = state.get("hunger", 0.0)
    affection = state.get("affection", 0.5)

    console.print(
        f"  {_AGENT_AVATAR} {_AGENT_NAME} fed x{amount}  "
        f"hunger -> {hunger:.2f}  affection -> {affection:.2f}",
        style="green",
    )
    bar = _render_emotion_bar(state)
    console.print(f"  {bar}", style="dim")
    console.print()


async def _cmd_reflect(console: "Console", agent: Any) -> None:
    console.print("  [dim]Triggering reflection...[/dim]")
    try:
        await agent.check_idle_reflection()
        console.print("  [green]Reflection complete.[/green]")
    except Exception as exc:
        console.print(f"  [red]Reflection error: {exc}[/red]")
    console.print()


async def _cmd_heartbeat(console: "Console", agent: Any) -> None:
    console.print("  [dim]Triggering heartbeat tick...[/dim]")
    try:
        engine = agent.emotion_engine
        state = agent.emotion_state
        engine.tick(state)
        trigger = engine.on_heartbeat(state)
        agent.workspace.save_state(state.to_dict())
        bar = _render_emotion_bar(state.to_dict())
        console.print(f"  {bar}", style="dim")
        if trigger:
            console.print(f"  [yellow]Heartbeat trigger: {trigger}[/yellow]")
        else:
            console.print("  [green]Heartbeat tick completed (no trigger).[/green]")
    except Exception as exc:
        console.print(f"  [red]Heartbeat error: {exc}[/red]")
    console.print()


async def _cmd_config(console: "Console", agent: Any) -> None:
    from rich.panel import Panel

    config_dict = agent.config.to_dict()
    formatted = json.dumps(config_dict, indent=2, ensure_ascii=False)
    console.print(Panel(formatted, title="Configuration", border_style="blue"))
    console.print()


async def _cmd_plugins(console: "Console", agent: Any) -> None:
    from rich.table import Table
    from rich import box

    # The plugin loader lives on the agent; if not wired up, show a message
    loader = getattr(agent, "_plugin_loader", None)
    if loader is None:
        console.print("  [dim]Plugin loader not initialized.[/dim]")
        console.print()
        return

    plugins = loader.plugins
    if not plugins:
        console.print("  [dim]No plugins loaded.[/dim]")
        console.print()
        return

    table = Table(title="Loaded Plugins", box=box.SIMPLE)
    table.add_column("Name", style="bold")
    table.add_column("Version")
    table.add_column("Description")

    for name, plugin in plugins.items():
        table.add_row(name, getattr(plugin, "version", "?"), getattr(plugin, "description", ""))

    console.print(table)
    console.print()


async def _cmd_clear(console: "Console") -> None:
    console.clear()

# ---------------------------------------------------------------------------
# Core TUI REPL
# ---------------------------------------------------------------------------

class ParadiseTUI:
    """Paradise Terminal User Interface.

    Uses ``prompt_toolkit`` for the input area (fixed at the bottom, with
    history and auto-completion) and ``rich`` for formatted output.
    """

    def __init__(self, agent: Any, session_id: str | None = None):
        self.agent = agent
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self._running = False

        # Prompt-toolkit components (initialized lazily)
        self._prompt_session: Any = None
        self._completer: Any = None

        # Rich console
        from rich.console import Console
        self.console = Console()

        # Load user profile from data/user.json
        self._user_profile = _load_user_profile()
        self._user_name = self._user_profile.get("name", "主人")
        self._user_avatar = self._user_profile.get("avatar") or _USER_AVATAR

        # Load soul/memory from workspace (data/agents/{id}/)
        self._soul_md = agent.workspace.read_md("soul")
        self._memory_md = agent.workspace.read_md("memory")

    # ── Setup ──────────────────────────────────────────────────────────

    def _init_prompt(self) -> None:
        """Initialize prompt_toolkit session with history and completion."""
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.completion import WordCompleter
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.styles import Style as PTStyle

        _ensure_history_dir()

        self._completer = WordCompleter(
            list(SLASH_COMMANDS.keys()),
            ignore_case=True,
            sentence=True,
        )

        pt_style = PTStyle.from_dict({
            "avatar": "bold cyan",
            "prompt": "bold white",
        })

        self._prompt_session = PromptSession(
            history=FileHistory(str(_HISTORY_FILE)),
            completer=self._completer,
            auto_suggest=AutoSuggestFromHistory(),
            multiline=False,
            style=pt_style,
        )

    # ── Main Loop ──────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main REPL loop."""
        self._running = True
        self._init_prompt()

        _print_banner()

        # Show initial status
        await _cmd_status(self.console, self.agent)

        while self._running:
            try:
                # Build styled prompt using prompt_toolkit formatted text
                from prompt_toolkit.formatted_text import FormattedText

                prompt_text = FormattedText([
                    ("class:avatar", f" {self._user_avatar} "),
                    ("class:prompt", f"{self._user_name}: "),
                ])

                user_input = await self._prompt_session.prompt_async(prompt_text)

                text = user_input.strip()
                if not text:
                    continue

                # Dispatch
                if text.startswith("/"):
                    await self._handle_slash(text)
                else:
                    await self._handle_message(text)

            except KeyboardInterrupt:
                # Ctrl-C: continue the loop (clear input)
                continue
            except EOFError:
                # Ctrl-D exits
                self.console.print("\n  [dim]Goodbye~[/dim]")
                break
            except Exception as exc:
                logger.exception("REPL error")
                self.console.print(f"\n  [red]Error: {exc}[/red]\n")

    # ── Slash command dispatch ─────────────────────────────────────────

    async def _handle_slash(self, text: str) -> None:
        """Parse and execute a slash command."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/quit", "/exit", "/q"):
            self.console.print("  [dim]Goodbye~[/dim]")
            self._running = False

        elif cmd == "/help":
            await _cmd_help(self.console)

        elif cmd == "/status":
            await _cmd_status(self.console, self.agent)

        elif cmd == "/feed":
            await _cmd_feed(self.console, self.agent, arg)

        elif cmd == "/reflect":
            await _cmd_reflect(self.console, self.agent)

        elif cmd == "/heartbeat":
            await _cmd_heartbeat(self.console, self.agent)

        elif cmd == "/config":
            await _cmd_config(self.console, self.agent)

        elif cmd == "/plugins":
            await _cmd_plugins(self.console, self.agent)

        elif cmd == "/clear":
            await _cmd_clear(self.console)

        else:
            self.console.print(f"  [yellow]Unknown command: {cmd}[/yellow]")
            self.console.print("  [dim]Type /help for available commands.[/dim]\n")

    # ── Message handling (chat) ────────────────────────────────────────

    async def _handle_message(self, text: str) -> None:
        """Send a user message to the agent and stream the response."""
        from paradise.core.context import LoopContext

        # Record user message to workspace session
        self.agent.workspace.append_session(self.session_id, {
            "role": "user",
            "content": text,
        })

        # Build context
        ctx = LoopContext(
            agent_id=self.agent.agent_id,
            agent_name=_AGENT_NAME,
            session_id=self.session_id,
            user_message=text,
            user_name=self._user_name,
            user_personality=self._user_profile.get("personality", ""),
            user_description=self._user_profile.get("description", ""),
            soul_md=self._soul_md,
            memory_md=self._memory_md,
            enable_tools=self.agent.config.enable_tools,
        )

        # Show user message
        self.console.print()
        self.console.print(f"  {self._user_avatar} {self._user_name}: ", style="bold", end="")
        self.console.print(text)
        self.console.print()

        # Stream agent response
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        prev_state = self.agent.emotion_state.to_dict()
        first_thinking = True
        first_content = True

        try:
            async for event in self.agent.handle_message(ctx):
                etype = event.get("type")

                if etype == "thinking":
                    chunk = event.get("content", "")
                    thinking_parts.append(chunk)
                    if first_thinking:
                        self.console.print(f"  {_AGENT_AVATAR} {_AGENT_NAME} ", style="bold", end="")
                        self.console.print("[thinking] ", style="dim italic", end="")
                        first_thinking = False
                    self.console.print(chunk, style="dim italic", end="", highlight=False)

                elif etype == "content":
                    chunk = event.get("content", "")
                    content_parts.append(chunk)
                    if first_content:
                        # Close thinking block if any, start content line
                        if thinking_parts:
                            self.console.print()
                        self.console.print(f"  {_AGENT_AVATAR} {_AGENT_NAME}: ", style="bold", end="")
                        first_content = False
                    # Stream content character by character
                    await _stream_text(self.console, chunk, style="")

                elif etype == "error":
                    err = event.get("content", "Unknown error")
                    self.console.print(f"\n  [red]Error: {err}[/red]")

                elif etype == "done":
                    pass  # Response complete

            self.console.print()

        except Exception as exc:
            logger.exception("handle_message error")
            self.console.print(f"\n  [red]Agent error: {exc}[/red]")

        # Show emotion state changes
        current_state = self.agent.emotion_state.to_dict()
        if current_state != prev_state:
            bar = _render_emotion_bar(current_state)
            self.console.print(f"  {bar}", style="dim")
            self.console.print()

        # Save agent response to session
        full_response = "".join(content_parts)
        if full_response:
            self.agent.workspace.append_session(self.session_id, {
                "role": "assistant",
                "content": full_response,
            })
            # Refresh memory_md (may have been updated by reflection)
            self._memory_md = self.agent.workspace.read_md("memory")


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

async def _create_agent() -> Any:
    """Create and initialize a ParadiseAgent from config."""
    from paradise.config import ParadiseConfig
    from paradise.core.agent import ParadiseAgent
    from paradise.memory.workspace import DATA_DIR

    config_dict = _load_config()
    config = ParadiseConfig.from_dict(config_dict)

    # Default agent_id
    agent_id = config_dict.get("agent_id", "xiaoju")
    config.agent_id = agent_id

    agent = ParadiseAgent(agent_id, config)

    # Read soul/memory from workspace (data/agents/{id}/), not from config
    # Only pass them if workspace files don't exist yet (agent.initialize creates them)
    await agent.initialize()

    return agent


async def _destroy_agent(agent: Any) -> None:
    """Gracefully shut down the agent."""
    try:
        await agent.destroy()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for ``python -m paradise.tui``."""
    # Graceful import errors
    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        print("ERROR: prompt_toolkit is required.  Install with: pip install prompt_toolkit")
        sys.exit(1)

    try:
        import rich  # noqa: F401
    except ImportError:
        print("ERROR: rich is required.  Install with: pip install rich")
        sys.exit(1)

    async def _amain() -> None:
        from rich.console import Console
        console = Console()

        # Create agent
        console.print("[dim]Initializing Paradise Agent...[/dim]")
        try:
            agent = await _create_agent()
        except Exception as exc:
            console.print(f"[red]Failed to initialize agent: {exc}[/red]")
            console.print("[dim]Check your config at ~/.paradise/config.yaml or set env vars:[/dim]")
            console.print("[dim]  PARADISE_API_URL, PARADISE_API_KEY, PARADISE_MODEL[/dim]")
            sys.exit(1)

        # Run TUI
        tui = ParadiseTUI(agent)
        try:
            await tui.run()
        finally:
            await _destroy_agent(agent)

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
