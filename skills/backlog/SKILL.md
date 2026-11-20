---
name: backlog
description: Hand the current task to the usage-harvester backlog, so an unattended run finishes it later on leftover Claude usage.
disable-model-invocation: true
argument-hint: "[priority N] [extra instructions]"
---

Move the task you are working on into the harvester backlog. Later, a fresh unattended `claude -p` run picks it up in the same folder. It cannot see this conversation and has no one to ask, so the prompt you write is all it gets.

Notes from the user: $ARGUMENTS

## 1. Pick the folder

Use the folder the work belongs in, normally the project root (`git rev-parse --show-toplevel`). If you are in a git worktree that may be cleaned up before the run (a desktop-app or `.claude/worktrees` checkout), ask the user which folder to use instead of guessing; uncommitted work there would be lost.

## 2. Write the prompt

Write it to a temporary file (your scratchpad directory if you have one, else `%TEMP%`). Include:

- **Goal** and **done when**: the outcome and how to verify it (test command, expected behavior).
- **Context** the run cannot cheaply rediscover: decisions made with the user and why, constraints, approaches already rejected, the relevant files and symbols.
- **Current state**: branch, what is already done (including uncommitted changes in the working tree), and what is left as a checklist.
- **Rules for an unattended run**:
  - When a choice is the user's, take the conservative option and list it under "Open questions".
  - Do not commit, push or publish unless the user's instructions above say to.
  - Finish with a short summary: what changed, how it was verified, open questions. That summary is saved as the task's result.

Never put secrets or credentials in the prompt.

## 3. Choose settings

- `-t`: tools the run may use without asking. File reads and edits are already allowed (`acceptEdits`); any shell command must be listed, e.g. `-t "Bash(npm test:*) Bash(git status) Bash(git diff:*)"`. List only what the task needs.
- `-p N`: priority from the user's notes; higher runs first. Default 0.
- `-m MODEL`: only if the user asked for one.

## 4. Add it

```
hv add -f "<prompt file>" -d "<folder>" [-t "..."] [-p N] [-m MODEL]
```

Then delete the prompt file; the backlog keeps its own copy.

## 5. Stop and report

Stop working on the task. Do not keep editing files the backlog run will touch: two writers on the same files collide.

Tell the user, briefly: the task ID, the folder, the allowed tools, and that `hv show <ID>` shows the prompt and, later, the result. Run `hv status`; if it says the app is not running, mention `hv app`.
