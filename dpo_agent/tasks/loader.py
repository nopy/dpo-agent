"""Task discovery and prompt loading.

A "task" is a directory under dpo_agent.tasks containing 3
system prompts:
- reviewer.md    — the main extraction / review prompt
- critique.md    — the self-refine critique prompt
- navigator.md   — Stage 1 of the find-then-extract pipeline

The loader discovers tasks at runtime — to add a new task, drop
a new directory under dpo_agent.tasks with the 3 prompt files.
No code changes are required.

Use list_tasks() to enumerate available tasks, and
load_prompt(task, "reviewer") to load a specific prompt.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Literal


# The 3 prompt types in every task. Tasks may omit some (the loader
# will raise if you ask for an omitted one) but the convention is
# that every task ships all three.
PromptType = Literal["reviewer", "critique", "navigator", "reduce"]


def list_tasks() -> list[str]:
    """Return the names of all available tasks.

    A task is a subdirectory of dpo_agent.tasks that contains
    the expected prompt files. Tasks missing prompt files are
    skipped with a warning to stderr (in production, raise
    instead).
    """
    tasks_pkg = resources.files("dpo_agent.tasks")
    names: list[str] = []
    for entry in tasks_pkg.iterdir():
        if not entry.is_dir():
            continue
        # A task is recognized if it has ANY of the recognized
        # prompt files. The classic 3-prompt tasks have a
        # reviewer.md; chunked tasks might only have reduce.md.
        if any(
            (entry / f"{pt}.md").is_file()
            for pt in ("reviewer", "critique", "navigator", "reduce")
        ):
            names.append(entry.name)
    return sorted(names)


def load_prompt(task: str, prompt_type: PromptType) -> str:
    """Load a system prompt for the given task.

    Args:
        task: the task name (a subdirectory of dpo_agent.tasks).
        prompt_type: which prompt to load.

    Returns:
        The prompt text.

    Raises:
        FileNotFoundError: if the task or prompt type doesn't
            exist. The error message lists the available tasks
            and prompt types to help debugging.
    """
    available = list_tasks()
    if task not in available:
        raise FileNotFoundError(
            f"Task {task!r} not found. Available tasks: {available}. "
            f"To add a new task, create dpo_agent/tasks/{task}/ with "
            f"reviewer.md, critique.md, navigator.md, and (optionally) "
            f"reduce.md files."
        )

    prompt_path = resources.files(f"dpo_agent.tasks.{task}").joinpath(
        f"{prompt_type}.md"
    )
    if not prompt_path.is_file():
        raise FileNotFoundError(
            f"Prompt {prompt_type!r} not found for task {task!r}. "
            f"Expected file: dpo_agent/tasks/{task}/{prompt_type}.md"
        )
    return prompt_path.read_text(encoding="utf-8")
