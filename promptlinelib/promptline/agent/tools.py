# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""tools.py - what the agent can do, and running its commands

>>> import io
>>> sink = io.BytesIO()
>>> run_command('echo out; echo err >&2; exit 3', '/', 'sh', sink)
(3, 'out\\nerr')
>>> sink.getvalue()
b'out\\nerr\\n'
>>> trim_output('x' * 10, limit=6)
'xxx\\n[... 4 characters omitted ...]\\nxxx'
"""

import fcntl
import os
import pty
import re
import select
import signal
import subprocess
import termios
import tty

OUTPUT_LIMIT = 16000

RUN_COMMAND = {'type': 'function', 'function': {
        'name': 'run_command',
        'description': (
            'Run a shell command in the user\'s terminal, after the user '
            'approves it (they may edit it first or decline). Returns the '
            'exit status and the combined stdout/stderr.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {'type': 'string',
                            'description': 'The command line to run.'},
                'reason': {'type': 'string',
                           'description': 'One short line telling the user '
                                          'why, shown with the approval '
                                          'prompt.'},
            },
            'required': ['command', 'reason'],
            'additionalProperties': False,
        },
    }}

TOOLS = [
    RUN_COMMAND,
    {'type': 'function', 'function': {
        'name': 'place_on_prompt',
        'description': (
            'Type a command at the user\'s shell prompt once you finish, '
            'without running it; the user presses Enter if they want it. '
            'Use this for commands that must change the user\'s own shell '
            '(cd, export, source, activating an environment) or that they '
            'should run themselves. Only the last one placed is used.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'command': {'type': 'string'},
            },
            'required': ['command'],
            'additionalProperties': False,
        },
    }},
    {'type': 'function', 'function': {
        'name': 'remember',
        'description': (
            'Save a lasting fact about the user (their tools, preferences, '
            'environments, habits) to your memory, which you will see in '
            'future conversations. The user is told what you saved. Never '
            'save secrets.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'fact': {'type': 'string',
                         'description': 'One short, specific fact.'},
                'replaces': {'type': 'string',
                             'description': 'Text of an existing fact this '
                                            'one supersedes, or empty.'},
            },
            'required': ['fact', 'replaces'],
            'additionalProperties': False,
        },
    }},
    {'type': 'function', 'function': {
        'name': 'forget',
        'description': 'Remove facts from your memory that contain this '
                       'text.',
        'parameters': {
            'type': 'object',
            'properties': {
                'match': {'type': 'string'},
            },
            'required': ['match'],
            'additionalProperties': False,
        },
    }},
]

# Added while the agent works toward a goal (@agent --goal)
GOAL_TOOLS = [
    {'type': 'function', 'function': {
        'name': 'goal_complete',
        'description': (
            'Call this when the goal has been reached and you have checked '
            'that it has. Ends the work. Give a short summary of what you '
            'did and found.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'summary': {'type': 'string'},
            },
            'required': ['summary'],
            'additionalProperties': False,
        },
    }},
    {'type': 'function', 'function': {
        'name': 'goal_blocked',
        'description': (
            'Call this only when you cannot make any further progress '
            'without the user: something you cannot do, access you lack, '
            'or a decision only they can make. Ends the work. Say what is '
            'blocking you and what you tried.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'reason': {'type': 'string'},
            },
            'required': ['reason'],
            'additionalProperties': False,
        },
    }},
]


def trim_output(text, limit=OUTPUT_LIMIT):
    """Keep the start and end of long output"""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return '%s\n[... %d characters omitted ...]\n%s' % (
        text[:head], len(text) - head - tail, text[-tail:])


def run_command(command, cwd, shell, sink, terminal=None,
                limit=OUTPUT_LIMIT):
    """Run command with shell -c in cwd, copying its output to sink (a
    binary stream) as it arrives. Returns (exit status, output text).

    With terminal (the fd of the user's terminal), the command gets a
    terminal of its own and the user's keys are passed through to it, so
    full-screen programs (installer dialogs, pagers, editors) and password
    prompts work. Ctrl+C interrupts the command and then the agent."""
    if terminal is not None:
        return run_in_terminal(command, cwd, shell, sink, terminal, limit)
    process = subprocess.Popen([shell, '-c', command], cwd=cwd,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    chunks = Output(limit)
    try:
        while True:
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                break
            sink.write(data)
            sink.flush()
            chunks.add(data)
        status = process.wait()
    except KeyboardInterrupt:
        process.send_signal(signal.SIGINT)
        process.wait()
        raise
    finally:
        process.stdout.close()
    return exit_status(status), chunks.text()


def run_quiet(command, cwd, shell, limit, timeout, processes=None):
    """Run command with shell -c in cwd for a subagent: no terminal and no
    keyboard, output collected rather than shown, ended after timeout
    seconds. Returns (exit status, output text). processes, if given, holds
    the running process so that it can be killed from elsewhere.

    >>> run_quiet('echo hi; echo oops >&2; exit 4', '/', 'sh', 1000, 5)
    (4, 'hi\\noops')
    >>> run_quiet('sleep 5', '/', 'sh', 1000, 0.2)[0]
    124
    >>> run_quiet('cat', '/', 'sh', 1000, 5)       # nothing to read
    (0, '')
    """
    process = subprocess.Popen([shell, '-c', command], cwd=cwd,
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               start_new_session=True)
    if processes is not None:
        processes.append(process)
    timed_out = False
    try:
        data, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        data, _ = process.communicate()
    finally:
        if processes is not None and process in processes:
            processes.remove(process)
    text = plain(data.decode('utf-8', 'replace'))
    if timed_out:
        text += '\n[stopped: it ran for more than %g seconds]' % timeout
        return 124, trim_output(text.strip(), limit)
    return exit_status(process.returncode), trim_output(text, limit)


def run_in_terminal(command, cwd, shell, sink, terminal,
                    limit=OUTPUT_LIMIT):
    """run_command, with the command on a pseudo-terminal that the user's
    terminal is connected to while it runs"""
    master, slave = pty.openpty()
    copy_window_size(terminal, slave)

    def controlling_terminal():
        # The new session (start_new_session) takes the pty as its
        # terminal, so Ctrl+C and full-screen programs work inside it
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    try:
        process = subprocess.Popen([shell, '-c', command], cwd=cwd,
                                   stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True,
                                   preexec_fn=controlling_terminal)
    finally:
        os.close(slave)

    previous_winch = signal.signal(
        signal.SIGWINCH, lambda *_: copy_window_size(terminal, master))
    saved = termios.tcgetattr(terminal)
    chunks = Output(limit)
    interrupted = False
    try:
        # Raw: every key goes to the command, which has its own terminal
        tty.setraw(terminal)
        while True:
            readable = select.select([master, terminal], [], [], 0.05)[0]
            if terminal in readable:
                data = os.read(terminal, 1024)
                interrupted = interrupted or b'\x03' in data
                os.write(master, data)
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError:     # EIO: the command's terminal closed
                    data = b''
                if not data:
                    break
                sink.write(data)
                sink.flush()
                chunks.add(data)
            elif process.poll() is not None:
                # Done, and nothing left to read (a background job it
                # started may still hold the terminal open)
                break
        status = process.wait()
    finally:
        termios.tcsetattr(terminal, termios.TCSADRAIN, saved)
        signal.signal(signal.SIGWINCH, previous_winch)
        os.close(master)
    if interrupted and status in (-signal.SIGINT, 128 + signal.SIGINT):
        raise KeyboardInterrupt()
    return exit_status(status), plain(chunks.text())


def copy_window_size(source, target):
    try:
        size = fcntl.ioctl(source, termios.TIOCGWINSZ, b'\0' * 8)
        fcntl.ioctl(target, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def exit_status(status):
    """A shell-style exit status: 128 + N for death by signal N"""
    return 128 - status if status < 0 else status


# Colour, cursor movement and window titles: noise to the model
ESCAPES = re.compile(r'\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)'
                     r'|[()][0-9A-Za-z]|[=>78DEHMc])')


def plain(text):
    r"""Terminal output as text for the model

    >>> plain('\x1b[1;31mred\x1b[0m line\r\n\x1b]0;title\x07next\r\n')
    'red line\nnext'
    """
    return ESCAPES.sub('', text).replace('\r\n', '\n').strip()


class Output(object):
    """Collects a command's output, bounded: on runaway output it keeps
    the first chunk and a window of the most recent ones"""
    def __init__(self, limit=OUTPUT_LIMIT):
        self.limit = limit
        self.chunks = []
        self.size = 0

    def add(self, data):
        self.chunks.append(data)
        self.size += len(data)
        while self.size > self.limit * 8 and len(self.chunks) > 2:
            self.size -= len(self.chunks.pop(1))

    def text(self):
        output = b''.join(self.chunks).decode('utf-8', 'replace')
        return trim_output(output.rstrip(), self.limit)
