#!/usr/bin/env python
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""Tests for the Promptline page of the Preferences window"""

import pytest
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk

from promptlinelib.config import Config
from promptlinelib.promptline import prefs


@pytest.fixture
def config():
    config = Config()
    config.inhibit_save()   # never write the developer's real config
    saved = dict((key, config[key]) for key in
                 ('promptline_enabled', 'promptline_api_key_env',
                  'promptline_api_key_file', 'promptline_agent_reasoning',
                  'promptline_provider', 'promptline_agent_provider',
                  'promptline_agent_model', 'promptline_autocomplete_provider',
                  'promptline_autocomplete_model',
                  'promptline_autocomplete_reasoning',
                  'promptline_agent_api_key_file',
                  'promptline_autocomplete_api_key_file',
                  'promptline_agent_max_steps',
                  'promptline_goal_approval_wait'))
    yield config
    for key, value in saved.items():
        config[key] = value
    config.uninhibit_save()


def widget_labelled(grid, text, kind):
    """The widget of type kind that is, or sits next to, the label text"""
    children = grid.get_children()
    for child in children:
        if isinstance(child, kind) and getattr(child, 'get_label',
                                               lambda: None)() == text:
            return child
    for child in children:
        if isinstance(child, Gtk.Label) and child.get_text() == text:
            top = grid.child_get_property(child, 'top-attach')
            return grid.get_child_at(1, top)
    raise LookupError(text)


def test_widgets_write_config(config):
    page = prefs.PromptlinePage(config)
    check = widget_labelled(page.grid, 'Enable Promptline', Gtk.CheckButton)
    assert check.get_active() == config['promptline_enabled']
    check.set_active(not check.get_active())
    assert config['promptline_enabled'] == check.get_active()

    combo = widget_labelled(page.grid, 'Reasoning effort', Gtk.ComboBoxText)
    combo.get_child().set_text('low')
    assert config['promptline_autocomplete_reasoning'] == 'low' or \
        config['promptline_agent_reasoning'] == 'low'


def test_key_status_never_shows_the_key(config, monkeypatch, tmp_path):
    monkeypatch.setenv('PL_TEST_KEY', 'sk-test-secret-value')
    page = prefs.PromptlinePage(config)
    page.set('api_key_file', '')
    page.set('api_key_env', 'PL_TEST_KEY_MISSING')
    assert page.key_status.get_text().startswith('No API key')
    page.set('api_key_env', 'PL_TEST_KEY')
    assert page.key_status.get_text() == 'An API key was found.'
    assert 'secret' not in page.key_status.get_text()


def test_choosing_a_provider_forgets_the_old_ones_key_and_suggests_models(
        config, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-secret')
    monkeypatch.setenv('OPENCODE_API_KEY', 'zen-secret')
    page = prefs.PromptlinePage(config)
    page.set('api_key_file', '/some/openai-key')
    page.set('agent_reasoning', 'xhigh')
    page.widgets['promptline_provider'].set_active_id('anthropic')
    assert config['promptline_provider'] == 'anthropic'
    assert config['promptline_api_key_file'] == ''
    assert config['promptline_agent_model'] == 'claude-sonnet-5-5'
    assert config['promptline_autocomplete_model'].startswith('claude-haiku')
    assert config['promptline_agent_reasoning'] == ''
    assert page.key_status.get_text() == 'An API key was found.'

    # Prediction on its own provider leaves @agent alone
    page.widgets['promptline_autocomplete_provider'].set_active_id('ollama')
    assert config['promptline_autocomplete_provider'] == 'ollama'
    assert config['promptline_agent_model'] == 'claude-sonnet-5-5'
    assert config['promptline_provider'] == 'anthropic'
    assert page.key_status.get_text() == 'An API key was found.'

    # A purpose without a key is told apart, and the reason is given
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    page.widgets['promptline_autocomplete_provider'].set_active_id('deepseek')
    assert page.key_status.get_text() == (
        '@agent: An API key was found.\nPrediction: No API key found: '
        'DEEPSEEK_API_KEY is not set in the environment Promptline was '
        'started with.')

    page.widgets['promptline_agent_provider'].set_active_id('opencode')
    assert config['promptline_agent_model'] == 'claude-sonnet-5-5'
    page.widgets['promptline_autocomplete_provider'].set_active_id('')
    assert config['promptline_autocomplete_provider'] == ''


def test_a_purposes_key_file_only_shows_for_a_different_provider(config):
    config['promptline_provider'] = 'opencode'
    config['promptline_agent_provider'] = 'opencode'
    page = prefs.PromptlinePage(config)
    assert not page.key_rows['agent'][1].get_visible()
    page.widgets['promptline_agent_provider'].set_active_id('deepseek')
    assert page.key_rows['agent'][1].get_visible()
    page.widgets['agent_api_key_file'].set_text('/home/x/deepseek-key')
    assert config['promptline_agent_api_key_file'] == '/home/x/deepseek-key'
    page.widgets['promptline_agent_provider'].set_active_id('')
    assert not page.key_rows['agent'][1].get_visible()


def test_page_is_added_to_notebook(config):
    notebook = Gtk.Notebook()
    notebook.append_page(Gtk.Label(label='Global'), Gtk.Label(label='Global'))
    prefs.add_page(notebook, config)
    assert notebook.get_n_pages() == 2
    assert notebook.get_tab_label_text(notebook.get_nth_page(1)) == \
        'Promptline'


def test_import_terminator_config(monkeypatch, tmp_path):
    from promptlinelib import util
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    (tmp_path / 'terminator' / 'plugins').mkdir(parents=True)
    (tmp_path / 'terminator' / 'config').write_text('[global_config]\n')
    (tmp_path / 'terminator' / 'plugins' / 'mine.py').write_text('')
    # A stray snapshot must not stop the import
    (tmp_path / 'promptline').mkdir()
    (tmp_path / 'promptline' / 'config_cur').write_text('')
    util.import_terminator_config()
    assert (tmp_path / 'promptline' / 'config').read_text() == \
        '[global_config]\n'
    assert (tmp_path / 'promptline' / 'plugins' / 'mine.py').exists()
    assert (tmp_path / 'terminator' / 'config').exists()     # copied, not moved
    # Never overwrite Promptline's own config
    (tmp_path / 'promptline' / 'config').write_text('mine')
    util.import_terminator_config()
    assert (tmp_path / 'promptline' / 'config').read_text() == 'mine'


def test_full_permission_needs_guardrails(config, monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    saved = config['promptline_agent_mode']
    try:
        config['promptline_agent_mode'] = 'ask'
        page = prefs.PromptlinePage(config)
        page.mode_combo.set_active_id('full')
        assert page.mode_combo.get_active_id() == 'ask'
        assert config['promptline_agent_mode'] == 'ask'
        assert page.mode_note.get_text().startswith(
            'Full permission is locked')

        (tmp_path / 'promptline').mkdir(exist_ok=True)
        (tmp_path / 'promptline' / 'guardrails.md').write_text(
            '<!-- hint -->\n- never touch prod\n- no scans outside the lab\n'
            '- ask before deleting anything\n')
        page.mode_combo.set_active_id('full')
        assert config['promptline_agent_mode'] == 'full'
        assert page.mode_note.get_text().startswith('DANGEROUS')
    finally:
        config['promptline_agent_mode'] = saved


def test_a_config_that_leaves_out_empty_settings_is_valid(tmp_path):
    """Keys whose default is empty used to be reported as invalid when the
    file didn't mention them"""
    from configobj import ConfigObj
    from validate import Validator
    from promptlinelib.config import Config
    parser = ConfigObj(['[global_config]', '  promptline_llm_autocomplete = True'],
                       configspec=Config().base.defaults_to_configspec())
    assert parser.validate(Validator(), preserve_errors=True) is True


def test_step_limit_and_goal_wait_are_settings(config):
    page = prefs.PromptlinePage(config)
    assert config['promptline_agent_max_steps'] == 25
    page.widgets['agent_max_steps'].set_value(60)
    assert config['promptline_agent_max_steps'] == 60
    page.widgets['goal_approval_wait'].set_value(0)
    assert config['promptline_goal_approval_wait'] == 0
