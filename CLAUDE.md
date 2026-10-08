# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Promptline**: a traditional terminal with intelligence built into the command line. It
has inline command suggestions and prediction, plus an `@agent` the user invokes from
the prompt. It is a downstream of [Terminator](https://github.com/gnome-terminator/terminator)
(Python 3 + GTK3 + VTE), renamed so the two install side by side (package
`promptlinelib`, command `promptline`, D-Bus `io.github.m_tek39.Promptline`, config
`~/.config/promptline`), and it still merges upstream Terminator releases.

- Repo: `origin` = `git@github.com:MurcusM/promptline.git` (public), branch `master`;
  `upstream` = gnome-terminator/terminator. Merge upstream, never rebase (`doc/UPSTREAM.md`).
- Design, decisions and progress notes: `doc/promptline-plan.md`. User docs: `README.md`.
- `../v1/` is an unrelated, reference-only Wave Terminal fork. Don't develop there, don't
  mix its conventions in, and **never copy Wave code or prompt text**: Wave is
  Apache-2.0, which is incompatible with this GPL-2.0-only codebase.

## Product rules (don't break these)

1. **Terminal first.** No side panels, dashboards, persistent chat windows, or output
   "blocks". AI stays on the command line: dimmed text after the cursor, and `@agent`
   running as an ordinary program in the terminal.
2. **With Promptline off it is Terminator.** `promptline_enabled = False`, a VTE without
   termprops, or an unsupported shell must all give plain Terminator behaviour.
3. **Local first, never slow.** Local suggestions run synchronously on every keystroke
   and must stay in the microsecond-to-millisecond range. Anything networked runs on
   a worker thread after a pause in typing, hands results back with `GLib.idle_add`,
   and drops results that are stale.
4. **Degrade quietly.** No key, offline, rate-limited, or a bad model: the terminal keeps
   working, and errors are logged once (auth errors disable prediction for the session).
5. **Approval is the default, and autonomy is opt-in.** `run_command` always goes
   through a policy in `agent/approval.py`. `ask` (default) asks every time.
   `auto-review` runs only what a reviewer model calls safe and asks for anything
   else, including failed or unreadable reviews. `full` runs without asking, but only
   once the user has written guardrails (`personal.guardrails_ready()`, re-checked on
   every run). `HARD_STOPS` always ask, in every mode. Never make a mode more
   permissive by default, never let a review failure allow, and add new modes as
   new policies.
6. **Keep providers swappable.** Callers use `providers.make_provider(purpose, settings)`.
   A provider is a preset in `providers.PRESETS` (name, API kind, default URL and key
   variable); kinds are `openai` (Chat Completions and Responses), `anthropic` (Messages)
   and `opencode` (picks the format per model). Prediction and `@agent` may use different
   providers (`promptline_<purpose>_provider`). Add a provider as a preset, or a new
   file in `providers/` when the wire format differs. Providers take and return Chat
   Completions-shaped messages; translate inside the provider.

## Privacy rules

- Model prediction is **opt-in** (`promptline_llm_autocomplete = False` by default).
- A command typed with a **leading space** is private (`CommandRecord.private`). It is
  never learned, never sent, and never put in agent context. Promptline's own
  `_promptline_agent` launcher runs are private too.
- Run everything sent to a model through `suggest/llm.redact()`, and send tails of
  output, not whole scrollback.
- **API keys are never written anywhere.** They come from the env var named in
  `promptline_api_key_env` or the file in `promptline_api_key_file`; the agent request
  file carries settings, not the key. The Preferences page says whether a key was
  found and never shows it.
- The user's files (`personal.md`, `guardrails.md`, `memory.md`, `agent-audit.log`) are
  0600. `<!-- -->` guidance is stripped before sending, and memory facts go through
  `redact()` before they are saved.
- Tests must never reach a real provider (the fixtures stub `make_provider`), and
  must never touch the user's config or data (`conftest.py` isolates both XDG dirs).
- Every command the agent runs is appended to the audit log with how it was
  approved. Keep that true for any new way of running commands.

## Commands

```sh
python3 promptline -u              # run from the source tree; -u = don't hand off over D-Bus
python3 promptline -u -d           # debug output (Promptline logs "promptline mark ...", predictions)
python3 promptline -g /tmp/cfg     # alternate config file (or set XDG_CONFIG_HOME)

xvfb-run -a pytest                 # full suite (CI: Ubuntu 22.04 / Python 3.10, and Ubuntu 26.04 for the end-to-end tests)
xvfb-run -a pytest tests/test_promptline_shell.py   # end-to-end: real bash/zsh in a real VTE
xvfb-run -a pytest promptlinelib/promptline         # Promptline doctests
python -m compileall -f promptlinelib/ tests/ promptline-remote promptline promptline-agent

dpkg-buildpackage -us -uc -b       # Debian package → ../promptline_<version>_all.deb
debian/rules clean                 # remove .pybuild/ and debian/promptline/ afterwards
```

- **Branches and releases** follow `CONTRIBUTING.md`: every change reaches `master` or a
  `release/X.Y.x` branch through a pull request (rulesets block direct and force pushes),
  and releases are tagged on the release branch. **Releasing:** in a PR to the release
  branch, bump `APP_VERSION` in `promptlinelib/version.py` and add a `debian/changelog`
  entry with the same version (its bullets become the release notes). After it merges,
  tag the merge commit `vX.Y.Z`. `.github/workflows/release.yml` checks that the three
  agree, builds and validates the `.deb`, and publishes a GitHub release with it and
  `SHA256SUMS`; on pull requests into a release branch it only builds and checks.
  Then merge the release branch into `master` through a PR.
  Tagging publishes to the public repo, so only do it when the user asks. The desktop file and AppStream ID is `APP_ID`
  (`io.github.m_tek39.Promptline`). Files the app reads at runtime from its package
  directory (glade, `themes/`, `promptline/shell/`) must be listed in
  `setup.py`'s `package_data`. Check a staged build with `desktop-file-validate`
  and `appstreamcli validate --no-net`.

- `pytest.ini` sets `--doctest-modules`, so `>>>` examples in `promptlinelib/**/*.py` are
  tests. Promptline's pure modules keep their unit tests as doctests.
- The root `conftest.py` points `XDG_CONFIG_HOME` at a temp dir for the whole run, because
  upstream's Preferences tests write `config_cur` into the real config dir. Keep it.
- `conftest.py` also sets `GDK_BACKEND=x11` under `xvfb-run`, so tests never open windows
  on the desktop's Wayland session. The whole suite passes locally; anything failing is yours.
- Drawing suggestions needs `python3-gi-cairo`. Without it, suggestions are computed but
  not drawn, and one error is logged.
- Test live against a provider only when the user asks, and keep it to one or two
  small requests on their key.

## How Promptline works

All Promptline code is in `promptlinelib/promptline/`:

| Module | Role |
| --- | --- |
| `shell/promptline.bash`, `shell/promptline.zsh`, `shell/zdotdir/.zshenv` | Integration loaded after the user's own rc files (bash `--rcfile`, zsh `ZDOTDIR` shim). Emits marks and OSC 7; defines `_promptline_agent` |
| `shellint.py` | Rewrites argv/env at spawn (`Terminal.spawn_child`); adds `PROMPTLINE*` env vars |
| `marks.py` | The protocol: termprops `vte.ext.promptline.{exec,done,prompt,input,after}` set with `OSC 666 ; name=value ST` |
| `controller.py` | One per `Terminal`: VTE signals → session, suggestion refresh, key handling, `@agent` launch |
| `session.py` | GTK-free state machine: reads the typed line off the screen, logs commands (cwd, exit status, output) |
| `suggest/` | `history.py` (shell histories + own log, frecency), `paths.py` (unambiguous path completion), `llm.py` (context, redaction, `Predictor`) |
| `ghost.py` | Transparent `Gtk.Overlay` layer drawing the suggestion on VTE's cell grid; never writes to the pty |
| `providers/` | `PRESETS`, `make_provider`, key resolution; `openai.py` (Chat Completions; Responses for OpenAI tool calls and Zen's GPT models), `anthropic.py` (Messages API) |
| `agent/` | `@agent`: request handoff (`__init__`), `loop.py`, `tools.py` (`run_command`, `place_on_prompt`, `remember`, `forget`), `prompts.py` (mode text, personalisation, memory, guardrails), `cli.py` (the `promptline-agent` program: policy choice, audit log, streaming UI) |
| `personal.py` | The user's files: personalisation, guardrails (`guardrails_ready`), memory (`Memory`), editing (`promptline -P/--guardrails/--memory`, hooked in `optionparse.py`) |
| `agent/approval.py` | Permission modes: `AskEveryTime`, `AutoReview` + `ModelReviewer`, `FullPermission`; `HARD_STOPS` |
| `prefs.py` | Preferences → Promptline page, built in code |

**Flow.** The shell emits marks, `Controller.on_termprops_changed` applies them in
`marks.ORDER` (one VTE batch can carry several), and `ShellSession` updates. On every
cursor or contents change the controller refreshes the suggestion: a pending model
prediction first, then history, then paths. Right/End accepts it with `feed_child`,
Ctrl+Right accepts one word.

**`@agent`.** On Enter, the controller reads the line off the screen (so quotes and `?`
never reach the shell). It writes `$XDG_RUNTIME_DIR/promptline/TOKEN.{json,query}`
(0600), clears the line with Ctrl+E Ctrl+U, and types ` _promptline_agent TOKEN`. The
shell function records `@agent <question>` in history and runs `promptline-agent`,
which redraws the line, runs the loop in the terminal, and may leave `TOKEN.prefill`.
The controller types that at the next prompt. This needs no D-Bus, so it works with
`-u`. Conversations persist per terminal (`conversation-*.json`, 30 idle minutes).

## Working on the code

- **Where changes go.** Put new behaviour in `promptlinelib/promptline/` (or another new
  file). Changes to Terminator's files must be small hooks; the current list is in
  `doc/UPSTREAM.md`, so update it when you add one.
- **New settings** go in `promptlinelib/promptline_defaults.py` (`GLOBAL_DEFAULTS`,
  `PROFILE_DEFAULTS`), never in `config.py`'s `DEFAULTS` literal, where upstream adds its
  own options. The type of the default drives validation. Expose settings on the
  Preferences page in `prefs.py`, not in `preferences.glade`.
- **Keep for compatibility:** the `terminator-*` CSS classes, the `TERMINATOR_UUID` and
  `TERMINATOR_DBUS_*` env vars, and `Terminator()` / `terminator.py` internals.
- **Translations:** `po/` catalogues come from upstream; the template is
  `po/promptline.pot` (`cd po && ./genpot.sh`, needs gettext + intltool). Add new files
  with `_()` strings to `po/POTFILES.in`. `.tx/config` is Terminator's Transifex
  project: don't push to it.
- **Tests:** pure logic gets doctests; anything touching shells or VTE goes in
  `tests/test_promptline_shell.py`, driven through `FakeTerminal`, `start_shell`, `run` and
  `typed`, with the fake OpenAI server for prediction and `@agent`. Wait on conditions
  with `wait_for`, never fixed sleeps, except when checking that something does *not*
  happen.

### Traps we have hit

- **Termprops** must be registered before any `Vte.Terminal` is realized. `terminal.py`
  imports `promptline.marks` for this, and `test_termprops_registered_before_first_terminal`
  guards it. Nothing else may make that import lazy.
- `promptline-agent` must stay light: don't import `marks`, controller or GTK widgets
  from agent code.
- The termprop protocol: values must end with ST (`ESC \`; BEL is ignored), `;`
  truncates a value (hence base64 for command text), and VTE ignores OSC 133.
- zsh: `status` is a read-only parameter (use `ret`). Hooks are finalised on the first
  precmd so plugin managers can't displace them. bash: *prepend* to `PS0`, because
  distro `vte.sh` leaves a trailing backslash.
- Alt+Right is Terminator's `go_right`; our key hook runs after keybinding lookup.
- `Signalman` allows one handler per signal per widget, so the controller keeps its own.
- Require `Gdk` 3.0 explicitly in new modules and tests: importing `Gdk` before `Gtk`
  picks GDK 4.
- Model prediction must ignore input starting with `@`, and the agent test disables
  prediction (it shares the fake server).
- Provider settings: `promptline_base_url` and `promptline_api_key_env` default to empty,
  meaning the preset's own. A purpose naming a *different* provider takes all four
  connection settings from its own keys and never mixes in the default's; one naming the
  same provider (or none) shares the default's connection, its own settings winning. (It
  once didn't, so the default's key file was ignored.) Changing provider in Preferences
  clears the key file, so one provider's key is never sent to another.
- OpenCode Zen: model name decides the API (`opencode_api`); Gemini models need Google's
  format and give an `Unsupported` provider whose error explains why.
- OpenAI: tools combined with `reasoning_effort` are rejected on Chat Completions for
  reasoning models, so they go through the Responses API with encrypted reasoning
  carried in `_reasoning`. Reasoning tokens count against the output limit
  (`REASONING_BUDGET`).

## Terminator background

- **Borg singletons** (`borg.py`): `Terminator()`, `Config()`, `Factory()`,
  `PluginRegistry()` share state. Class attributes are declared `= None` and set in
  `prepare_attributes()`.
- **Widget tree:** `Window` → (`Notebook`) → `HPaned`/`VPaned` → `Terminal`, all `Container`s.
  Create them with `Factory().make_*()` and check types with
  `Factory().isinstance(obj, 'Terminal')`, which avoids circular imports.
- **Terminal** (`terminal.py`) wraps `Vte.Terminal`. Keybindings: defaults in
  `DEFAULTS['keybindings']`, dispatched to `key_<action>()`.
- **Config** (`config.py`): ConfigObj at `~/.config/promptline/config`. Only values that
  differ from the defaults are written. On first run, `util.import_terminator_config()`
  copies Terminator's config and plugins.
- **Plugins** (`plugin.py`, `promptlinelib/plugins/`, `~/.config/promptline/plugins/`)
  list their classes in `AVAILABLE` and load only if named in `enabled_plugins`.
- **Signals** are tracked with `Signalman` so they can be removed on reparent or destroy.

## Conventions

- Log with `dbg()` / `err()` from `util.py`, never `print`/`logging`. `dbg` prints only with `-d`.
- Wrap user-visible strings in `_()` from `promptlinelib.translation`.
- GPL v2 only. New files carry the same `# Terminator by Chris Jones <cmsj@tenshu.net>` /
  `# GPL v2 only` header as the rest of the tree. Keep `AUTHORS`, `COPYING` and existing
  copyright headers intact.
- Write plain, specific docstrings and comments that explain *why*, matching the existing
  Promptline modules.
