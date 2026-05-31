#!/usr/bin/env python3
"""Regression tests for cb-arb-agent-hook fallback behavior."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

PLUGIN_PATH = Path(__file__).with_name("__init__.py")


def load_plugin():
    spec = importlib.util.spec_from_file_location("cb_arb_agent_hook_under_test", PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FailingDelegateContext:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        return json.dumps(self.payload)


class SequencedDelegateContext:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def dispatch_tool(self, name, args):
        self.calls.append((name, args))
        if not self.payloads:
            raise AssertionError("unexpected extra delegate dispatch")
        return json.dumps(self.payloads.pop(0))


class RaisingDelegateContext:
    def dispatch_tool(self, name, args):
        raise RuntimeError("requires a parent agent context.")


class RawDelegateContext:
    def dispatch_tool(self, name, args):
        return "requires a parent agent context."


def assert_parent_error_sanitized(context: str):
    assert "delegate_task requires a parent agent context" not in context
    assert "delegate_task unavailable in this context" in context


def test_brief_review_falls_back_when_delegate_requires_parent_context():
    plugin = load_plugin()
    setattr(plugin, "_CTX", FailingDelegateContext({"error": "delegate_task requires a parent agent context."}))

    result = plugin._brief_review_context("Please review cb-arb agents for PM workflow")

    assert result and "context" in result
    assert "FALLBACK CONTEXT" in result["context"]
    assert_parent_error_sanitized(result["context"])
    assert "SINGLE SPECIALIST REVIEW RESULTS" not in result["context"]


def test_explicit_panel_falls_back_when_delegate_requires_parent_context():
    plugin = load_plugin()
    setattr(plugin, "_CTX", FailingDelegateContext({"error": "delegate_task requires a parent agent context."}))

    result = plugin._panel_context("Run cbarbpanel for cb-arb review")

    assert result and "context" in result
    assert "FALLBACK CONTEXT" in result["context"]
    assert_parent_error_sanitized(result["context"])
    assert "FOCUSED SPECIALIST PANEL RESULTS" not in result["context"]


def test_brief_review_falls_back_when_plugin_context_missing():
    plugin = load_plugin()
    setattr(plugin, "_CTX", None)

    result = plugin._brief_review_context("Please review cb-arb agents for PM workflow")

    assert result and "context" in result
    assert "FALLBACK CONTEXT" in result["context"]
    assert "plugin context unavailable" not in result["context"]
    assert "SINGLE SPECIALIST REVIEW RESULTS" not in result["context"]


def test_brief_review_falls_back_when_dispatch_raises_parent_context_error():
    plugin = load_plugin()
    setattr(plugin, "_CTX", RaisingDelegateContext())

    result = plugin._brief_review_context("Please review cb-arb agents for PM workflow")

    assert result and "context" in result
    assert "FALLBACK CONTEXT" in result["context"]
    assert_parent_error_sanitized(result["context"])
    assert "SINGLE SPECIALIST REVIEW RESULTS" not in result["context"]


def test_brief_review_falls_back_when_dispatch_returns_raw_parent_context_error():
    plugin = load_plugin()
    setattr(plugin, "_CTX", RawDelegateContext())

    result = plugin._brief_review_context("Please review cb-arb agents for PM workflow")

    assert result and "context" in result
    assert "FALLBACK CONTEXT" in result["context"]
    assert_parent_error_sanitized(result["context"])
    assert "SINGLE SPECIALIST REVIEW RESULTS" not in result["context"]


def test_two_round_panel_falls_back_when_reconciliation_delegate_fails():
    plugin = load_plugin()
    setattr(
        plugin,
        "_CTX",
        SequencedDelegateContext(
            [
                {"results": [{"summary": "round one ok"}]},
                {"error": "delegate_task requires a parent agent context."},
            ]
        ),
    )
    old_rounds = os.environ.get("CB_ARB_PANEL_ROUNDS")
    os.environ["CB_ARB_PANEL_ROUNDS"] = "2"
    try:
        result = plugin._panel_context("Run cbarbpanel for cb-arb review")
    finally:
        if old_rounds is None:
            os.environ.pop("CB_ARB_PANEL_ROUNDS", None)
        else:
            os.environ["CB_ARB_PANEL_ROUNDS"] = old_rounds

    assert result and "context" in result
    assert "FALLBACK CONTEXT" in result["context"]
    assert_parent_error_sanitized(result["context"])
    assert "FOCUSED SPECIALIST PANEL RESULTS" not in result["context"]


if __name__ == "__main__":
    test_brief_review_falls_back_when_delegate_requires_parent_context()
    test_explicit_panel_falls_back_when_delegate_requires_parent_context()
    test_brief_review_falls_back_when_plugin_context_missing()
    test_brief_review_falls_back_when_dispatch_raises_parent_context_error()
    test_brief_review_falls_back_when_dispatch_returns_raw_parent_context_error()
    test_two_round_panel_falls_back_when_reconciliation_delegate_fails()
    print("cb-arb-agent-hook fallback tests passed")
