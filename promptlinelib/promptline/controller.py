# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""controller.py - per-terminal glue between VTE and Promptline

One Controller is attached to each Terminal. It feeds shell marks from VTE
into a ShellSession, shows and accepts inline suggestions, and launches
@agent.
"""

import os

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import GLib, Gdk, Gtk, Vte

from ..config import Config
from ..signalman import Signalman
from ..util import dbg
from .. import promptline
from . import marks
from .agent import agent_program, parse_invocation, take_prefill, \
    write_request
from .ghost import GhostText
from . import personal
from .providers import make_provider, provider_settings
from .session import ShellSession
from .suggest import Suggester
from .suggest.history import shared_store
from .suggest.llm import Predictor, build_messages

# Exit status the shells use for "command not found": not worth learning
NOT_FOUND = 127
# Wait for a pause in typing before asking the model
PREDICT_DELAY_MS = 350
PREDICT_MIN_TYPED = 2
AGENT_LAUNCHER = '_promptline_agent'


def setting(key):
    """A Promptline setting; None if there is no such key (a stand-in
    Config in a test may leave some out)"""
    try:
        return Config()[key]
    except KeyError:
        return None


class VteScreen(object):
    """The screen interface ShellSession expects, backed by a Vte.Terminal"""
    def __init__(self, vte):
        self.vte = vte

    def cursor(self):
        column, row = self.vte.get_cursor_position()
        return (row, column)

    def columns(self):
        return self.vte.get_column_count()

    def text(self, row0, col0, row1, col1):
        text = self.vte.get_text_range_format(Vte.Format.TEXT, row0, col0,
                                              row1, col1)[0]
        return text or ''


class Controller(object):
    """Promptline state for one Terminal"""
    def __init__(self, terminal):
        self.terminal = terminal
        self.vte = terminal.vte
        self.session = ShellSession(VteScreen(self.vte))
        self.history = shared_store()
        self.suggester = Suggester(self.history)
        self.ghost = None
        self.suggestion = None      # (input it extends, suffix)
        self.refresh_id = None
        self.last_learned = None
        self.predictor = Predictor(lambda: make_provider('autocomplete'))
        self.prediction = None      # full command line the model predicted
        self.predict_id = None
        self.predicted_for = None   # input the last request was made for
        self.agent_token = None     # the @agent request running here
        self.agent_started = False
        self.cnxids = Signalman()
        self.cnxids.new(self.vte, 'termprops-changed',
                        self.on_termprops_changed)
        self.cnxids.new(self.vte, 'cursor-moved', self.on_cursor_moved)
        self.cnxids.new(self.vte, 'contents-changed', self.schedule_refresh)
        self.cnxids.new(self.vte, 'selection-changed', self.schedule_refresh)
        self.cnxids.new(self.vte.get_vadjustment(), 'value-changed',
                        self.schedule_refresh)

    def wrap(self, vte):
        """Return the widget to pack in place of the VTE: the VTE with the
        suggestion layer over it"""
        overlay = Gtk.Overlay()
        overlay.add(vte)
        self.ghost = GhostText(vte, lambda: self.terminal.fgcolor_active)
        overlay.add_overlay(self.ghost)
        overlay.set_overlay_pass_through(self.ghost, True)
        overlay.show_all()
        return overlay

    def destroy(self):
        """Disconnect from the VTE"""
        self.cnxids.remove_all()
        for source in (self.refresh_id, self.predict_id):
            if source is not None:
                GLib.source_remove(source)
        self.refresh_id = self.predict_id = None

    def on_termprops_changed(self, vte, props, _count):
        """Collect our marks from this batch and apply them in order"""
        values = {}
        for prop in props:
            name = Vte.query_termprop_by_id(prop)[1]
            if name.startswith(marks.PREFIX):
                values[name] = vte.get_termprop_string_by_id(prop)[0]
        for name in marks.ORDER:
            if name in values:
                self.handle_mark(name, values[name])
        return False

    def handle_mark(self, name, value):
        """Apply one shell mark to the session"""
        dbg('promptline mark %s=%r' % (name[len(marks.PREFIX):], value))
        session = self.session
        if name == marks.EXEC:
            self.reset_prediction()
            if self.agent_token is not None:
                self.agent_started = True
            session.on_exec(marks.decode_command(value),
                            self.terminal.get_cwd())
        elif name == marks.DONE:
            session.on_done(marks.parse_int(value))
        elif name == marks.PROMPT:
            session.on_prompt(marks.parse_int(value, 1))
        elif name == marks.INPUT:
            self.reset_prediction()
            session.on_input()
            if session.log:
                self.learn(session.log[-1])
            if self.agent_token is not None and self.agent_started:
                self.agent_finished()
        elif name == marks.AFTER:
            session.on_after(marks.parse_int(value))
        self.schedule_refresh()

    def learn(self, record):
        """Add a finished command to the suggestion history"""
        if record is self.last_learned:
            return
        self.last_learned = record
        if record.command.startswith(AGENT_LAUNCHER + ' '):
            # The agent's own run: its transcript isn't the user's context
            record.private = True
        dbg('promptline command %r exited %s with %d chars output' %
            (record.command, record.exit_status, len(record.output or '')))
        if record.private or record.exit_status == NOT_FOUND:
            return
        self.history.add(record.command, record.cwd, record.exit_status)

    def on_cursor_moved(self, _vte):
        self.session.note_cursor()
        self.schedule_refresh()

    def schedule_refresh(self, *_args):
        """Recompute the suggestion once the current batch of changes is in,
        before GTK redraws"""
        if self.ghost is not None and self.refresh_id is None:
            self.refresh_id = GLib.idle_add(self.refresh,
                                            priority=GLib.PRIORITY_HIGH_IDLE)

    def scrolled_back(self):
        adjustment = self.vte.get_vadjustment()
        return adjustment.get_value() + adjustment.get_page_size() < \
            adjustment.get_upper() - 0.5

    def refresh(self):
        """Show the best suggestion for what's typed, or nothing"""
        self.refresh_id = None
        suffix = None
        line = self.session.current_input()
        if (line is not None and line.at_end and
                not self.vte.get_has_selection() and
                not self.scrolled_back()):
            cwd = self.terminal.get_cwd()
            prediction = self.prediction
            if prediction and prediction.startswith(line.text) and \
                    len(prediction) > len(line.text):
                suffix = prediction[len(line.text):]
            elif promptline.enabled('autocomplete'):
                suffix = self.suggester.suggest(line.text, cwd)
            if promptline.enabled('llm_autocomplete'):
                self.schedule_prediction(line.text, cwd)
        if suffix:
            suffix = suffix.split('\n')[0]
            row, column = self.session.screen.cursor()
            room = self.vte.get_column_count() - column
            if self.session.right_prompt and row == self.session.anchor[0]:
                room -= len(self.session.right_prompt) + 1
            if room > 0:
                self.suggestion = (line.text, suffix)
                self.ghost.show_text(suffix[:room], row, column)
                return False
        self.suggestion = None
        self.ghost.clear()
        return False

    def reset_prediction(self):
        """Forget the prediction: the context it was made in has changed"""
        self.prediction = self.predicted_for = None
        if self.predict_id is not None:
            GLib.source_remove(self.predict_id)
            self.predict_id = None

    def schedule_prediction(self, typed, cwd):
        """Ask the model about typed once the user pauses, unless there's
        nothing worth asking"""
        if typed == self.predicted_for:
            return
        self.predicted_for = typed
        if self.predict_id is not None:
            GLib.source_remove(self.predict_id)
            self.predict_id = None
        prediction = self.prediction
        if prediction and prediction.startswith(typed) and \
                len(prediction) > len(typed):
            return      # still typing along the last prediction
        if typed.lstrip().startswith('@'):
            return      # an @agent question, not a command
        if typed.strip():
            if len(typed.strip()) < PREDICT_MIN_TYPED:
                return
        elif not (self.session.log and promptline.enabled('predict_next')):
            return
        last = self.session.log[-1] if self.session.log else None
        key = (typed, cwd, id(last))
        cached = self.predictor.cached(key)
        if cached is not False:
            self.prediction = cached
            return
        self.predict_id = GLib.timeout_add(PREDICT_DELAY_MS, self.predict,
                                           key, typed, cwd)

    def predict(self, key, typed, cwd):
        self.predict_id = None
        messages = build_messages(typed, cwd, list(self.session.log),
                                  personal=personal.personal_text(),
                                  memory=personal.Memory().text())
        self.predictor.request(key, typed, messages, self.on_prediction)
        return False

    def on_prediction(self, _key, typed, prediction):
        line = self.session.current_input()
        if prediction and line is not None and \
                prediction.startswith(line.text) and \
                line.text.startswith(typed):
            self.prediction = prediction
            self.schedule_refresh()

    def on_keypress(self, event):
        """Launch @agent on Enter; accept the suggestion on Right/End (all
        of it) or Ctrl+Right (the next word; Alt+Right is Terminator's
        go_right). Returns True if the key was consumed."""
        modifiers = event.state & Gtk.accelerator_get_default_mod_mask()
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                modifiers == 0:
            return self.launch_agent()
        if self.suggestion is None:
            return False
        right = event.keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right)
        end = event.keyval in (Gdk.KEY_End, Gdk.KEY_KP_End)
        if (right or end) and modifiers == 0:
            whole = True
        elif right and modifiers == Gdk.ModifierType.CONTROL_MASK:
            whole = False
        else:
            return False

        base, suffix = self.suggestion
        line = self.session.current_input()
        if line is None or not line.at_end or line.text != base:
            return False
        if not whole:
            stripped = suffix.lstrip(' ')
            word_end = stripped.find(' ')
            if word_end != -1:
                suffix = suffix[:len(suffix) - len(stripped) + word_end]
        self.suggestion = None
        self.ghost.clear()
        self.vte.feed_child(suffix.encode('utf-8'))
        return True

    def launch_agent(self):
        """If the line being entered is '@agent ...', hand it to the agent
        instead of the shell. Returns True if it did."""
        line = self.session.current_input()
        if line is None:
            return False
        query = parse_invocation(self.session.whole_input())
        if query is None or agent_program() is None:
            return False
        request = {
            'query': query,
            'prompt_prefix': self.session.prompt_prefix,
            'cwd': self.terminal.get_cwd(),
            'shell': self.shell(),
            'terminal': getattr(getattr(self.terminal, 'uuid', None), 'urn',
                                None),
            'records': [record.as_dict() for record in self.session.log
                        if not record.private],
            'settings': provider_settings('agent'),
            'agent': {
                'mode': Config()['promptline_agent_mode'],
                'review_reasoning': Config()['promptline_review_reasoning'],
                'max_steps': setting('promptline_agent_max_steps'),
                'context_window': setting('promptline_context_window'),
                'subagents': setting('promptline_subagents'),
                'subagent_flow': setting('promptline_subagent_flow'),
                'subagent_parallel': setting('promptline_subagent_parallel'),
                'approval_wait': setting('promptline_goal_approval_wait'),
            },
        }
        try:
            token = write_request(request, query)
        except OSError as ex:
            dbg('promptline: unable to write agent request: %s' % ex)
            return False
        self.agent_token, self.agent_started = token, False
        self.suggestion = None
        self.ghost.clear()
        # Ctrl+E Ctrl+U empties the line in bash and zsh; the leading space
        # keeps the launcher out of history
        self.vte.feed_child(b'\x05\x15')
        self.vte.feed_child((' %s %s\n' % (AGENT_LAUNCHER, token)).encode())
        return True

    def agent_finished(self):
        """The agent exited: type anything it left for the prompt"""
        token, self.agent_token = self.agent_token, None
        text = take_prefill(token)
        if text:
            self.vte.feed_child(text.split('\n')[0].encode('utf-8'))

    def shell(self):
        """The shell running in this terminal"""
        pid = getattr(self.terminal, 'pid', None)
        if pid:
            try:
                return os.readlink('/proc/%d/exe' % pid)
            except OSError:
                pass
        return os.environ.get('SHELL')
