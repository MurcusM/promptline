# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""loop.py - the agent's tool-calling loop

The loop is independent of any terminal: it talks to the user through a UI
object and runs commands through an executor, so it is tested with fakes.

UI methods: thinking(fn) runs fn while showing progress and returns its
result; stream(text) and end_stream() for replies printed as they arrive;
say(text) for replies that arrived whole; approve(command, reason, note) -> ('approve'|'cancel',
command); auto_approved(command, mode, note); running(command);
finished(status); note(text). Optionally, for policies that review
commands (a model call that takes a moment): review_start(command, reason)
shows the command before the review, review_text(piece) the review as it
streams in, review_end(decision) finishes it; approve() is then called with
shown=True.

>>> class Provider(object):
...     def __init__(self, replies): self.replies = list(replies)
...     def chat(self, messages, tools, on_text=None):
...         return self.replies.pop(0)
>>> class UI(object):
...     def __init__(self, answer): self.answer, self.log = answer, []
...     def thinking(self, fn): return fn()
...     def say(self, text): self.log.append(('say', text))
...     def approve(self, command, reason, note=None):
...         self.log.append(('approve?', command, reason)); return self.answer
...     def auto_approved(self, command, mode, note):
...         self.log.append(('auto', mode, command))
...     def running(self, command): self.log.append(('run', command))
...     def finished(self, status): pass
...     def note(self, text): self.log.append(('note', text))
>>> def call(name, **args):
...     return {'role': 'assistant', 'content': None, 'tool_calls': [
...         {'id': 'c1', 'type': 'function', 'function': {
...          'name': name, 'arguments': json.dumps(args)}}]}
>>> def executor(command): return 0, '3000/tcp: node'
>>> provider = Provider([call('run_command', command='lsof -i :3000',
...                           reason='find the process'),
...                      {'role': 'assistant', 'content': 'node holds it.'}])
>>> ui = UI(('approve', 'lsof -i :3000'))
>>> agent = Agent(provider, ui, executor)
>>> agent.run({'role': 'user', 'content': 'port 3000?'})
>>> ui.log
[('approve?', 'lsof -i :3000', 'find the process'), ('run', 'lsof -i :3000'), ('say', 'node holds it.')]
>>> json.loads(agent.messages[2]['content'])
{'exit_status': 0, 'output': '3000/tcp: node', 'approved': 'by the user'}

Declining is reported back to the model:

>>> provider = Provider([call('run_command', command='rm -rf /tmp/x',
...                           reason='clean up'),
...                      {'role': 'assistant', 'content': 'OK, left it.'}])
>>> agent = Agent(provider, UI(('cancel', 'rm -rf /tmp/x')), executor)
>>> agent.run({'role': 'user', 'content': 'clean'})
>>> agent.messages[2]['content']
'The user declined to run this command.'

Memory tools, and edits reported back to the model:

>>> class Memory(object):
...     def __init__(self): self.facts = []
...     def add(self, fact, replaces=None): self.facts.append(fact); return True
...     def forget(self, match): return []
>>> provider = Provider([call('remember', fact='Uses Nessus, not nmap',
...                           replaces=''),
...                      call('run_command', command='nmap -sV 10.0.0.5',
...                           reason='check services'),
...                      {'role': 'assistant', 'content': 'Done.'}])
>>> ui = UI(('approve', 'nessuscli scan --target 10.0.0.5'))
>>> agent = Agent(provider, ui, executor, memory=Memory())
>>> agent.run({'role': 'user', 'content': 'scan 10.0.0.5'})
>>> ui.log[0]
('note', 'Remembered: Uses Nessus, not nmap')
>>> json.loads(agent.messages[4]['content'])['note']
'The user edited your command and ran this instead: nessuscli scan --target 10.0.0.5'

In full permission mode commands run unasked, except hard stops, and
every command run is audited:

>>> from .approval import FullPermission
>>> provider = Provider([call('run_command', command='df -h', reason='disk'),
...                      call('run_command', command='sudo reboot',
...                           reason='apply'),
...                      {'role': 'assistant', 'content': 'Done.'}])
>>> ui, audit = UI(('cancel', 'sudo reboot')), []
>>> agent = Agent(provider, ui, executor, policy=FullPermission(),
...               audit=lambda *entry: audit.append(entry))
>>> agent.run({'role': 'user', 'content': 'check disk then reboot'})
>>> ui.log[:3]
[('auto', 'full', 'df -h'), ('run', 'df -h'), ('approve?', 'sudo reboot', 'apply')]
>>> audit
[('df -h', 'full', 0)]

place_on_prompt remembers the command for the user's prompt:

>>> provider = Provider([call('place_on_prompt', command='cd /srv/app'),
...                      {'role': 'assistant', 'content': 'Done.'}])
>>> agent = Agent(provider, UI(None), executor)
>>> agent.run({'role': 'user', 'content': 'go to the app'})
>>> agent.prefill
'cd /srv/app'
"""

import json
import time

from ..providers import ProviderError
from .approval import ALLOW, DENY, AskEveryTime
from .digest import STEERING_PREFIX, digest
from .tools import GOAL_TOOLS, TOOLS

MAX_STEPS = 25
# A goal run that stops calling tools without finishing is nudged this many
# times in a row before giving up
MAX_NUDGES = 3
# Waits (seconds) between tries when the provider has a passing problem
# (rate limit, timeout, overloaded) during a goal run
RETRY_WAITS = (5, 15, 30, 60, 120, 120)
# The same command failing this many times in a row: warn the model, then
# end a goal run
STUCK_WARNING = 3
STUCK_GIVE_UP = 6
# When the conversation grows past this many characters, older steps are
# summarised, keeping the first request and the latest KEEP_RECENT messages
COMPACT_CHARS = 300000
KEEP_RECENT = 24

NUDGE = (
    "The goal has not been marked complete. If it is reached, and you have "
    "checked that it is, call goal_complete. If you cannot go on without "
    "the user, call goal_blocked. Otherwise keep working: run the next "
    "command.")

SUMMARY_PROMPT = (
    "You are condensing the earlier part of an AI terminal agent's work so "
    "that it can carry on in a smaller context. Write plain-text notes: the "
    "goal or request, what has been done and found (commands, files, "
    "hosts, results, errors), decisions made, what the user said or "
    "approved, and what remains. Keep specifics such as paths, names and "
    "numbers. No preamble.")

DONE = ('complete', 'blocked', 'stuck', 'stalled')


class Agent(object):
    def __init__(self, provider, ui, executor, policy=None, messages=None,
                 max_steps=MAX_STEPS, memory=None, audit=None, steering=None):
        self.provider = provider
        self.ui = ui
        self.executor = executor
        self.policy = policy or AskEveryTime()
        self.memory = memory
        self.audit = audit          # audit(command, how, exit status)
        self.messages = list(messages or [])
        self.max_steps = max_steps
        self.steering = steering    # take() -> messages typed meanwhile
        self.prefill = None
        self.goal = None            # the goal being worked towards, if any
        self.outcome = None         # how a goal run ended: see DONE
        self.summary = ''           # ...and what the agent said about it
        self.failures = {}          # command -> times it failed in a row
        self.sleep = time.sleep
        attach = getattr(self.policy, 'attach', None)
        if attach:
            attach(self)

    def run(self, user_message, goal=None):
        """Handle one request from the user, until the model stops calling
        tools. With a goal, work until it is reached or can't be: there is
        no step limit then, the model is nudged if it stops early, and
        passing provider errors are retried."""
        self.goal = goal
        self.outcome, self.summary = None, ''
        tools = TOOLS + GOAL_TOOLS if goal else TOOLS
        self.messages.append(user_message)
        nudges, step = 0, 0
        while goal or step < self.max_steps:
            step += 1
            self.inject_steering()
            self.compact()
            reply = self.chat(tools)
            calls = reply.get('tool_calls') or []
            if not calls:
                if self.steering is not None and self.steering.pending():
                    continue    # the user said something as it finished
                if not goal:
                    return
                nudges += 1
                if nudges > MAX_NUDGES:
                    self.outcome = 'stalled'
                    self.summary = 'The agent stopped before reaching the ' \
                        'goal.'
                    return
                self.messages.append({'role': 'user', 'content': NUDGE})
                continue
            nudges = 0
            for call in calls:
                self.messages.append({'role': 'tool',
                                      'tool_call_id': call.get('id'),
                                      'content': self.handle(call)})
            if self.outcome in DONE:
                return
            if goal and step % 25 == 0:
                self.ui.note('Still working on the goal: %d steps so far. '
                             'Type to steer, Ctrl+C to stop.' % step)
        self.ui.note('Stopped after %d steps. Say "@agent continue" to carry '
                     'on.' % self.max_steps)

    def chat(self, tools):
        """One model reply, appended to the conversation and shown"""
        streamed = []

        def on_text(piece):
            streamed.append(piece)
            self.ui.stream(piece)
        for attempt in range(len(RETRY_WAITS) + 1 if self.goal else 1):
            try:
                reply = self.ui.thinking(
                    lambda: self.provider.chat(self.messages, tools,
                                               on_text=on_text))
                break
            except ProviderError as ex:
                if streamed:
                    self.ui.end_stream()
                    streamed[:] = []
                if ex.auth or attempt >= len(RETRY_WAITS) or not self.goal:
                    raise
                wait = RETRY_WAITS[attempt]
                self.ui.note('%s. Trying again in %d seconds.' % (ex, wait))
                self.sleep(wait)
        self.messages.append(reply)
        if streamed:
            self.ui.end_stream()
        elif reply.get('content'):
            self.ui.say(reply['content'].strip())
        return reply

    def inject_steering(self):
        """Put what the user typed while the agent worked into the
        conversation. Returns whether there was anything."""
        texts = self.steering.take() if self.steering is not None else []
        if not texts:
            return False
        for text in texts:
            self.ui.note('You: %s' % text)
        self.messages.append({'role': 'user', 'content':
                              STEERING_PREFIX + ' / '.join(texts)})
        return True

    def compact(self):
        """Summarise the older part of a conversation that has grown too
        big, so a long run can go on"""
        if size(self.messages) < COMPACT_CHARS:
            return
        anchor = 2 if len(self.messages) > 1 and \
            self.messages[1].get('role') == 'user' else 1
        cut = len(self.messages) - KEEP_RECENT
        while cut < len(self.messages) and \
                self.messages[cut].get('role') == 'tool':
            cut += 1
        if cut <= anchor:
            return
        older = self.messages[anchor:cut]
        notes = digest(older, budget=30000)
        complete = getattr(self.provider, 'complete', None)
        if complete is not None:
            try:
                notes = self.ui.thinking(lambda: complete(
                    [{'role': 'system', 'content': SUMMARY_PROMPT},
                     {'role': 'user', 'content': notes}],
                    max_tokens=1500, timeout=90), label='summarising')
            except ProviderError:
                pass    # the digest itself will do
        self.messages = self.messages[:anchor] + [{
            'role': 'user', 'content': '[Your work so far, summarised to '
            'save space]\n' + notes}] + self.messages[cut:]

    def handle(self, call):
        """Carry out one tool call; returns the text sent back to the model"""
        function = call.get('function', {})
        name = function.get('name')
        try:
            args = json.loads(function.get('arguments') or '{}')
        except ValueError:
            return 'Error: the arguments were not valid JSON.'
        if name in ('remember', 'forget'):
            return self.handle_memory(name, args)
        if name in ('goal_complete', 'goal_blocked'):
            return self.handle_goal(name, args)
        command = (args.get('command') or '').strip()
        if not command:
            return 'Error: no command given.'

        if name == 'place_on_prompt':
            self.prefill = command
            self.ui.note('Will be placed on your prompt: %s' % command)
            return 'The command will be typed at the user\'s prompt.'

        if name != 'run_command':
            return 'Error: there is no tool called %r.' % name
        reason = args.get('reason', '')
        shown = self.policy.reviews and hasattr(self.ui, 'review_start')
        if shown:
            self.ui.review_start(command, reason)
            decision = self.ui.thinking(
                lambda: self.policy.decide(command, reason,
                                           on_text=self.ui.review_text),
                label='reviewing')
            self.ui.review_end(decision)
        else:
            decision = self.policy.decide(command, reason)
        if decision.action == DENY:
            return 'This command is not allowed.'
        proposed = command
        if decision.action == ALLOW:
            if self.steering is not None and self.steering.pending():
                # Guidance typed while this was being decided may well
                # change it; nothing should run unseen before it is read
                self.ui.note('Not run, because you sent guidance first: %s'
                             % command)
                return ('Not run: the user sent you new guidance before this '
                        'command ran. Read it, then decide again.')
            if not shown:
                self.ui.auto_approved(command, self.policy.mode,
                                      decision.note)
            how = self.policy.mode
        else:
            if shown:
                answer, command = self.ui.approve(command, reason,
                                                  decision.note, shown=True)
            else:
                answer, command = self.ui.approve(command, reason,
                                                  decision.note)
            self.policy.remember(command, answer == 'approve')
            if answer == 'timeout':
                return ('The user was not at the terminal to approve this '
                        'command, so it was not run. Do not try it again. '
                        'Carry on with whatever does not need approval; if '
                        'nothing does, say what is waiting for them%s.' % (
                            ' and call goal_blocked' if self.goal else ''))
            if answer != 'approve':
                result = 'The user declined to run this command.'
                if decision.note:
                    result += ' (It was flagged: %s.)' % decision.note
                return result
            how = 'edited' if command != proposed else 'approved'
        self.ui.running(command)
        status, output = self.executor(command)
        self.ui.finished(status)
        if self.audit is not None:
            self.audit(command, how, status)
        result = {'exit_status': status, 'output': output}
        if how in ('approved', 'edited'):
            result['approved'] = 'by the user'
        self.track_failures(command, status, result)
        if command != proposed:
            # How the user changes a command is worth learning from
            result['note'] = 'The user edited your command and ran this ' \
                'instead: %s' % command
        return json.dumps(result)

    def track_failures(self, command, status, result):
        """Warn the model about a command that keeps failing, and give up on
        a goal that is going round in circles"""
        if not status:
            self.failures.pop(command, None)
            return
        count = self.failures[command] = self.failures.get(command, 0) + 1
        if count >= STUCK_WARNING:
            result['warning'] = (
                'This exact command has now failed %d times in a row. Do '
                'something different%s.' % (
                    count, ', or if there is nothing else to try, call '
                    'goal_blocked' if self.goal else ''))
        if self.goal and count >= STUCK_GIVE_UP:
            self.outcome = 'stuck'
            self.summary = 'The same command failed %d times in a row: %s' \
                % (count, command)

    def handle_goal(self, name, args):
        if not self.goal:
            return 'Error: there is no goal set.'
        if name == 'goal_complete':
            self.outcome = 'complete'
            self.summary = (args.get('summary') or '').strip()
        else:
            self.outcome = 'blocked'
            self.summary = (args.get('reason') or '').strip()
        return 'Noted.'

    def handle_memory(self, name, args):
        if self.memory is None:
            return 'Error: memory is not available.'
        if name == 'remember':
            fact = (args.get('fact') or '').strip()
            if not fact:
                return 'Error: no fact given.'
            if self.memory.add(fact, (args.get('replaces') or '').strip()
                               or None):
                self.ui.note('Remembered: %s' % fact)
                return 'Saved to memory.'
            return 'Already in memory.'
        match = (args.get('match') or '').strip()
        gone = self.memory.forget(match) if match else []
        for fact in gone:
            self.ui.note('Forgot: %s' % fact)
        return 'Forgot %d fact(s).' % len(gone)


def size(messages):
    """About how many characters of conversation these are"""
    return sum(len(message.get('content') or '') +
               len(json.dumps(message.get('tool_calls') or ''))
               for message in messages)
