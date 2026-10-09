#!/usr/bin/env python
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""End-to-end tests for Promptline's shell integration: a real shell in a
real VTE, reporting marks that a Controller turns into a command log."""

import shutil
import time

import pytest
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Vte', '2.91')
from gi.repository import Gdk, GLib, Gtk, Vte

from promptlinelib.promptline import controller as controller_module
from promptlinelib.promptline import marks, shellint
from promptlinelib.promptline.controller import Controller
from promptlinelib.promptline.suggest.history import HistoryStore

pytestmark = pytest.mark.skipif(not marks.INSTALLED,
                                reason='VTE too old for termprops')


class FakeTerminal(object):
    """Just enough of promptlinelib.terminal.Terminal for a Controller"""
    def __init__(self):
        self.vte = Vte.Terminal()
        self.fgcolor_active = Gdk.RGBA(1, 1, 1, 1)
        self.window = Gtk.Window()

    def attach(self, controller):
        self.window.add(controller.wrap(self.vte))
        self.window.show_all()

    def get_cwd(self):
        uri = self.vte.ref_termprop_uri(Vte.TERMPROP_CURRENT_DIRECTORY_URI)
        return GLib.filename_from_uri(uri.to_string())[0] if uri else None


def wait_for(predicate, timeout=10):
    """Run the main loop until predicate() is true"""
    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not predicate():
        if time.monotonic() > deadline:
            return False
        context.iteration(False)
        time.sleep(0.005)
    return True


def start_shell(shell, home, environ, with_controller=False):
    terminal = FakeTerminal()
    controller = Controller(terminal)
    terminal.attach(controller)
    argv, envv = shellint.inject(shell, [shell], ['HOME=%s' % home,
                                                  'TERM=xterm-256color'],
                                 environ=environ)
    terminal.vte.spawn_sync(Vte.PtyFlags.DEFAULT, home, [shell] + argv,
                            envv, GLib.SpawnFlags.FILE_AND_ARGV_ZERO,
                            None, None, None)
    session = controller.session
    assert wait_for(lambda: session.state == session.PROMPT), \
        'shell never reported a prompt'
    if with_controller:
        return terminal, session, controller
    return terminal, session


def run(terminal, session, command):
    count = len(session.log)
    terminal.vte.feed_child((command + '\n').encode())
    assert wait_for(lambda: len(session.log) > count and
                    session.state == session.PROMPT), \
        'no record for %r' % command
    return session.log[-1]


@pytest.fixture(autouse=True)
def history(monkeypatch):
    """Keep tests away from the user's real history"""
    store = HistoryStore(path=None)
    monkeypatch.setattr(controller_module, 'shared_store', lambda: store)
    monkeypatch.setattr(controller_module.promptline, 'enabled',
                        lambda feature=None: True)
    # Never reach a real provider, even if the developer has a key set
    monkeypatch.setattr(controller_module, 'make_provider',
                        lambda purpose: None)
    return store


@pytest.fixture
def home(tmp_path):
    (tmp_path / 'sub dir').mkdir()
    (tmp_path / 'zdot').mkdir()
    (tmp_path / 'zdot' / '.zshrc').write_text("PS1='%~ %# '\n")
    (tmp_path / '.bashrc').write_text("PS1='\\w \\$ '\n")
    return tmp_path


SHELLS = [s for s in ('bash', 'zsh') if shutil.which(s)]


@pytest.mark.parametrize('name', SHELLS)
def test_command_log(name, home):
    environ = {'ZDOTDIR': str(home / 'zdot')}
    terminal, session = start_shell(shutil.which(name), str(home), environ)

    record = run(terminal, session, "echo \"it's; fine\"")
    assert record.command == "echo \"it's; fine\""
    assert record.exit_status == 0
    assert record.output == "it's; fine"
    assert record.cwd == str(home)

    record = run(terminal, session, 'printf "a\\nb\\n"; false')
    assert record.exit_status == 1
    assert record.output == 'a\nb'

    record = run(terminal, session, "cd 'sub dir'")
    record = run(terminal, session, 'pwd')
    assert record.cwd == str(home / 'sub dir')
    assert record.output == str(home / 'sub dir')


@pytest.mark.parametrize('name', SHELLS)
def test_current_input(name, home):
    environ = {'ZDOTDIR': str(home / 'zdot')}
    terminal, session = start_shell(shutil.which(name), str(home), environ)

    terminal.vte.feed_child(b'git che')
    assert wait_for(lambda: session.current_input() is not None and
                    session.current_input().text == 'git che')
    assert session.current_input().at_end

    # Cursor moved left: input is known but we're no longer at its end
    terminal.vte.feed_child(b'\x1b[D')
    assert wait_for(lambda: not session.current_input().at_end)


def press(controller, keyval, state=0):
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.keyval = keyval
    event.state = state
    return controller.on_keypress(event)


def typed(terminal, session, controller, text):
    """Type text and wait until the suggestion has been worked out for it"""
    terminal.vte.feed_child(text.encode())
    assert wait_for(lambda: session.current_input() is not None and
                    session.current_input().text.endswith(text) and
                    controller.refresh_id is None)


@pytest.mark.parametrize('name', SHELLS)
def test_autocomplete(name, home, history):
    environ = {'ZDOTDIR': str(home / 'zdot')}
    terminal, session, controller = start_shell(
        shutil.which(name), str(home), environ, with_controller=True)

    run(terminal, session, 'echo hello big world')
    run(terminal, session, ' echo private')
    assert history.best('echo h') == 'echo hello big world'
    assert history.best('echo p') is None

    typed(terminal, session, controller, 'echo h')
    assert controller.suggestion == ('echo h', 'ello big world')
    assert controller.ghost.text == 'ello big world'

    # Ctrl+Right takes one word, Right takes the rest
    assert press(controller, Gdk.KEY_Right, Gdk.ModifierType.CONTROL_MASK)
    assert wait_for(lambda: session.current_input().text == 'echo hello'
                    and controller.suggestion == ('echo hello', ' big world'))
    assert press(controller, Gdk.KEY_Right)
    assert wait_for(lambda: session.current_input().text ==
                    'echo hello big world' and controller.suggestion is None)
    assert not press(controller, Gdk.KEY_Right)
    run(terminal, session, '')

    # Nothing in history: fall back to completing the path
    typed(terminal, session, controller, 'ls su')
    assert controller.suggestion == ('ls su', 'b\\ dir/')


class FakeOpenAI(object):
    """A local stand-in for the Chat Completions endpoint"""
    def __init__(self):
        import http.server
        import json
        import threading
        server = self
        self.requests = []
        self.reply = lambda body: ''
        self.status = 200

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers['Content-Length'])
                body = json.loads(self.rfile.read(length))
                server.requests.append(body)
                if server.status != 200:
                    payload = {'error': {'message': 'bad key'}}
                else:
                    payload = {'choices': [{'message': {
                        'role': 'assistant', 'content': server.reply(body)}}]}
                data = json.dumps(payload).encode()
                self.send_response(server.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/v1' % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def prompt(self, index=-1):
        return self.requests[index]['messages'][-1]['content']


@pytest.fixture
def fake_openai(monkeypatch):
    from promptlinelib.promptline.providers.openai import OpenAIProvider
    server = FakeOpenAI()
    monkeypatch.setattr(controller_module, 'make_provider',
                        lambda purpose: OpenAIProvider(server.url, None,
                                                       'test-model'))
    yield server
    server.httpd.shutdown()


def test_prediction(home, history, fake_openai):
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run(terminal, session, 'git push 2>/dev/null || echo "no upstream"; false')
    run(terminal, session, ' echo my-secret-thing')

    # Typing: the model completes what was started, using the context
    fake_openai.reply = lambda body: 'git push --set-upstream origin main'
    typed(terminal, session, controller, 'git pu')
    assert wait_for(lambda: controller.suggestion ==
                    ('git pu', 'sh --set-upstream origin main'))
    prompt = fake_openai.prompt()
    assert 'Typed so far: git pu' in prompt
    assert '[exit 1]' in prompt and 'no upstream' in prompt
    assert 'my-secret-thing' not in prompt
    assert fake_openai.requests[-1]['model'] == 'test-model'

    # Typing along the prediction keeps it without asking again
    asked = len(fake_openai.requests)
    typed(terminal, session, controller, 'sh')
    assert controller.suggestion == ('git push', ' --set-upstream origin main')
    time.sleep(0.5)
    assert len(fake_openai.requests) == asked

    # After a command finishes, the empty prompt gets a next-command guess
    terminal.vte.feed_child(b'\x15')    # clear the line
    fake_openai.reply = lambda body: '$ ls -la'
    run(terminal, session, 'echo done')
    assert wait_for(lambda: controller.suggestion == ('', 'ls -la'))
    assert 'Nothing typed yet' in fake_openai.prompt()

    # An @agent question is never sent for prediction
    asked = len(fake_openai.requests)
    typed(terminal, session, controller, '@agent why is it slow')
    time.sleep(0.6)
    assert len(fake_openai.requests) == asked


def test_prediction_auth_failure(home, history, fake_openai):
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    fake_openai.status = 401
    typed(terminal, session, controller, 'git st')
    assert wait_for(lambda: controller.predictor.disabled)
    asked = len(fake_openai.requests)
    typed(terminal, session, controller, 'a')
    time.sleep(0.5)
    assert len(fake_openai.requests) == asked


def screen_text(terminal):
    vte = terminal.vte
    row = vte.get_cursor_position()[1]
    return vte.get_text_range_format(Vte.Format.TEXT, 0, 0, row,
                                     vte.get_column_count())[0]


@pytest.fixture
def agent_setup(monkeypatch, tmp_path, fake_openai):
    """Run the real promptline-agent against the fake server, with its
    runtime files in a temporary directory"""
    import os
    from promptlinelib.promptline import agent as agent_module
    program = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'promptline-agent')
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
    # Personalisation, guardrails and memory for this test only
    (tmp_path / 'config' / 'promptline').mkdir(parents=True)
    (tmp_path / 'config' / 'promptline' / 'personal.md').write_text(
        '<!-- hint -->\n## My role\nSOC analyst\n## Tools I avoid\n')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setenv('PYTHONPATH', os.path.dirname(program))
    monkeypatch.setattr(agent_module, 'agent_program', lambda: program)
    monkeypatch.setattr(controller_module, 'agent_program', lambda: program)
    # Prediction would share the fake server; it's tested separately
    monkeypatch.setattr(controller_module.promptline, 'enabled',
                        lambda feature=None: feature != 'llm_autocomplete')
    monkeypatch.setattr(controller_module, 'provider_settings',
                        lambda purpose: {
                            'promptline_provider': 'openai',
                            'promptline_base_url': fake_openai.url,
                            'promptline_api_key_env': '',
                            'promptline_api_key_file': '',
                            'model': 'agent-model', 'reasoning': ''})
    return fake_openai


def tool_call(name, **args):
    import json
    return {'id': 'call_%s' % name, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


@pytest.mark.parametrize('name', SHELLS)
def test_agent(name, home, history, agent_setup):
    import json
    server = agent_setup
    steps = [
        {'tool_calls': [tool_call('run_command', command='echo agent-ran',
                                  reason='check something')]},
        {'tool_calls': [tool_call('remember',
                                  fact='Uses Nessus for enterprise scans',
                                  replaces='')]},
        {'tool_calls': [tool_call('place_on_prompt', command='cd /tmp')]},
        {'content': 'All done.'},
    ]

    # Let the fake server return whole messages, not just text
    original = server.httpd.RequestHandlerClass.do_POST

    def do_POST(handler):
        length = int(handler.headers['Content-Length'])
        body = json.loads(handler.rfile.read(length))
        server.requests.append(body)
        message = dict({'role': 'assistant', 'content': None},
                       **steps[len(server.requests) - 1])
        data = json.dumps({'choices': [{'message': message}]}).encode()
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    server.httpd.RequestHandlerClass.do_POST = do_POST
    try:
        terminal, session, controller = start_shell(
            shutil.which(name), str(home), {'ZDOTDIR': str(home / 'zdot')},
            with_controller=True)
        run(terminal, session, 'ls /nonexistent-dir')

        typed(terminal, session, controller, "@agent what's wrong?")
        assert press(controller, Gdk.KEY_Return)
        # Starting the agent program is the slow part on a loaded machine
        assert wait_for(lambda: '[a]pprove' in screen_text(terminal),
                        timeout=30), screen_text(terminal)
        terminal.vte.feed_child(b'a')
        assert wait_for(lambda: session.state == session.PROMPT and
                        session.current_input() is not None and
                        session.current_input().text == 'cd /tmp',
                        timeout=20), screen_text(terminal)

        screen = screen_text(terminal)
        assert "@agent what's wrong?" in screen
        assert '_promptline_agent' not in screen
        assert 'agent-ran' in screen and 'All done.' in screen

        first = server.requests[0]
        assert first['model'] == 'agent-model'
        assert [t['function']['name'] for t in first['tools']] == \
            ['run_command', 'place_on_prompt', 'remember', 'forget']
        # Personalisation reaches the agent (hints and empty sections don't)
        system = first['messages'][0]['content']
        assert 'About the user, in their own words:\n## My role\nSOC analyst' \
            in system
        assert 'hint' not in system and 'Tools I avoid' not in system
        # ...and the memory it saved is on disk and was announced
        memory = home / 'data' / 'promptline' / 'memory.md'
        assert '- Uses Nessus for enterprise scans' in memory.read_text()
        assert 'Remembered: Uses Nessus for enterprise scans' in screen
        request = first['messages'][-1]['content']
        assert "Request: what's wrong?" in request
        assert 'ls /nonexistent-dir' in request and '[exit 2]' in request
        result = json.loads(server.requests[1]['messages'][-1]['content'])
        assert result == {'exit_status': 0, 'output': 'agent-ran',
                          'approved': 'by the user'}

        # History has the question, not the launcher; the agent's run isn't
        # learned as a command
        terminal.vte.feed_child(b'\x15')
        record = run(terminal, session, 'fc -ln -5')
        assert "@agent what's wrong?" in record.output
        assert '_promptline_agent' not in record.output
        assert history.best('_promptline') is None
    finally:
        server.httpd.RequestHandlerClass.do_POST = original


def test_termprops_registered_before_first_terminal():
    """VTE refuses new termprops once a terminal exists, so they must be
    registered when terminal.py is imported. Run in a fresh process: this
    test module has already imported marks itself."""
    import subprocess
    import sys
    code = ('import gi\n'
            'gi.require_version("Gtk", "3.0")\n'
            'gi.require_version("Gdk", "3.0")\n'
            'import promptlinelib.terminal\n'
            'from gi.repository import Gtk, Vte\n'
            'window = Gtk.Window()\n'
            'window.add(Vte.Terminal())\n'
            'window.show_all()\n'
            'from promptlinelib.promptline import available\n'
            'print(available(), Vte.query_termprop("vte.ext.promptline.exec")[0])\n')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                            text=True, timeout=60)
    assert result.stdout.split() == ['True', 'True'], result.stderr
    assert 'CRITICAL' not in result.stderr


def scripted(server, steps, review=None, delay=0):
    """Make the fake server play `steps` as the agent's replies (each after
    `delay` seconds), and answer auto-review requests with
    review(command) -> (verdict, reason)"""
    import json

    def do_POST(handler):
        length = int(handler.headers['Content-Length'])
        body = json.loads(handler.rfile.read(length))
        system = body['messages'][0]['content']
        if system.startswith('You review shell commands'):
            command = body['messages'][-1]['content'].split(
                'Command: ')[1].split('\n')[0]
            verdict, reason = review(command)
            message = {'role': 'assistant',
                       'content': '%s: %s' % (verdict.upper(), reason)}
            server.reviews.append(command)
        else:
            server.requests.append(body)
            time.sleep(delay)
            message = dict({'role': 'assistant', 'content': None},
                           **steps[len(server.requests) - 1])
        data = json.dumps({'choices': [{'message': message}]}).encode()
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    server.reviews = []
    server.httpd.RequestHandlerClass.do_POST = do_POST


def run_agent(terminal, session, controller, question):
    typed(terminal, session, controller, '@agent ' + question)
    assert press(controller, Gdk.KEY_Return)


def agent_done(terminal, session):
    return wait_for(lambda: session.state == session.PROMPT and
                    'All done.' in screen_text(terminal), timeout=30)


def audit_lines(home):
    log = home / 'data' / 'promptline' / 'agent-audit.log'
    return [line.split('\t') for line in log.read_text().splitlines()]


def test_agent_full_permission(home, history, agent_setup, monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'full',
        'promptline_review_reasoning': 'low'})
    (home / 'config' / 'promptline' / 'guardrails.md').write_text(
        '- Never touch production\n- No scans outside 10.20.0.0/16\n'
        '- Ask before deleting anything\n')
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo unasked',
                                  reason='harmless')]},
        {'tool_calls': [tool_call('run_command', command='sudo reboot',
                                  reason='apply changes')]},
        {'content': 'All done.'},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'do it')
    # The hard stop still asks, even in full permission mode
    assert wait_for(lambda: 'Always asks: shuts down' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'c')
    assert agent_done(terminal, session), screen_text(terminal)

    screen = screen_text(terminal)
    assert 'Full permission mode:' in screen
    assert 'unasked' in screen
    system = agent_setup.requests[0]['messages'][0]['content']
    assert 'full permission mode' in system
    assert '- Ask before deleting anything' in system
    declined = agent_setup.requests[2]['messages'][-1]['content']
    assert declined.startswith('The user declined') and 'shuts down' in declined
    [(_, mode, how, status, _cwd, command)] = audit_lines(home)
    assert (mode, how, status, command) == ('full', 'full', 'exit=0',
                                            'echo unasked')


def test_agent_full_permission_locked(home, history, agent_setup,
                                      monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'full',
        'promptline_review_reasoning': 'low'})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo hi',
                                  reason='check')]},
        {'content': 'All done.'},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'hello')
    assert wait_for(lambda: '[a]pprove' in screen_text(terminal), timeout=30)
    assert 'Full permission mode is locked' in screen_text(terminal)
    terminal.vte.feed_child(b'a')
    assert agent_done(terminal, session)
    # The agent was told it is in ask mode, not full
    system = agent_setup.requests[0]['messages'][0]['content']
    assert 'The user approves each one' in system
    assert 'full permission mode' not in system


def test_agent_auto_review(home, history, agent_setup, monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'auto-review',
        'promptline_review_reasoning': 'low'})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo looked',
                                  reason='read-only check')]},
        {'tool_calls': [tool_call('run_command',
                                  command='echo pretend-restart',
                                  reason='restart the service')]},
        {'content': 'All done.'},
    ], review=lambda command: ('safe', 'only prints text')
       if 'looked' in command else ('ask', 'restarts a service'))
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'check and restart')
    assert wait_for(lambda: 'Reviewer: restarts a service' in
                    screen_text(terminal), timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'a')
    assert agent_done(terminal, session), screen_text(terminal)

    screen = screen_text(terminal)
    assert '$ echo looked\n  reviewer: only prints text' in screen
    assert '$ echo pretend-restart\nReviewer: restarts a service\n' \
        '  [a]pprove' in screen
    assert agent_setup.reviews == ['echo looked', 'echo pretend-restart']
    assert [(line[2], line[5]) for line in audit_lines(home)] == [
        ('auto-review', 'echo looked'), ('approved', 'echo pretend-restart')]


def test_agent_streams_replies(home, history, agent_setup):
    import json
    import threading
    server = agent_setup
    release = threading.Event()

    def do_POST(handler):
        length = int(handler.headers['Content-Length'])
        body = json.loads(handler.rfile.read(length))
        server.requests.append(body)
        assert body.get('stream') is True
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.end_headers()

        def send(delta):
            chunk = {'choices': [{'delta': delta}]}
            handler.wfile.write(('data: %s\n\n' % json.dumps(chunk)).encode())
            handler.wfile.flush()
        send({'content': 'First half is here'})
        release.wait(20)            # the rest only after the test has looked
        send({'content': ', and All done.'})
        handler.wfile.write(b'data: [DONE]\n\n')
        handler.close_connection = True
    server.httpd.RequestHandlerClass.do_POST = do_POST

    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'stream please')
    try:
        assert wait_for(lambda: 'First half is here' in screen_text(terminal),
                        timeout=30), screen_text(terminal)
        assert 'All done.' not in screen_text(terminal)
    finally:
        release.set()
    assert agent_done(terminal, session), screen_text(terminal)
    assert 'First half is here, and All done.' in screen_text(terminal)


def approve(terminal):
    assert wait_for(lambda: '[a]pprove' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'a')


def test_agent_command_gets_a_terminal(home, history, agent_setup):
    """Installer dialogs (debconf's whiptail) read single raw keys from a
    terminal; the agent's commands must get one, and the user's keys"""
    # The marker is computed, so the command shown for approval can't match
    dialog = ('[ -t 1 ] && stty raw -echo && echo raw-$((6*7)); '
              'key=$(dd bs=1 count=1 2>/dev/null); stty sane; '
              'printf "\\033[1mgot-%s\\033[0m\\n" "$key"')
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command=dialog,
                                  reason='ask a question')]},
        {'content': 'All done.'},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'install it')
    approve(terminal)
    assert wait_for(lambda: 'raw-42' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'y')
    assert agent_done(terminal, session), screen_text(terminal)
    assert 'got-y' in screen_text(terminal)
    result = agent_setup.requests[1]['messages'][-1]['content']
    # The model gets the text without the terminal's escape codes
    assert 'got-y' in result and '\\u001b' not in result, result


def test_agent_command_ctrl_c(home, history, agent_setup):
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command',
                                  command='echo started; sleep 60',
                                  reason='wait')]},
        {'content': 'All done.'},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'wait')
    approve(terminal)
    assert wait_for(lambda: 'started' in screen_text(terminal), timeout=30)
    terminal.vte.feed_child(b'\x03')
    # One Ctrl+C stops the command and the agent, and the shell is back
    assert wait_for(lambda: 'Interrupted.' in screen_text(terminal) and
                    session.state == session.PROMPT, timeout=5), \
        screen_text(terminal)
    assert len(agent_setup.requests) == 1


@pytest.mark.parametrize('recall', ['ctrl-a'] + (
    ['zsh-up-arrow'] if shutil.which('zsh') else []))
@pytest.mark.parametrize('name', SHELLS)
def test_agent_with_cursor_not_at_end(name, recall, home, history,
                                      agent_setup):
    """Enter runs the whole line, wherever the cursor is: zsh's
    history-beginning-search-backward recalls a line with the cursor left at
    its start"""
    if recall == 'zsh-up-arrow' and name != 'zsh':
        pytest.skip('zsh key binding')
    (home / '.zsh_history').write_text('@agent where am i\n')
    (home / '.zshrc').write_text(
        'HISTFILE=~/.zsh_history; HISTSIZE=100; SAVEHIST=100\n'
        "bindkey '^[[A' history-beginning-search-backward\n")
    scripted(agent_setup, [{'content': 'All done.'}])
    terminal, session, controller = start_shell(
        shutil.which(name), str(home), {}, with_controller=True)
    if recall == 'ctrl-a':
        typed(terminal, session, controller, '@agent where am i')
        terminal.vte.feed_child(b'\x01')
    else:
        terminal.vte.feed_child(b'\x1b[A')
    assert wait_for(lambda: '@agent where am i' in screen_text(terminal) and
                    session.current_input() is not None and
                    not session.current_input().at_end), screen_text(terminal)
    assert press(controller, Gdk.KEY_Return)
    assert agent_done(terminal, session), screen_text(terminal)
    assert agent_setup.requests[0]['messages'][-1]['content'].endswith(
        'where am i')


def test_agent_streams_reviews(home, history, agent_setup, monkeypatch):
    """The command shows at once, and the review prints as it arrives"""
    import json
    import threading
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'auto-review',
        'promptline_review_reasoning': 'low'})
    server = agent_setup
    release = threading.Event()
    steps = [
        {'tool_calls': [tool_call('run_command', command='echo looked',
                                  reason='read-only check')]},
        {'content': 'All done.'},
    ]

    def do_POST(handler):
        length = int(handler.headers['Content-Length'])
        body = json.loads(handler.rfile.read(length))
        if not body['messages'][0]['content'].startswith('You review'):
            server.requests.append(body)
            message = dict({'role': 'assistant', 'content': None},
                           **steps[len(server.requests) - 1])
            data = json.dumps({'choices': [{'message': message}]}).encode()
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        assert body.get('stream') is True
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.end_headers()

        def send(text):
            chunk = {'choices': [{'delta': {'content': text}}]}
            handler.wfile.write(('data: %s\n\n' % json.dumps(chunk)).encode())
            handler.wfile.flush()
        send('SAFE: it only')
        release.wait(20)            # the rest only after the test has looked
        send(' prints text.')
        handler.wfile.write(b'data: [DONE]\n\n')
        handler.close_connection = True
    server.httpd.RequestHandlerClass.do_POST = do_POST

    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'check')
    try:
        assert wait_for(lambda: 'reviewer: it only' in screen_text(terminal),
                        timeout=30), screen_text(terminal)
        assert '$ echo looked' in screen_text(terminal)
        assert 'prints text' not in screen_text(terminal)
    finally:
        release.set()
    assert agent_done(terminal, session), screen_text(terminal)
    screen = screen_text(terminal)
    assert '$ echo looked\n  reviewer: it only prints text.\nlooked' in screen


def test_agent_step_limit_is_a_setting(home, history, agent_setup,
                                       monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_agent_max_steps': 2})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo one',
                                  reason='r')]},
        {'tool_calls': [tool_call('run_command', command='echo two',
                                  reason='r')]},
        {'content': 'never reached'},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'keep going')
    for _ in range(2):
        assert wait_for(lambda: screen_text(terminal).count('[a]pprove') >
                        _, timeout=30), screen_text(terminal)
        terminal.vte.feed_child(b'a')
    assert wait_for(lambda: 'Stopped after 2 steps' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    assert len(agent_setup.requests) == 2


def test_agent_goal_runs_until_it_is_reached(home, history, agent_setup,
                                             monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_agent_max_steps': 1,
        'promptline_goal_approval_wait': 0})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo first',
                                  reason='r')]},
        {'tool_calls': [tool_call('run_command', command='echo second',
                                  reason='r')]},
        {'tool_calls': [tool_call('goal_complete',
                                  summary='All done. Both ran.')]},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, '--goal run two commands')
    for _ in range(2):
        assert wait_for(lambda: screen_text(terminal).count('[a]pprove') >
                        _, timeout=30), screen_text(terminal)
        terminal.vte.feed_child(b'a')
    assert agent_done(terminal, session), screen_text(terminal)

    # The step limit of 1 doesn't apply to a goal
    assert len(agent_setup.requests) == 3
    first = agent_setup.requests[0]
    assert 'goal_complete' in [t['function']['name'] for t in first['tools']]
    assert 'You have been given a GOAL' in first['messages'][0]['content']
    assert first['messages'][-1]['content'].endswith(
        'Request: Goal: run two commands')
    screen = screen_text(terminal)
    assert 'Goal reached. All done. Both ran.' in screen
    assert '@agent --goal run two commands' in screen


def test_agent_can_be_steered_while_it_thinks(home, history, agent_setup,
                                              monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low'})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo one',
                                  reason='r')]},
        {'content': 'All done.'},
    ], delay=2)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'do the thing')
    assert wait_for(lambda: 'Type to steer' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'use port 8080 instead\r')
    assert wait_for(lambda: '[a]pprove' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'a')
    assert agent_done(terminal, session), screen_text(terminal)

    follow_up = agent_setup.requests[1]['messages']
    assert follow_up[-1]['content'] == \
        'The user sent this while you were working: use port 8080 instead'
    assert 'You: use port 8080 instead' in screen_text(terminal)
    # It was read by the agent, not left for the shell to run
    assert 'use port 8080' not in [r.command for r in session.log]


def test_agent_goal_survives_ctrl_c_and_resumes(home, history, agent_setup,
                                                monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_goal_approval_wait': 0})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo one',
                                  reason='r')]},
        {'tool_calls': [tool_call('goal_complete', summary='All done.')]},
    ])
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, '--goal tidy the repo')
    assert wait_for(lambda: '[a]pprove' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'\x03')
    assert wait_for(lambda: 'Interrupted. Say "@agent --goal" to resume' in
                    screen_text(terminal) and
                    session.state == session.PROMPT,
                    timeout=30), screen_text(terminal)

    # A bare --goal picks the unfinished goal up where it stopped
    run_agent(terminal, session, controller, '--goal')
    assert agent_done(terminal, session), screen_text(terminal)
    resumed = agent_setup.requests[1]['messages']
    assert resumed[-1]['content'].endswith(
        'Request: Carry on towards the goal: tidy the repo')
    assert any('Goal: tidy the repo' in (m.get('content') or '')
               for m in resumed)
    # The command that was never answered isn't left hanging in the history
    assert not any(m.get('tool_calls') for m in resumed)


def team(server, main=None):
    """Make the fake server answer as a team of subagents and the agent,
    by what each is told it is. main(messages) is the agent's reply."""
    import json

    def do_POST(handler):
        length = int(handler.headers['Content-Length'])
        body = json.loads(handler.rfile.read(length))
        server.requests.append(body)
        messages = body['messages']
        system, step = messages[0]['content'], len(messages)
        message = {'role': 'assistant', 'content': None}
        if 'You make plans' in system:
            message['content'] = ('- Is the service up?\nApproach: just '
                                  'look.')
        elif 'You investigate one question' in system:
            if step == 2:
                message['tool_calls'] = [tool_call(
                    'run_command', command='echo explored', reason='look')]
            else:
                message['content'] = 'It is up.'
        elif 'You carry out the task' in system:
            if step == 2:
                message['tool_calls'] = [tool_call(
                    'run_command', command='echo changed', reason='apply')]
            else:
                message['content'] = 'Changed it.'
        elif 'You are a critical reviewer' in system:
            message['content'] = 'PASS: matches the request'
        elif 'You check independently' in system:
            message['content'] = 'PASS: it works'
        else:
            message.update((main or (lambda m: {'content': 'All done.'}))(
                messages))
        data = json.dumps({'choices': [{'message': message}]}).encode()
        handler.send_response(200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    server.httpd.RequestHandlerClass.do_POST = do_POST


def roles(server):
    markers = (('You make plans', 'planner'),
               ('You investigate one question', 'explorer'),
               ('You carry out the task', 'worker'),
               ('You are a critical reviewer', 'reviewer'),
               ('You check independently', 'verifier'))
    found = []
    for request in server.requests:
        system = request['messages'][0]['content']
        found.append(next((role for marker, role in markers
                           if marker in system), 'agent'))
    return found


def test_agent_ultra_runs_a_team_on_a_fixed_workflow(home, history,
                                                     agent_setup,
                                                     monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_subagents': 'off'})       # ultra ignores this
    team(agent_setup)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, '--ultra check the service')
    # The worker changes things, so the user is asked, and told who asks
    assert wait_for(lambda: '[a]pprove' in screen_text(terminal),
                    timeout=60), screen_text(terminal)
    assert '[worker]' in screen_text(terminal)
    terminal.vte.feed_child(b'a')
    assert agent_done(terminal, session), screen_text(terminal)

    screen = screen_text(terminal)
    assert 'Ultra: maximum reasoning' in screen
    for stage in ('plan', 'explore', 'act', 'check'):
        assert 'Stage %d of 4: %s' % (
            ['plan', 'explore', 'act', 'check'].index(stage) + 1,
            stage) in screen
    assert '[explorer 1/1] done (1 command)' in screen
    # Every request asked for the most reasoning, and the agent's own came
    # last, with the team's record
    assert all(r.get('reasoning_effort') == 'xhigh'
               for r in agent_setup.requests)
    assert sorted(set(roles(agent_setup))) == [
        'agent', 'explorer', 'planner', 'reviewer', 'verifier', 'worker']
    assert roles(agent_setup)[-1] == 'agent'
    final = agent_setup.requests[-1]['messages'][-1]['content']
    assert 'ultra mode' in final and '## plan' in final
    assert '### Is the service up?\nIt is up.' in final and '## act' in final
    # Their commands are in the audit log, with who ran them and how
    hows = dict((line[5], line[2]) for line in audit_lines(home))
    assert hows == {'echo explored': 'explorer 1/1:read-only',
                    'echo changed': 'worker:approved'}


def test_agent_subagents_can_be_switched_on_without_ultra(home, history,
                                                          agent_setup,
                                                          monkeypatch):
    import json
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_subagents': 'auto',
        'promptline_subagent_parallel': 2})

    def main(messages):
        if messages[-1]['role'] == 'user':
            return {'tool_calls': [{'id': 'd1', 'type': 'function',
                                    'function': {'name': 'delegate',
                                                 'arguments': json.dumps({
                                                     'agent': 'explorer',
                                                     'task': 'is it up?'})}}]}
        return {'content': 'All done. ' + messages[-1]['content']}
    team(agent_setup, main)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'is the service up?')
    assert agent_done(terminal, session), screen_text(terminal)

    first = agent_setup.requests[0]
    assert 'delegate' in [t['function']['name'] for t in first['tools']]
    assert 'hand tasks to subagents' in first['messages'][0]['content']
    assert roles(agent_setup) == ['agent', 'explorer', 'explorer', 'agent']
    assert 'reasoning_effort' not in first       # no reasoning level set
    assert '[explorer] done (1 command)' in screen_text(terminal)
    assert 'It is up.' in screen_text(terminal)
    assert 'Ultra' not in screen_text(terminal)


def test_agent_a_users_own_workflow_runs_with_always(home, history,
                                                     agent_setup,
                                                     monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_subagents': 'always',
        'promptline_subagent_flow': 'peek'})
    (home / 'config' / 'promptline' / 'flows').mkdir()
    (home / 'config' / 'promptline' / 'flows' / 'peek.md').write_text(
        'look: explorer -> Have a look.\n')
    team(agent_setup)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'is the service up?')
    assert agent_done(terminal, session), screen_text(terminal)
    assert roles(agent_setup) == ['explorer', 'explorer', 'agent']
    assert 'Stage 1 of 1: look' in screen_text(terminal)
    assert 'Use subagents' not in screen_text(terminal)
    system = agent_setup.requests[-1]['messages'][0]['content']
    assert 'Work with subagents on every request' in system


def test_agent_reports_a_workflow_that_cannot_run(home, history, agent_setup,
                                                  monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'ask',
        'promptline_review_reasoning': 'low',
        'promptline_subagents': 'always',
        'promptline_subagent_flow': 'nope'})
    team(agent_setup)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'hello')
    assert wait_for(lambda: 'no flow called' in screen_text(terminal) and
                    session.state == session.PROMPT,
                    timeout=30), screen_text(terminal)
    assert agent_setup.requests == []       # nothing was asked of the model


def test_agent_a_stop_skips_a_command_about_to_run_unseen(home, history,
                                                          agent_setup,
                                                          monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'auto-review',
        'promptline_review_reasoning': 'low'})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo must-not-run',
                                  reason='check')]},
        {'content': 'All done.'},
    ], review=lambda command: ('safe', 'only prints text'), delay=2)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'do the thing')
    assert wait_for(lambda: 'Type to steer' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'stop, leave it alone\r')
    assert agent_done(terminal, session), screen_text(terminal)

    screen = screen_text(terminal)
    assert 'Not run, because you said stop: echo must-not-run' in screen
    assert not (home / 'data' / 'promptline' / 'agent-audit.log').exists()
    last = agent_setup.requests[1]['messages'][-1]['content']
    assert last.startswith('The user sent this while you were working: stop, '
                           'leave it alone') and 'told you to stop' in last


def test_agent_ordinary_guidance_lets_an_unseen_command_run(home, history,
                                                            agent_setup,
                                                            monkeypatch):
    monkeypatch.setattr(controller_module, 'Config', lambda: {
        'promptline_agent_mode': 'auto-review',
        'promptline_review_reasoning': 'low'})
    scripted(agent_setup, [
        {'tool_calls': [tool_call('run_command', command='echo did-run',
                                  reason='check')]},
        {'content': 'All done.'},
    ], review=lambda command: ('safe', 'only prints text'), delay=2)
    terminal, session, controller = start_shell(
        shutil.which('bash'), str(home), {}, with_controller=True)
    run_agent(terminal, session, controller, 'do the thing')
    assert wait_for(lambda: 'Type to steer' in screen_text(terminal),
                    timeout=30), screen_text(terminal)
    terminal.vte.feed_child(b'by the way, the host is 10.0.0.5\r')
    assert agent_done(terminal, session), screen_text(terminal)

    assert 'Not run' not in screen_text(terminal)
    assert [line[5] for line in audit_lines(home)] == ['echo did-run']
    last = agent_setup.requests[1]['messages'][-1]['content']
    assert last == ('The user sent this while you were working: by the way, '
                    'the host is 10.0.0.5')
