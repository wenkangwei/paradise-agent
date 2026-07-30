"""Paradise Agent Framework — 统一异常体系."""


class ParadiseError(Exception):
    """Paradise 框架基础异常."""
    pass


class TransportError(ParadiseError):
    """Transport 层异常 — LLM API 调用失败."""
    def __init__(self, message: str, provider: str = "", status_code: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class TransportNotFoundError(ParadiseError):
    """未找到匹配的 Transport."""
    def __init__(self, api_mode: str):
        super().__init__(f"No transport registered for api_mode: {api_mode}")
        self.api_mode = api_mode


class ToolError(ParadiseError):
    """工具执行异常."""
    def __init__(self, message: str, tool_name: str = ""):
        super().__init__(message)
        self.tool_name = tool_name


class ToolNotFoundError(ToolError):
    """工具未注册."""
    def __init__(self, tool_name: str):
        super().__init__(f"Tool not found: {tool_name}", tool_name)


class SandboxViolation(ToolError):
    """沙箱安全违规."""
    def __init__(self, message: str, tool_name: str = ""):
        super().__init__(f"Sandbox violation: {message}", tool_name)


class MemoryError(ParadiseError):
    """内存/记忆系统异常."""
    pass


class MemoryProviderError(MemoryError):
    """外部 Memory Provider 异常."""
    def __init__(self, message: str, provider_name: str = ""):
        super().__init__(message)
        self.provider_name = provider_name


class PluginError(ParadiseError):
    """插件系统异常."""
    def __init__(self, message: str, plugin_name: str = ""):
        super().__init__(message)
        self.plugin_name = plugin_name


class PluginLoadError(PluginError):
    """插件加载失败."""
    pass


class ConfigError(ParadiseError):
    """配置异常."""
    pass


class HeartbeatError(ParadiseError):
    """心跳系统异常."""
    pass


class ReflectionError(ParadiseError):
    """反思系统异常."""
    pass
