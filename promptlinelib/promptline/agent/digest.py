# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""digest.py - a conversation as short text

Used to tell the reviewer what the agent has been doing and what the user
has said and approved, and to summarise older work when a long run gets
too big for the model's context.

>>> messages = [
...     {'role': 'user', 'content': 'Shell: bash\\n\\nRequest: fix the build'},
...     {'role': 'assistant', 'content': 'Looking.', 'tool_calls': [
...         {'id': 'c1', 'type': 'function', 'function': {
...          'name': 'run_command',
...          'arguments': '{"command": "make", "reason": "build"}'}}]},
...     {'role': 'tool', 'tool_call_id': 'c1', 'content':
...      '{"exit_status": 2, "output": "undefined reference", '
...      '"approved": "by the user"}'},
...     {'role': 'user', 'content': 'The user sent this while you were '
...      'working: use clang'},
...     {'role': 'user', 'content': 'Request: continue'}]
>>> print(digest(messages))
User: fix the build
Agent: Looking.
Agent ran: make
  -> exit 2 (the user approved it): undefined reference
User (while you worked): use clang
User: continue
>>> short = digest(messages * 40, budget=300).split('\\n')
>>> short[:2]
['User: fix the build', '[... 229 earlier steps left out ...]']
>>> short[-1]
'User: continue'
"""

import json

STEERING_PREFIX = 'The user sent this while you were working: '
REQUEST_MARKER = '\n\nRequest: '


def clip(text, limit):
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def request_text(message):
    """What the user asked, without the terminal snapshot that goes with it"""
    content = message.get('content') or ''
    if REQUEST_MARKER in content:
        content = content.split(REQUEST_MARKER, 1)[1]
    elif content.startswith('Request: '):
        content = content[len('Request: '):]
    return content


def describe(message):
    """The lines that say what this message was"""
    role = message.get('role')
    content = message.get('content') or ''
    if role == 'user':
        if content.startswith(STEERING_PREFIX):
            return ['User (while you worked): ' +
                    clip(content[len(STEERING_PREFIX):], 600)]
        return ['User: ' + clip(request_text(message), 600)]
    if role == 'assistant':
        lines = []
        if content:
            lines.append('Agent: ' + clip(content, 400))
        for call in message.get('tool_calls') or []:
            function = call.get('function') or {}
            try:
                args = json.loads(function.get('arguments') or '{}')
            except ValueError:
                args = {}
            if function.get('name') == 'run_command':
                lines.append('Agent ran: ' + clip(args.get('command'), 300))
            else:
                lines.append('Agent used %s: %s' % (
                    function.get('name'), clip(
                        ' '.join(str(v) for v in args.values()), 200)))
        return lines
    if role == 'tool':
        if content.startswith('The user declined'):
            return ['  -> the user declined it']
        try:
            result = json.loads(content)
            status = result['exit_status']
        except (ValueError, KeyError, TypeError):
            return ['  -> ' + clip(content, 200)]
        how = ' (the user approved it)' if result.get('approved') else ''
        return ['  -> exit %s%s: %s' % (status, how, clip(
            result.get('output'), 160))]
    return []


def digest(messages, budget=6000):
    """The conversation, oldest first, within about budget characters. The
    first request is always kept, then the most recent steps that fit."""
    lines = [line for message in messages if message.get('role') != 'system'
             for line in describe(message)]
    if not lines:
        return ''
    first, rest = lines[0], lines[1:]
    kept, used = [], len(first)
    for line in reversed(rest):
        used += len(line) + 1
        if used > budget:
            break
        kept.append(line)
    kept.reverse()
    omitted = len(rest) - len(kept)
    if omitted:
        kept.insert(0, '[... %d earlier steps left out ...]' % omitted)
    return '\n'.join([first] + kept)
