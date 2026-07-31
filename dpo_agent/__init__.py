"""dpo-agent: tool-using LLM agents for contract review and metadata extraction.

The package exposes a small set of generic agent classes, each
parameterized by a `task` name. A task is a directory of 3 system
prompts under dpo_agent.tasks. Two tasks ship by default:

- "dpo"        — Data Protection Officer contract review
- "metadata"   — generic metadata extraction from any document

Add a new task by dropping a new directory under dpo_agent.tasks
with reviewer.md, critique.md, and navigator.md files. No code
changes required.

Main entry points:
    Agent              — single-pass tool-using agent for any task
    AgentTwoPass       — self-refine (review + critique) for any task
    Navigator          — Stage 1 of find-then-extract for any task
    StreamingAgent     — event-streaming wrapper for any task

Backwards-compat aliases (old class names still work):
    DPOAgent           = Agent
    DPOAgentTwoPass    = AgentTwoPass
    DPONavigator       = Navigator
    DPOStreamingAgent  = StreamingAgent

Example (DPO):
    from dpo_agent import Agent, DocumentTools

    tools = DocumentTools(
        get_document_size=lambda d: len(my_store.get(d)),
        retrieve_whole_document_content=lambda d: my_store.get(d),
        get_number_of_chunks=lambda d: my_store.chunk_count(d),
        get_document_chunk_by_index=lambda d, i: my_store.get_chunk(d, i),
    )
    agent = Agent(tools=tools, task="dpo")
    result = agent.run(document_id="contract-001")
    print(result.review)

Example (metadata):
    from dpo_agent import Agent
    import json

    schema = json.dumps({
        "parties": {"type": "list", "description": "All parties"},
        "effective_date": {"type": "string", "description": "ISO 8601"},
        "term_months": {"type": "int"},
        "governing_law": {"type": "string"},
    })
    agent = Agent(tools=tools, task="metadata")
    result = agent.run(document_id="contract-001", schema=schema)
    metadata = json.loads(result.review)
"""

from .agent import Agent, AgentConfig, ReviewResult
from .llm_client import (
    AnthropicClient,
    LLMClient,
    LLMResponse,
    LLMStreamContext,
    MockClient,
    OpenAICompatClient,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    Usage,
    create_client,
)
from .models import (
    all_resolved_models,
    resolve_model,
    resolve_optional_model,
    DEFAULT_MODELS,
    ALL_KINDS,
)
from .exceptions import (
    AgentStoppedError,
    ConfigurationError,
    DPOError,
    MaxIterationsError,
    ToolError,
)
from .navigator import Navigator, NavigatorResult
from .pipeline import (
    DEFAULT_TRIAGE_PLAN,
    PipelineConfig,
    PipelineStage,
    TriagePipeline,
    TriageReport,
    triage,
)
from .streaming import AgentEvent, StreamingAgent, StreamingConfig
from .tasks.loader import list_tasks, load_prompt
from .tools import TOOLS, DocumentTools, dispatch
from .two_pass import AgentTwoPass, TwoPassConfig, TwoPassResult


# Backwards-compat aliases for pre-refactor class names.
# Old code:  DPOAgent(tools=...)
# New code:  Agent(tools=..., task="dpo")
# Both work. The alias is the same class, not a wrapper.
DPOAgent = Agent
DPONavigator = Navigator
DPOAgentTwoPass = AgentTwoPass
DPOStreamingAgent = StreamingAgent


__all__ = [
    # Generic agents
    "Agent",
    "AgentTwoPass",
    "Navigator",
    "StreamingAgent",
    # Triage pipeline
    "TriagePipeline",
    "TriageReport",
    "PipelineConfig",
    "PipelineStage",
    "DEFAULT_TRIAGE_PLAN",
    "triage",
    # Backwards-compat aliases
    "DPOAgent",
    "DPOAgentTwoPass",
    "DPONavigator",
    "DPOStreamingAgent",
    # Tools
    "DocumentTools",
    "TOOLS",
    "dispatch",
    # Configs
    "AgentConfig",
    "TwoPassConfig",
    # Model resolution
    "resolve_model",
    "resolve_optional_model",
    "all_resolved_models",
    "DEFAULT_MODELS",
    "ALL_KINDS",
    # LLMClient abstraction (Path D — Anthropic / OpenAI-compat / Mock)
    "LLMClient",
    "LLMResponse",
    "LLMStreamContext",
    "AnthropicClient",
    "OpenAICompatClient",
    "MockClient",
    "create_client",
    "TextBlock",
    "ToolUseBlock",
    "Usage",
    "StreamEvent",
    "StreamingConfig",
    # Results
    "ReviewResult",
    "TwoPassResult",
    "NavigatorResult",
    # Streaming
    "AgentEvent",
    # Task discovery
    "list_tasks",
    "load_prompt",
    # Exceptions
    "DPOError",
    "ToolError",
    "MaxIterationsError",
    "AgentStoppedError",
    "ConfigurationError",
]


__version__ = "0.3.0"
