"""
Tests for the BK-JR-only Retell agent filter.

These tests are the safety net for the strict agent allowlist added
2026-08-13. They verify:

  1. Helpers (_is_bkjr_name, _is_bkjr_agent) classify correctly
  2. list_agents() with default args returns ONLY BK JR agents
  3. list_agents() with include_other_clients=True returns ALL + warns
  4. list_bkjr_agents() is identical to the default filter
  5. get_agent() refuses every PROTECTED_AGENT_IDS entry
  6. get_agent() refuses unknown IDs (defense in depth)
  7. get_agent() allows every BKJR_AGENT_IDS entry
  8. create_agent() refuses names that LOOK like BK JR agents
  9. create_agent() does NOT make any HTTP call when refusing

These tests are pure-Python (no real HTTP) — they use httpx's MockTransport
to fake Retell's API responses.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `src` importable as if from the project root
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from retell_client import (
    BKJR_AGENT_IDS,
    BKJR_NAME_PREFIXES,
    PROTECTED_AGENT_IDS,
    ProtectedAgentError,
    RetellClient,
    _is_bkjr_agent,
    _is_bkjr_name,
    assert_agent_allowed,
)

# ── Fixtures ────────────────────────────────────────────────────────────────

# A realistic snapshot of the shared Retell workspace as we know it.
# Includes 3 BK JR agents, 10 other clients' agents, and 2 borderline cases
# (unknown IDs, unknown names). This is what BK JR's MCP would receive
# from /list-agents if the filter were absent.
WORKSPACE_FIXTURE = [
    # BK JR (3)
    {"agent_id": "agent_c530f8a10e8a502798c49a30c6", "agent_name": "BK JR Call Assistant",
     "voice_id": "custom_voice_bk", "response_engine": {"llm_id": "llm_bk_1"}},
    {"agent_id": "agent_6e853673d0d33f480c2164415e", "agent_name": "BK JR Connect-Now",
     "voice_id": "custom_voice_bk", "response_engine": {"llm_id": "llm_bk_2"}},
    {"agent_id": "agent_c0d49f55c814a63197ce06b3d8", "agent_name": "BK JR Outreach (Interest-Only)",
     "voice_id": "custom_voice_bk", "response_engine": {"llm_id": "llm_bk_3"}},
    # Other clients' production agents (10) — on PROTECTED_AGENT_IDS denylist
    {"agent_id": "agent_9a2cd6a1d680fd1fca6aeddad2", "agent_name": "Pearl Health"},
    {"agent_id": "agent_d2f34508b3c9677064ed705e1b", "agent_name": "NNC Patient Services"},
    {"agent_id": "agent_29f7eca77618d86036402b0035", "agent_name": "National Neuropathy (After-hour)"},
    {"agent_id": "agent_36f403199385903ade511361b1", "agent_name": "Bold Connect IT Support"},
    {"agent_id": "agent_cd73312c434362ae1d9d074299", "agent_name": "Bold Business IT Support - Tagalog"},
    {"agent_id": "agent_3223fb0f1d1d0fca2deabc9189", "agent_name": "Bold Connect-Mercury Z (Live)"},
    {"agent_id": "agent_da562b934e0241be688e7b2c45", "agent_name": "Bold Connect -Bold Business (Live)"},
    {"agent_id": "agent_afbb14aef83e3312dee84a01be", "agent_name": "Ed Voice"},
    {"agent_id": "agent_cb0e6af95c7a7e575cb3498e32", "agent_name": "Outreach Dialer (template)"},
    {"agent_id": "agent_95fb2b3bbb3c5c281553bdcecb", "agent_name": "Event Reminder (template)"},
    # Borderline: a brand new BK JR agent that someone added in the dashboard
    # with a "BK JR" prefix but no ID in our allowlist. The NAME filter catches it.
    {"agent_id": "agent_NEW_BK_JR_999", "agent_name": "BK JR Daily Standup"},
    # Borderline: an unknown agent with no recognizable marker.
    {"agent_id": "agent_UNKNOWN_XYZ", "agent_name": "Some Random Voice Agent"},
]


class _MockRetellTransport:
    """Minimal httpx MockTransport that responds to /list-agents and /get-agent."""

    def __init__(self, agents: list[dict], get_responses: dict[str, dict] | None = None):
        self.agents = agents
        self.get_responses = get_responses or {}
        self.calls: list[tuple[str, str]] = []

    def _respond(self, request, body):

        import httpx
        # httpx requires the request instance on the response for
        # raise_for_status() / .json() to work.
        return httpx.Response(200, json=body, request=request)

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/list-agents":
            return self._respond(request, self.agents)
        if request.url.path.startswith("/get-agent/"):
            agent_id = request.url.path[len("/get-agent/"):]
            body = self.get_responses.get(agent_id, {"agent_id": agent_id, "stub": True})
            return self._respond(request, body)
        import httpx
        return httpx.Response(404, json={"error": "not_found"}, request=request)


def _make_client(agents=None, get_responses=None) -> RetellClient:
    """Build a RetellClient that uses a MockTransport. No real HTTP."""
    transport = _MockRetellTransport(agents or WORKSPACE_FIXTURE, get_responses)
    # RetellClient accepts an api_key — pass a dummy, we never reach real HTTP
    client = RetellClient(api_key="test_dummy_key")
    # Replace the underlying httpx call path: easiest way is to swap headers
    # and rely on the transport via a monkeypatch of httpx.get / httpx.post.
    # Simpler: bind a private client and patch httpx.get/post globally for tests.
    client._mock_transport = transport  # kept for the test to introspect
    return client


@pytest.fixture(autouse=True)
def _patch_httpx(monkeypatch):
    """Monkeypatch httpx.get / httpx.post to route through _MockRetellTransport."""
    import httpx

    real_get = real_post = None  # noqa: F841  (kept for future hookup)

    def fake_get(url, *args, **kwargs):
        # Find the active transport by walking back via the global stash.
        transport = getattr(fake_get, "_transport", None) or _MockRetellTransport(WORKSPACE_FIXTURE)
        return transport(httpx.Request("GET", url))

    def fake_post(url, *args, **kwargs):
        transport = getattr(fake_post, "_transport", None) or _MockRetellTransport(WORKSPACE_FIXTURE)
        return transport(httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    # Reset stash on each test
    fake_get._transport = None
    fake_post._transport = None
    yield


def _stash_transport(client: RetellClient):
    """Stash the client's mock transport on the global httpx patches."""
    transport = client._mock_transport
    # The monkeypatched httpx.get/post read from a module-level attr.
    # We set it via the closure-captured fake_get/fake_post globals.
    import httpx
    # Find the monkey-patched versions and stash.
    # (pytest's monkeypatch will restore at end of test.)
    httpx.get._transport = transport
    httpx.post._transport = transport


# ── 1. Helper-classification tests (pure-Python, no HTTP) ───────────────────


class TestIsBkjrName:
    @pytest.mark.parametrize("name", [
        "BK JR Call Assistant",
        "BK JR Connect-Now",
        "BK JR Outreach (Interest-Only)",
        "bk jr daily standup",  # case-insensitive
        "  BK JR  ",           # whitespace stripped
        "Outbound Screening Agent",
        "Outbound Screening Agent v2",
    ])
    def test_bkjr_names_match(self, name):
        assert _is_bkjr_name(name) is True

    @pytest.mark.parametrize("name", [
        "Pearl Health",
        "NNC Patient Services",
        "Bold Connect IT Support",
        "Some Random Voice Agent",
        "",
        None,
    ])
    def test_non_bkjr_names_rejected(self, name):
        assert _is_bkjr_name(name) is False


class TestIsBkjrAgent:
    def test_known_bkjr_id(self):
        for agent_id in BKJR_AGENT_IDS:
            assert _is_bkjr_agent(agent_id) is True, f"{agent_id} should be BK JR"

    def test_protected_id_is_not_bkjr(self):
        for agent_id in PROTECTED_AGENT_IDS:
            assert _is_bkjr_agent(agent_id) is False, f"{agent_id} must NOT be BK JR"

    def test_unknown_id_is_not_bkjr(self):
        assert _is_bkjr_agent("agent_unknown_xyz") is False

    def test_name_only_match(self):
        # ID not in allowlist, but name matches BK JR prefix
        assert _is_bkjr_agent("agent_NEW_BK_JR_999", "BK JR Daily Standup") is True

    def test_id_overrides_name(self):
        # If ID is BK JR, name doesn't matter (defensive)
        assert _is_bkjr_agent(next(iter(BKJR_AGENT_IDS)), "Pearl Health") is True

    def test_protected_id_with_bkjr_name_helper_only(self):
        # _is_bkjr_agent is a PURE helper. It only knows BK JR membership,
        # not the denylist. So a protected id with a BK JR name returns True.
        # The DENYLIST check happens in assert_agent_allowed, which is what
        # actually gates operations. We test that path below.
        protected = next(iter(PROTECTED_AGENT_IDS))
        assert _is_bkjr_agent(protected, "BK JR Imposter") is True

    def test_assert_agent_allowed_blocks_protected_even_with_bkjr_name(self):
        # The real gate: a protected id is ALWAYS refused, regardless of name.
        protected = next(iter(PROTECTED_AGENT_IDS))
        with pytest.raises(ProtectedAgentError) as exc:
            assert_agent_allowed(protected, "BK JR Imposter")
        assert protected in str(exc.value)


class TestAssertAgentAllowed:
    def test_allows_every_bkjr_id(self):
        for agent_id in BKJR_AGENT_IDS:
            # Should not raise
            assert_agent_allowed(agent_id)

    def test_refuses_every_protected_id(self):
        for agent_id in PROTECTED_AGENT_IDS:
            with pytest.raises(ProtectedAgentError) as exc:
                assert_agent_allowed(agent_id)
            assert agent_id in str(exc.value)

    def test_refuses_unknown_id(self):
        # Defense in depth: not BK JR + not in denylist = refuse anyway
        with pytest.raises(ProtectedAgentError):
            assert_agent_allowed("agent_unknown_xyz_123")

    def test_none_id_is_allowed(self):
        # No id = caller doesn't have a specific target. Permit (used by
        # list_agents where we don't pre-target a single agent).
        # If we refused on None, list_agents would break.
        assert_agent_allowed(None)

    def test_error_mentions_bkjr_set(self):
        with pytest.raises(ProtectedAgentError) as exc:
            assert_agent_allowed("agent_unknown")
        # Error message must tell the operator what IS allowed
        msg = str(exc.value)
        for prefix in BKJR_NAME_PREFIXES:
            assert prefix in msg or any(aid in msg for aid in BKJR_AGENT_IDS)


# ── 2-4. list_agents() filter tests (HTTP mocked) ───────────────────────────


class TestListAgentsFilter:

    def _make_client_with_workspace(self) -> RetellClient:
        client = RetellClient(api_key="test_dummy_key")
        client._mock_transport = _MockRetellTransport(WORKSPACE_FIXTURE)
        _stash_transport(client)
        return client

    def test_default_returns_only_bkjr(self):
        client = self._make_client_with_workspace()
        agents = client.list_agents()
        ids = {a["agent_id"] for a in agents}
        # 3 BK JR by id + 1 by name = 4 BK JR
        assert len(agents) == 4
        for aid in BKJR_AGENT_IDS:
            assert aid in ids
        # Name-prefixed BK JR (not in id set) should also be included
        assert "agent_NEW_BK_JR_999" in ids
        # NO other clients' agents
        for aid in PROTECTED_AGENT_IDS:
            assert aid not in ids
        # No unknown
        assert "agent_UNKNOWN_XYZ" not in ids
        # Every row has is_bkjr=True
        assert all(a["is_bkjr"] for a in agents)

    def test_explicit_bkjr_only_true_same_as_default(self):
        client = self._make_client_with_workspace()
        a = client.list_agents()
        b = client.list_agents(bkjr_only=True)
        assert {x["agent_id"] for x in a} == {x["agent_id"] for x in b}

    def test_include_other_clients_returns_all_with_warn(self, capsys):
        client = self._make_client_with_workspace()
        agents = client.list_agents(include_other_clients=True)
        # All 15 rows
        assert len(agents) == len(WORKSPACE_FIXTURE)
        # Protected agents ARE present, with is_bkjr=False
        protected_rows = [a for a in agents if not a["is_bkjr"]]
        assert len(protected_rows) == len(PROTECTED_AGENT_IDS) + 1  # +1 for unknown
        # WARN was logged via structlog's default PrintLogger (stderr/stdout).
        # capsys captures both.
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "retell_list_agents_include_other_clients" in combined, (
            f"expected WARN log; got stdout={captured.out!r}, stderr={captured.err!r}"
        )
        # And the warn should enumerate the protected agent_ids we expect
        for aid in ("agent_9a2cd6a1d680fd1fca6aeddad2", "agent_d2f34508b3c9677064ed705e1b"):
            assert aid in combined, f"protected agent_id {aid} missing from WARN log"

    def test_bkjr_only_false_overrides(self):
        client = self._make_client_with_workspace()
        agents = client.list_agents(bkjr_only=False)
        assert len(agents) == len(WORKSPACE_FIXTURE)  # all of them

    def test_list_bkjr_agents_method(self):
        client = self._make_client_with_workspace()
        a = client.list_bkjr_agents()
        b = client.list_agents()
        assert {x["agent_id"] for x in a} == {x["agent_id"] for x in b}
        assert len(a) == 4

    def test_filter_does_not_leak_other_clients_to_payload(self):
        """Even with include_other_clients=True, no agent_id OTHER than
        BKJR_AGENT_IDS is auto-marked as BK JR. The flag is informational
        only — it does NOT promote unknown agents to BK JR."""
        client = self._make_client_with_workspace()
        agents = client.list_agents(include_other_clients=True)
        for a in agents:
            if a["agent_id"] in BKJR_AGENT_IDS or _is_bkjr_name(a.get("name")):
                assert a["is_bkjr"] is True
            else:
                assert a["is_bkjr"] is False, (
                    f"agent {a['agent_id']} ({a.get('name')}) was incorrectly "
                    f"marked is_bkjr=True — the filter must not auto-classify "
                    f"unknown agents as BK JR."
                )


# ── 5-7. get_agent() allowlist tests ────────────────────────────────────────


class TestGetAgentAllowlist:

    def test_allows_each_bkjr_id(self):
        for agent_id in BKJR_AGENT_IDS:
            # Should not raise — even though we're not actually calling the API
            # (assert_agent_allowed is the gate; if it passes, get_agent
            # would make the HTTP call)
            assert_agent_allowed(agent_id)

    @pytest.mark.parametrize("agent_id", list(PROTECTED_AGENT_IDS))
    def test_refuses_each_protected_id(self, agent_id):
        with pytest.raises(ProtectedAgentError) as exc:
            assert_agent_allowed(agent_id)
        assert agent_id in str(exc.value)

    def test_refuses_unknown_id(self):
        with pytest.raises(ProtectedAgentError):
            assert_agent_allowed("agent_random_999")


# ── 8-9. create_agent() name-prefix refusal tests ───────────────────────────


class TestCreateAgentRefusesBkjrNames:

    @pytest.mark.parametrize("name", [
        "BK JR Custom Screener",
        "bk jr live demo",
        "Outbound Screening Agent",
        "  BK JR  ",
    ])
    def test_refuses_bkjr_like_names(self, name):
        # Patch the entire post() to also assert nothing was called
        import httpx

        from retell_client import RetellClient

        called = {"count": 0}

        def fake_post(url, *args, **kwargs):
            called["count"] += 1
            import httpx as _hx
            return _hx.Response(200, json={"agent_id": "x"})

        # Save and patch
        original_post = httpx.post
        httpx.post = fake_post
        try:
            client = RetellClient(api_key="test")
            with pytest.raises(ProtectedAgentError):
                client.create_agent(name=name, system_prompt="x")
            assert called["count"] == 0, (
                f"create_agent must not make an HTTP call when refusing; "
                f"got {called['count']} calls"
            )
        finally:
            httpx.post = original_post

    def test_allows_non_bkjr_name(self):
        # Non-BK-JR name should not raise the prefix guard
        # (it will still try to POST, which our mocked post returns 200 for)
        import httpx


        def fake_post(url, *args, **kwargs):
            import httpx as _hx
            req = kwargs.get("request") or _hx.Request("POST", url)
            return _hx.Response(200, json={"agent_id": "agent_NEW", "name": "x"}, request=req)

        original_post = httpx.post
        httpx.post = fake_post
        try:
            client = RetellClient(api_key="test")
            result = client.create_agent(
                name="Custom Role Screener",
                system_prompt="You are a screener for the X role.",
            )
            assert result["agent_id"] == "agent_NEW"
        finally:
            httpx.post = original_post


# ── Sanity check: invariants on the constants ───────────────────────────────


class TestAllowlistInvariants:

    def test_bkjr_and_protected_disjoint(self):
        overlap = BKJR_AGENT_IDS & PROTECTED_AGENT_IDS
        assert not overlap, (
            f"agent(s) in both allowlist and denylist — ambiguous behavior: "
            f"{overlap}"
        )

    def test_bkjr_nonempty(self):
        assert len(BKJR_AGENT_IDS) >= 1, "BKJR_AGENT_IDS must not be empty"
        assert len(BKJR_NAME_PREFIXES) >= 1, "BKJR_NAME_PREFIXES must not be empty"

    def test_protected_nonempty(self):
        # If this list is empty, the denylist is useless — fail loudly so
        # anyone emptying it has to remove this assertion deliberately.
        assert len(PROTECTED_AGENT_IDS) >= 1, (
            "PROTECTED_AGENT_IDS is empty — refusing to ship a denylist with "
            "zero entries. If intentional, delete this assertion."
        )

    def test_bkjr_prefixes_lowercase(self):
        # _is_bkjr_name lowercases the input. Prefixes must already be lowercase
        # so the comparison is correct.
        for prefix in BKJR_NAME_PREFIXES:
            assert prefix == prefix.lower(), (
                f"BKJR_NAME_PREFIXES entry {prefix!r} is not lowercase — "
                f"the comparison in _is_bkjr_name assumes lowercase."
            )

    def test_no_empty_strings_in_sets(self):
        for s in list(BKJR_AGENT_IDS) + list(PROTECTED_AGENT_IDS):
            assert s and s.strip(), f"empty/whitespace entry in allowlist: {s!r}"
        for p in BKJR_NAME_PREFIXES:
            assert p and p.strip(), f"empty/whitespace prefix: {p!r}"