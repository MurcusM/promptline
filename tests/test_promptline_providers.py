#!/usr/bin/env python
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""Tests for Promptline's providers: choosing one, and talking to each API
format against a local fake server (never a real provider)"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from promptlinelib.promptline import providers
from promptlinelib.promptline.providers import (
    ProviderError, Unsupported, make_provider, missing_key_hint,
    provider_settings)
from promptlinelib.promptline.providers.anthropic import AnthropicProvider
from promptlinelib.promptline.providers.openai import OpenAIProvider


class FakeServer(object):
    """Answers every POST with reply(path, body), recording what it saw"""
    def __init__(self):
        self.requests = []
        self.status = 200
        self.stream = False
        self.reply = lambda path, body: {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers['Content-Length'])))
                server.requests.append((self.path, dict(
                    (k.lower(), v) for k, v in self.headers.items()), body))
                reply = server.reply(self.path, body)
                if server.status != 200:
                    data = json.dumps({'error': {'message': 'nope'}}).encode()
                    kind = 'application/json'
                elif server.stream:
                    data = ''.join('data: %s\n\n' % json.dumps(event)
                                   for event in reply).encode()
                    kind = 'text/event-stream'
                else:
                    data = json.dumps(reply).encode()
                    kind = 'application/json'
                self.send_response(server.status)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server():
    server = FakeServer()
    yield server
    server.httpd.shutdown()


TOOLS = [{'type': 'function', 'function': {
    'name': 'run_command', 'description': 'Run it',
    'parameters': {'type': 'object',
                   'properties': {'command': {'type': 'string'}}}}}]


class FakeConfig(dict):
    """The promptline_* keys of a Config"""
    def __init__(self, **values):
        dict.__init__(self, {
            'promptline_provider': 'openai', 'promptline_base_url': '',
            'promptline_api_key_env': '', 'promptline_api_key_file': '',
            'promptline_autocomplete_model': 'fast',
            'promptline_autocomplete_reasoning': '',
            'promptline_agent_model': 'smart',
            'promptline_agent_reasoning': ''})
        for purpose in ('autocomplete', 'agent'):
            for name in providers.SETTINGS:
                self['promptline_%s_%s' % (purpose, name)] = ''
        self.update(values)


def settings_for(purpose, **values):
    return provider_settings(purpose, FakeConfig(**values))


def test_a_purpose_can_use_its_own_provider():
    config = FakeConfig(promptline_provider='opencode',
                        promptline_base_url='https://gateway.example/v1',
                        promptline_autocomplete_provider='ollama')
    agent = provider_settings('agent', config)
    assert agent['promptline_provider'] == 'opencode'
    assert agent['promptline_base_url'] == 'https://gateway.example/v1'
    assert agent['model'] == 'smart'
    # Nothing is borrowed from the default provider's address
    fast = provider_settings('autocomplete', config)
    assert fast['promptline_provider'] == 'ollama'
    assert fast['promptline_base_url'] == ''
    assert providers.connection(fast)[1:] == ('http://localhost:11434/v1', '')


@pytest.mark.parametrize('name, url, key_env', [
    ('deepseek', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY'),
    ('qwen', 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
     'DASHSCOPE_API_KEY'),
    ('muse', 'https://api.meta.ai/v1', 'MODEL_API_KEY'),
])
def test_direct_providers(monkeypatch, name, url, key_env):
    monkeypatch.setenv(key_env, 'k')
    provider = make_provider('agent', settings_for(
        'agent', promptline_provider=name))
    assert type(provider) is OpenAIProvider
    assert (provider.base_url, provider.api_key) == (url, 'k')
    # A region or workspace address replaces the preset's
    other = make_provider('agent', settings_for(
        'agent', promptline_provider=name, promptline_base_url='http://h/v1'))
    assert other.base_url == 'http://h/v1'


def test_a_purpose_naming_the_default_provider_shares_its_key_file(
        monkeypatch, tmp_path):
    """The same provider chosen for @agent and prediction must not lose the
    default's key file (it did: each purpose's own settings were empty)"""
    monkeypatch.delenv('OPENCODE_API_KEY', raising=False)
    key = tmp_path / 'api-key'
    key.write_text('OPENCODE_API_KEY=zen-key\n')
    config = FakeConfig(promptline_provider='opencode',
                        promptline_api_key_file=str(key),
                        promptline_agent_provider='opencode',
                        promptline_autocomplete_provider='opencode',
                        promptline_agent_model='claude-sonnet-5-5')
    for purpose in ('agent', 'autocomplete'):
        settings = provider_settings(purpose, config)
        assert settings['promptline_api_key_file'] == str(key)
        assert providers.resolve_api_key(settings) == 'zen-key'
    assert make_provider('agent', provider_settings('agent', config)).api_key \
        == 'zen-key'
    # What the purpose sets itself wins
    config['promptline_agent_base_url'] = 'https://gateway.example/v1'
    assert provider_settings('agent', config)['promptline_base_url'] == \
        'https://gateway.example/v1'
    # A different provider never borrows the default's key file
    config['promptline_autocomplete_provider'] = 'deepseek'
    other = provider_settings('autocomplete', config)
    assert other['promptline_api_key_file'] == ''


def test_openai_stays_the_default():
    assert providers.connection(settings_for('agent')) == (
        'openai', 'https://api.openai.com/v1', 'OPENAI_API_KEY')


@pytest.mark.parametrize('model, kind, api, name', [
    ('claude-sonnet-5-5', AnthropicProvider, None, 'claude-sonnet-5-5'),
    ('gpt-6-luna', OpenAIProvider, 'responses', 'gpt-6-luna'),
    ('grok-4.7', OpenAIProvider, 'responses', 'grok-4.7'),
    ('kimi-k3', OpenAIProvider, 'chat', 'kimi-k3'),
    ('messages:qwen3.6-plus', AnthropicProvider, None, 'qwen3.6-plus'),
    ('chat:claude-haiku-4-5', OpenAIProvider, 'chat', 'claude-haiku-4-5'),
])
def test_opencode_serves_each_model_in_its_own_format(
        monkeypatch, model, kind, api, name):
    monkeypatch.setenv('OPENCODE_API_KEY', 'zen-key')
    provider = make_provider('agent', settings_for(
        'agent', promptline_provider='opencode', promptline_agent_model=model))
    assert type(provider) is kind
    assert provider.model == name
    assert provider.base_url == 'https://opencode.ai/zen/v1'
    assert provider.api_key == 'zen-key'
    if kind is OpenAIProvider:
        assert provider.api == api


def test_opencode_gemini_explains_itself(monkeypatch):
    monkeypatch.setenv('OPENCODE_API_KEY', 'zen-key')
    provider = make_provider('agent', settings_for(
        'agent', promptline_provider='opencode',
        promptline_agent_model='gemini-3.8-flash'))
    assert isinstance(provider, Unsupported)
    with pytest.raises(ProviderError) as caught:
        provider.chat([])
    assert caught.value.auth and 'Gemini' in str(caught.value)


def test_keys_and_unknown_providers(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    settings = settings_for('agent', promptline_provider='anthropic')
    assert make_provider('agent', settings) is None
    assert 'ANTHROPIC_API_KEY' in missing_key_hint(settings)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')
    assert type(make_provider('agent', settings)) is AnthropicProvider
    # A server on this machine needs no key
    local = make_provider('agent', settings_for(
        'agent', promptline_provider='ollama'))
    assert local.base_url == 'http://localhost:11434/v1'
    assert make_provider('agent', settings_for(
        'agent', promptline_provider='nonsense')) is None
    assert make_provider('agent', settings_for(
        'agent', promptline_provider='custom')) is None   # no address


def test_existing_openai_compatible_settings_still_work(monkeypatch):
    monkeypatch.setenv('PL_KEY', 'k')
    provider = make_provider('agent', settings_for(
        'agent', promptline_base_url='http://10.0.0.5:8000/v1',
        promptline_api_key_env='PL_KEY'))
    assert type(provider) is OpenAIProvider
    assert provider.base_url == 'http://10.0.0.5:8000/v1'
    assert provider.api_key == 'k'


# The wire formats

def message_reply(*blocks):
    return {'type': 'message', 'role': 'assistant', 'content': list(blocks)}


def test_anthropic_chat_with_tools(server):
    server.reply = lambda path, body: message_reply(
        {'type': 'text', 'text': 'Checking.'},
        {'type': 'tool_use', 'id': 'c1', 'name': 'run_command',
         'input': {'command': 'uname -r'}})
    provider = AnthropicProvider(server.url + '/v1', 'secret', 'claude-x')
    reply = provider.chat([{'role': 'system', 'content': 'Be brief.'},
                           {'role': 'user', 'content': 'kernel?'}], TOOLS)
    path, headers, body = server.requests[0]
    assert path == '/v1/messages'
    assert headers['x-api-key'] == 'secret'
    assert headers['anthropic-version']
    assert body['system'] == 'Be brief.'
    assert body['messages'] == [{'role': 'user', 'content': 'kernel?'}]
    assert body['tools'][0]['input_schema']['type'] == 'object'
    assert reply['content'] == 'Checking.'
    call = reply['tool_calls'][0]
    assert call['id'] == 'c1'
    assert json.loads(call['function']['arguments']) == {
        'command': 'uname -r'}


def test_anthropic_gateways_get_a_bearer_token_too(server):
    server.reply = lambda path, body: message_reply(
        {'type': 'text', 'text': 'ls'})
    provider = AnthropicProvider(server.url, 'zen-key', 'claude-x')
    assert provider.complete([{'role': 'user', 'content': 'x'}]) == 'ls'
    assert server.requests[0][1]['authorization'] == 'Bearer zen-key'


def test_anthropic_streams(server):
    server.stream = True
    server.reply = lambda path, body: [
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'text_delta', 'text': 'git '}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'text_delta', 'text': 'status'}},
        {'type': 'message_stop'}]
    pieces = []
    provider = AnthropicProvider(server.url, 'k', 'claude-x')
    reply = provider.complete([{'role': 'user', 'content': 'x'}],
                              on_text=pieces.append)
    assert server.requests[0][2]['stream'] is True
    assert pieces == ['git ', 'status'] and reply == 'git status'


@pytest.mark.parametrize('status, auth', [(401, True), (429, False)])
def test_anthropic_errors(server, status, auth):
    server.status = status
    provider = AnthropicProvider(server.url, 'k', 'claude-x')
    with pytest.raises(ProviderError) as caught:
        provider.complete([{'role': 'user', 'content': 'x'}])
    assert caught.value.auth is auth and 'nope' in str(caught.value)


def test_openai_responses_only_servers_get_every_request_there(server):
    server.reply = lambda path, body: {'output': [
        {'type': 'message', 'role': 'assistant',
         'content': [{'type': 'output_text', 'text': 'ls -la'}]}]}
    provider = OpenAIProvider(server.url, 'k', 'gpt-x', api='responses')
    assert provider.complete([{'role': 'user', 'content': 'x'}]) == 'ls -la'
    path, _headers, body = server.requests[0]
    assert path == '/responses' and 'tools' not in body
    chat = OpenAIProvider(server.url, 'k', 'kimi-x', api='chat')
    server.reply = lambda path, body: {'choices': [
        {'message': {'role': 'assistant', 'content': 'pwd'}}]}
    assert chat.chat([{'role': 'user', 'content': 'x'}], TOOLS)[
        'content'] == 'pwd'
    assert server.requests[1][0] == '/chat/completions'


def test_key_problem_says_why(tmp_path):
    base = settings_for('agent', promptline_provider='opencode')
    assert 'OPENCODE_API_KEY is not set' in providers.key_problem(base, {})
    assert 'is set but empty' in providers.key_problem(
        base, {'OPENCODE_API_KEY': ''})
    missing = dict(base, promptline_api_key_file=str(tmp_path / 'nope'))
    assert "can't be read" in providers.key_problem(missing, {})
    (tmp_path / 'empty').write_text('\n')
    empty = dict(base, promptline_api_key_file=str(tmp_path / 'empty'))
    assert 'is empty' in providers.key_problem(empty, {})
