# BK Jr. - Cloneable Agent Profile

Drop-in profile for the BK Jr. recruiting MCP. Agent-agnostic: nothing here is
tied to one client.

    AGENT.md                  identity, transport, the 24 tools
    USER.md                   who BK is, what BK wants, when to escalate
    SOUL.md                   voice, principles, refusals
    rules/operating-rules.md  R1-R8, the hard constraints
    skills/                   outreach - screening calls - building agents
    connectors/               Claude Desktop (bridge), generic MCP, raw HTTP
    hooks/                    confirmation, opt-out, write-back, error halts

## Live endpoints
- MCP:     https://bkjr-mcp.getbijou.xyz/mcp   (24 tools, bearer required)
- Health:  https://bkjr-mcp.getbijou.xyz/health
- Backend: https://bkjr-api.getbijou.xyz       (webhook: POST /webhook/quo)

## Clone it
1. Copy `agent-profile/` into your agent context directory.
2. Set `SMS_AGENT_API_KEY` in your secret store (never commit it).
3. Wire a connector from `connectors/` - read the gotchas for your client.
4. Verify: `GET /health` -> `{"tools":24}`, then the 401/401/200 auth check.

## Load order
`AGENT.md` -> `USER.md` -> `SOUL.md` -> `rules/` -> the relevant `skills/` file.
Rules outrank skills; skills outrank tone.

## Scaling
Adding a tool to the MCP server exposes it automatically via `tools/list` -
update the capability list in `AGENT.md` and add a skill if it needs judgement.
The same profile drives Claude Desktop, a recruiter dashboard, or a headless loop.
