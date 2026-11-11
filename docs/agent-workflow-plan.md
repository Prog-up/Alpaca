# Agent tasks: clarify, plan, execute

Implementation plan and user guide, 5 October 2026.

## Intended experience

Agent mode uses the selected Ollama model, which must advertise native tool
support. New chats and Quick Ask honor the instance's configured default model,
including the first entry in the model list. Existing conversations may keep the
model that last answered them.

The workflow combines clarification questions with a visible plan preview.
Questions and planning are model-driven; structured tools define transitions,
and the controller validates their arguments before accepting them.

1. The user submits a goal in chat or Quick Ask.
2. Alpaca asks only for missing information that affects the work: the desired
   result, target machine or project, constraints, and how success will be checked.
   Ask a small batch of questions at a time, with suggested answers when useful.
   Reuse information already supplied rather than asking it again.
3. The user's replies complete the same task. Ask follow-up questions only while
   a material decision remains unresolved. Allow the user to edit the goal or
   cancel throughout this stage.
4. The model produces a short, visible plan with concrete steps and completion
   checks. Save the agreed requirements and plan in the chat.
5. With automatic agent execution enabled, start once the requirements are
   sufficiently clear and the plan is ready. A separate plan-review preference
   can require a Start button; it should not interrupt users who selected
   automatic execution.
6. Run a step, inspect its tool output, and update the next step. Continue until
   the completion checks pass, the user stops the run, or new information is
   required. Report what changed and what was verified.

Example: “Set up my container for Python development” first prompts for the
project/dependencies and the desired verification. If the selected target is an
SSH container, retain that target across the whole run.
After the answers, show the steps, execute them, and run the agreed checks.

## Settings and controls

- **Agent Mode**: regular chat or clarify → plan → execute. Default to regular
  chat for existing installations; the user explicitly opts into the agent mode.
- **Start Automatically After Planning**: allows the ready plan to begin without
  another confirmation. When disabled, the ready plan waits for **Start**.
- Reuse **Execute Commands Automatically** for the terminal tool. Agent mode
  must honor this setting rather than silently bypassing manual confirmation.
- Capture the terminal target, working directory, model, connection, and task scope
  when starting a run. Show the selected target beside the plan and commands.
- Keep **Stop**, **Pause**, **Resume**, and **Edit Requirements** available. Stop
  cancels pending prompts/tool calls and stops the active terminal process.
- Add a maximum tool-call count, execution deadline, and repeated-error limit.
  When a limit is reached, explain the blocker and wait for the user.

## Implementation stages

### 1. A task controller and saved state

Add an `AgentController` independent of GTK with explicit states:
`clarifying`, `waiting_for_start`, `executing`, `waiting_for_answer`,
`paused`, `completed`, `failed`, and `canceled`.

Persist the task goal, answers, plan, target, and progress in SQLite. Restore
interrupted runs as paused. Never resume commands automatically after an app
restart. GTK remains on the main thread; network requests and tool execution
remain on workers.

### 2. Clarification and planning tools

Provide structured tools to request clarification, publish a plan, and
report completion. A clarification response suspends the run instead of blocking
a worker on a modal dialog. The next user reply resumes that run with the
recorded answers. Use Ollama's native tool calling and validate the returned
arguments. Do not infer task readiness from arbitrary prose.

Readiness requires a sufficiently specified outcome, an execution target when
needed, the material constraints, and an observable completion check. The model
can proceed with stated, low-impact assumptions rather than asking endless
questions.

### 3. Execution and terminal cancellation

Wrap the existing Ollama tool loop with controller state, cancellation, and run
limits. Feed actual command output and exit status back to the model. Keep the selected SSH
target throughout the run; the host is not a fallback for a failed SSH command.
Extend terminal completion results to include exit status and cancellation, not
only captured text. If the target or requirements change materially, return to
clarification/planning.

### 4. Chat and Quick Ask presentation

Show questions, a compact plan with step status, and execution
controls in both interfaces. During clarification, typed replies are answers to
the active task. During execution, one new task can wait in the prompt queue,
with its full text displayed and Edit/Clear controls. Intervention, Stop, and
clarification answers have their own path. Editing a waiting goal and resending
it cannot consume it as an answer to the current task.
Closing Quick Ask cancels its active run unless the user explicitly transfers it
to a saved main-window chat.

### 5. Verification before installation

- Questions retain the same task, model, target, and attachments across replies.
- A complete goal progresses to planning without unnecessary questions.
- Automatic mode starts a ready plan; review mode waits for Start.
- Command confirmation follows the terminal preference in both modes.
- Real SSH checks return the configured user, `root` under sudo, and the expected OS from the
  configured container. Compound and multiline commands stay remote.
- Failed commands cause useful recovery or clarification rather than repeated
  identical attempts. Loop limits and Stop release workers and terminal activity.
- Queue ordering, switching chats, saving Quick Ask, pausing, and restart recovery
  preserve task state and avoid sending an answer as a separate task.
- Slow Ollama responses and image downloads leave GTK responsive. No worker
  constructs or mutates GTK widgets.

## Usage and persistence

Enable **Preferences → General → Agent Tasks → Agent Mode**. Enable **Start
Automatically After Planning** to start ready plans without clicking Start.
Terminal commands independently honor **Preferences → Activities → Terminal →
Execute Commands Automatically**. Both automation settings are off by default.

The working directory is interpreted on the captured terminal host; an empty
entry uses the terminal's normal directory. A nonexistent directory fails the
command rather than executing it somewhere else. Changing the global connection
or model selection does not redirect an active task. A restored task rejects a
changed instance URL and asks the user to start a new task.

Type answers into the composer when the task shows **Waiting for Your Answer**.
Use **Edit Requirements** to revise the task and return to planning. **Pause**
stops the active command; **Resume** continues from recorded results. **Stop**
cancels the task and queue. **New Task** clears the task panel before a fresh goal.
When execution is busy, queued prompts are new goals and wait until the task
completes; they cannot be consumed as clarification answers.

Saved chats persist goals, answers, plans, target settings, tool messages, output,
and exit status in SQLite. Instance credentials remain in the instance settings.
Quick Ask tasks stay temporary until **Save Conversation to Alpaca** transfers
them into a chat. Saving or restarting pauses unfinished tasks; resuming is always
explicit. Chat exports/duplicates keep the transcript, but do not copy executable
task state. Closing Quick Ask cancels its unsaved task.

Tool-call and duration limits apply to each uninterrupted run; resuming or answering
starts a new run budget. Three repeated tool/command errors pause the task. Stop
interrupts a terminal wait immediately. A pending model request unwinds when its
stream next yields or its network timeout expires; GTK stays responsive throughout.

## Implementation status

The controller, tools, task panel, settings, SSH quoting, cancellation, recovery,
default-model selection, and prompt queue are implemented. Automated tests live
in `tests/test_agent.py`, `tests/test_terminal.py`, and `tests/test_prompt_queue.py`.
`tests/agent_smoke.py` exercises actual GTK and VTE, and supports an opt-in live
Ollama/SSH workflow. See `tests/README.md` for build and verification commands.
