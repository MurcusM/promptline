# Promptline V2 — migration audit, architecture and plan

Status: draft for review (2026-09-23). Target codebase: `v2/` (Terminator fork).

## 1. Audit findings

### V1 (Wave fork) contains no Promptline code

`v1/` is byte-identical to upstream Wave `a4447c15` except for the fork
attribution added to `NOTICE` (and `.github/` path fixes for the move into
`v1/`). The Promptline work done on the Wave fork was lost before it was backed
up. What survives is the plan from that session:

- Autocomplete: client-side shadow buffer (Wave cannot see the typed line),
  two tiers — instant local (history + paths + flag/pipe heuristics) and a
  debounced 300–500 ms LLM tier.
- `@agent`: reuse Wave's provider-agnostic tool loop, add a
  `term_run_command` tool; per-command approval; OpenAI as the demo provider;
  OpenAI-compatible custom endpoints (Ollama, LM Studio) through a generic setting.
- Known weakness: ghost text desyncs after up-arrow recall / shell Tab
  completion; planned fix was richer shell-integration scripts.

So the "migration" is: carry that plan's requirements and decisions forward,
and use Wave's **upstream** AI features as design research only.

### Licensing constraint (important)

Wave is Apache-2.0; Terminator is **GPL-2.0-only**. Apache-2.0 code is not
compatible with GPLv2-only, so **no Wave code or prompt text may be copied
into v2**. Everything below is reimplemented from the ideas.

### Verified Terminator/VTE capabilities (VTE 0.84 on this machine)

| Probe | Result |
| --- | --- |
| Read typed input from the screen (`get_text_range_format` from prompt-end to cursor) | Works — returned `git che` exactly |
| OSC 7 cwd (`get_current_directory_uri`) | Works (deprecated accessor; use `TERMPROP_CURRENT_DIRECTORY_URI`) |
| OSC 133 (FinalTerm marks) | **Ignored** by VTE — no events |
| Legacy OSC 777 `precmd`/`preexec` (with `set_enable_legacy_osc777(True)`) | Fire `vte.shell.precmd` / `vte.shell.preexec` termprops |
| Distro `vte.sh` (sourced by bash here) | Already emits `OSC 666 vte.shell.precmd!`/`preexec!`/`postexec=<exit>` — but no prompt-end mark |
| `get_text_range_format(fmt, r0, c0, r1, c1)` | Rows inclusive, end column exclusive (`-1` is not accepted) |
| Custom termprops `vte.ext.promptline.*` installed via `Vte.install_termprop`, set with `OSC 666 ; name=value ST` | Works, delivered via `termprops-changed` |
| Same with BEL terminator | Not delivered — must use ST (`ESC \`) |
| Value containing `;` | Truncated at `;` — values must be encoded (base64) |

Terminator already exports `TERMINATOR_UUID`, `TERMINATOR_DBUS_NAME/PATH` to
every shell, and has a DBus service (`ipc.py`) that `remotinator` uses.

**Consequence:** the shadow buffer from the V1 plan is unnecessary. With a
prompt-end mark we read the *actual* input line off the screen, so up-arrow
recall, Tab completion, paste and Ctrl-W are all handled for free.

## 2. Migration map

| V1 plan item / Wave concept | Class | V2 home | Notes |
| --- | --- | --- | --- |
| Promptline branding | ADAPT (defer) | `promptlinelib/version.py`, desktop file, About, window title | Keep internal names (`promptlinelib`, config path) to stay rebase-friendly; user-visible rename in one late phase |
| OpenAI provider (demo) | REWRITE | `promptlinelib/promptline/providers/openai.py` | stdlib `urllib` + SSE parsing, no new deps; Chat Completions shape so one adapter also covers Ollama/LM Studio/vLLM via `base_url` |
| Provider/model config | ADAPT | `config.py` `DEFAULTS['global_config']` (`promptline_*` keys), later a Promptline tab in `prefseditor.py` | API key read from env var named in config (default `OPENAI_API_KEY`); never written to config |
| Multi-provider abstraction | REWRITE (small) | `providers/base.py` | One `Provider` protocol: `chat(messages, tools, stream) -> events`. No Wave-style mode/preset system |
| Shell integration (Wave OSC 16162 A/C/D/M/I + OSC 7) | ADAPT | `promptlinelib/promptline/shell/{bash,zsh,fish}` + spawn hook in `Terminal.spawn_child` | Emit `OSC 666 vte.ext.promptline.mark=A/B/C/D`, `…cmd=<b64>`, `…exit=<n>`, plus OSC 7. Opt-out setting |
| Shadow keystroke buffer | DROP | — | Replaced by reading the input line from VTE (see above) |
| Instant local autocomplete (history, paths, heuristics) | PORT | `promptline/suggest/{history,paths,rank}.py` (pure Python, no GTK) | Prefix-only suggestions (ghost text must be a suffix of what's typed) |
| Debounced LLM autocomplete | PORT | `promptline/suggest/llm.py` + worker thread → `GLib.idle_add` | Opt-in, cancelled on every keystroke, result dropped if it no longer prefixes the input |
| Ghost-text rendering (Wave: xterm.js decoration) | REWRITE | `promptline/ghost.py`: a transparent, input-less `Gtk.DrawingArea` in a `Gtk.Overlay` around the VTE (`Terminal.create_terminalbox`) | Nothing is written to the pty; hidden on alt-screen, scrollback, selection, mid-line cursor |
| Accept suggestion | PORT | `Terminal.on_keypress` hook | Right/End at end-of-input accepts via `feed_child(suffix)`; otherwise keys pass through untouched |
| Suggestion RPC / dropdown (Wave `pkg/suggestion`) | DROP | — | In-process calls; no dropdown UI |
| Wave agent loop (`aiusechat`) | REWRITE | `promptline/agent/loop.py` | Small provider-agnostic tool loop, fully unit-testable with a fake provider |
| `term_run_command` tool | REWRITE | `promptline/agent/tools.py` | See decision in §4 |
| Tool approval registry (Wave `toolapproval.go`) | ADAPT | `promptline/agent/approval.py` | `ApprovalPolicy.decide(cmd) -> ask/allow/deny`; V2 ships only "always ask". Session/trusted policies slot in later without touching execution |
| Approve / Edit / Cancel UI | REWRITE | inline in the terminal (TTY prompt), Edit prefilled via `readline` | No dialogs, no panels |
| Terminal context (Wave `term_get_scrollback` + RTInfo last cmd/exit) | ADAPT | `promptline/context.py`: per-terminal `CommandLog` ring buffer filled from marks C/D (cmd, cwd, exit, output rows) | Output read from VTE between marks, truncated, secrets redacted before sending |
| Wave system prompts | REWRITE | `promptline/agent/prompts.py` | Own text (license); written for a CLI agent, not a side panel |
| Wave AI panel, widgets, blocks, chat store, modes, cloud/premium, telemetry, file/screenshot/web/tsunami tools | DROP | — | Conflict with the traditional-terminal goal |
| Tests | PORT (approach) | `tests/test_promptline_*.py` | Pure modules tested without GTK; overlay/keys under `xvfb-run` |

## 3. Architecture

```
promptlinelib/promptline/        # all new code lives here (GPLv2 headers)
  __init__.py      feature detection: VTE termprops available? config enabled?
  settings.py      typed view over Config()['promptline_*']
  shellint.py      termprop install, mark parsing, spawn arg/env wrapping
  shell/           bash/zsh/fish integration scripts
  context.py       CommandLog (per terminal), InputLine reader
  suggest/         history.py, paths.py, rank.py, llm.py   (no GTK imports)
  ghost.py         overlay widget drawing the suggestion suffix
  controller.py    per-Terminal glue: termprops → context → suggest → ghost
  providers/       base.py, openai.py
  agent/           loop.py, tools.py, approval.py, prompts.py, cli.py
promptline-agent   entry script (like remotinator)
```

Upstream touch points kept deliberately small: `terminal.py`
(`create_terminalbox`, `on_keypress`, `spawn_child`, one controller
attach), `config.py` (defaults), `ipc.py` (one context method),
`prefseditor.py`/glade (later), `setup.py`, `po/POTFILES.in`.

Threading: GTK work on the main loop only; network in worker threads that
hand results back with `GLib.idle_add`. Local suggestions run synchronously
and must stay under a few milliseconds.

Graceful degradation, in order: no VTE termprops (VTE < 0.78) or
integration disabled → plain Terminator; no API key/offline → local
autocomplete only and `@agent` prints a one-line explanation; remote
shells (ssh/tmux) without integration → no ghost text in that pane.

### Input line reading

On mark `B` (prompt end) remember the cursor cell as the input anchor. On
`contents-changed`/`cursor-moved`, the input is the text from the anchor to the
cursor. Ghost text is shown only when the cursor is at the end of the input.
We suppress it when the anchor row no longer matches the prompt (Ctrl-R
search, vi-mode status), when the alternate screen is active, or when the view
is scrolled.

### `@agent` invocation

Terminator intercepts Enter when the input line starts with `@agent`. It
takes the raw text straight from the screen, so apostrophes and `?` never
reach the shell parser. The shell line is then cleared and the agent is
launched in that same terminal (§4). The agent reads context over DBus using
`TERMINATOR_UUID`.

## 4. Decision: where approved agent commands run — **A (decided 2026-09-23)**

**A. The agent runs in the terminal as a foreground program (chosen).**
Promptline starts `promptline-agent` in the pane, like any other command. It
streams its answer, asks `Run: lsof -ti :3000  [a]pprove [e]dit [c]ancel`,
runs approved commands with `$SHELL -c` in the current cwd/env, echoes their
output live, and feeds output and exit code back into the loop. Ctrl-C works,
and scrollback reads like a normal session. Limitation: `cd`/`export`/aliases
don't persist into your shell. For those, the agent places the command on your
prompt (typed but not executed) for you to press Enter.

**B. The agent types approved commands into your live shell.** Terminator
drives the conversation itself and injects each command with `feed_child`,
reading the result back between marks C/D. Shell state persists, but the
agent's own text has to be painted around the shell's prompt redraws. It
also needs shell integration, and captured output is lossy for TUIs and
long output.

## 5. Risks

1. **VTE version.** The termprop API needs VTE ≥ 0.78 (to confirm). Ubuntu
   22.04/24.04 ship older versions, so feature-detect and fall back to plain
   Terminator. CI (Python 3.10 / 22.04) will exercise the fallback path.
2. **Shell integration injection** (bash `--rcfile` wrapper, zsh `ZDOTDIR`
   wrapper, fish `--init-command`) can clash with user frameworks (p10k,
   starship, bash-preexec). Chain hooks and never replace them; provide an
   opt-out.
3. **Input reading edge cases**: zsh `RPROMPT`, multi-line/wrapped input,
   wide characters, vi mode.
4. **Ghost overlay alignment**: font metrics, padding, HiDPI, bidi.
5. **Privacy**: LLM autocomplete and agent context send terminal content to a
   provider. LLM autocomplete defaults to off; redact obvious secrets; never
   send when the provider is unconfigured.
6. **DBus optional** (`-u`, missing `python3-dbus`): the agent then gets only
   cwd/env context.
7. **Upstream drift**: keep core diffs small and isolated.

## 6. Phases

| Phase | Deliverable | User-visible? |
| --- | --- | --- |
| 0 | `promptline` package skeleton, feature detection, `promptline_*` config defaults, test scaffolding | No |
| 1 | Shell integration scripts + injection, termprop listener, `CommandLog`, input-line reader; `-d` debug output | No (debug only) |
| 2 | Local ghost-text autocomplete: history store, path completion, ranking, overlay, accept keys | **Yes — first feature, works offline** |
| 3 | Provider layer (OpenAI + compatible `base_url`) and opt-in LLM autocomplete tier | Yes (opt-in) |
| 4 | `@agent`: interception, DBus context method, agent loop, `run_command` tool, Approve/Edit/Cancel | Yes |
| 5 | Preferences tab, docs/README, user-visible branding, packaging | Yes |
| Later | Approval policies (session/trusted), libsecret key storage, more providers, ssh/remote integration | — |

Each phase ends with passing `xvfb-run -a pytest` and a manual run of
`python3 terminator -u -g /tmp/pl-cfg`.

## 7. Progress notes

- **Phases 0–2 done** (2026-09-23). Word-accept uses Ctrl+Right, since
  Alt+Right is Terminator's `go_right`.
- **Phase 3 done: command prediction.** With `promptline_llm_autocomplete`
  on, a pause in typing (350 ms) sends the model the shell/OS, cwd, a short
  directory listing, the git branch, the last 8 non-private commands with
  exit statuses, the tail of the last output (secrets redacted) and the typed
  prefix. The reply must extend what's typed. On an empty prompt after a
  command, it predicts the next command (`promptline_predict_next`). The
  prediction outranks history. Typing along it doesn't trigger another
  request, and results are cached per (input, cwd, last command). A 401/403/404
  disables prediction for the session with one error. Default model
  `gpt-6-luna` at `xhigh` reasoning (user's choice, 2026-09-23); the token
  limit and timeout grow with the reasoning effort, since reasoning tokens
  count against `max_completion_tokens`.
- **Coexisting with zsh-autosuggestions / fish:** Promptline only draws when
  nothing else is drawn after the cursor, so the shell plugin wins while it
  has a history match. To let prediction lead, skip the plugin when
  `$PROMPTLINE` is set; Promptline's own history tier covers the same ground.
- **Phase 4 done: `@agent`.** Enter on a line starting with `@agent` is
  caught by Terminator, not the shell. The raw text is read off the screen,
  so quotes and `?` are safe. Terminator writes a request (question,
  context snapshot, provider settings, never the key) to
  `$XDG_RUNTIME_DIR/promptline/`. That works without D-Bus, which `-u`
  disables. It then clears the line and runs ` _promptline_agent TOKEN`.
  That shell function records `@agent <question>` in history (the
  launcher line itself is kept out) and runs `promptline-agent`. The
  agent redraws the line as the user typed it, then works in the terminal
  like any program. `run_command` always asks Approve/Edit/Cancel
  (`approval.AskEveryTime`; future policies plug in there).
  `place_on_prompt` types a command at the next prompt. Conversations
  persist per terminal for 30 idle minutes.
- **OpenAI API finding:** `gpt-6-luna` rejects tools combined with
  `reasoning_effort` on Chat Completions, so tool requests to
  api.openai.com use the Responses API (`store: false`, encrypted
  reasoning carried between turns). Verified live, including a follow-up
  turn. Prediction and local servers stay on Chat Completions.
- **Phase 5 done: identity, preferences, docs.** Renamed so Promptline
  installs next to Terminator (`promptlinelib`, `promptline` command, D-Bus
  `io.github.m_tek39.Promptline`, `~/.config/promptline` with a first-run
  copy of the Terminator config). Added a Preferences → Promptline page
  built in code, the README/INSTALL, and `doc/UPSTREAM.md`. A trial merge
  of a simulated upstream release showed edits following the rename,
  added files landing in `promptlinelib/` (with
  `merge.directoryRenames=true`), and a conflict in `config.py`, which is
  now avoided by `promptline_defaults.py`.
- **Regression fixed during phase 5:** from phase 4 on, termprops were
  registered after the first terminal existed, which VTE refuses, so the
  real app got no shell marks. `terminal.py` now registers them at import,
  and a fresh-process test guards it.
- **Reasoning max/ultra, subagents, 1M context (2026-10-09).** `max` and
  `ultra` reasoning levels (Claude `output_config.effort` with thinking blocks
  carried, `xhigh` for OpenAI-style servers). Subagents (`off`/`auto`/`always`,
  user-defined in `agents/`), strict workflows in `flows/`, and `ultra`: `max`
  plus a built-in plan, parallel explore, act, check workflow that overrides the
  user's subagent settings. Read-only subagents run unasked through an
  allowlist. Default context window 1M tokens, with summarise-and-retry when a
  model has less.
- **Goals, steering, reviewer context (2026-10-09).** `@agent --goal` works
  without a step limit (retries, nudges, stuck detection, summarising when
  the context grows, approval timeout that skips and never approves).
  Typing while the agent thinks steers it. The auto-review reviewer now sees
  the conversation, which fixes `@agent continue` reaching it as just
  "continue". Step limit and approval wait are settings.
- **Multi-provider (2026-10-08).** `providers.PRESETS` names OpenAI,
  Anthropic (Messages API, `providers/anthropic.py`), OpenCode Zen (format
  chosen per model; Gemini models unsupported), OpenRouter, Gemini's
  compatibility endpoint, Ollama, LM Studio and a custom OpenAI-compatible
  server. Prediction and `@agent` can use different providers. API keys only,
  no subscription sign-in. Not yet verified against live providers.
- **0.2 features (2026-09-28).** Personalisation (`promptline -P`, a template
  for security and network work) and agent memory (`remember`/`forget` tools,
  learning from edited commands) feed both prediction and `@agent`. There are
  three permission modes: ask (default), auto-review (a reviewer model, where
  anything not clearly safe asks) and full (locked until the user writes 3 or
  more guardrail rules). A hard stop list always asks, and every agent
  command goes to an audit log. Agent replies stream (Responses API events
  for OpenAI, Chat Completions chunks for compatible servers). `-p` stays
  Terminator's `--profile`, so personalisation is `-P`.
