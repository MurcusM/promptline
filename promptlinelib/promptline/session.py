# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""session.py - what the shell in one terminal is doing, and what it did

ShellSession turns the marks from the shell integration into:

* the line the user is currently typing (read straight off the screen, so
  history recall, Tab completion and paste are all reflected), and
* a log of recent commands with their cwd, exit status and output.

It knows nothing about GTK; it talks to the terminal through a "screen"
object with cursor(), columns() and text(row0, col0, row1, col1) (rows
inclusive, end column exclusive, like Vte.Terminal.get_text_range_format).

>>> screen = FakeScreen()
>>> session = ShellSession(screen)
>>> screen.write('$ ')
>>> session.on_prompt(1); session.on_input()
>>> screen.write('git sta')
>>> session.current_input()
InputLine(text='git sta', at_end=True)
>>> screen.write('tus\\n')
>>> session.on_exec(None, '/src')
>>> screen.write('On branch main\\nnothing to commit\\n')
>>> session.on_done(0)
>>> screen.write('$ ')
>>> session.on_prompt(1); session.on_input()
>>> record = session.log[-1]
>>> record.command, record.cwd, record.exit_status
('git status', '/src', 0)
>>> record.output
'On branch main\\nnothing to commit'
>>> session.current_input()
InputLine(text='', at_end=True)
"""

import collections
import time

OUTPUT_LIMIT = 100000
LOG_LENGTH = 50

InputLine = collections.namedtuple('InputLine', ['text', 'at_end'])


class CommandRecord(object):
    """One command the user ran. A command typed with a leading space is
    private, following the shells' ignorespace convention: Promptline won't
    learn it or share it."""
    def __init__(self, command, cwd):
        self.private = command.startswith(' ')
        self.command = command.strip()
        self.cwd = cwd
        self.exit_status = None
        self.output = None
        self.started = time.time()
        self.finished = None

    def as_dict(self):
        """Plain data for serialising"""
        return {'private': self.private, 'command': self.command, 'cwd': self.cwd,
                'exit_status': self.exit_status, 'output': self.output,
                'started': self.started, 'finished': self.finished}


class ShellSession(object):
    """State machine fed by shell marks"""
    UNKNOWN = 'unknown'     # no marks seen yet: no shell integration
    PROMPT = 'prompt'       # user is editing a command line
    RUNNING = 'running'     # a command is executing

    def __init__(self, screen, log_length=LOG_LENGTH):
        self.screen = screen
        self.state = self.UNKNOWN
        self.log = collections.deque(maxlen=log_length)
        self.prompt_rows = 1
        self.anchor = None          # (row, col) where input starts
        self.prompt_prefix = None   # prompt text left of the anchor
        self.right_prompt = ''      # text right of the anchor (zsh RPROMPT)
        self.input_end_row = None   # lowest row the input reached
        self.pending = None         # record waiting for its output
        self.after = None           # input characters after the cursor
        self.output_start = None

    def on_prompt(self, rows):
        """A prompt of the given height is being drawn"""
        self.prompt_rows = max(1, rows or 1)

    def on_input(self):
        """The prompt is drawn and the cursor sits where input starts"""
        row, col = self.screen.cursor()
        self._finish_pending(row)
        self.anchor = (row, col)
        self.prompt_prefix = self.screen.text(row, 0, row, col)
        self.right_prompt = self.screen.text(row, col, row,
                                             self.screen.columns()).strip()
        self.input_end_row = row
        self.after = None
        self.state = self.PROMPT

    def on_after(self, count):
        """The shell says count characters of the input follow the cursor"""
        self.after = count

    def on_exec(self, command, cwd):
        """A command is about to run. command is None if the shell didn't
        say, in which case we read it from the screen."""
        if self.state == self.PROMPT:
            # Not the cursor: this mark may arrive in the same batch as the
            # command's first output, so the cursor could already be past it
            end_row = self.input_end_row
            if command is not None:
                end_row = max(end_row, self._rows_for(command) - 1 +
                              self.anchor[0])
            if command is None:
                row, col = self.anchor
                command = self.screen.text(row, col, end_row,
                                           self.screen.columns())
                command = self._strip_right_prompt(command).rstrip()
            self.output_start = end_row + 1
        else:
            self.output_start = None
        if command and command.strip():
            self.pending = CommandRecord(command, cwd)
        else:
            self.pending = None
        self.state = self.RUNNING

    def on_done(self, exit_status):
        """The running command finished"""
        if self.pending is not None:
            self.pending.exit_status = exit_status
            self.pending.finished = time.time()

    def note_cursor(self):
        """Call when the cursor moves, so we know how far the input spans"""
        if self.state == self.PROMPT:
            row, col = self.screen.cursor()
            # Column 0 of a new row is where Enter leaves the cursor; typing
            # that wraps always leaves it further right.
            if row > self.input_end_row and col > 0:
                self.input_end_row = row

    def _rows_for(self, command):
        """How many screen rows a command typed at the anchor occupies.
        Continuation lines are assumed to have a two-column prompt."""
        columns = max(1, self.screen.columns())
        rows = 0
        for index, line in enumerate(command.split('\n')):
            width = len(line) + (self.anchor[1] if index == 0 else 2)
            rows += max(1, -(-width // columns))
        return rows

    def current_input(self):
        """Return the InputLine being edited, or None if there isn't one
        we can trust (a command is running, the prompt was redrawn, a
        search prompt replaced it, ...)"""
        if self.state != self.PROMPT:
            return None
        row, col = self.screen.cursor()
        arow, acol = self.anchor
        if (row, col) < (arow, acol):
            return None
        if self.screen.text(arow, 0, arow, acol) != self.prompt_prefix:
            return None
        text = self.screen.text(arow, acol, row, col)
        if '\n' in text:
            return None
        after = self.screen.text(row, col, row, self.screen.columns()).strip()
        at_end = after == '' or (row == arow and after == self.right_prompt)
        return InputLine(text, at_end)

    def whole_input(self):
        r"""The whole line being edited, wherever the cursor is in it (Enter
        runs all of it), or None. The part after the cursor is what the
        shell says is there (zsh) or the rest of the line on screen.

        >>> screen = FakeScreen()
        >>> session = ShellSession(screen)
        >>> screen.write('$ '); session.on_prompt(1); session.on_input()
        >>> screen.write('@agent where am i')
        >>> screen.cursor = lambda: (0, 2)    # history search left it here
        >>> session.current_input().text, session.whole_input()
        ('', '@agent where am i')
        >>> session.on_after(15)    # ' i' is a suggestion drawn after the input
        >>> session.whole_input()
        '@agent where am'
        """
        line = self.current_input()
        if line is None:
            return None
        if line.at_end:
            return line.text
        row, col = self.screen.cursor()
        columns = max(1, self.screen.columns())
        rows = (self.after or 0) // columns + 1 if self.after is not None \
            else 50
        rest = self.screen.text(row, col, row + rows, columns)
        # Soft-wrapped rows come back joined; a newline ends the input
        rest = rest.split('\n')[0]
        if self.after is not None:
            rest = rest[:self.after]
        else:
            rest = self._strip_right_prompt(rest).rstrip()
        return line.text + rest

    def _strip_right_prompt(self, text):
        if self.right_prompt and text.rstrip().endswith(self.right_prompt):
            return text.rstrip()[:-len(self.right_prompt)]
        return text

    def _finish_pending(self, prompt_row):
        """Capture the output of the command that just finished: it lies
        between the command line and the new prompt."""
        record, self.pending = self.pending, None
        if record is None:
            return
        if self.output_start is not None:
            last = prompt_row - self.prompt_rows
            if last >= self.output_start:
                output = self.screen.text(self.output_start, 0, last,
                                          self.screen.columns())
                record.output = output.rstrip()[-OUTPUT_LIMIT:]
            else:
                record.output = ''
        self.log.append(record)


class FakeScreen(object):
    """A minimal terminal screen for tests: text plus a cursor, no
    wrapping, no escape sequences"""
    def __init__(self, columns=80):
        self.lines = ['']
        self._columns = columns

    def write(self, text):
        """Append text at the cursor, which is always at the end"""
        for char in text:
            if char == '\n':
                self.lines.append('')
            else:
                self.lines[-1] += char

    def cursor(self):
        return (len(self.lines) - 1, len(self.lines[-1]))

    def columns(self):
        return self._columns

    def text(self, row0, col0, row1, col1):
        parts = []
        for row in range(row0, row1 + 1):
            if row >= len(self.lines):
                break
            line = self.lines[row]
            start = col0 if row == row0 else 0
            end = col1 if row == row1 else len(line)
            parts.append(line[start:end])
        return '\n'.join(parts)
