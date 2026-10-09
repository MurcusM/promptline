# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""flows.py - strict workflows of subagents

A flow is a fixed series of stages run by the program, not chosen by the
model: each stage hands a task to one or more subagents, and what they
report is passed on to the next stage. When it is done, the agent gets the
whole record and writes the answer.

The user can write their own in ~/.config/promptline/flows/NAME.md and
choose one with promptline_subagent_flow. One stage per line:

    description: Look, then change, then check
    look: explorer -> Find out what is running on port 8080.
    change: worker -> Restart it with the new setting.
    check: reviewer, verifier -> Confirm it worked. [retry change x2]

NAME: AGENT[, AGENT...] -> what to do
    Every agent named gets the task at the same time. Add "fanout" before
    the agent (look: fanout explorer -> ...) to run one for each "- item"
    in the previous stage's report instead. A stage that ends with
    [retry STAGE xN] is a gate: if any agent's report starts with FAIL, the
    flow goes back to STAGE, up to N times, with the failure in the record.

The built-in flow ULTRA is what promptline_agent_reasoning = ultra (or
@agent --ultra) runs, in place of any flow the user has configured.

>>> flow = parse_flow('demo', '''
... # a comment
... description: Try it
... plan: planner -> Make a plan.
... look: fanout explorer -> Answer your question.
... check: reviewer, verifier -> Check it. [retry look x2]
... ''')
>>> flow.description, [stage.name for stage in flow.stages]
('Try it', ['plan', 'look', 'check'])
>>> flow.stages[1].fanout, flow.stages[2].agents, flow.stages[2].retry
(True, ('reviewer', 'verifier'), ('look', 2))
>>> parse_flow('bad', 'oops')
Traceback (most recent call last):
...
ValueError: line 1: expected NAME: AGENT -> what to do
>>> parse_flow('bad', 'a: explorer -> x [retry b]')
Traceback (most recent call last):
...
ValueError: line 1: [retry b] must name this stage or an earlier one
>>> [stage.name for stage in ULTRA.stages]
['plan', 'explore', 'act', 'check']
>>> verdict('PASS: all good'), verdict('fail - missing the log'), verdict('')
(True, False, True)
>>> render([('plan', 'step one'), ('act', 'did it')])
'## plan\\nstep one\\n\\n## act\\ndid it'
"""

import collections
import os
import re

from ...util import get_config_dir
from .subagents import items

Stage = collections.namedtuple('Stage', 'name agents fanout text retry')
Flow = collections.namedtuple('Flow', 'name description stages')

MAX_FANOUT = 8
CONTEXT_LIMIT = 6000        # characters of terminal context given to a stage
ENTRY_LIMIT = 8000          # ...and of each earlier stage's report
RECORD_LIMIT = 60000        # ...and of the record as a whole

STAGE = re.compile(
    r'^(?P<name>[\w-]+):\s*(?P<fanout>fanout\s+)?'
    r'(?P<agents>[\w-]+(?:\s*,\s*[\w-]+)*)\s*->\s*(?P<text>.*?)'
    r'(?:\s*\[retry\s+(?P<to>[\w-]+)(?:\s+x(?P<times>\d+))?\])?\s*$')


def parse_flow(name, text):
    """A Flow from the text of its file; ValueError says what's wrong"""
    description, stages = '', []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.lower().startswith('description:'):
            description = line.split(':', 1)[1].strip()
            continue
        match = STAGE.match(line)
        if not match:
            raise ValueError('line %d: expected NAME: AGENT -> what to do'
                             % number)
        agents = tuple(a.strip() for a in match.group('agents').split(','))
        fanout = bool(match.group('fanout'))
        if fanout and len(agents) != 1:
            raise ValueError('line %d: fanout takes one agent' % number)
        retry = None
        if match.group('to'):
            target = match.group('to')
            if target not in [s.name for s in stages] + [match.group('name')]:
                raise ValueError('line %d: [retry %s] must name this stage '
                                 'or an earlier one' % (number, target))
            retry = (target, int(match.group('times') or 1))
        stages.append(Stage(match.group('name'), agents, fanout,
                            match.group('text').strip(), retry))
    if not stages:
        raise ValueError('no stages')
    return Flow(name, description, tuple(stages))


def load_flow(name, directory=None):
    """The user's flow of that name, or the built-in one. ValueError if
    there is none or it can't be read."""
    if name == 'ultra':
        return ULTRA
    directory = directory or os.path.join(get_config_dir(), 'flows')
    if not re.match(r'^[\w-]+$', name or ''):
        raise ValueError('no flow called %r' % name)
    try:
        with open(os.path.join(directory, name + '.md'),
                  encoding='utf-8') as handle:
            return parse_flow(name, handle.read())
    except OSError:
        raise ValueError('no flow called %r (looked for %s)' % (
            name, os.path.join(directory, name + '.md')))


def problems(flow, agents):
    """What stops the flow from running with these subagents"""
    return ['stage %s: there is no subagent called %r' % (stage.name, agent)
            for stage in flow.stages for agent in stage.agents
            if agent not in agents]


def verdict(report):
    """Whether a checking agent's report passes: it doesn't start with FAIL

    The first word decides, so a report that mentions failures in passing
    still passes.
    """
    first = (report or '').strip().split(None, 1)
    return not (first and re.match(r'(?i)^\W*fail', first[0]))


def render(record, limit=RECORD_LIMIT):
    """The record of what the stages reported, as text for the next one"""
    parts = []
    for name, text in record:
        if len(text) > ENTRY_LIMIT:
            text = text[:ENTRY_LIMIT] + '\n[... cut ...]'
        parts.append('## %s\n%s' % (name, text))
    text = '\n\n'.join(parts)
    return text if len(text) <= limit else '[... earlier stages cut ...]\n' + \
        text[-limit:]


class FlowRunner(object):
    """Runs a flow with a SubagentRunner"""
    def __init__(self, runner, ui):
        self.runner = runner
        self.ui = ui

    def run(self, flow, request, context=''):
        """The record of the flow: a list of (stage, report). request is
        what the user asked; context describes their terminal."""
        record, used, index = [], {}, 0
        names = [stage.name for stage in flow.stages]
        while index < len(flow.stages):
            stage = flow.stages[index]
            if self.runner.stop_requested():
                break
            self.ui.note('Stage %d of %d: %s' % (index + 1, len(names),
                                                 stage.name))
            try:
                reports = self.ui.thinking(
                    lambda: self.run_stage(stage, request, context, record),
                    label='%s: subagents working' % stage.name)
            except BaseException:
                self.runner.cancel()
                raise
            record.append((stage.name, reports))
            if stage.retry and not verdict_of(reports, stage) and \
                    used.get(stage.name, 0) < stage.retry[1]:
                used[stage.name] = used.get(stage.name, 0) + 1
                self.ui.note('%s did not pass; going back to %s (%d of %d)'
                             % (stage.name, stage.retry[0], used[stage.name],
                                stage.retry[1]))
                index = names.index(stage.retry[0])
                continue
            index += 1
        return record

    def task(self, stage, request, context, record, focus=None):
        parts = ['The user asked: ' + request]
        if context:
            parts.append('Their terminal:\n' + context[:CONTEXT_LIMIT])
        if record:
            parts.append('Work so far, from earlier stages:\n' +
                         render(record))
        said = self.runner.guidance()
        if said:
            parts.append('The user added this while the team was working: ' +
                         ' / '.join(said))
        if focus:
            parts.append('Your focus: ' + focus)
        parts.append('Your stage (%s): %s' % (stage.name, stage.text))
        return '\n\n'.join(parts)

    def run_stage(self, stage, request, context, record):
        if stage.fanout:
            found = items(record[-1][1]) if record else []
            found = found[:MAX_FANOUT]
            if found:
                agent = stage.agents[0]
                jobs = [(agent, self.task(stage, request, context, record,
                                          focus), '%s %d/%d' % (
                                              agent, number, len(found)))
                        for number, focus in enumerate(found, 1)]
                reports = self.runner.run_many(jobs)
                return '\n\n'.join('### %s\n%s' % (focus, report)
                                   for focus, report in zip(found, reports))
        jobs = [(agent, self.task(stage, request, context, record), agent)
                for agent in stage.agents]
        reports = self.runner.run_many(jobs)
        if len(jobs) == 1:
            return reports[0]
        return '\n\n'.join('### %s\n%s' % (agent, report)
                           for agent, report in zip(stage.agents, reports))


def verdict_of(reports, stage):
    """Whether every checking agent in a stage's combined report passed"""
    if len(stage.agents) == 1 and not stage.fanout:
        return verdict(reports)
    sections = re.split(r'(?m)^### .*\n', reports)
    return all(verdict(section) for section in sections if section.strip())


ULTRA = parse_flow('ultra', '''
description: Plan, investigate in parallel, act, then check independently
plan: planner -> Work out what must be found out before acting. List the questions as lines that each start with "- ": at most 6, each one self-contained and answerable by looking at this machine with read-only commands. After them, on a line starting "Approach:", say how you would carry the request out once they are answered. If the request is only a question, say so, and list the questions that would answer it.
explore: fanout explorer -> Answer your focus question with evidence from this machine: say which commands you ran and quote the key lines of their output. Say plainly what you could not find out.
act: worker -> Carry out the request, following the plan and what the explorers found. Make the changes with commands, one step at a time, checking each result. If the request only asked a question, answer it from the findings and change nothing. Report what you did, what you checked, and the state things are in now.
check: reviewer, verifier -> Check the worker's report against what the user asked, and against the machine itself. Your first word must be PASS or FAIL, then a colon and your reasons. FAIL if anything the user asked for is not done, not shown to work, or has broken something. [retry act x2]
''')
