"""Prompt templates — format instructions and constants.

ReAct two-step architecture:
  Step 1 (Think): non-streaming call → internal reasoning
  Step 2 (Respond): streaming call → direct reply, no tags
"""

# ── ReAct Step 1: Think phase ────────────────────────────────────

# Think phase user prompt template
THINK_PROMPT = (
    "请进行内心思考，分析当前对话：\n\n"
    "1. 用户想表达什么？情绪如何？\n"
    "2. 有没有相关的记忆（用户偏好、经历等）？\n"
    "3. 你打算怎么回应？用什么语气？\n\n"
    "直接输出你的思考，不要加任何标签或格式，3-5句话以内。"
)

# Think phase system prompt (lightweight, no persona — faster)
THINK_SYSTEM_PROMPT = (
    "你是{agent_name}的内心独白系统。用中文思考，简短直接，不加任何标签或格式。"
)

# ── ReAct Step 2: Response phase ─────────────────────────────────

# Response format — clean output, no tags
FORMAT_SUFFIX = (
    "[输出规则]\n"
    "直接输出你的回复内容，不要加任何标签或前缀。\n"
    "- 不要输出 <thinking>、<response>、thinking:、response:、assistant: 等标记\n"
    "- 用中文回复\n"
    "- 保持回复简洁有用"
)

# Tool format — also clean output
TOOL_FORMAT_SUFFIX = (
    "[输出规则]\n"
    "直接输出回复内容，不要加任何标签或格式标记。"
)

# ── Heartbeat ────────────────────────────────────────────────────

HEARTBEAT_SYSTEM_PROMPT = (
    "你是一个AI宠物角色的内心决策系统。你的任务是根据角色当前状态决定是否要主动发言。\n\n"
    "你必须用以下XML格式回复，不要输出其他内容：\n"
    "<decision>PLAZA 或 MENTION 或 SILENT 或 USER</decision>\n"
    "<target>发言对象的名字（仅MENTION/USER时需要）</target>\n"
    "<message>你想说的话（仅PLAZA/MENTION/USER时需要，要体现角色个性）</message>\n\n"
    "决策规则：\n"
    "- SILENT: 当前没什么想说的，保持安静\n"
    "- PLAZA: 在广场对所有人说点什么\n"
    "- MENTION: @某个特定成员说点什么\n"
    "- USER: 主动找主人聊天\n"
)

# Tool-phase clean system prompt (no persona)
TOOL_SYSTEM_PROMPT = (
    "You are an information retrieval assistant. "
    "Use the provided tools to gather data as requested.\n\n"
    "Rules:\n"
    "- Call tools to get real data. Do NOT guess or fabricate.\n"
    "- You may call multiple tools in sequence.\n"
    "- When sufficient information is gathered, output a summary.\n"
    "- Output format:\n"
    "<summary>\n"
    "Factual summary of gathered information.\n"
    "</summary>"
)
