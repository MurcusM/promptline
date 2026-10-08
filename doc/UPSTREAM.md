# Merging upstream Terminator

Promptline is a downstream of [Terminator](https://github.com/gnome-terminator/terminator).
It picks up Terminator's fixes by merging upstream releases. It never rebases,
because Promptline's history is public.

## Keeping merges cheap

- New Promptline code goes in `promptlinelib/promptline/` (or another new
  file). Upstream never touches those, so they never conflict.
- Changes to Terminator's files are small hooks that call into
  `promptlinelib/promptline/`. Currently:

  | File | Promptline change |
  | --- | --- |
  | `terminal.py` | attach a controller, wrap the VTE for the suggestion layer, Enter/→ key hook, shell integration at spawn, register termprops on import |
  | `config.py` | two lines merging `promptline_defaults.GLOBAL_DEFAULTS` and `PROFILE_DEFAULTS` into `DEFAULTS`; `defaults_to_configspec()` quotes every empty-string default, as it already did for `custom_url_handler` |
  | `prefseditor.py` | add the Promptline page to the notebook |
  | `optionparse.py` | `-P/--personalise`, `--guardrails`, `--memory`: edit the file and exit, like `--list-profiles` |
  | `notebook.py` | crash fixes: `remove()` passes the widget to `detach_tab()`, not the page number; `closetab()` tolerates a missing `last_active_term` entry. Keep whichever side upstream fixes. |
  | `util.py` | config directory name, first-run import of Terminator settings |
  | `ipc.py` | D-Bus name |
  | `version.py`, `setup.py`, `data/`, `doc/*.adoc` | identity (name, version, desktop entry, icons, man pages) |
- New settings go in `promptlinelib/promptline_defaults.py`, not in the
  `DEFAULTS` literal, where upstream adds its own options.
- The package was renamed from `terminatorlib` with `git mv`, so git follows
  upstream edits into `promptlinelib/`.

## Merging a release

```sh
git remote add upstream https://github.com/gnome-terminator/terminator.git  # once
git fetch upstream --tags

git switch -c merge-upstream-vX.Y.Z master
git -c merge.directoryRenames=true merge vX.Y.Z
```

`merge.directoryRenames=true` makes git put files that upstream *added*
under `terminatorlib/` into `promptlinelib/`, instead of stopping to ask.

Then:

1. **Resolve conflicts.** Keep upstream's change and reapply the Promptline
   hook next to it.
2. **Fix imports in files upstream added.** New files arrive with
   `terminatorlib` imports:
   ```sh
   git grep -l terminatorlib -- promptlinelib promptline promptline-remote tests \
     | xargs -r sed -i 's/terminatorlib/promptlinelib/g'
   ```
   Leave `CHANGELOG*`, `RELEASE.md` and `po/` alone. They are history and
   translation comments.
3. **Check new identity strings.** Look for new `'terminator'` icon names,
   paths or D-Bus names in the diff:
   `git diff master... | grep -in "terminator"`.
4. **Record the base.** Set `UPSTREAM_VERSION` in `promptlinelib/version.py`.
5. **Test.** Run `xvfb-run -a pytest` and `python3 promptline -u -d`. Upstream's
   `test_prefseditor_keybindings.py::...[input_key_params2-...]` also fails
   on unmodified Terminator in some keyboard setups.
6. Open a pull request from the merge branch.

## Translations

`po/*.po` are Terminator's catalogues, updated upstream through Transifex;
merge them as they come. The gettext domain is now `promptline`, and
Promptline's own strings (for example in `promptline/prefs.py`) are listed in
`po/POTFILES.in`. Regenerate the template with `cd po && ./genpot.sh` when
strings change.
