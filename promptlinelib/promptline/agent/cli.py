# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""cli.py - the promptline-agent program: @agent's terminal front end

It runs in the user's terminal like any other command, so its output is
ordinary scrollback and Ctrl+C stops it.

>>> import tempfile
>>> store = ConversationStore('urn:uuid:test', directory=tempfile.mkdtemp())
>>> store.load()
[]
>>> store.save([{'role': 'user', 'content': 'hi'},
...             {'role': 'assistant', 'content': 'hello'}])
>>> [m['content'] for m in store.load()]
['hi', 'hello']
>>> trim_history([{'role': 'user', 'content': str(i)} for i in range(5)], 3)
[{'role': 'user', 'content': '2'}, {'role': 'user', 'content': '3'}, {'role': 'user', 'content': '4'}]
"""

import hashlib
import json
import math
import os
import shutil
import sys
import threading
import time

from . import read_request, runtime_dir, write_prefill
from .. import personal
from .approval import ALLOW, AskEveryTime, AutoReview, FullPermission, \
    ModelReviewer, ReviewStream
from .loop import Agent
from .prompts import system_prompt, user_message
from .tools import run_command
from ..providers import (ProviderError, make_provider, missing_key_hint,
                         provider_settings)

CONVERSATION_TTL = 30 * 60
MAX_MESSAGES = 60
STORED_TOOL_OUTPUT = 4000

BOLD = '\033[1m'
DIM = '\033[2m'
ACCENT = '\033[36m'
WARN = '\033[33m'
RESET = '\033[0m'


def trim_history(messages, limit=MAX_MESSAGES):
    """Keep the most recent messages, starting at a user message so tool
    results are never separated from the call that produced them"""
    if len(messages) <= limit:
        return messages
    start = len(messages) - limit
    while start < len(messages) and messages[start].get('role') != 'user':
        start += 1
    return messages[start:]


class ConversationStore(object):
    """Follow-up questions in the same terminal continue the conversation,
    until it has been idle for CONVERSATION_TTL"""
    def __init__(self, terminal, directory=None):
        name = hashlib.sha256((terminal or 'none').encode()).hexdigest()[:16]
        self.path = os.path.join(directory or runtime_dir(),
                                 'conversation-%s.json' % name)

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        if time.time() - data.get('updated', 0) > CONVERSATION_TTL:
            return []
        return data.get('messages', [])

    def save(self, messages):
        stored = []
        for message in trim_history(messages):
            if message.get('role') == 'tool' and \
                    len(message.get('content') or '') > STORED_TOOL_OUTPUT:
                message = dict(message, content=message['content'][
                    :STORED_TOOL_OUTPUT] + ' [...]')
            stored.append(message)
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'updated': time.time(), 'messages': stored}, handle)


class TtyUI(object):
    """Talks to the user in the terminal"""
    SPINNER = '|/-\\'

    def __init__(self, out=None, tty=None):
        self.out = out or sys.stdout
        self.tty = tty if tty is not None else sys.stdin.isatty()
        # The spinner runs here while replies stream in from the provider's
        # thread; the lock keeps them from writing over each other
        self.lock = threading.Lock()
        self.streaming = False
        self.last_char = '\n'

    def write(self, text):
        self.out.write(text)
        self.out.flush()

    def header(self, prompt_prefix, injected, query):
        """Replace the line the shell echoed (our launcher command) with
        what the user actually typed"""
        if not self.tty or prompt_prefix is None:
            return
        columns = shutil.get_terminal_size().columns
        rows = max(1, math.ceil((len(prompt_prefix) + len(injected)) /
                                max(1, columns)))
        self.write('\033[1A\r\033[2K' * rows + prompt_prefix + BOLD +
                   '@agent' + RESET + ' ' + query + '\n')

    def thinking(self, fn, label='thinking'):
        result = {}

        def work():
            try:
                result['value'] = fn()
            except BaseException as ex:
                result['error'] = ex

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        frame = 0
        while thread.is_alive():
            with self.lock:
                if self.tty and not self.streaming:
                    self.write('\r%s%s %s%s' % (
                        DIM, self.SPINNER[frame % len(self.SPINNER)], label,
                        RESET))
            frame += 1
            thread.join(0.12)
        with self.lock:
            if self.tty and not self.streaming:
                self.write('\r\033[K')
        if 'error' in result:
            raise result['error']
        return result['value']

    def say(self, text):
        self.write(text + '\n\n')

    def stream(self, text):
        """Print part of a reply as it arrives"""
        with self.lock:
            if not self.streaming:
                text = text.lstrip()
                if not text:
                    return
                if self.tty:
                    self.out.write('\r\033[K')    # the spinner's line
                self.streaming = True
            self.write(text)
            self.last_char = text[-1]

    def end_stream(self):
        with self.lock:
            if self.streaming:
                self.write('\n' if self.last_char == '\n' else '\n\n')
            self.streaming = False

    def note(self, text):
        self.write(DIM + text + RESET + '\n')

    def error(self, text):
        self.write(BOLD + 'promptline-agent: ' + RESET + text + '\n')

    def mode_banner(self, mode):
        if mode == 'auto-review':
            self.note('Auto-review: commands the reviewer judges safe run '
                      'without asking.')
        elif mode == 'full':
            self.write(WARN + BOLD + 'Full permission mode:' + RESET + WARN +
                       ' commands run without asking (a few dangerous ones '
                       'still ask).' + RESET + '\n')

    def auto_approved(self, command, mode, note):
        self.write('  ' + BOLD + '$ ' + command + RESET + '  ' + DIM +
                   '(%s)' % (note or 'full permission') + RESET + '\n')

    def review_start(self, command, reason):
        """Show the command while the reviewer looks at it"""
        self.review = ReviewStream()
        if reason:
            self.write(DIM + reason + RESET + '\n')
        self.write('  ' + BOLD + '$ ' + command + RESET + '\n')

    def review_text(self, piece):
        """Print the reviewer's reason as it arrives (provider's thread)"""
        text = self.review.feed(piece)
        with self.lock:
            if not self.streaming:
                text = text.lstrip()
                if not text:
                    return
                if self.tty:
                    self.out.write('\r\033[K')    # the spinner's line
                self.streaming = True
                self.write(DIM + '  reviewer: ' if self.review.verdict ==
                           'safe' else WARN + 'Reviewer: ')
            self.write(text)

    def review_end(self, decision):
        with self.lock:
            streamed, self.streaming = self.streaming, False
        if streamed:
            self.write(RESET + '\n')
        # Show the decision's own note if nothing streamed, or if it isn't
        # what streamed (the review failed part way, say)
        agrees = streamed and (self.review.verdict == 'safe') == (
            decision.action == ALLOW) and not \
            (decision.note or '').startswith('Review failed')
        if decision.note and not agrees:
            if decision.action == ALLOW:
                self.write('  ' + DIM + decision.note + RESET + '\n')
            else:
                self.write(WARN + decision.note + RESET + '\n')

    def approve(self, command, reason, note=None, shown=False):
        if not shown:
            if reason:
                self.write(DIM + reason + RESET + '\n')
            if note:
                self.write(WARN + note + RESET + '\n')
            self.write('  ' + BOLD + '$ ' + command + RESET + '\n')
        if not self.tty:
            self.note('Not running it: no terminal to ask for approval.')
            return 'cancel', command
        while True:
            self.write('  ' + ACCENT + '[a]' + RESET + 'pprove  ' + ACCENT +
                       '[e]' + RESET + 'dit  ' + ACCENT + '[c]' + RESET +
                       'ancel ')
            key = self.read_key().lower()
            if key in ('a', 'y'):
                self.write('approved\n')
                return 'approve', command
            if key in ('c', 'n', 'q', '\x1b'):
                self.write('cancelled\n\n')
                return 'cancel', command
            if key == 'e':
                self.write('edit\n')
                edited = self.edit(command)
                if not edited:
                    self.write('  cancelled\n\n')
                    return 'cancel', command
                return 'approve', edited
            self.write('\r\033[K')

    def read_key(self):
        """One keypress, without waiting for Enter (Ctrl+C still works)"""
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            return os.read(fd, 1).decode('utf-8', 'replace')
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    def edit(self, command):
        """Let the user edit the command line, starting from command"""
        import readline
        readline.set_startup_hook(lambda: readline.insert_text(command))
        try:
            return input('  $ ').strip()
        except EOFError:
            return ''
        finally:
            readline.set_startup_hook()

    def running(self, command):
        pass

    def finished(self, status):
        if status:
            self.note('exit %d' % status)
        self.write('\n')


class AuditLog(object):
    """Every command the agent runs, and how it was allowed to run:
    time, mode, approval, exit status, directory, command (tab-separated)"""
    def __init__(self, cwd, mode, filename=None):
        self.cwd = cwd
        self.mode = mode
        self.filename = filename or os.path.join(
            os.path.dirname(personal.path('memory')), 'agent-audit.log')

    def __call__(self, command, how, status):
        line = '\t'.join([time.strftime('%Y-%m-%dT%H:%M:%S%z'), self.mode,
                          how, 'exit=%s' % status, self.cwd,
                          ' '.join(command.split('\n'))]) + '\n'
        try:
            os.makedirs(os.path.dirname(self.filename), mode=0o700,
                        exist_ok=True)
            fd = os.open(self.filename,
                         os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'a', encoding='utf-8') as handle:
                handle.write(line)
        except OSError:
            pass


def choose_policy(ui, options, settings, query, cwd, guardrails):
    """The approval policy for this run, and the mode it implements. Full
    permission falls back to asking unless the user's guardrails are set."""
    mode = options.get('mode', 'ask')
    if mode == 'full':
        ready, why = personal.guardrails_ready()
        if not ready:
            ui.note('Full permission mode is locked: %s (`promptline '
                    '--guardrails`). Asking before each command instead.'
                    % why)
            mode = 'ask'
    if mode == 'full':
        return FullPermission(), mode
    if mode == 'auto-review':
        review = dict(settings or {}, reasoning=options.get(
            'review_reasoning', 'medium'))
        reviewer = make_provider('agent', review)
        if reviewer is not None:
            return AutoReview(ModelReviewer(reviewer, query, cwd,
                                            guardrails)), mode
    return AskEveryTime(), 'ask'


def first_time_tip(ui):
    """Once: point out personalisation, which helps from the first request"""
    marker = os.path.join(os.path.dirname(personal.path('memory')),
                          'personalisation-tip')
    if os.path.exists(marker):
        return
    ui.note('Tip: tell Promptline about your work, tools and environments '
            'with `promptline -P`.')
    try:
        os.makedirs(os.path.dirname(marker), mode=0o700, exist_ok=True)
        open(marker, 'w').close()
    except OSError:
        pass


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) != 2:
        sys.stderr.write('usage: promptline-agent REQUEST\n'
                         'Type "@agent <question>" at a Promptline prompt '
                         'instead of running this directly.\n')
        return 2
    path = argv[1]
    try:
        request = read_request(path)
    except (OSError, ValueError):
        sys.stderr.write('promptline-agent: request %s not found\n' % path)
        return 2

    ui = TtyUI()
    token = os.path.basename(path)[:-len('.json')]
    query = request.get('query', '')
    ui.header(request.get('prompt_prefix'), ' _promptline_agent ' + token,
              query)
    if not query:
        ui.note('Usage: @agent <question or task>, e.g. '
                '@agent why did that command fail?')
        return 0

    provider = make_provider('agent', request.get('settings'))
    if provider is None:
        ui.error(missing_key_hint(request.get('settings') or
                                  provider_settings('agent')))
        return 1

    cwd = request.get('cwd') or os.getcwd()
    shell = request.get('shell') or os.environ.get('SHELL', '/bin/sh')
    out = sys.stdout.buffer
    terminal = sys.stdin.fileno() if sys.stdin.isatty() else None

    def executor(command):
        sys.stdout.flush()
        return run_command(command, cwd, shell, out, terminal)

    memory = personal.Memory()
    about = personal.personal_text()
    if not about and not memory.facts():
        first_time_tip(ui)

    guardrails = personal.guardrail_rules()
    policy, mode = choose_policy(ui, request.get('agent') or {},
                                 request.get('settings'), query, cwd,
                                 guardrails)
    ui.mode_banner(mode)

    store = ConversationStore(request.get('terminal'))
    history = store.load()
    prompt = system_prompt(shell, mode=mode, personal=about,
                           memory=memory.text(), guardrails=guardrails)
    agent = Agent(provider, ui, executor, policy=policy, memory=memory,
                  audit=AuditLog(cwd, mode), messages=[
                      {'role': 'system', 'content': prompt}] + history)
    try:
        agent.run(user_message(request))
    except KeyboardInterrupt:
        ui.write('\n')
        ui.note('Interrupted.')
        return 130
    except ProviderError as ex:
        ui.error(str(ex))
        return 1
    store.save(agent.messages[1:])
    if agent.prefill:
        write_prefill(path, agent.prefill)
    return 0
