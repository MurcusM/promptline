# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""subagents.py - helpers the agent can hand tasks to

A subagent is the same loop as the agent, started fresh with its own
instructions and a narrower job: its own conversation, so what it reads
does not fill the agent's context, and only its final report comes back.
Several can work at once. Subagents that only look (read-only commands,
which run unasked) can run unattended; ones that change things go through
the user's usual approval settings, one prompt at a time.

Built in: explorer, planner, reviewer (read-only), worker, verifier. The
user can add their own, or replace these, with files in
~/.config/promptline/agents/NAME.md:

    ---
    description: Looks for exposed services
    tools: read
    reasoning: high
    ---
    You scan this machine's listening services...

>>> agent = parse_agent('sec', "---\\ndescription: Scans\\ntools: read\\n"
...                     "reasoning: high\\n---\\nLook at ports.\\n")
>>> agent.name, agent.description, agent.tools, agent.reasoning
('sec', 'Scans', 'read', 'high')
>>> agent.prompt
'Look at ports.'
>>> parse_agent('x', 'No front matter, so all tools.').tools
'all'
>>> sorted(BUILTIN)
['explorer', 'planner', 'reviewer', 'verifier', 'worker']
>>> tool = delegate_tool(BUILTIN)
>>> tool['function']['name'], tool['function']['parameters']['properties']['agent']['enum'][:2]
('delegate', ['explorer', 'planner'])
>>> items('Questions:\\n- Is nginx running?\\n* What port?\\n1. not this\\n-no space')
['Is nginx running?', 'What port?']
"""

import collections
import os
import re
import threading

from ...util import get_config_dir
from ..providers import ProviderError
from .approval import AskEveryTime
from .loop import Agent
from .readonly import ReadOnlyPolicy
from .tools import OUTPUT_LIMIT, RUN_COMMAND, run_quiet

SUBAGENT_STEPS = 20         # model replies one subagent may take
COMMAND_TIMEOUT = 120       # seconds a subagent's command may run
RUN_LIMIT = 60              # subagents one request may start in all
REPORT_LIMIT = 12000        # characters of a report passed on

Subagent = collections.namedtuple(
    'Subagent', 'name description prompt tools reasoning')

COMMON = (
    "You are a subagent working for the Promptline terminal agent, which "
    "is working for a user at their terminal. You were given one task. Do "
    "it and reply with your report: your final message is handed back to "
    "the agent that gave you the task, and nobody sees your steps in "
    "between. Use run_command to look at the system. Commands run with "
    "`%s -c` in the user's current directory, without a terminal, so "
    "nothing interactive works. Be concrete: say what you ran and what you "
    "found, with the key lines of output, and say plainly what you could "
    "not find out. Plain text, no Markdown headings or tables. Never ask "
    "the user a question: make reasonable assumptions and state them.")

READ_ONLY_TEXT = (
    "You can only run read-only commands. Anything that changes a file, a "
    "setting, a service or the network is refused, so don't try.")

ALL_TOOLS_TEXT = (
    "Your commands go through the user's approval settings, so each may be "
    "shown to them before it runs. Make only the changes your task calls "
    "for, check the result of each, and prefer changes that are easy to "
    "undo.")

BUILTIN = collections.OrderedDict((agent.name, agent) for agent in (
    Subagent('explorer',
             'Looks into one question about the system, project or '
             'environment, with read-only commands, and reports what it '
             'found',
             "You investigate one question about the user's machine, "
             "project or environment. Use read-only commands to find the "
             "answer, follow the leads they give you, and stop when you "
             "have it or have run out of ways to look. Report facts with "
             "evidence, not guesses, in under 300 words.",
             'read', None),
    Subagent('planner',
             'Looks at the situation and writes a plan, without changing '
             'anything',
             "You make plans. Look at whatever you need with read-only "
             "commands, then write the plan: what is being asked, the "
             "steps in order, what could go wrong, and how to tell it "
             "worked. Follow any format your task asks for exactly.",
             'read', None),
    Subagent('reviewer',
             'Checks work critically and reports PASS or FAIL',
             "You are a critical reviewer. Look for what is wrong or "
             "missing in the work you are shown: does it do what the user "
             "asked, was anything left undone, could it have broken "
             "something, are its claims backed by evidence? Check with "
             "read-only commands rather than taking its word for it. The "
             "very first word of your reply must be PASS or FAIL, then a "
             "colon and your reasons (a short paragraph, or a list of the "
             "problems if you FAIL).",
             'read', None),
    Subagent('worker',
             'Carries out a task: runs the commands that make the changes '
             'and checks the result',
             "You carry out the task you are given: run the commands that "
             "make the changes it asks for, one step at a time, check each "
             "result before the next, and stop and report if something "
             "unexpected happens. If the task only asks a question, answer "
             "it from what you find and change nothing. Report what you "
             "did, what you checked, and the state things are in now.",
             'all', None),
    Subagent('verifier',
             'Runs the tests and checks that show whether a result works, '
             'and reports PASS or FAIL',
             "You check independently that the result works: run the "
             "tests, linters and status commands that show whether what "
             "the user wanted is now true. Change nothing except what a "
             "check itself needs. The very first word of your reply must "
             "be PASS or FAIL, then a colon, then what you ran and what "
             "you saw.",
             'all', None),
))


def parse_agent(name, text):
    """A Subagent from the text of its file"""
    fields, body = {}, text
    match = re.match(r'---\s*\n(.*?)\n---\s*\n?', text, re.DOTALL)
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            key, _colon, value = line.partition(':')
            if _colon:
                fields[key.strip().lower()] = value.strip()
    tools = 'read' if fields.get('tools', 'all').lower() in (
        'read', 'read-only', 'readonly') else 'all'
    return Subagent(name, fields.get('description', ''), body.strip(), tools,
                    fields.get('reasoning') or None)


def load_agents(directory=None):
    """The built-in subagents, with the user's own added (and replacing any
    of the same name)"""
    agents = collections.OrderedDict(BUILTIN)
    directory = directory or os.path.join(get_config_dir(), 'agents')
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return agents
    for filename in names:
        name, extension = os.path.splitext(filename)
        if extension != '.md' or not re.match(r'^[\w-]+$', name):
            continue
        try:
            with open(os.path.join(directory, filename),
                      encoding='utf-8') as handle:
                agent = parse_agent(name, handle.read())
        except OSError:
            continue
        if agent.prompt:
            agents[name] = agent
    return agents


def delegate_tool(agents):
    """The tool the agent uses to hand a task to a subagent"""
    return {'type': 'function', 'function': {
        'name': 'delegate',
        'description': (
            'Hand a self-contained task to a subagent, which works on it '
            'in a conversation of its own and returns a report. Use it to '
            'look into something without filling your own context, to get '
            'a second opinion, or to do several independent things at '
            'once: several delegate calls in one reply run at the same '
            'time. The subagent knows nothing of this conversation, so '
            'put everything it needs in the task. Available: ' +
            '; '.join('%s (%s)' % (a.name, a.description or 'no '
                                   'description') for a in agents.values())),
        'parameters': {
            'type': 'object',
            'properties': {
                'agent': {'type': 'string', 'enum': list(agents)},
                'task': {'type': 'string',
                         'description': 'What to do, with all the context '
                                        'it needs, and what to report.'},
            },
            'required': ['agent', 'task'],
            'additionalProperties': False,
        },
    }}


def items(text):
    """The list items ("- ..." or "* ...") in text, for splitting work"""
    return [match.group(1).strip() for match in
            re.finditer(r'^[ \t]*[-*][ \t]+(\S.*)$', text, re.MULTILINE)]


class PolicyProxy(AskEveryTime):
    """The user's approval policy, for a subagent's commands. Like the
    policy it stands for, except that it doesn't take over the agent's
    conversation (attach) or learn from the subagent's questions."""
    def __init__(self, policy):
        self.policy = policy
        self.mode = policy.mode
        self.reviews = False

    def decide(self, command, reason='', on_text=None):
        return self.policy.decide(command, reason)

    def attach(self, agent):
        pass


class SubUI(object):
    """What a subagent shows of itself: one line when it starts and when it
    ends, and its commands; the user's questions go through the real UI"""
    def __init__(self, parent, label):
        self.parent = parent
        self.label = label
        self.commands = 0

    def emit(self, text):
        emit = getattr(self.parent, 'emit', None)
        (emit or self.parent.note)('  [%s] %s' % (self.label, text))

    def thinking(self, fn, label=''):
        return fn()

    def say(self, text):
        pass

    def stream(self, text):
        pass

    def end_stream(self):
        pass

    def note(self, text):
        self.emit(text)

    def auto_approved(self, command, mode, note):
        self.commands += 1
        self.emit('$ ' + command.split('\n')[0][:100])

    def running(self, command):
        pass

    def finished(self, status):
        if status:
            self.emit('exit %d' % status)

    def approve(self, command, reason, note=None, shown=False):
        self.commands += 1
        try:
            return self.parent.approve(command, reason, note,
                                       who=self.label)
        except TypeError:       # a UI that doesn't say who is asking
            return self.parent.approve(command, reason, note)


class SubagentRunner(object):
    """Runs subagents on behalf of an agent, one or several at once"""
    def __init__(self, provider_for, ui, policy, agents, cwd, audit=None,
                 shell='sh', max_parallel=4, cancelled=None, guidance=None,
                 context_tokens=1000000, output_limit=OUTPUT_LIMIT):
        self.provider_for = provider_for    # reasoning -> provider
        self.ui = ui
        self.policy = policy                # the user's approval policy
        self.agents = agents
        self.cwd = cwd
        self.output_limit = output_limit
        self.audit = audit
        self.shell = shell
        self.max_parallel = max(1, max_parallel)
        self.cancelled = cancelled or (lambda: False)
        self.guidance = guidance or (lambda: [])    # what the user has typed
        self.context_tokens = context_tokens
        self.count = 0
        self.lock = threading.Lock()
        self.stopped = False
        self.processes = []

    def cancel(self):
        """Stop what is running: subagents end at their next step, and their
        commands are killed"""
        self.stopped = True
        for process in list(self.processes):
            try:
                os.killpg(process.pid, 9)
            except OSError:
                pass

    def stop_requested(self):
        return self.stopped or self.cancelled()

    def quiet_executor(self, command):
        """A subagent's command: no terminal, bounded in time"""
        return run_quiet(command, self.cwd, self.shell, self.output_limit,
                         COMMAND_TIMEOUT, self.processes)

    def run(self, name, task, label=None):
        """One subagent, to the end: its report as text"""
        agent = self.agents.get(name)
        if agent is None:
            return 'Error: there is no subagent called %r (there are: %s).' \
                % (name, ', '.join(self.agents))
        with self.lock:
            self.count += 1
            if self.count > RUN_LIMIT:
                return ('Error: too many subagents have been started for '
                        'this request.')
        label = label or name
        ui = SubUI(self.ui, label)
        ui.emit('working: ' + ' '.join(task.split())[:90])
        audit = (lambda command, how, status:
                 self.audit(command, '%s:%s' % (label, how), status)) \
            if self.audit else None
        policy = ReadOnlyPolicy() if agent.tools == 'read' \
            else PolicyProxy(self.policy)
        prompt = '\n\n'.join([COMMON % os.path.basename(self.shell),
                              READ_ONLY_TEXT if agent.tools == 'read'
                              else ALL_TOOLS_TEXT, agent.prompt])
        worker = Agent(self.provider_for(agent.reasoning), ui,
                       self.quiet_executor, policy=policy, audit=audit,
                       messages=[{'role': 'system', 'content': prompt}],
                       max_steps=SUBAGENT_STEPS, tools=[RUN_COMMAND],
                       context_tokens=self.context_tokens)
        worker.stop_requested = self.stop_requested
        try:
            worker.run({'role': 'user', 'content': task})
        except ProviderError as ex:
            if ex.auth:
                raise
            ui.emit('failed: %s' % ex)
            return 'Failed: %s' % ex
        report = report_of(worker)
        ui.emit('done%s' % (' (%d command%s)' % (
            ui.commands, 's' * (ui.commands != 1)) if ui.commands else ''))
        return report

    def run_many(self, jobs):
        """jobs, a list of (name, task, label), at once (at most
        max_parallel at a time); the reports in the same order"""
        results = [None] * len(jobs)
        pending = list(enumerate(jobs))
        errors = []

        def work():
            while True:
                with self.lock:
                    if not pending or errors:
                        return
                    index, (name, task, label) = pending.pop(0)
                try:
                    results[index] = self.run(name, task, label)
                except BaseException as ex:     # reported by the caller
                    errors.append(ex)
                    return
        threads = [threading.Thread(target=work, daemon=True)
                   for _ in range(min(len(jobs), self.max_parallel))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]
        return results


def report_of(agent):
    """What a subagent reported: its last words"""
    for message in reversed(agent.messages):
        if message.get('role') == 'assistant' and message.get('content'):
            text = message['content'].strip()
            if len(text) > REPORT_LIMIT:
                text = text[:REPORT_LIMIT] + '\n[... report cut ...]'
            return text
    return '(the subagent finished without a report)'

