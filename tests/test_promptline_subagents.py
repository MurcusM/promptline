#!/usr/bin/env python
# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""Tests for subagents and workflows. The model is a fake that answers by
who is asking (the subagent's instructions), so several can work at once."""

import json
import threading
import time

import pytest

from promptlinelib.promptline.agent import flows, subagents
from promptlinelib.promptline.agent.approval import AskEveryTime, \
    FullPermission
from promptlinelib.promptline.agent.cli import parse_options
from promptlinelib.promptline.agent.loop import Agent
from promptlinelib.promptline.agent.subagents import (
    BUILTIN, PolicyProxy, SubagentRunner, delegate_tool, load_agents,
    parse_agent)
from promptlinelib.promptline.agent.tools import RUN_COMMAND, TOOLS
from promptlinelib.promptline.providers import ProviderError


def tool_call(name, **args):
    tool_call.count += 1
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'c%d' % tool_call.count, 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(args)}}]}


tool_call.count = 0


def text(content):
    return {'role': 'assistant', 'content': content}


class Model(object):
    """Fake provider: script(role, messages) -> the reply, where role is the
    kind of subagent (from its instructions) or 'agent'"""
    ROLES = (('You investigate one question', 'explorer'),
             ('You make plans', 'planner'),
             ('You are a critical reviewer', 'reviewer'),
             ('You carry out the task', 'worker'),
             ('You check independently', 'verifier'))

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.lock = threading.Lock()
        self.efforts = []

    def role(self, messages):
        system = messages[0]['content']
        for marker, role in self.ROLES:
            if marker in system:
                return role
        return 'agent'

    def chat(self, messages, tools, on_text=None):
        role = self.role(messages)
        with self.lock:
            self.calls.append((role, [t['function']['name'] for t in tools]))
        return self.script(role, messages)

    complete = None


class UI(object):
    """Thread-safe recording UI; approvals are answered by `answer`"""
    def __init__(self, answer='approve'):
        self.lines, self.lock, self.answer = [], threading.Lock(), answer
        self.asked, self.inside, self.overlap = [], 0, False

    def emit(self, line):
        with self.lock:
            self.lines.append(line)

    def note(self, line):
        self.emit(line)

    def thinking(self, fn, label=''):
        return fn()

    def say(self, text):
        pass

    def stream(self, text):
        pass

    def end_stream(self):
        pass

    def auto_approved(self, command, mode, note):
        pass

    def running(self, command):
        pass

    def finished(self, status):
        pass

    def approve(self, command, reason, note=None, shown=False, who=None):
        with self.lock:
            self.inside += 1
            self.overlap = self.overlap or self.inside > 1
        time.sleep(0.05)       # a person takes a moment to answer
        with self.lock:
            self.inside -= 1
            self.asked.append((who, command))
        return self.answer, command


def runner_for(model, ui=None, policy=None, cwd='/tmp', **options):
    ui = ui or UI()
    agents = options.pop('agents', None) or load_agents('/nonexistent')
    runner = SubagentRunner(lambda reasoning: model, ui,
                            policy or FullPermission(), agents, cwd,
                            **options)
    return runner, ui


def test_an_explorer_looks_unasked_but_cannot_change_anything():
    results = []

    def script(role, messages):
        results.append(messages[-1]['content'])
        if len(messages) == 2:
            return tool_call('run_command', command='echo found-it',
                             reason='look')
        if len(messages) == 4:
            return tool_call('run_command', command='rm -rf /tmp/x',
                             reason='tidy')
        return text('Report: found-it, and I was not allowed to delete.')
    model = Model(script)
    runner, ui = runner_for(model, policy=AskEveryTime())
    report = runner.run('explorer', 'what is in /tmp?')
    assert report.startswith('Report: found-it')
    assert json.loads(results[1]) == {'exit_status': 0,
                                      'output': 'found-it'}
    assert results[2].startswith('This command is not allowed')
    assert ui.asked == []                       # nobody was asked
    assert any('[explorer] working: what is in /tmp?' in line
               for line in ui.lines)
    assert any('done (1 command)' in line for line in ui.lines)


def test_what_an_explorer_is_refused_comes_back_to_it():
    seen = []

    def script(role, messages):
        seen.append(messages[-1]['content'])
        if len(messages) == 2:
            return tool_call('run_command', command='rm -rf /tmp/x',
                             reason='tidy')
        return text('ok')
    runner, ui = runner_for(Model(script))
    runner.run('explorer', 'look')
    assert seen[1].startswith('This command is not allowed: this subagent '
                              'may only run read-only commands')


def test_a_worker_asks_the_user_through_the_real_ui_and_says_who():
    def script(role, messages):
        if len(messages) == 2:
            return tool_call('run_command', command='echo changed',
                             reason='apply')
        return text('Changed it.')
    ui = UI()
    runner, _ = runner_for(Model(script), ui=ui, policy=AskEveryTime())
    assert runner.run('worker', 'make the change') == 'Changed it.'
    assert ui.asked == [('worker', 'echo changed')]


def test_a_declined_command_is_reported_to_the_worker():
    seen = []

    def script(role, messages):
        seen.append(messages[-1]['content'])
        if len(messages) == 2:
            return tool_call('run_command', command='echo x', reason='r')
        return text('Understood.')
    runner, _ = runner_for(Model(script), ui=UI(answer='cancel'),
                           policy=AskEveryTime())
    runner.run('worker', 'do it')
    assert seen[1] == 'The user declined to run this command.'


def test_several_subagents_work_at_the_same_time():
    barrier = threading.Barrier(3, timeout=5)

    def script(role, messages):
        barrier.wait()      # only passes if all three are running at once
        return text('Report from %s.' % messages[1]['content'])
    runner, _ = runner_for(Model(script))
    reports = runner.run_many([('explorer', 'one', 'explorer 1/3'),
                               ('explorer', 'two', 'explorer 2/3'),
                               ('explorer', 'three', 'explorer 3/3')])
    assert reports == ['Report from one.', 'Report from two.',
                       'Report from three.']


def test_the_parallel_limit_holds():
    running, peak, lock = [0], [0], threading.Lock()

    def script(role, messages):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return text('ok')
    runner, _ = runner_for(Model(script), max_parallel=2)
    runner.run_many([('explorer', str(n), 'e') for n in range(6)])
    assert peak[0] == 2


def test_questions_from_parallel_workers_are_asked_one_at_a_time():
    def script(role, messages):
        if len(messages) == 2:
            return tool_call('run_command', command='echo %s' %
                             messages[1]['content'], reason='r')
        return text('done')
    ui = UI()
    runner, _ = runner_for(Model(script), ui=ui, policy=AskEveryTime())
    runner.run_many([('worker', n, 'worker %s' % n) for n in 'abc'])
    assert sorted(command for _who, command in ui.asked) == [
        'echo a', 'echo b', 'echo c']
    assert sorted(who for who, _command in ui.asked) == [
        'worker a', 'worker b', 'worker c']


def test_the_agent_can_delegate_and_gets_the_reports_back():
    def script(role, messages):
        if role == 'agent':
            if len(messages) == 2:
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'a', 'type': 'function', 'function': {
                        'name': 'delegate', 'arguments': json.dumps(
                            {'agent': 'explorer', 'task': 'look at disk'})}},
                    {'id': 'b', 'type': 'function', 'function': {
                        'name': 'delegate', 'arguments': json.dumps(
                            {'agent': 'explorer', 'task': 'look at ram'})}}]}
            return text('Both done.')
        return text('Findings about: ' + messages[1]['content'])
    model = Model(script)
    runner, ui = runner_for(model)
    agent = Agent(model, ui, lambda c: (0, ''), policy=FullPermission(),
                  tools=TOOLS + [delegate_tool(BUILTIN)], subagents=runner)
    agent.messages = [{'role': 'system', 'content': 'You are the agent.'}]
    agent.run({'role': 'user', 'content': 'check the box'})
    results = [m['content'] for m in agent.messages if m['role'] == 'tool']
    assert results == ['[explorer]\nFindings about: look at disk',
                       '[explorer]\nFindings about: look at ram']
    assert [t for r, t in model.calls if r == 'explorer'] == \
        [['run_command']] * 2          # a subagent has only run_command


def test_delegating_to_a_missing_subagent_or_with_no_task_is_an_error():
    model = Model(lambda role, messages: text('ok'))
    runner, ui = runner_for(model)
    assert 'no subagent called' in runner.run('plumber', 'fix')
    agent = Agent(model, ui, lambda c: (0, ''), subagents=runner)
    assert 'needs a task' in agent.delegate([('explorer', ' ')])[0]
    bare = Agent(model, ui, lambda c: (0, ''))
    assert 'not available' in bare.delegate([('explorer', 'x')])[0]


def test_steering_ends_subagents_at_their_next_step():
    steered = threading.Event()

    def script(role, messages):
        steered.set()               # the user types after the first reply
        return tool_call('run_command', command='echo again', reason='r')
    model = Model(script)
    runner, _ = runner_for(model, cancelled=steered.is_set)
    runner.run('explorer', 'keep looking')
    assert len(model.calls) == 1        # it did not carry on to a 2nd reply


def test_cancelling_kills_a_running_command():
    started = threading.Event()

    def script(role, messages):
        if len(messages) == 2:
            started.set()
            return tool_call('run_command', command='sleep 30', reason='r')
        return text('stopped')
    runner, _ = runner_for(Model(script))
    thread = threading.Thread(target=runner.run, args=('explorer', 'wait'))
    thread.start()
    assert started.wait(5)
    time.sleep(0.5)
    begin = time.monotonic()
    runner.cancel()
    thread.join(10)
    assert not thread.is_alive() and time.monotonic() - begin < 5


def test_a_provider_that_rejects_the_key_stops_everything():
    def script(role, messages):
        raise ProviderError('401: bad key', auth=True)
    runner, _ = runner_for(Model(script))
    with pytest.raises(ProviderError):
        runner.run_many([('explorer', 'a', 'x'), ('explorer', 'b', 'y')])
    # other failures are reported to the agent instead
    runner, _ = runner_for(Model(lambda r, m: (_ for _ in ()).throw(
        ProviderError('503: overloaded'))))
    assert runner.run('explorer', 'a') == 'Failed: 503: overloaded'


def test_the_runner_stops_at_its_limit(monkeypatch):
    monkeypatch.setattr(subagents, 'RUN_LIMIT', 2)
    runner, _ = runner_for(Model(lambda role, messages: text('ok')))
    assert runner.run('explorer', 'a') == 'ok'
    assert runner.run('explorer', 'b') == 'ok'
    assert 'too many subagents' in runner.run('explorer', 'c')


def test_a_policy_proxy_does_not_take_over_the_conversation():
    class Policy(AskEveryTime):
        attached = []

        def attach(self, agent):
            self.attached.append(agent)
    proxy = PolicyProxy(Policy())
    Agent(None, UI(), lambda c: (0, ''), policy=proxy)
    assert Policy.attached == []


# Writing your own

def test_user_subagents_are_loaded_and_can_replace_built_in_ones(tmp_path):
    (tmp_path / 'scanner.md').write_text(
        '---\ndescription: Looks at ports\ntools: read\n---\nScan ports.\n')
    (tmp_path / 'explorer.md').write_text('Be thorough.\n')
    (tmp_path / 'empty.md').write_text('---\ndescription: nothing\n---\n')
    (tmp_path / 'notes.txt').write_text('ignored')
    (tmp_path / 'bad name.md').write_text('ignored')
    agents = load_agents(str(tmp_path))
    assert agents['scanner'].tools == 'read'
    assert agents['scanner'].prompt == 'Scan ports.'
    assert agents['explorer'].prompt == 'Be thorough.'
    assert 'empty' not in agents and 'notes' not in agents
    assert set(BUILTIN) <= set(agents)
    tool = delegate_tool(agents)
    assert 'scanner' in tool['function']['parameters']['properties'][
        'agent']['enum']
    assert 'Looks at ports' in tool['function']['description']


# Workflows

def team_model(verdicts):
    """A team that plans two questions, explores them, does the work, and
    checks it with each verdict in `verdicts` in turn"""
    state = {'checks': 0}
    lock = threading.Lock()

    def script(role, messages):
        task = messages[1]['content']
        if role == 'planner':
            return text('- Is nginx running?\n- Which port?\nApproach: '
                        'restart it.')
        if role == 'explorer':
            return text('Evidence for: ' + task.split('Your focus: ')[1]
                        .split('\n')[0])
        if role == 'worker':
            return text('Worker report, saw %d earlier checks.' %
                        task.count('## check'))
        if role == 'reviewer':
            with lock:
                state['checks'] += 1
                verdict = verdicts[min(state['checks'] - 1,
                                       len(verdicts) - 1)]
            return text(verdict)
        if role == 'verifier':
            return text('PASS: it works')
        return text('Final answer.')
    return Model(script), state


def test_the_ultra_workflow_runs_its_stages_in_order():
    model, _ = team_model(['PASS: matches the request'])
    runner, ui = runner_for(model)
    record = flows.FlowRunner(runner, ui).run(
        flows.ULTRA, 'restart nginx', 'Shell: bash')
    assert [name for name, _ in record] == ['plan', 'explore', 'act',
                                            'check']
    roles = [role for role, _ in model.calls]
    assert roles.count('planner') == 1 and roles.count('explorer') == 2
    assert roles.count('worker') == 1
    assert roles.count('reviewer') == roles.count('verifier') == 1
    # One explorer per question, each told its own
    explore = dict(record)['explore']
    assert '### Is nginx running?\nEvidence for: Is nginx running?' in explore
    assert '### Which port?\nEvidence for: Which port?' in explore
    # Later stages see what earlier ones found
    assert 'Worker report' in dict(record)['act']
    assert [line for line in ui.lines if line.startswith('Stage')] == [
        'Stage 1 of 4: plan', 'Stage 2 of 4: explore', 'Stage 3 of 4: act',
        'Stage 4 of 4: check']


def test_a_failed_check_sends_the_workflow_back_to_act_with_the_reasons():
    model, state = team_model(['FAIL: nginx is still down', 'PASS: fixed'])
    runner, ui = runner_for(model)
    record = flows.FlowRunner(runner, ui).run(flows.ULTRA, 'restart nginx')
    assert [name for name, _ in record] == [
        'plan', 'explore', 'act', 'check', 'act', 'check']
    assert 'saw 1 earlier checks' in record[4][1]    # it saw the failure
    assert any('did not pass; going back to act (1 of 2)' in line
               for line in ui.lines)
    # ...and gives up after its retries
    model, state = team_model(['FAIL: still down'])
    runner, ui = runner_for(model)
    record = flows.FlowRunner(runner, ui).run(flows.ULTRA, 'restart nginx')
    assert [name for name, _ in record].count('act') == 3


def test_flows_can_be_written_and_checked(tmp_path):
    (tmp_path / 'quick.md').write_text(
        'description: One look, one change\n'
        'look: explorer -> Look.\nchange: worker -> Change it.\n')
    flow = flows.load_flow('quick', str(tmp_path))
    assert flow.description == 'One look, one change'
    assert flows.problems(flow, BUILTIN) == []
    assert flows.problems(flows.parse_flow('x', 'a: plumber -> fix'),
                          BUILTIN) == ["stage a: there is no subagent "
                                       "called 'plumber'"]
    assert flows.load_flow('ultra') is flows.ULTRA
    with pytest.raises(ValueError, match='no flow called'):
        flows.load_flow('missing', str(tmp_path))
    with pytest.raises(ValueError, match='no flow called'):
        flows.load_flow('../etc/passwd', str(tmp_path))
    with pytest.raises(ValueError, match='fanout takes one agent'):
        flows.parse_flow('x', 'a: fanout explorer, worker -> x')
    with pytest.raises(ValueError, match='no stages'):
        flows.parse_flow('x', '# nothing\n')


def test_a_fanout_with_no_list_runs_one_subagent():
    model = Model(lambda role, messages: text('No list here.'
                                              if role == 'planner'
                                              else 'Looked.'))
    runner, ui = runner_for(model)
    flow = flows.parse_flow('x', 'plan: planner -> Plan.\n'
                                 'look: fanout explorer -> Look.')
    record = flows.FlowRunner(runner, ui).run(flow, 'do it')
    assert record[1] == ('look', 'Looked.')


def test_the_flow_stops_when_the_user_says_stop():
    steered = threading.Event()

    def script(role, messages):
        steered.set()
        return text('- one\n- two')
    runner, ui = runner_for(Model(script), cancelled=steered.is_set)
    record = flows.FlowRunner(runner, ui).run(flows.ULTRA, 'x')
    assert [name for name, _ in record] == ['plan']


def test_guidance_typed_during_a_flow_goes_to_the_stages_after_it():
    typed = []

    def script(role, messages):
        if role == 'planner':
            typed.append('use port 8080')       # typed during stage 1
            return text('- one')
        return text('ok: ' + messages[1]['content'])
    runner, ui = runner_for(Model(script), guidance=lambda: list(typed))
    record = flows.FlowRunner(runner, ui).run(flows.ULTRA, 'x')
    assert [name for name, _ in record] == [
        'plan', 'explore', 'act', 'check']       # nothing was cut short
    assert 'The user added this while the team was working: use port 8080' \
        in dict(record)['explore']
    assert 'use port 8080' not in dict(record)['plan']


def test_options_come_before_the_request():
    assert parse_options('--ultra fix it') == ({'ultra'}, 'fix it')
    assert parse_options('--max --goal fix it') == ({'max', 'goal'}, 'fix it')
    assert parse_options('fix --ultra it') == (set(), 'fix --ultra it')
