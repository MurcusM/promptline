# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""readonly.py - which commands a read-only subagent may run unasked

Subagents that only look (explorers, planners, reviewers) run without
approval prompts, so that several can work at once while the user is away.
That is only safe for commands that cannot change anything, so this is an
allowlist, not a blocklist: a command runs only if every part of it is a
known read-only command, with no redirection to files, no command
substitution, no wrappers (sudo, env, xargs) and no reading of the places
credentials live. Anything else is refused, and the subagent is told so.

It is a guard against mistakes, not a sandbox: reading files is still
reading files.

>>> check('ls -la /etc | grep ssh')
(True, 'read-only')
>>> check('git -C /tmp log --oneline -5')[0]
False
>>> check('git log --oneline -5 2>/dev/null')
(True, 'read-only')
>>> check('find . -name "*.py" -newer setup.py')[0]
True
>>> check('find . -name "*.pyc" -delete')[1]
'find -delete changes things'
>>> check('cat notes.txt > /etc/passwd')[1]
'redirects output to a file'
>>> check('echo $(rm -rf x)')[1]
'uses command substitution'
>>> check('ls; sudo reboot')[1]
"'sudo' is not on the read-only list"
>>> check('cat ~/.ssh/id_ed25519')[1]
'touches credentials or keys'
>>> check('systemctl status nginx')[0], check('systemctl restart nginx')[0]
(True, False)
>>> check('tail -f /var/log/syslog')[1]
'tail -f never ends'
>>> check('docker ps -a && docker rm web')[1]
"'docker rm' is not on the read-only list"
"""

import os
import re
import shlex

from .approval import ALLOW, DENY, AskEveryTime, Decision, hard_stop

# Commands that only read, whatever their arguments
SIMPLE = frozenset((
    'ls', 'find', 'cat', 'head', 'tail', 'wc', 'grep', 'egrep', 'fgrep', 'rg', 'stat',
    'file', 'du', 'df', 'pwd', 'echo', 'printf', 'date', 'uname', 'hostname',
    'whoami', 'id', 'uptime', 'free', 'ps', 'pgrep', 'lsof', 'ss', 'netstat',
    'which', 'whereis', 'type', 'lsb_release', 'sort', 'cut', 'tr',
    'basename', 'dirname', 'realpath', 'readlink', 'diff', 'cmp', 'tree',
    'sha256sum', 'sha1sum', 'md5sum', 'lsblk', 'lscpu', 'lsmod', 'lspci',
    'lsusb', 'jq', 'apt-cache', 'getent', 'nproc', 'arch', 'groups', 'last',
    'w', 'who', 'true', 'false', 'test', '[', 'sleep', 'seq', 'nl', 'rev',
    'fold', 'column', 'strings', 'od', 'hexdump', 'ldd', 'locale',
))

# Command -> the subcommands (first argument) that only read
SUBCOMMANDS = {
    'git': ('status', 'log', 'diff', 'show', 'rev-parse', 'ls-files', 'blame',
            'describe', 'shortlog', 'ls-remote', 'grep', 'cat-file',
            'rev-list', 'name-rev', 'whatchanged'),
    'systemctl': ('status', 'is-active', 'is-enabled', 'is-failed',
                  'list-units', 'list-unit-files', 'list-timers', 'show',
                  'cat'),
    'docker': ('ps', 'images', 'logs', 'inspect', 'version', 'info', 'top',
               'port', 'history', 'stats'),
    'apt': ('list', 'show', 'search', 'policy'),
    'pip': ('list', 'show', 'freeze', 'check'),
    'pip3': ('list', 'show', 'freeze', 'check'),
    'npm': ('ls', 'list', 'view', 'outdated'),
    'journalctl': None,         # any, except following (below)
    'dmesg': None,
    'ip': None,
    'ifconfig': None,
    'dpkg': ('-l', '-L', '-s', '-S', '-p', '-V', '--list', '--listfiles',
             '--status', '--search', '--verify'),
    'rpm': ('-q', '-qa', '-ql', '-qi', '-qf', '-V'),
}

# Arguments that make an otherwise read-only command change things or never
# end: command -> {argument: why}
BAD_ARGS = {
    'find': {a: 'find %s changes things' % a for a in (
        '-delete', '-exec', '-execdir', '-ok', '-okdir', '-fprint',
        '-fprintf', '-fls')},
    'tail': {'-f': 'tail -f never ends', '-F': 'tail -f never ends',
             '--follow': 'tail -f never ends'},
    'journalctl': {'-f': 'journalctl -f never ends',
                   '--follow': 'journalctl -f never ends',
                   '--vacuum-size': 'changes the journal',
                   '--vacuum-time': 'changes the journal',
                   '--rotate': 'changes the journal'},
    'ip': {a: 'changes the network' for a in (
        'add', 'del', 'delete', 'set', 'flush', 'change', 'replace', 'up',
        'down')},
    'sort': {'-o': 'sort -o writes a file', '--output': 'sort -o writes a file'},
    'git': {'-c': 'git -c can run commands'},
    'docker': {'-f': 'following never ends', '--follow': 'following never ends'},
    'ifconfig': {a: 'changes the network' for a in ('up', 'down')},
    'date': {'-s': 'sets the clock', '--set': 'sets the clock'},
    'ss': {'-K': 'kills sockets', '--kill': 'kills sockets'},
    'dmesg': {'-c': 'clears the kernel log', '-C': 'clears the kernel log',
              '--clear': 'clears the kernel log', '-w': 'never ends',
              '--follow': 'never ends'},
    'rg': {'--pre': 'rg --pre runs commands'},
    'file': {'-C': 'file -C writes a file', '--compile': 'file -C writes a file'},
}

# Words that, anywhere in a command, mean it is after secrets
SENSITIVE = re.compile(
    r'(?:^|/)\.(?:ssh|gnupg|aws|kube|netrc|git-credentials|npmrc|pypirc|'
    r'docker/config\.json|env(?:\.|$))|id_(?:rsa|dsa|ecdsa|ed25519)|'
    r'/etc/(?:shadow|gshadow|sudoers)|\.(?:pem|p12|pfx|kdbx)$|'
    r'/proc/\d+/environ|credentials?(?:\.|$)|\.key$')

# Redirections that don't write anywhere that matters
HARMLESS_REDIRECTS = re.compile(r'\s*(?:\d?>\s*/dev/null|\d>&\d|&>\s*/dev/null)')

OPERATORS = ('|', '||', '&&', ';')


def check(command):
    """(True, 'read-only') if command only reads, else (False, why not)"""
    text = HARMLESS_REDIRECTS.sub(' ', command)
    if '`' in text or '$(' in text or '<(' in text or '>(' in text:
        return False, 'uses command substitution'
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments, current = [], []
    try:
        for token in lexer:
            if token in OPERATORS:
                segments.append(current)
                current = []
            elif token and all(c in '<>&|;()' for c in token):
                return False, ('redirects output to a file'
                               if '>' in token else
                               'uses shell syntax that is not read-only')
            else:
                current.append(token)
    except ValueError:
        return False, 'could not be read as a command line'
    segments.append(current)
    segments = [s for s in segments if s]
    if not segments:
        return False, 'is empty'
    for words in segments:
        ok, why = check_words(words)
        if not ok:
            return False, why
    return True, 'read-only'


def check_words(words):
    """check() for one command: its words, without shell operators"""
    name = words[0]
    if name.startswith(('/usr/bin/', '/bin/', '/usr/sbin/', '/sbin/')):
        name = os.path.basename(name)
    if '/' in name or '=' in name:
        return False, "'%s' is not on the read-only list" % words[0]
    args = words[1:]
    if any(SENSITIVE.search(word) for word in args):
        return False, 'touches credentials or keys'
    if name in SUBCOMMANDS:
        allowed = SUBCOMMANDS[name]
        if allowed is not None:
            if name in ('git', 'dpkg', 'rpm'):
                # Options before git's subcommand (-c, -C) can run or move
                # things; dpkg and rpm take their query as the first option
                first = args[0] if args else None
            else:
                rest = [a for a in args if not a.startswith('-')]
                first = rest[0] if rest else None
            if first not in allowed:
                return False, "'%s%s' is not on the read-only list" % (
                    name, ' ' + first if first else '')
            if name == 'docker' and first == 'stats' and \
                    '--no-stream' not in args:
                return False, 'docker stats never ends without --no-stream'
    elif name not in SIMPLE:
        return False, "'%s' is not on the read-only list" % name
    for arg in args:
        why = BAD_ARGS.get(name, {}).get(arg)
        if why:
            return False, why
        if name == 'git' and arg.startswith('--output'):
            return False, 'git --output writes a file'
    if name == 'hostname' and any(not a.startswith('-') for a in args):
        return False, 'sets the hostname'
    return True, 'read-only'


class ReadOnlyPolicy(AskEveryTime):
    """For subagents that only look: read-only commands run, nothing else
    does (there is no one to ask)"""
    mode = 'read-only'
    reviews = False

    def decide(self, command, reason='', on_text=None):
        stop = hard_stop(command)
        if stop:
            return Decision(DENY, stop)
        ok, why = check(command)
        if ok:
            return Decision(ALLOW, why)
        return Decision(DENY, 'this subagent may only run read-only '
                        'commands, and the command %s' % why)
