# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""providers - language model backends

Messages use the common role/content dict shape ({'role': 'system' |
'user' | 'assistant', 'content': str}). A provider turns them into a reply.

Each provider is a preset: a name, the kind of API it speaks, and the
defaults for its base URL and API key variable. Three kinds exist:

  openai     OpenAI's API, and anything compatible with its Chat Completions
             API (OpenRouter, Gemini's compatibility endpoint, Ollama,
             LM Studio, vLLM...)
  anthropic  Anthropic's Messages API
  opencode   OpenCode Zen, which serves each model through the API format
             that suits it. The model name picks the format.

promptline_provider names the preset. promptline_base_url and
promptline_api_key_env override the preset's own values when they are set,
which is how any other OpenAI-compatible server is used. Prediction and
@agent can each use a different provider (promptline_autocomplete_provider
and promptline_agent_provider).

API keys are never stored in Terminator's config. They come from the
environment variable named by promptline_api_key_env, or from a file named
by promptline_api_key_file (useful when Terminator is started from a
desktop launcher that doesn't see your shell's environment).

>>> resolve_api_key({'promptline_provider': 'anthropic',
...                  'promptline_base_url': '', 'promptline_api_key_env': '',
...                  'promptline_api_key_file': ''},
...                 {'ANTHROPIC_API_KEY': ' k0 '})
'k0'
>>> custom = {'promptline_provider': 'custom', 'promptline_base_url': '',
...           'promptline_api_key_env': 'PL_TEST_KEY',
...           'promptline_api_key_file': ''}
>>> resolve_api_key(custom, {'PL_TEST_KEY': ' k1 '})
'k1'
>>> resolve_api_key(custom, {}) is None
True
>>> connection({'promptline_provider': 'opencode', 'promptline_base_url': '',
...             'promptline_api_key_env': ''})
('opencode', 'https://opencode.ai/zen/v1', 'OPENCODE_API_KEY')
>>> connection({'promptline_provider': 'ollama', 'promptline_base_url': '',
...             'promptline_api_key_env': ''})
('ollama', 'http://localhost:11434/v1', '')

OpenCode picks the API format from the model name:

>>> [opencode_api(model) for model in ('claude-sonnet-5-5', 'gpt-6-luna',
...      'grok-4.7', 'kimi-k3', 'gemini-3.8-flash', 'messages:qwen3.6-plus')]
['messages', 'responses', 'responses', 'chat', None, 'messages']
>>> opencode_api('chat:claude-haiku-4-5'), opencode_model('chat:claude-haiku-4-5')
('chat', 'claude-haiku-4-5')
>>> opencode_model('kimi-k3')
'kimi-k3'
"""

import collections
import os
import urllib.parse

from ...config import Config
from ...translation import _
from ...util import err
from ...version import APP_VERSION

# Sent with every request. Python's own default is blocked outright by the
# Cloudflare firewall in front of some APIs (OpenCode Zen answers it with a
# 403, error code 1010, before the request reaches the API)
USER_AGENT = 'promptline/%s' % APP_VERSION


class ProviderError(Exception):
    """A request failed. auth is True when retrying won't help until the
    user fixes their key or settings. status is the HTTP status, if any."""
    def __init__(self, message, auth=False, status=None):
        Exception.__init__(self, message)
        self.auth = auth
        self.status = status


# Reasoning levels, lowest first. 'max' asks for the most thinking a model
# gives; 'ultra' is 'max' plus a team of subagents (see agent/flows.py), so
# to a provider the two are the same.
REASONING_LEVELS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh',
                    'max', 'ultra')

# Reasoning tokens count against the output limit, so each level needs room
# to think on top of the visible reply, and time to do it in:
# (extra tokens, minimum timeout in seconds)
REASONING_BUDGET = {
    'minimal': (512, 30),
    'low': (2048, 30),
    'medium': (4096, 60),
    'high': (8192, 90),
    'xhigh': (16384, 120),
    'max': (32768, 240),
    'ultra': (32768, 240),
}

# Context windows (tokens) of models known to be smaller than the default
SMALL_WINDOWS = (('claude-haiku-4-5', 200000),)


def context_window(setting, model=''):
    """The context window to plan for: the setting, unless the model is
    known to have less

    >>> context_window(1000000, 'claude-sonnet-5-5')
    1000000
    >>> context_window(1000000, 'claude-haiku-4-5-20251001')
    200000
    >>> context_window(128000, 'claude-haiku-4-5')
    128000
    """
    for prefix, window in SMALL_WINDOWS:
        if (model or '').startswith(prefix):
            return min(setting, window)
    return setting


Preset = collections.namedtuple(
    'Preset', 'name label kind base_url key_env autocomplete_model '
              'agent_model')

# The models are only suggestions, offered when the provider is chosen in
# Preferences. Where they are empty, the model names are the user's to know.
PRESETS = collections.OrderedDict((preset.name, preset) for preset in (
    Preset('openai', 'OpenAI', 'openai', 'https://api.openai.com/v1',
           'OPENAI_API_KEY', 'gpt-6-luna', 'gpt-6-luna'),
    Preset('anthropic', 'Anthropic', 'anthropic',
           'https://api.anthropic.com/v1', 'ANTHROPIC_API_KEY',
           'claude-haiku-4-5-20251001', 'claude-sonnet-5-5'),
    Preset('opencode', 'OpenCode Zen', 'opencode',
           'https://opencode.ai/zen/v1', 'OPENCODE_API_KEY',
           'claude-haiku-4-5', 'claude-sonnet-5-5'),
    Preset('openrouter', 'OpenRouter', 'openai',
           'https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY', '', ''),
    Preset('gemini', 'Google Gemini', 'openai',
           'https://generativelanguage.googleapis.com/v1beta/openai',
           'GEMINI_API_KEY', '', ''),
    Preset('deepseek', 'DeepSeek', 'openai', 'https://api.deepseek.com',
           'DEEPSEEK_API_KEY', 'deepseek-v4-flash', 'deepseek-v4-pro'),
    Preset('qwen', 'Qwen (Alibaba Cloud)', 'openai',
           'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
           'DASHSCOPE_API_KEY', '', ''),
    Preset('muse', 'Meta Muse', 'openai', 'https://api.meta.ai/v1',
           'MODEL_API_KEY', 'muse-spark-1.3', 'muse-spark-1.3'),
    Preset('ollama', 'Ollama (local)', 'openai', 'http://localhost:11434/v1',
           '', '', ''),
    Preset('lmstudio', 'LM Studio (local)', 'openai',
           'http://localhost:1234/v1', '', '', ''),
    Preset('custom', 'Other OpenAI-compatible server', 'openai', '', '',
           '', ''),
))


def needs_key(base_url):
    """Hosted APIs need a key; a server on this machine doesn't"""
    host = urllib.parse.urlparse(base_url).hostname or ''
    return host not in ('localhost', '127.0.0.1', '::1')


def connection(settings):
    """(provider name, base URL, key variable) after the preset's defaults
    fill what the settings leave empty"""
    name = settings['promptline_provider']
    preset = PRESETS.get(name)
    base_url = settings['promptline_base_url'] or \
        (preset.base_url if preset else '')
    key_env = settings['promptline_api_key_env'] or \
        (preset.key_env if preset else '')
    return name, base_url, key_env


def clean_key(text, key_env=''):
    """The key in the text of a key file. People write it as the shell
    would, so 'export NAME=' and quotes around the key are tolerated, when
    NAME is the variable this provider uses.

    >>> clean_key(' sk-abc\\n')
    'sk-abc'
    >>> clean_key('export OPENCODE_API_KEY="sk-abc"\\n', 'OPENCODE_API_KEY')
    'sk-abc'
    >>> clean_key('OPENCODE_API_KEY=sk-abc', 'OPENCODE_API_KEY')
    'sk-abc'
    >>> clean_key('OTHER=sk-abc', 'OPENCODE_API_KEY')
    'OTHER=sk-abc'
    >>> clean_key('  \\n') is None
    True
    """
    key = text.strip()
    if key.startswith('export '):
        key = key[len('export '):].lstrip()
    if key_env and key.startswith(key_env + '='):
        key = key[len(key_env) + 1:].strip()
        if len(key) > 1 and key[0] == key[-1] and key[0] in '"\'':
            key = key[1:-1]
    return key or None


def resolve_api_key(config, environ=None):
    """Find the API key without ever writing it anywhere"""
    environ = os.environ if environ is None else environ
    name = connection(config)[2]
    if name and environ.get(name, '').strip():
        return environ[name].strip()
    path = config['promptline_api_key_file']
    if path:
        try:
            with open(os.path.expanduser(path), encoding='utf-8') as handle:
                return clean_key(handle.read(), connection(config)[2])
        except OSError as ex:
            err('promptline: unable to read API key file %s: %s' %
                (path, ex.strerror))
    return None


def key_problem(settings, environ=None):
    """Why resolve_api_key() found nothing, as a short sentence for the
    user. An app started from a menu doesn't see variables that are only
    exported in shell startup files, which is the usual cause."""
    environ = os.environ if environ is None else environ
    name = connection(settings)[2]
    reasons = []
    if name:
        if name in environ:
            reasons.append(_('%s is set but empty') % name)
        else:
            reasons.append(_('%s is not set in the environment Promptline '
                             'was started with') % name)
    path = settings['promptline_api_key_file']
    if path:
        path = os.path.expanduser(path)
        try:
            with open(path, encoding='utf-8') as handle:
                if not handle.read().strip():
                    reasons.append(_('the key file %s is empty') % path)
        except OSError as ex:
            reasons.append(_('the key file %s can\'t be read (%s)')
                           % (path, ex.strerror))
    return '; '.join(reasons)


def missing_key_hint(settings):
    """What to tell the user when make_provider() found no key"""
    name, _base_url, key_env = connection(settings)
    preset = PRESETS.get(name)
    label = preset.label if preset else name
    if key_env:
        return ('no API key found for %s. Set %s for Promptline, or point '
                'promptline_api_key_file in ~/.config/promptline/config at '
                'a file containing the key (Preferences > Promptline).'
                % (label, key_env))
    return ('no API key found for %s. Set promptline_api_key_env or '
            'promptline_api_key_file in ~/.config/promptline/config '
            '(Preferences > Promptline).' % label)


SETTINGS = ('provider', 'base_url', 'api_key_env', 'api_key_file')


def provider_settings(purpose, config=None):
    """The provider settings for 'autocomplete' or 'agent' as a plain dict
    (no key in it), so they can be handed to the agent process. A purpose
    that names a different provider from the default takes the whole
    connection from its own settings, so that nothing is mixed with the
    default provider's URL or key. One that names the same provider, or
    none, shares the default's connection, and what it sets itself wins."""
    config = Config() if config is None else config
    own = 'promptline_%s_' % purpose
    name = config[own + 'provider']
    if name and name != config['promptline_provider']:
        settings = dict(('promptline_' + field, config[own + field])
                        for field in SETTINGS)
    else:
        settings = dict(('promptline_' + field,
                         (config[own + field] if name else '') or
                         config['promptline_' + field])
                        for field in SETTINGS)
    settings['model'] = config['promptline_%s_model' % purpose]
    settings['reasoning'] = config['promptline_%s_reasoning' % purpose]
    return settings


def opencode_api(model):
    """The API format OpenCode Zen serves model through: 'messages',
    'responses' or 'chat', or None for Google's own format, which Promptline
    doesn't speak. A leading 'messages:', 'responses:' or 'chat:' in the
    model name chooses the format for a model that this guess gets wrong."""
    forced, _sep, bare = model.partition(':')
    if bare and forced in ('messages', 'responses', 'chat'):
        return forced
    if model.startswith('claude-'):
        return 'messages'
    if model.startswith(('gpt-', 'grok-', 'muse-')):
        return 'responses'
    if model.startswith('gemini-'):
        return None
    return 'chat'


def opencode_model(model):
    """model without the format prefix that opencode_api() understands"""
    forced, _sep, bare = model.partition(':')
    return bare if bare and forced in ('messages', 'responses', 'chat') \
        else model


class Unsupported(object):
    """Stands in for a provider that can't serve the chosen model, so that
    the reason reaches the user as an ordinary provider error"""
    def __init__(self, message):
        self.message = message

    def chat(self, *_args, **_kwargs):
        raise ProviderError(self.message, auth=True)

    def complete(self, *_args, **_kwargs):
        raise ProviderError(self.message, auth=True)


def make_provider(purpose, settings=None):
    """Return a provider for 'autocomplete' or 'agent', or None if one
    isn't configured (no key for a hosted API)"""
    settings = provider_settings(purpose) if settings is None else settings
    name, base_url, _key_env = connection(settings)
    preset = PRESETS.get(name)
    if preset is None:
        err('promptline: unknown provider %r (known: %s)'
            % (name, ', '.join(PRESETS)))
        return None
    if not base_url:
        err('promptline: provider %r needs promptline_base_url' % name)
        return None
    key = resolve_api_key(settings)
    if key is None and needs_key(base_url):
        return None
    model, reasoning = settings['model'], settings['reasoning']

    if preset.kind == 'opencode':
        api = opencode_api(model)
        model = opencode_model(model)
        if api is None:
            return Unsupported(
                '%s is served in Google\'s own API format, which Promptline '
                'does not speak. Use the Google Gemini provider, or another '
                'model.' % model)
    else:
        api = 'messages' if preset.kind == 'anthropic' else None
    if api == 'messages':
        from .anthropic import AnthropicProvider
        return AnthropicProvider(base_url, key, model, reasoning)
    from .openai import OpenAIProvider
    return OpenAIProvider(base_url, key, model, reasoning,
                          api=api if preset.kind == 'opencode' else None)
