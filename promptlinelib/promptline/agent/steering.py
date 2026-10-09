# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""steering.py - messages the user types while the agent is working

The agent runs in the user's terminal, so its keyboard is the user's
keyboard. While it waits for the model, Steering reads what the user types
(without echo, which the spinner line shows instead) and queues each line
for the agent to take before its next step. Keys typed while one of the
agent's commands runs belong to that command, as before.

LineBuffer is the line editor, kept apart from the terminal so it can be
tested:

>>> buffer = LineBuffer()
>>> buffer.feed(b'use cl')
[]
>>> buffer.text
'use cl'
>>> buffer.feed(b'ang\\x7f\\x7fng\\r')
['use clang']
>>> buffer.text
''
>>> buffer.feed(b'one\\x15two\\n\\x1b[Athree four\\x17five\\r')
['two', 'three five']
>>> LineBuffer().feed('caf\\u00e9\\r'.encode())
['caf\u00e9']
>>> half = '\\u00e9'.encode()
>>> buffer = LineBuffer()
>>> buffer.feed(b'a' + half[:1]), buffer.feed(half[1:] + b'\\r')
([], ['a\u00e9'])
"""

import codecs
import os
import select
import termios
import tty


class LineBuffer(object):
    """Collects typed bytes into lines, with the usual editing keys"""
    def __init__(self):
        self.text = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('ignore')
        self.escape = 0     # inside an escape sequence (arrow keys, ...)

    def feed(self, data):
        """Add bytes; returns the lines completed by Enter"""
        lines = []
        for char in self.decoder.decode(data):
            if self.escape == 1:
                # ESC [ ... final byte, or ESC O x: skip the whole sequence
                self.escape = 2 if char in '[O' else 0
            elif self.escape == 2:
                if '@' <= char <= '~':
                    self.escape = 0
            elif char == '\x1b':
                self.escape = 1
            elif char in '\r\n':
                line = self.text.strip()
                self.text = ''
                if line:
                    lines.append(line)
            elif char in '\x7f\x08':
                self.text = self.text[:-1]
            elif char == '\x15':
                self.text = ''
            elif char == '\x17':
                self.text = self.text.rstrip()
                self.text = self.text[:self.text.rfind(' ') + 1]
            elif char >= ' ':
                self.text += char
        return lines


class Steering(object):
    """Reads the user's steering messages from the terminal at fd"""
    def __init__(self, fd):
        self.fd = fd
        self.buffer = LineBuffer()
        self.queue = []
        self.saved = None

    def start(self):
        """No echo and no line mode (Ctrl+C still interrupts), keeping what
        the user has already typed"""
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd, termios.TCSANOW)

    def stop(self):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            self.saved = None

    def poll(self, timeout):
        """Wait up to timeout seconds for keys, and take them in"""
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                data = os.read(self.fd, 1024)
            except OSError:
                return
            self.queue.extend(self.buffer.feed(data))

    @property
    def typed(self):
        """The line being typed"""
        return self.buffer.text

    def pending(self):
        return bool(self.queue)

    def take(self):
        """The messages sent since last time"""
        taken, self.queue = self.queue, []
        return taken

    def discard(self):
        """Throw away anything unsent, and keys the user typed that nothing
        read, so they don't land at the shell prompt as a command"""
        self.queue, self.buffer = [], LineBuffer()
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except termios.error:
            pass
