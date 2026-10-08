# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""openai.py - OpenAI, and servers compatible with its Chat Completions API

Uses only the standard library. The same wire format is served by Ollama,
LM Studio, vLLM and others, so pointing promptline_base_url at one of those
works without a key.

Callers use Chat Completions-shaped messages. Requests with tools to OpenAI
go to its Responses API instead, since OpenAI's reasoning models only accept
tools there; the messages are translated both ways, and the model's
(encrypted) reasoning is carried along in a '_reasoning' key on assistant
messages so it survives between tool calls.

>>> to_responses_input([
...     {'role': 'user', 'content': 'hi'},
...     {'role': 'assistant', 'content': None, '_reasoning': [{'type': 'reasoning', 'id': 'r1'}],
...      'tool_calls': [{'id': 'c1', 'type': 'function',
...                      'function': {'name': 'run_command', 'arguments': '{}'}}]},
...     {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}])
[{'role': 'user', 'content': 'hi'}, {'type': 'reasoning', 'id': 'r1'}, {'type': 'function_call', 'call_id': 'c1', 'name': 'run_command', 'arguments': '{}'}, {'type': 'function_call_output', 'call_id': 'c1', 'output': 'ok'}]
>>> reply = from_responses_output([
...     {'type': 'reasoning', 'id': 'r2', 'summary': []},
...     {'type': 'message', 'role': 'assistant',
...      'content': [{'type': 'output_text', 'text': 'Checking.'}]},
...     {'type': 'function_call', 'call_id': 'c2', 'name': 'run_command',
...      'arguments': '{"command": "uname -r"}'}])
>>> reply['content'], reply['tool_calls'][0]['function']['name'], len(reply['_reasoning'])
('Checking.', 'run_command', 1)

Streaming: server-sent events are reassembled into the usual reply, and
text is handed to on_text as it arrives.

>>> def sse(*items):
...     for item in items:
...         yield 'data: ' + (item if isinstance(item, str) else json.dumps(item))
...         yield ''
>>> pieces = []
>>> collect_responses_stream(iter_sse(sse(
...     {'type': 'response.output_text.delta', 'delta': 'Hel'},
...     {'type': 'response.output_text.delta', 'delta': 'lo'},
...     {'type': 'response.completed', 'response': {'output': []}})),
...     pieces.append)
{'output': []}
>>> pieces
['Hel', 'lo']
>>> list(iter_sse([': keep-alive', '', 'event: ping', 'data: 1', '']))
[('ping', '1')]
>>> def call(**function):
...     return {'choices': [{'delta': {'tool_calls': [
...         dict(index=0, function=function, **({'id': 'c1'} if 'name' in function else {}))]}}]}
>>> message = collect_chat_stream(iter_sse(sse(
...     {'choices': [{'delta': {'content': 'Look'}}]},
...     call(name='run_command', arguments='{"comm'),
...     call(arguments='and": "ls"}'),
...     '[DONE]')), None)['choices'][0]['message']
>>> message['content'], message['tool_calls'][0]['id'], message['tool_calls'][0]['function']
('Look', 'c1', {'name': 'run_command', 'arguments': '{"command": "ls"}'})
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from . import ProviderError, needs_key

# Reasoning tokens count against max_completion_tokens, so each effort level
# needs room to think on top of the visible reply, and time to do it in
REASONING_BUDGET = {
    'minimal': (512, 30),
    'low': (2048, 30),
    'medium': (4096, 60),
    'high': (8192, 90),
    'xhigh': (16384, 120),
}


class OpenAIProvider(object):
    """Talks to {base_url}/chat/completions, or {base_url}/responses.

    api is 'chat' or 'responses' for a server that only serves one of them
    (OpenCode Zen serves GPT models on Responses only). Left as None, chat
    is used, except that OpenAI itself gets tool requests on Responses."""
    needs_key = staticmethod(needs_key)

    def __init__(self, base_url, api_key, model, reasoning_effort='',
                 api=None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api = api

    def uses_responses_api(self, tools):
        if self.api:
            return self.api == 'responses'
        return bool(tools) and \
            urllib.parse.urlparse(self.base_url).hostname == 'api.openai.com'

    def budget(self, max_tokens, timeout):
        """(token limit, timeout) allowing for the reasoning effort"""
        extra_tokens, min_timeout = REASONING_BUDGET.get(
            self.reasoning_effort, (0, 0))
        return max_tokens + extra_tokens, max(timeout, min_timeout)

    def complete(self, messages, max_tokens=256, timeout=15, on_text=None):
        """Return the reply text for messages. max_tokens is the size of the
        visible reply; room for reasoning is added on top. With on_text, the
        reply is streamed to it as it arrives."""
        return self.chat(messages, max_tokens=max_tokens, timeout=timeout,
                         on_text=on_text)['content'] or ''

    def chat(self, messages, tools=None, max_tokens=4096, timeout=60,
             on_text=None):
        """Return the assistant message for messages, as a dict with
        'content' and, if the model called tools, 'tool_calls' (the
        OpenAI shape, which is also what the agent keeps its history in).
        With on_text, the reply is streamed and on_text(piece) is called as
        text arrives (servers that don't stream simply reply at once)."""
        max_tokens, timeout = self.budget(max_tokens, timeout)
        if self.uses_responses_api(tools):
            return self._responses(messages, tools, max_tokens, timeout,
                                   on_text)
        body = {'model': self.model,
                'messages': [dict((k, v) for k, v in m.items()
                                  if not k.startswith('_'))
                             for m in messages],
                'max_completion_tokens': max_tokens}
        if self.reasoning_effort:
            body['reasoning_effort'] = self.reasoning_effort
        if tools:
            body['tools'] = tools
        if on_text:
            body['stream'] = True
        reply = self._post('/chat/completions', body, timeout, on_text)
        try:
            message = reply['choices'][0]['message']
        except (KeyError, IndexError, TypeError):
            raise ProviderError('unexpected reply from %s' % self.base_url)
        result = {'role': 'assistant', 'content': message.get('content')}
        if message.get('tool_calls'):
            result['tool_calls'] = message['tool_calls']
        return result

    def _responses(self, messages, tools, max_tokens, timeout, on_text=None):
        body = {'model': self.model,
                'input': to_responses_input(messages),
                'max_output_tokens': max_tokens,
                # Nothing is kept on OpenAI's side; the reasoning comes back
                # encrypted so it can be passed along with the next request
                'store': False,
                'include': ['reasoning.encrypted_content']}
        if tools:
            body['tools'] = [dict(tool['function'], type='function')
                             for tool in tools]
        if self.reasoning_effort:
            body['reasoning'] = {'effort': self.reasoning_effort}
        if on_text:
            body['stream'] = True
        reply = self._post('/responses', body, timeout, on_text)
        if not isinstance(reply.get('output'), list):
            raise ProviderError('unexpected reply from %s' % self.base_url)
        return from_responses_output(reply['output'])

    def _post(self, path, body, timeout, on_text=None):
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        request = urllib.request.Request(self.base_url + path,
                                         data=json.dumps(body).encode(),
                                         headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 'text/event-stream' in \
                        response.headers.get('Content-Type', ''):
                    events = iter_sse(line.decode('utf-8', 'replace')
                                      for line in response)
                    if path == '/responses':
                        return collect_responses_stream(events, on_text)
                    return collect_chat_stream(events, on_text)
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as ex:
            raise ProviderError('%s: %s' % (ex.code, error_message(ex)),
                                auth=ex.code in (401, 403, 404))
        except (urllib.error.URLError, OSError, ValueError) as ex:
            raise ProviderError(str(getattr(ex, 'reason', ex)))


def error_message(http_error):
    """The API's own explanation, if it sent one"""
    try:
        detail = json.loads(http_error.read().decode('utf-8'))
        return detail['error']['message']
    except (ValueError, KeyError, TypeError, OSError):
        return http_error.reason


def to_responses_input(messages):
    """Chat Completions-shaped messages to Responses API input items"""
    items = []
    for message in messages:
        role = message.get('role')
        if role == 'tool':
            items.append({'type': 'function_call_output',
                          'call_id': message.get('tool_call_id'),
                          'output': message.get('content') or ''})
            continue
        items.extend(message.get('_reasoning') or [])
        if message.get('content'):
            items.append({'role': role, 'content': message['content']})
        for call in message.get('tool_calls') or []:
            items.append({'type': 'function_call', 'call_id': call['id'],
                          'name': call['function']['name'],
                          'arguments': call['function']['arguments']})
    return items


def from_responses_output(output):
    """Responses API output items to one Chat Completions-shaped message"""
    texts, calls, reasoning = [], [], []
    for item in output:
        kind = item.get('type')
        if kind == 'message':
            texts.extend(part.get('text', '') for part in item.get('content', [])
                         if part.get('type') == 'output_text')
        elif kind == 'function_call':
            calls.append({'id': item.get('call_id'), 'type': 'function',
                          'function': {'name': item.get('name'),
                                       'arguments': item.get('arguments')}})
        elif kind == 'reasoning':
            reasoning.append(item)
    message = {'role': 'assistant', 'content': '\n'.join(texts) or None}
    if calls:
        message['tool_calls'] = calls
    if reasoning:
        message['_reasoning'] = reasoning
    return message


def iter_sse(lines):
    """(event, data) pairs from server-sent event lines"""
    event, data = None, []
    for line in lines:
        line = line.rstrip('\r\n')
        if not line:
            if data:
                yield event, '\n'.join(data)
            event, data = None, []
        elif line.startswith(':'):
            continue
        elif line.startswith('event:'):
            event = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].lstrip())
    if data:
        yield event, '\n'.join(data)


def collect_responses_stream(events, on_text):
    """The final response object from a Responses API event stream"""
    for event, data in events:
        try:
            item = json.loads(data)
        except ValueError:
            continue
        kind = item.get('type') or event
        if kind == 'response.output_text.delta' and on_text:
            on_text(item.get('delta', ''))
        elif kind in ('response.completed', 'response.incomplete'):
            return item.get('response', {})
        elif kind in ('response.failed', 'error'):
            error = (item.get('response') or {}).get('error') or item
            raise ProviderError(str(error.get('message') or error))
    raise ProviderError('the reply stream ended early')


def collect_chat_stream(events, on_text):
    """A Chat Completions reply rebuilt from its streamed chunks"""
    content, calls = [], {}
    for _event, data in events:
        if data.strip() == '[DONE]':
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if chunk.get('error'):
            raise ProviderError(str(chunk['error'].get('message')
                                    or chunk['error']))
        for choice in chunk.get('choices') or []:
            delta = choice.get('delta') or {}
            if delta.get('content'):
                content.append(delta['content'])
                if on_text:
                    on_text(delta['content'])
            for part in delta.get('tool_calls') or []:
                call = calls.setdefault(part.get('index', 0), {
                    'id': None, 'type': 'function',
                    'function': {'name': '', 'arguments': ''}})
                call['id'] = call['id'] or part.get('id')
                function = part.get('function') or {}
                if function.get('name') and not call['function']['name']:
                    call['function']['name'] = function['name']
                call['function']['arguments'] += function.get('arguments') or ''
    message = {'role': 'assistant', 'content': ''.join(content) or None}
    if calls:
        message['tool_calls'] = [calls[index] for index in sorted(calls)]
    return {'choices': [{'message': message}]}
