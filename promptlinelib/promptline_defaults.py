# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""promptline_defaults.py - Promptline's configuration defaults

Kept out of config.py's DEFAULTS literal, where upstream Terminator adds its
own options, so that merging upstream releases doesn't conflict. This module
must not import anything from promptlinelib: config.py imports it.
"""

GLOBAL_DEFAULTS = {
    'promptline_enabled': True,
    'promptline_shell_integration': True,
    'promptline_autocomplete': True,
    # Model-based command prediction sends terminal context to the provider,
    # so it is opt-in
    'promptline_llm_autocomplete': False,
    'promptline_predict_next': True,
    # A name from providers.PRESETS. The base URL and key variable are
    # overrides: empty means the preset's own
    'promptline_provider': 'openai',
    'promptline_base_url': '',
    'promptline_api_key_env': '',
    'promptline_api_key_file': '',
    # Prediction and @agent can use a different provider from the one above.
    # An empty provider means "use the one above"; if set, these four
    # replace it as a group
    'promptline_autocomplete_provider': '',
    'promptline_autocomplete_base_url': '',
    'promptline_autocomplete_api_key_env': '',
    'promptline_autocomplete_api_key_file': '',
    'promptline_agent_provider': '',
    'promptline_agent_base_url': '',
    'promptline_agent_api_key_env': '',
    'promptline_agent_api_key_file': '',
    'promptline_autocomplete_model': 'gpt-6-luna',
    'promptline_autocomplete_reasoning': 'xhigh',
    'promptline_agent_model': 'gpt-6-luna',
    'promptline_agent_reasoning': 'xhigh',
    # ask | auto-review | full. Full also needs the user's guardrails
    'promptline_agent_mode': 'ask',
    # How many model replies one @agent request may take. @agent --goal
    # has no limit
    'promptline_agent_max_steps': 25,
    # The context window, in tokens, that @agent plans for. Older steps are
    # summarised when a conversation passes most of it. A model that has
    # less says so, and the agent summarises and tries again
    'promptline_context_window': 1000000,
    # Subagents: off | auto (@agent may hand tasks to them) | always (it is
    # told to, and runs the flow below first, if there is one). Reasoning
    # 'ultra' always uses subagents and its own flow, whatever this says
    'promptline_subagents': 'off',
    # The name of a flow in ~/.config/promptline/flows/NAME.md
    'promptline_subagent_flow': '',
    # How many subagents may work at the same time
    'promptline_subagent_parallel': 4,
    # Minutes a goal run waits for an approval before skipping that command
    # and carrying on with other work. 0 waits for ever
    'promptline_goal_approval_wait': 15,
    'promptline_review_reasoning': 'medium',
}

# Changes to Terminator's own profile defaults
PROFILE_DEFAULTS = {
    # The focused terminal's titlebar: dark grey instead of Terminator's
    # red. Blue stays reserved for terminals receiving broadcast input
    # (title_receive_bg_color), so the two remain easy to tell apart.
    'title_transmit_bg_color': '#2e3436',
}
