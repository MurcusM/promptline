# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""prefs.py - the Promptline page of the Preferences window

Built in code rather than in preferences.glade, so that Promptline's UI
stays out of the upstream Terminator files.
"""

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from ..translation import _
from . import personal
from .providers import (PRESETS, connection, key_problem, needs_key,
                        provider_settings, resolve_api_key)

REASONING = ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']

MODES = [
    ('ask', _('Ask before every command')),
    ('auto-review', _('Auto-review')),
    ('full', _('Full permission')),
]
MODE_NOTES = {
    'ask': _('Every command waits for Approve / Edit / Cancel.'),
    'auto-review': _('A reviewer model checks each command. Commands it '
                     'judges safe run without asking; anything else asks '
                     'you, with its reason. A review can be wrong: only use '
                     'this where a mistake is recoverable.'),
    'full': _('DANGEROUS: commands run without asking. Only a short list of '
              'catastrophic commands still asks. Requires your guardrails, '
              'which are added to the agent\'s instructions. Every command '
              'is logged to ~/.local/share/promptline/agent-audit.log.'),
}


class PromptlinePage(object):
    """Widgets bound to the promptline_* keys in [global_config]"""
    def __init__(self, config):
        self.config = config
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=6,
                             margin=18)
        self.row = 0
        self.key_status = None
        self.reverting_mode = False
        self.widgets = {}
        self.key_rows = {}

        self.heading(_('Promptline'))
        self.note(_('With these off, Promptline behaves exactly like '
                    'Terminator. Changes apply to new terminals.'))
        self.check('enabled', _('Enable Promptline'))
        self.check('shell_integration',
                   _('Load shell integration (bash and zsh)'))
        self.check('autocomplete',
                   _('Suggest from history and paths as you type'))

        self.heading(_('Command prediction'))
        self.check('llm_autocomplete', _('Predict commands with a model'))
        self.note(_('Sends the current directory, recent commands, their '
                    'exit status and the end of the last output to the '
                    'provider. Things that look like secrets are removed, '
                    'and commands typed with a leading space are never '
                    'sent.'))
        self.check('predict_next',
                   _('Predict the next command on an empty prompt'))
        self.provider_chooser('autocomplete')
        self.key_file('autocomplete')
        self.entry('autocomplete_model', _('Model'))
        self.reasoning('autocomplete_reasoning', _('Reasoning effort'))

        self.heading(_('Personalisation'))
        self.note(_('Tell Promptline about your work, the tools you prefer '
                    'and your environments, so predictions and @agent fit '
                    'you from the start. The agent also keeps a memory of '
                    'what it learns about you. All three are plain text '
                    'files you can edit.'))
        self.file_buttons()

        self.heading(_('@agent'))
        self.note(_('Type "@agent" and a question or task at the prompt.'))
        self.provider_chooser('agent')
        self.key_file('agent')
        self.entry('agent_model', _('Model'))
        self.reasoning('agent_reasoning', _('Reasoning effort'))
        self.mode_chooser()

        self.heading(_('Default provider'))
        self.note(_('Used by prediction and @agent unless they choose '
                    'their own above. Picking a provider clears the fields '
                    'below and suggests models for it. The fields only '
                    'override the provider\'s own address and key '
                    'variable, e.g. for another OpenAI-compatible server '
                    '(choose "Other"). Local servers need no key. '
                    'Reasoning effort is sent to OpenAI-compatible servers '
                    'and ignored by Anthropic models; leave it empty for '
                    'servers that reject it.'))
        self.provider_chooser()
        self.entry('base_url', _('API base URL'))
        self.entry('api_key_env', _('Key environment variable'))
        self.key_file()
        self.refresh_placeholders()
        self.refresh_key_rows()
        self.key_status = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
        self.attach(self.key_status)
        self.update_key_status()

    # Rows

    def attach(self, widget, label=None):
        if label is None:
            self.grid.attach(widget, 0, self.row, 2, 1)
        else:
            self.grid.attach(Gtk.Label(label=label, xalign=0), 0, self.row,
                             1, 1)
            widget.set_hexpand(True)
            self.grid.attach(widget, 1, self.row, 1, 1)
        self.row += 1

    def heading(self, text):
        label = Gtk.Label(xalign=0)
        label.set_markup('<b>%s</b>' % text)
        if self.row:
            label.set_margin_top(12)
        self.attach(label)

    def note(self, text):
        label = Gtk.Label(label=text, xalign=0, wrap=True,
                          max_width_chars=70)
        label.get_style_context().add_class('dim-label')
        self.attach(label)

    def check(self, key, text):
        button = Gtk.CheckButton(label=text)
        button.set_active(bool(self.config['promptline_' + key]))
        button.connect('toggled', lambda b: self.set(key, b.get_active()))
        self.attach(button)

    def entry(self, key, text):
        entry = Gtk.Entry(text=self.config['promptline_' + key])
        entry.connect('changed', lambda e: self.set(key, e.get_text().strip()))
        self.widgets[key] = entry
        self.attach(entry, text)

    def reasoning(self, key, text):
        combo = Gtk.ComboBoxText.new_with_entry()
        for effort in REASONING:
            combo.append_text(effort)
        combo.get_child().set_text(self.config['promptline_' + key])
        combo.connect('changed', lambda c: self.set(
            key, (c.get_active_text() or '').strip()))
        self.widgets[key] = combo.get_child()
        self.attach(combo, text)

    def provider_chooser(self, purpose=None):
        """Choose the default provider, or the one for a purpose"""
        combo = Gtk.ComboBoxText()
        if purpose:
            combo.append('', _('Same as the default provider'))
        for preset in PRESETS.values():
            combo.append(preset.name, preset.label)
        combo.set_active_id(self.config[self.provider_key(purpose)])
        combo.connect('changed', self.on_provider_changed, purpose)
        self.attach(combo, _('Provider'))
        self.widgets[self.provider_key(purpose)] = combo

    @staticmethod
    def provider_key(purpose):
        return 'promptline_%sprovider' % (purpose + '_' if purpose else '')

    def mode_chooser(self):
        combo = Gtk.ComboBoxText()
        for mode, label in MODES:
            combo.append(mode, label)
        current = self.config['promptline_agent_mode']
        combo.set_active_id(current if current in dict(MODES) else 'ask')
        self.mode_note = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
        self.mode_note.set_text(MODE_NOTES[combo.get_active_id()])
        combo.connect('changed', self.on_mode_changed)
        self.attach(combo, _('Permission mode'))
        self.attach(self.mode_note)
        self.mode_combo = combo

    def on_mode_changed(self, combo):
        mode = combo.get_active_id()
        if self.reverting_mode:
            return
        if mode == 'full':
            ready, why = personal.guardrails_ready()
            if not ready:
                previous = self.config['promptline_agent_mode']
                # Reverting fires 'changed' again; keep the explanation
                self.reverting_mode = True
                combo.set_active_id(previous if previous != 'full'
                                    else 'ask')
                self.reverting_mode = False
                self.mode_note.set_text(
                    _('Full permission is locked: %s. Use the Guardrails... '
                      'button above, then choose it again.') % why)
                return
        self.set('agent_mode', mode)
        self.mode_note.set_text(MODE_NOTES[mode])

    def file_buttons(self):
        box = Gtk.Box(spacing=6)
        for kind, label in (('personal', _('About me...')),
                            ('guardrails', _('Guardrails...')),
                            ('memory', _('Memory...'))):
            button = Gtk.Button(label=label)
            button.connect('clicked',
                           lambda _b, k=kind: personal.edit(k, gui=True))
            box.pack_start(button, False, False, 0)
        self.attach(box)

    def key_file(self, purpose=None):
        """The key file of the default provider, or of a purpose that uses
        a different one (that row only shows while it does)"""
        key = (purpose + '_' if purpose else '') + 'api_key_file'
        box = Gtk.Box(spacing=6)
        entry = Gtk.Entry(text=self.config['promptline_' + key],
                          hexpand=True,
                          placeholder_text=_('optional, e.g. '
                                             '~/.config/promptline/api-key'))
        entry.connect('changed',
                      lambda e: self.set(key, e.get_text().strip()))
        choose = Gtk.Button(label=_('Choose...'))
        choose.connect('clicked', self.on_choose_key_file, entry)
        box.pack_start(entry, True, True, 0)
        box.pack_start(choose, False, False, 0)
        self.attach(box, _('Key file'))
        self.widgets[key] = entry
        if purpose:
            row = [self.grid.get_child_at(0, self.row - 1), box]
            for widget in row:
                widget.set_no_show_all(True)
            self.key_rows[purpose] = row

    def refresh_key_rows(self):
        """A purpose's own key file is only needed for a different provider
        from the default, which would otherwise use the default's file"""
        default = self.config['promptline_provider']
        for purpose, row in self.key_rows.items():
            own = self.config['promptline_%s_provider' % purpose]
            for widget in row:
                widget.set_visible(bool(own) and own != default)

    # Behaviour

    def set(self, key, value):
        self.config['promptline_' + key] = value
        self.config.save()
        if 'api_key' in key and self.key_status is not None:
            self.update_key_status()

    def on_provider_changed(self, combo, purpose):
        """A provider was chosen: forget the old one's address and key
        (which would otherwise be sent to the new one), and suggest models
        for it"""
        name = combo.get_active_id()
        prefix = purpose + '_' if purpose else ''
        if name is None or name == self.config[self.provider_key(purpose)]:
            return
        for field in ('base_url', 'api_key_env', 'api_key_file'):
            self.set(prefix + field, '')
            if prefix + field in self.widgets:
                self.widgets[prefix + field].set_text('')
        self.set(prefix + 'provider', name)
        preset = PRESETS.get(name)
        for kind in [purpose] if purpose else ('autocomplete', 'agent'):
            if preset is None or (
                    not purpose and self.config['promptline_%s_provider' % kind]):
                continue
            self.widgets[kind + '_model'].set_text(
                getattr(preset, kind + '_model'))
            if name != 'openai':
                # Other servers may reject OpenAI's reasoning_effort
                self.widgets[kind + '_reasoning'].set_text('')
        self.refresh_placeholders()
        self.refresh_key_rows()
        self.update_key_status()

    def refresh_placeholders(self):
        """Show the chosen provider's own address and key variable in the
        fields that override them"""
        preset = PRESETS.get(self.config['promptline_provider'])
        self.widgets['base_url'].set_placeholder_text(
            preset.base_url if preset else '')
        self.widgets['api_key_env'].set_placeholder_text(
            preset.key_env if preset else '')

    def update_key_status(self):
        """Say whether a key is found, without ever showing it"""
        found = {}
        for purpose in ('agent', 'autocomplete'):
            settings = provider_settings(purpose, self.config)
            if resolve_api_key(settings) or \
                    not needs_key(connection(settings)[1]):
                found[purpose] = _('An API key was found.')
            else:
                found[purpose] = (_('No API key found: ') +
                                  (key_problem(settings) or
                                   _('set a key variable or a key file')) +
                                  '.')
        if found['agent'] == found['autocomplete']:
            lines = [found['agent']]
        else:
            lines = [_('@agent: ') + found['agent'],
                     _('Prediction: ') + found['autocomplete']]
        self.key_status.set_text('\n'.join(lines))

    def on_choose_key_file(self, button, entry):
        dialog = Gtk.FileChooserDialog(
            title=_('Choose the file containing your API key'),
            transient_for=button.get_toplevel(),
            action=Gtk.FileChooserAction.OPEN)
        dialog.add_buttons(_('Cancel'), Gtk.ResponseType.CANCEL,
                           _('Choose'), Gtk.ResponseType.ACCEPT)
        dialog.set_show_hidden(True)
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            entry.set_text(dialog.get_filename())
        dialog.destroy()


def add_page(notebook, config):
    """Append the Promptline page to the Preferences notebook"""
    page = PromptlinePage(config)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.add(page.grid)
    scroller.show_all()
    notebook.append_page(scroller, Gtk.Label(label=_('Promptline')))
    return page
