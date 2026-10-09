# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""anthropic.py - Anthropic's Messages API

Also what OpenCode Zen serves Claude models through. Uses only the standard
library. Callers use the same Chat Completions-shaped messages and tool
definitions as with every provider; they are translated both ways here.

The reasoning setting is not used: replies come without extended thinking.

>>> system, turns = to_messages([
...     {'role': 'system', 'content': 'Be brief.'},
...     {'role': 'user', 'content': 'hi'},
...     {'role': 'assistant', 'content': 'Looking.',
...      'tool_calls': [{'id': 'c1', 'type': 'function',
...                      'function': {'name': 'run_command',
...                                   'arguments': '{"command": "ls"}'}}]},
...     {'role': 'tool', 'tool_call_id': 'c1', 'content': 'a b'},
...     {'role': 'tool', 'tool_call_id': 'c2', 'content': 'c'}])
>>> system
'Be brief.'
>>> [turn['role'] for turn in turns]
['user', 'assistant', 'user']
>>> turns[1]['content'][1]
{'type': 'tool_use', 'id': 'c1', 'name': 'run_command', 'input': {'command': 'ls'}}
>>> [block['tool_use_id'] for block in turns[2]['content']]
['c1', 'c2']
>>> to_tools([{'type': 'function', 'function': {
...     'name': 'run_command', 'description': 'Run it',
...     'parameters': {'type': 'object'}}}])
[{'name': 'run_command', 'description': 'Run it', 'input_schema': {'type': 'object'}}]
>>> from_content([{'type': 'text', 'text': 'Checking.'},
...               {'type': 'tool_use', 'id': 'c3', 'name': 'run_command',
...                'input': {'command': 'uname -r'}}])
{'role': 'assistant', 'content': 'Checking.', 'tool_calls': [{'id': 'c3', 'type': 'function', 'function': {'name': 'run_command', 'arguments': '{"command": "uname -r"}'}}]}

Streaming: events are reassembled into the usual reply, and text is handed
to on_text as it arrives.

>>> def sse(*items):
...     for item in items:
...         yield 'data: ' + json.dumps(item)
...         yield ''
>>> pieces = []
>>> reply = collect_stream(iter_sse(sse(
...     {'type': 'content_block_start', 'index': 0,
...      'content_block': {'type': 'text', 'text': ''}},
...     {'type': 'content_block_delta', 'index': 0,
...      'delta': {'type': 'text_delta', 'text': 'Look'}},
...     {'type': 'content_block_delta', 'index': 0,
...      'delta': {'type': 'text_delta', 'text': 'ing'}},
...     {'type': 'content_block_start', 'index': 1,
...      'content_block': {'type': 'tool_use', 'id': 'c4',
...                        'name': 'run_command', 'input': {}}},
...     {'type': 'content_block_delta', 'index': 1,
...      'delta': {'type': 'input_json_delta', 'partial_json': '{"command'}},
...     {'type': 'content_block_delta', 'index': 1,
...      'delta': {'type': 'input_json_delta', 'partial_json': '": "ls"}'}},
...     {'type': 'message_stop'})), pieces.append)
>>> pieces, reply[0], reply[1]['input']
(['Look', 'ing'], {'type': 'text', 'text': 'Looking'}, {'command': 'ls'})
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import USER_AGENT, ProviderError
from .openai import error_message, iter_sse

API_VERSION = '2023-06-01'


class AnthropicProvider(object):
    """Talks to {base_url}/messages"""
    def __init__(self, base_url, api_key, model, reasoning_effort=''):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model

    def complete(self, messages, max_tokens=256, timeout=15, on_text=None):
        """Return the reply text for messages. With on_text, the reply is
        streamed to it as it arrives."""
        return self.chat(messages, max_tokens=max_tokens, timeout=timeout,
                         on_text=on_text)['content'] or ''

    def chat(self, messages, tools=None, max_tokens=4096, timeout=60,
             on_text=None):
        """Return the assistant message for messages, as a dict with
        'content' and, if the model called tools, 'tool_calls' (the
        OpenAI shape, which is also what the agent keeps its history in)"""
        system, turns = to_messages(messages)
        body = {'model': self.model, 'max_tokens': max_tokens,
                'messages': turns}
        if system:
            body['system'] = system
        if tools:
            body['tools'] = to_tools(tools)
        if on_text:
            body['stream'] = True
        content = self._post(body, timeout, on_text)
        return from_content(content)

    def headers(self):
        headers = {'Content-Type': 'application/json',
                   'User-Agent': USER_AGENT,
                   'anthropic-version': API_VERSION}
        if self.api_key:
            headers['x-api-key'] = self.api_key
            # Gateways such as OpenCode Zen authenticate with a bearer token
            if urllib.parse.urlparse(self.base_url).hostname != \
                    'api.anthropic.com':
                headers['Authorization'] = 'Bearer ' + self.api_key
        return headers

    def _post(self, body, timeout, on_text=None):
        request = urllib.request.Request(self.base_url + '/messages',
                                         data=json.dumps(body).encode(),
                                         headers=self.headers(),
                                         method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 'text/event-stream' in \
                        response.headers.get('Content-Type', ''):
                    return collect_stream(
                        iter_sse(line.decode('utf-8', 'replace')
                                 for line in response), on_text)
                reply = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            raise ProviderError('%s: %s' % (ex.code, error_message(ex)),
                                auth=ex.code in (401, 403, 404))
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise ProviderError(str(getattr(ex, 'reason', ex)))
        if not isinstance(reply.get('content'), list):
            raise ProviderError('unexpected reply from %s' % self.base_url)
        return reply['content']


def to_messages(messages):
    """(system prompt, turns) for the Messages API from Chat
    Completions-shaped messages. Tool results become blocks of the user turn
    that follows the assistant's tool calls."""
    system, turns = [], []
    for message in messages:
        role = message.get('role')
        if role == 'system':
            system.append(message.get('content') or '')
        elif role == 'tool':
            block = {'type': 'tool_result',
                     'tool_use_id': message.get('tool_call_id'),
                     'content': message.get('content') or ''}
            last = turns[-1] if turns else None
            if last and last['role'] == 'user' and \
                    isinstance(last['content'], list):
                last['content'].append(block)
            else:
                turns.append({'role': 'user', 'content': [block]})
        elif role == 'assistant':
            blocks = []
            if message.get('content'):
                blocks.append({'type': 'text', 'text': message['content']})
            for call in message.get('tool_calls') or []:
                try:
                    arguments = json.loads(call['function']['arguments']
                                           or '{}')
                except ValueError:
                    arguments = {}
                blocks.append({'type': 'tool_use', 'id': call['id'],
                               'name': call['function']['name'],
                               'input': arguments})
            if blocks:
                turns.append({'role': 'assistant', 'content': blocks})
        elif message.get('content'):
            turns.append({'role': 'user', 'content': message['content']})
    return '\n\n'.join(part for part in system if part), turns


def to_tools(tools):
    """Chat Completions tool definitions as Messages API tools"""
    return [{'name': tool['function']['name'],
             'description': tool['function'].get('description', ''),
             'input_schema': tool['function'].get(
                 'parameters', {'type': 'object', 'properties': {}})}
            for tool in tools]


def from_content(content):
    """Messages API content blocks as one Chat Completions-shaped message"""
    texts, calls = [], []
    for block in content:
        if block.get('type') == 'text':
            texts.append(block.get('text', ''))
        elif block.get('type') == 'tool_use':
            calls.append({'id': block.get('id'), 'type': 'function',
                          'function': {'name': block.get('name'),
                                       'arguments': json.dumps(
                                           block.get('input') or {})}})
    message = {'role': 'assistant', 'content': ''.join(texts) or None}
    if calls:
        message['tool_calls'] = calls
    return message


def collect_stream(events, on_text):
    """The content blocks of a reply, rebuilt from its streamed events"""
    blocks, json_parts = {}, {}
    for _event, data in events:
        try:
            item = json.loads(data)
        except ValueError:
            continue
        kind = item.get('type')
        index = item.get('index', 0)
        if kind == 'content_block_start':
            blocks[index] = dict(item.get('content_block') or {})
            json_parts[index] = []
        elif kind == 'content_block_delta':
            delta = item.get('delta') or {}
            block = blocks.setdefault(index, {'type': 'text', 'text': ''})
            if delta.get('type') == 'text_delta':
                block['text'] = block.get('text', '') + delta.get('text', '')
                if on_text:
                    on_text(delta.get('text', ''))
            elif delta.get('type') == 'input_json_delta':
                json_parts.setdefault(index, []).append(
                    delta.get('partial_json', ''))
        elif kind == 'message_stop':
            break
        elif kind == 'error':
            error = item.get('error') or item
            raise ProviderError(str(error.get('message') or error))
    else:
        raise ProviderError('the reply stream ended early')
    for index, block in blocks.items():
        if block.get('type') == 'tool_use':
            try:
                block['input'] = json.loads(''.join(json_parts.get(index, []))
                                            or '{}')
            except ValueError:
                block['input'] = {}
    return [blocks[index] for index in sorted(blocks)]
