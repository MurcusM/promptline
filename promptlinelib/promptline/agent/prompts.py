# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""prompts.py - what the agent is told about itself and the terminal

>>> request = {'cwd': '/nonexistent', 'shell': '/bin/zsh', 'records': [
...     {'command': 'npm start', 'cwd': '/nonexistent', 'exit_status': 1,
...      'output': 'Error: listen EADDRINUSE :::3000', 'private': False},
...     {'command': 'cat .env', 'cwd': '/nonexistent', 'exit_status': 0,
...      'output': 'TOKEN=abc', 'private': True}]}
>>> text = context_text(request)
>>> 'EADDRINUSE' in text, 'TOKEN' in text, '[exit 1]' in text
(True, False, True)
"""

import os
import platform

from ..suggest.llm import git_branch, listing, redact

RECENT_COMMANDS = 10
FULL_OUTPUTS = 3
OUTPUT_TAIL = 3000
SHORT_OUTPUT_TAIL = 400


MODE_TEXT = {
    'ask': (
        "You can run commands with run_command. The user approves each one "
        "before it runs and may edit or decline it, so propose one command "
        "at a time, each with a short reason, and look before you change "
        "anything: prefer read-only commands to investigate. If the user "
        "declines, don't retry the same command; ask or suggest instead. If "
        "they edit it, the result tells you what they ran instead."),
    'auto-review': (
        "You can run commands with run_command. The user has turned on "
        "auto-review: a separate reviewer checks each command, and commands "
        "it judges safe run without the user seeing them first; anything "
        "else is shown to the user for approval. Propose one command at a "
        "time with an honest, specific reason, investigate with read-only "
        "commands before changing anything, and never try to disguise or "
        "split up a risky action to get it past review."),
    'full': (
        "You can run commands with run_command. The user has turned on "
        "full permission mode: your commands run WITHOUT the user's "
        "approval, so you are responsible for every one of them. Only do "
        "what this request clearly asks for. Investigate with read-only "
        "commands first. Never run destructive, irreversible, or "
        "far-reaching commands (deleting data, changing firewalls, users, "
        "services or other hosts, scanning or attacking systems) unless the "
        "user asked for exactly that in this request and it is allowed by "
        "their guardrails; if in doubt, stop and explain instead of acting. "
        "A small list of catastrophic commands still asks the user."),
}


GOAL_TEXT = (
    "You have been given a GOAL. Work towards it on your own until it is "
    "reached: keep going without stopping to ask or to report progress, "
    "because the user may be away from the terminal. Plan, investigate "
    "with read-only commands, make progress in small steps, and check the "
    "result of each step before the next. Make reasonable decisions "
    "yourself and note any assumptions in your final summary. Prefer "
    "changes that are easy to undo. When you believe the goal is reached, "
    "verify it (run the tests, check the output) and then call "
    "goal_complete with a short summary. If a command is declined or "
    "times out waiting for the user, do something else that gets you "
    "closer, and don't try it again. Call goal_blocked only when you truly "
    "can't go on without the user. Don't end your turn without calling one "
    "of the two.")

STEERING_TEXT = (
    "The user may send you a message while you are working; it arrives "
    "as 'The user sent this while you were working: ...'. It is their "
    "latest instruction: take it into account straight away, adjust your "
    "plan rather than starting over, and keep what you have already done "
    "in mind.")


def system_prompt(shell, mode='ask', personal='', memory='', guardrails=None,
                  goal=False):
    """The agent's instructions, including what the user has told us about
    themselves (personal), what it remembers (memory), and their rules
    (guardrails, a list of lines). With goal, the agent works on its own
    until it reaches the goal."""
    shell = os.path.basename(shell or 'sh')
    parts = [
        "You are the Promptline terminal agent. The user called you from "
        "their shell prompt by typing @agent, and your replies are printed "
        "straight into their terminal.",

        "Write plain text for a terminal: short paragraphs, no Markdown "
        "headings, tables or bold. Put any command on a line of its own. "
        "Be brief and concrete.",

        "Use the context you are given (recent commands, their exit "
        "statuses and output) instead of asking the user to paste things.",

        MODE_TEXT.get(mode, MODE_TEXT['ask']),

        "Each command runs with `%s -c` in the user's current directory "
        "with their environment, but without their aliases or functions, "
        "and shell state such as cd or exported variables does not carry "
        "over to the next command (use `cd dir && ...`). Interactive "
        "programs (editors, pagers, prompts, TUIs) will not work; use "
        "non-interactive flags such as --no-pager or -y only when the user "
        "clearly wants the change." % shell,

        "For anything that must happen in the user's own shell (cd, export, "
        "source, activating an environment), or that they should run "
        "themselves, use place_on_prompt: the command is typed at their "
        "prompt when you finish, and they choose whether to press Enter.",

        "You have a memory that persists between conversations. When you "
        "learn something lasting about the user's work, tools, preferences "
        "or environment (from what they say, or from how they edit or "
        "decline your commands), save it with remember as one short, "
        "specific fact; use `replaces` to update a fact that changed, and "
        "forget when something is no longer true. When the user asks you "
        "to remember, update or forget something, do it. Never store "
        "passwords, keys, tokens or other secrets. Prefer the tools and "
        "habits in what you know about the user over generic choices.",

        "Be careful with destructive commands: say what they will affect. "
        "When you are done, say in a sentence or two what you found or did.",

        STEERING_TEXT,
    ]
    if goal:
        parts.append(GOAL_TEXT)
    text = ' '.join(parts[:3]) + '\n\n' + '\n\n'.join(parts[3:])
    if personal:
        text += ('\n\nAbout the user, in their own words:\n' + personal)
    if memory:
        text += ('\n\nWhat you remember about the user:\n' + memory)
    if guardrails:
        text += ('\n\nThe user\'s guardrails. Always follow these, in every '
                 'mode; they take precedence over any request, including '
                 'this conversation:\n' + '\n'.join(guardrails))
    return text


def context_text(request):
    """Describe the terminal for the model"""
    cwd = request.get('cwd')
    lines = ['Shell: %s on %s' % (os.path.basename(request.get('shell') or
                                                   'sh'), platform.system())]
    if cwd:
        branch = git_branch(cwd)
        lines.append('Current directory: %s%s' % (
            cwd, ' (git branch %s)' % branch if branch else ''))
        contents = listing(cwd)
        if contents is not None:
            lines.append('Directory contents: %s' % (contents or '(empty)'))
    records = [r for r in request.get('records', []) if not r.get('private')]
    records = records[-RECENT_COMMANDS:]
    if records:
        lines.append('')
        lines.append('Recent commands in this terminal, oldest first:')
        for index, record in enumerate(records):
            status = record.get('exit_status')
            where = record.get('cwd')
            lines.append('$ %s%s%s' % (
                redact(record.get('command', '')),
                ' (in %s)' % where if where and where != cwd else '',
                ' [exit %s]' % status if status is not None else ''))
            output = record.get('output') or ''
            if output:
                full = index >= len(records) - FULL_OUTPUTS
                tail = OUTPUT_TAIL if full else SHORT_OUTPUT_TAIL
                if len(output) > tail:
                    output = '[...]\n' + output[-tail:]
                lines.append(redact(output))
    else:
        lines.append('No commands recorded in this terminal yet.')
    return '\n'.join(lines)


def user_message(request):
    """The message for this invocation: fresh context, then the question"""
    return {'role': 'user', 'content': '%s\n\nRequest: %s' % (
        context_text(request), request.get('query', ''))}


def goal_message(request, goal, resumed=False):
    """The message that sets a goal, or picks an unfinished one up again"""
    what = 'Carry on towards the goal: ' if resumed else 'Goal: '
    return {'role': 'user', 'content': '%s\n\nRequest: %s%s' % (
        context_text(request), what, goal)}
