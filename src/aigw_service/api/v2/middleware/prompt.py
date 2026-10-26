"""Dynamic prompt blocks (spec §9 «Слои контекста»): every block goes into the one system message."""

from langchain_core.messages import SystemMessage


def append_block(system: SystemMessage | None, text: str) -> SystemMessage:
    """One system message only (GigaChat, spike S0.2): a block is appended to the existing one."""
    if system is None:
        return SystemMessage(text)
    blocks = system.content if isinstance(system.content, list) else [{"type": "text", "text": system.content}]
    return SystemMessage(content=[*blocks, {"type": "text", "text": "\n\n" + text}])
