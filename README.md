# Promptline

**A normal terminal that predicts the command you're about to type, and works
on a task for you when you ask it with `@agent`.**

Promptline is built on [Terminator](https://github.com/gnome-terminator/terminator).
It looks and behaves like the terminal you already use: no side panels, no
chat window, no output blocks. With its AI features off, it *is* Terminator.

```text
$ git push
fatal: The current branch main has no upstream branch.
$ git pu█sh --set-upstream origin main    ← dimmed prediction; → accepts

$ @agent what's using port 3000?
Find the process listening on port 3000.
  $ lsof -i :3000
  [a]pprove  [e]dit  [c]ancel approved
COMMAND   PID USER  FD  TYPE DEVICE SIZE/OFF NODE NAME
node    48213 dev   23u IPv6 412337      0t0  TCP *:3000 (LISTEN)

A Node.js process (PID 48213) is listening on port 3000.
```

## Features

**Inline suggestions.** As you type, the most likely completion appears
dimmed after the cursor. It comes from your shell history (bash and zsh
history are read, never modified), Promptline's own log of where each command
ran and whether it worked, and the file system. Suggestions are local and
instant and work offline. Press **→** or **End** to accept one, or
**Ctrl+→** to accept a word.

**Command prediction (optional).** With a model configured, Promptline also
predicts from context: the current directory, the last few commands and how
they ended, and the end of the last output. That turns a failed `git push`
into the right `--set-upstream` fix. On an empty prompt it suggests your
likely next command. Requests wait for a pause in typing and run in the
background, so typing never waits on the network.

**`@agent`.** Type `@agent` and a question or task at the prompt. The agent
already sees what's in the terminal, so you don't paste anything:

- every command it wants to run is shown first, and asks **approve / edit /
  cancel**;
- commands run in your current directory, with their output streaming into
  the terminal;
- for things that must happen in your own shell (`cd`, `export`, activating
  an environment) it types the command at your prompt, and you decide
  whether to press Enter;
- follow-up questions in the same terminal continue the conversation, for
  30 minutes, so `@agent continue` carries on where it stopped;
- it stops after a number of steps (25 by default; Preferences → Promptline →
  Step limit, or `promptline_agent_max_steps`). A step is one model reply;
- it's a normal program in your terminal: Ctrl+C stops it, replies appear as
  they are written, and its output is ordinary scrollback.

**Goals.** `@agent --goal fix the failing tests` gives the agent a goal to
work on until it is reached, with no step limit. It keeps going on its own,
checks its work, and ends with "Goal reached" and a summary, or "Blocked" and
what it needs from you. Along the way:

- passing provider errors (rate limits, timeouts) are retried;
- a command that fails the same way six times in a row ends the run, instead
  of going round in circles;
- long runs are summarised as they go, so they don't run out of context;
- a command that needs your approval waits 15 minutes (Preferences, or
  `promptline_goal_approval_wait`; 0 waits for ever), then is skipped, never
  approved, and the agent carries on with other work and reports what was
  left waiting;
- Ctrl+C stops it and keeps the conversation: `@agent --goal` on its own
  resumes the unfinished goal.

Goals ask before each command in the default mode. To leave one running while
you're away, use auto-review or full permission (below).

**Steering.** While the agent is thinking, anything you type is sent to it
when you press Enter ("Type to steer" shows on the spinner line). It reads
your message before its next step and takes it into account, **without
dropping what it was doing**: a message is guidance, not an interruption, so
commands still run, subagents keep working, and a workflow carries on (later
stages are told what you added). To interrupt, start the message with `stop`
(or `halt`, `abort`, `cancel`) or `!`: a command waiting to run unseen
(auto-review or full permission) is skipped, subagents end at their next step,
and the agent stops what it was doing and does what you say. While one of the
agent's commands is running, your keys go to that command, as always.

**Subagents.** A subagent is a helper the agent hands a task to. It works
in a conversation of its own, so what it reads doesn't fill the agent's
context, and only its report comes back; several can work at once. Built in:
`explorer`, `planner` and `reviewer` only run read-only commands, and those
run without asking, so they can work while you're away; `worker` and
`verifier` can change things, and go through your permission mode, one prompt
at a time, marked with who is asking. Turn them on in Preferences →
Promptline → Subagents, or with `promptline_subagents`:

| Setting | Meaning |
| --- | --- |
| `off` (default) | No subagents |
| `auto` | The agent may hand tasks to them when it helps (a `delegate` tool) |
| `always` | The agent is told to work with them on every request, at every reasoning level. If `promptline_subagent_flow` names a workflow, that runs first, every time |

`promptline_subagent_parallel` (default 4) is how many work at once. Typing
to steer doesn't interrupt them; saying `stop` does (see Steering). You can
add your own, or replace a built-in one, with a file at
`~/.config/promptline/agents/NAME.md`:

```markdown
---
description: Looks for services listening on the network
tools: read
reasoning: high
---
You look at what is listening on this machine and report each service, its
port, and which process owns it. Read-only commands only.
```

`tools: read` limits a subagent to read-only commands (an allowlist that
also keeps it away from `~/.ssh`, `.env` files and the like); the default,
`all`, uses your permission mode. `reasoning` is optional.

**Workflows.** A workflow is a fixed series of stages, run by Promptline, not
chosen by the model. Each stage hands a task to subagents, and what they
report goes on to the next. Then the agent writes the answer from the whole
record. Write one at `~/.config/promptline/flows/NAME.md`, one stage per
line, and choose it with `promptline_subagent_flow`:

```
description: Look, then change, then check
look: explorer -> Find out what is running on port 8080.
change: worker -> Restart it with the new setting.
check: reviewer, verifier -> Confirm it worked. [retry change x2]
```

- `name: agent, agent -> task` gives every agent listed the task at once;
- `name: fanout agent -> task` runs one agent for each `- item` in the
  previous stage's report;
- `[retry stage xN]` makes a stage a gate: if any agent's report starts with
  `FAIL`, the flow goes back to that stage, up to N times, with the failure
  in the record. Checking agents (`reviewer`, `verifier`) answer `PASS:` or
  `FAIL:` first.

**Reasoning: `max` and `ultra`.** Two levels above `xhigh`
(Preferences → Promptline, `promptline_agent_reasoning`, or per request with
`@agent --max ...` and `@agent --ultra ...`):

- `max` asks the model for the most thinking it gives: Claude's `max` effort,
  OpenAI-style servers' `xhigh`. Models that take no such setting are left
  alone.
- `ultra` is `max` plus a team of subagents on a strict built-in workflow:
  a planner lists the questions, one explorer per question investigates in
  parallel (read-only), a worker does the work, then a reviewer and a verifier
  check it and send it back to the worker, up to twice, if either says FAIL.
  The agent then writes the answer from that record. It **overrides** your
  subagent settings and workflow, whatever they are. Anything that changes
  something still goes through your permission mode. Combine it with
  `--goal`. It uses many more model calls than a normal request.

**Context window.** The agent plans for a 1M-token window by default
(`promptline_context_window`, Preferences → Promptline), for every model: it
keeps up to 400 messages of a conversation, much more command output, and
more of your terminal's history, and summarises the older part only once
the conversation passes 70% of the window. Claude Haiku 4.5 is known to have
200k, and is planned for as such. For any other model with less, the agent
copes: when a provider says a request is too big, it summarises the older
part, and tries again. Set the number lower to summarise sooner.

**Personalisation.** Tell Promptline about your work once with `promptline -P`:
your role, day-to-day tasks, the tools you use and avoid, your environments.
The template is written with security engineers and network admins in mind.
From then on, predictions and `@agent` fit you from the start, and don't have
to wait for your history to fill up.

**Memory.** The agent remembers lasting facts about you and says when it does
("Remembered: uses Nessus for enterprise scans, not nmap"). It learns from
what you tell it and from how you edit its commands. You can also ask
directly: `@agent update your memory, I use Nessus for enterprise scans`.
Review or edit memory with `promptline --memory`.

**Permission modes** (Preferences → Promptline), for when approving every
step gets in the way:

| Mode | What runs without asking |
| --- | --- |
| Ask (default) | Nothing. Every command waits for approve / edit / cancel. |
| Auto-review | Commands a separate reviewer model judges safe. Anything else asks, with the reviewer's reason. |
| Full permission | Everything except a short list of catastrophic commands. Locked until you've written your own guardrails (`promptline --guardrails`), which the agent must follow. |

The auto-review reviewer sees the conversation so far (what you asked, what
you said while the agent worked, what it has run, and what you approved or
declined), not just the latest line, so a follow-up like `@agent continue` is
judged in context.

In every mode, a built-in list of dangerous commands (wiping disks, deleting
system directories, piping downloads into a shell, firewall flushes,
shutdowns, user and sudo changes) always asks. Every command the agent runs
is logged to `~/.local/share/promptline/agent-audit.log`. Auto-review and
full permission are powerful: only use them where mistakes are recoverable.

## Privacy

- Nothing is sent anywhere unless you enable command prediction or run
  `@agent`.
- What is sent is described in the Preferences window: the directory, recent
  commands with exit statuses, and the end of recent output. Things that
  look like credentials (API keys, tokens, `password=` arguments) are
  removed first.
- A command typed with a **leading space** is private, as in bash's and
  zsh's `ignorespace`: Promptline never learns it or sends it.
- Your personalisation, guardrails and the agent's memory are plain text
  files readable only by you. Guidance inside `<!-- -->` is never sent, and
  credentials are removed from memory before it's saved.
- API keys are read from an environment variable or a file you point to.
  They are never written to Promptline's config.

## Requirements

- Linux, Python 3, GTK 3
- VTE with terminal-property support (0.78 or newer; developed against 0.84).
  On an older VTE Promptline runs as plain Terminator.
- bash or zsh for suggestions, prediction and `@agent` (fish isn't supported
  yet)

On Debian/Ubuntu:

```sh
sudo apt install python3-gi python3-gi-cairo python3-psutil python3-configobj \
  gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-keybinder-3.0 gir1.2-notify-0.7
```

## Installing

On Debian/Ubuntu, download the `.deb` from the
[latest release](https://github.com/MurcusM/promptline/releases/latest) and run
`sudo apt install ./promptline_<version>_all.deb`. Or build it yourself:

```sh
sudo apt install debhelper dh-python gettext intltool
git clone https://github.com/MurcusM/promptline.git
cd promptline
dpkg-buildpackage -us -uc -b
sudo apt install ../promptline_*_all.deb
```

Or run it straight from the checkout with `python3 promptline`. See
[INSTALL.md](INSTALL.md) for details.

Promptline installs alongside Terminator without conflicts: it has its own
command, config directory (`~/.config/promptline`), D-Bus name and desktop
entry. The first time it starts, it copies your Terminator settings and
plugins into its own config directory. Your Terminator files are left as
they are.

## Setting up a model

Open **Preferences → Promptline**, or edit `~/.config/promptline/config`:

```ini
[global_config]
  promptline_llm_autocomplete = True
  promptline_provider = anthropic
  promptline_api_key_file = ~/.config/promptline/api-key
```

| Setting | Default | |
| --- | --- | --- |
| `promptline_enabled` | `True` | Everything below; off means plain Terminator |
| `promptline_shell_integration` | `True` | Hooks loaded into new bash/zsh shells |
| `promptline_autocomplete` | `True` | Local suggestions from history and paths |
| `promptline_llm_autocomplete` | `False` | Model-based prediction (sends context) |
| `promptline_predict_next` | `True` | Predict the next command on an empty prompt |
| `promptline_autocomplete_model` / `_reasoning` | `gpt-6-luna` / `xhigh` | Model for prediction |
| `promptline_agent_model` / `_reasoning` | `gpt-6-luna` / `xhigh` | Model for `@agent` |
| `promptline_provider` | `openai` | `openai`, `anthropic`, `opencode`, `openrouter`, `gemini`, `deepseek`, `qwen`, `muse`, `ollama`, `lmstudio` or `custom` |
| `promptline_base_url` | *(empty)* | Overrides the provider's address; needed for `custom` |
| `promptline_api_key_env` | *(empty)* | Overrides the environment variable holding the key |
| `promptline_api_key_file` | *(empty)* | File holding the key (use `chmod 600`) |
| `promptline_autocomplete_provider` / `promptline_agent_provider` | *(empty)* | A different provider for prediction or `@agent`; empty uses `promptline_provider` |
| `promptline_agent_mode` | `ask` | `ask`, `auto-review` or `full` (full needs guardrails) |
| `promptline_agent_max_steps` | `25` | Model replies one `@agent` request may take (not for `--goal`) |
| `promptline_context_window` | `1000000` | Tokens of conversation the agent plans for before it summarises |
| `promptline_subagents` | `off` | `off`, `auto` or `always` |
| `promptline_subagent_flow` | *(empty)* | A workflow in `~/.config/promptline/flows/`, run first with `always` |
| `promptline_subagent_parallel` | `4` | Subagents working at once |
| `promptline_goal_approval_wait` | `15` | Minutes a goal run waits for an approval before skipping that command; `0` waits for ever |
| `promptline_review_reasoning` | `medium` | Reasoning effort of the auto-review reviewer |

| Command | File |
| --- | --- |
| `promptline -P` | `~/.config/promptline/personal.md`, about you |
| `promptline --guardrails` | `~/.config/promptline/guardrails.md`, your rules for the agent |
| `promptline --memory` | `~/.local/share/promptline/memory.md`, what the agent remembers |

**Providers.** Choose one in Preferences → Promptline, or set
`promptline_provider`. Each reads its key from its own variable unless you
override it:

| Provider | Key variable | Notes |
| --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | |
| `anthropic` | `ANTHROPIC_API_KEY` | Messages API. The reasoning setting is ignored |
| `opencode` | `OPENCODE_API_KEY` | [OpenCode Zen](https://opencode.ai/docs/zen/), one key for many models; see below |
| `openrouter` | `OPENROUTER_API_KEY` | |
| `gemini` | `GEMINI_API_KEY` | Google's OpenAI-compatible endpoint |
| `deepseek` | `DEEPSEEK_API_KEY` | |
| `qwen` | `DASHSCOPE_API_KEY` | Alibaba Cloud Model Studio, international endpoint. For another region or workspace, set `promptline_base_url` |
| `muse` | `MODEL_API_KEY` | Meta's Model API (`https://api.meta.ai/v1`) |
| `ollama`, `lmstudio` | none | Local servers need no key |
| `custom` | your choice | Any OpenAI-compatible server: set `promptline_base_url` |

Keys are API keys. Promptline does not sign in to a subscription.
Prediction and `@agent` can use different providers, for example a local
Ollama model to predict commands and a larger hosted model for `@agent`:

```ini
[global_config]
  promptline_provider = opencode
  promptline_agent_model = claude-sonnet-5-5
  promptline_autocomplete_provider = ollama
  promptline_autocomplete_model = qwen2.5-coder:7b
  promptline_autocomplete_reasoning =
```

Reasoning effort is sent to OpenAI-compatible servers, and some reject it,
so leave `_reasoning` empty for those. Levels: `none`, `minimal`, `low`,
`medium`, `high`, `xhigh`, `max`, and (for `@agent`) `ultra`. Claude models
that take an effort setting get it as `output_config.effort`, with their
thinking carried between tool calls; `max` and `ultra` are Claude's `max`, and
`xhigh` for the others.

**OpenCode Zen.** Zen serves its models in different API formats, and
Promptline picks one from the model name: `claude-*` use the Messages API,
`gpt-*`, `grok-*` and `muse-*` use Responses, and everything else (Kimi, GLM,
DeepSeek, MiniMax, Qwen, Mistral...) uses Chat Completions. Gemini models use
Google's own format, which Promptline doesn't speak. If the guess is wrong
for a model, name the format in front: `messages:qwen3.6-plus`. The model
IDs are listed at <https://opencode.ai/zen/v1/models>.

**zsh-autosuggestions and fish.** Promptline never draws over another
suggestion, so a shell plugin's history match takes precedence. To let
Promptline's predictions lead, skip the plugin inside Promptline:

```zsh
# in ~/.zshrc, before oh-my-zsh is loaded
[[ -n $PROMPTLINE ]] && plugins=(${plugins:#zsh-autosuggestions})
```

## How it works

Promptline starts bash and zsh with a small integration script that runs
after your own startup files. The script tells the terminal when a prompt
is drawn, where input begins, and when a command starts and how it exits.
Promptline then reads what you've typed straight from the screen, so history
recall, Tab completion and paste are all reflected. Suggestions are drawn on
a transparent layer over the terminal; nothing is sent to the shell until
you accept one. See [`doc/promptline-plan.md`](doc/promptline-plan.md) for
the design.

Known limits: `@agent` commands run in a fresh shell (your aliases aren't
available, and `cd` doesn't carry over; that's what "place on prompt" is
for). Shells inside ssh or tmux don't get the integration.

## Relationship to Terminator

Promptline is a downstream of Terminator. It regularly merges upstream
releases to pick up fixes, as described in [`doc/UPSTREAM.md`](doc/UPSTREAM.md).
All of Promptline's own code is in `promptlinelib/promptline/`, and the rest
of the tree stays as close to Terminator as possible. Terminator's original
README is in [`README.terminator.md`](README.terminator.md).

### Credits

Promptline exists because of Terminator. Terminator was started by Chris
Jones in 2007, maintained by Stephen Boddy from 2014 to 2020 and since then
by Matt Rose, with contributions from many others listed in
[AUTHORS](AUTHORS). Thank you.

The code Promptline inherits remains theirs: its copyright notices and
license are kept as they are, and the full Terminator history, with its
authors, is part of this repository's history. Promptline is based on
Terminator 2.1.6 (upstream commit `9f2d0b6c`).

## Development

```sh
xvfb-run -a pytest                 # all tests, including real bash/zsh sessions
python3 promptline -u -d           # run without D-Bus hand-off, with debug output
```

## License

GPL-2.0-only, like Terminator. See [COPYING](COPYING).
