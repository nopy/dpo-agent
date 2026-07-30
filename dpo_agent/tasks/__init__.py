"""Tasks shipped with dpo-agent.

Each task is a directory under this package containing 3 system
prompts:
- reviewer.md    — the main extraction / review prompt
- critique.md    — the self-refine critique prompt
- navigator.md   — Stage 1 of the find-then-extract pipeline

The agent classes load these by name: pass `task="dpo"` or
`task="metadata"` to construct an agent for that task.

To add a new task, create a new directory under this package
with the 3 prompt files. No code changes are required — the
loader discovers tasks at runtime.
"""
