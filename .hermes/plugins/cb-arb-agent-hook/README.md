# cb-arb Agent Hook Plugin

This local Hermes plugin injects lightweight cb-arb project guidance before LLM calls that mention cb-arb plus agent/review/delegation work. It keeps the standalone `/home/hermes/projects/cb-arb` workspace isolated from the broader Hermes systems while giving the parent agent enough context to avoid broad rediscovery.

## Behavior

- Default mode (`CB_ARB_HOOK_MODE=review`) attempts one concise cross-functional specialist review through `delegate_task`.
- Explicit `/cbarbpanel [request]` or `CB_ARB_HOOK_MODE=panel` attempts a focused multi-role specialist panel.
- `CB_ARB_HOOK_MODE=light` or the one-turn phrase `no-cbarb-panel` keeps behavior to deterministic lightweight context only.
- If the plugin runs in a gateway/pre-LLM context where `delegate_task` lacks a parent-agent context, it must not inject a failed review blob. It falls back to deterministic project guidance and tells the parent agent to run manual delegation only if a parent-agent context is available.

## Boundary Rules

- Do not inspect or mutate `/home/hermes/projects/cb-arb` as part of broad Hermes system reviews unless the user explicitly asks for a project-scoped cb-arb review.
- The hook may read small project guidance excerpts to build context, but it must not run cb-arb code, install dependencies, or write project files.
- Specialist output is advisory context only; the parent agent remains responsible for verifying any material findings before implementation.

## Verification

```bash
python3 -m py_compile /home/hermes/.hermes/plugins/cb-arb-agent-hook/__init__.py /home/hermes/.hermes/plugins/cb-arb-agent-hook/test_cb_arb_agent_hook.py
python3 /home/hermes/.hermes/plugins/cb-arb-agent-hook/test_cb_arb_agent_hook.py
```

Expected regression: when delegation is unavailable (`_CTX` missing, dispatch raises/returns `requires a parent agent context`, or JSON contains `delegate_task requires a parent agent context`), default review, first-round panel, and two-round panel reconciliation paths emit `FALLBACK CONTEXT` rather than `CB-ARB SINGLE SPECIALIST REVIEW RESULTS` or `CB-ARB FOCUSED SPECIALIST PANEL RESULTS`. The fallback sanitizes parent-context details to `delegate_task unavailable in this context`.
