<!-- cuga-harness-kit:start -->

## cuga-author-policy

_Use when the user wants to govern agent behavior with a cuga policy - block/redirect an intent, add step-by-step playbook guidance, require approval before a tool runs, enhance a tool's description, or reshape agent output format._

## Authoring a cuga policy

A cuga **policy** is a rule the runtime enforces on a `CugaAgent`, unlike a runtime *skill* (`cuga-build-cuga-skill`) which the agent optionally chooses to load. There are 5 policy types.

### Step 1 — identify which type

| The user wants to... | Type |
|---|---|
| Block or redirect a specific kind of request | `intent_guard` |
| Give the agent step-by-step guidance for a workflow | `playbook` |
| Require human approval before a tool runs | `tool_approval` |
| Add usage guidance/examples to a tool's description | `tool_guide` |
| Reshape the agent's response into JSON/table/markdown/etc. | `output_formatter` |

### Step 2 — pick programmatic SDK or markdown file

**Programmatic (recommended for code you already control):** attach at agent construction time via `agent.policies`:

```python
await agent.policies.add_intent_guard(
    name="Block Delete Operations",
    description="Prevents deletion of critical data",
    keywords=["delete", "remove", "erase"],
    response="Deletion operations are not permitted for security reasons.",
    priority=100,  # higher = checked first
)

await agent.policies.add_playbook(
    name="Budget Analysis Workflow",
    description="Multi-step process for analyzing financial budgets",
    natural_language_trigger=["When user asks to analyze their budget"],
    content="# Budget Analysis Workflow\n\n## Step 1: ...",
    priority=50,
)
```

**Markdown file (for the `manager` web UI, or policies you want checked into a project):** save a file with YAML frontmatter + markdown body to the matching `.cuga/` subfolder, then restart or re-publish so it's picked up:

| Type | Save to |
|---|---|
| `playbook` | `.cuga/playbooks/playbook_<name>.md` |
| `intent_guard` | `.cuga/guards/guard_<name>.md` |
| `tool_guide` | `.cuga/guides/guide_<name>.md` |
| `tool_approval` | `.cuga/approvals/approval_<name>.md` |
| `output_formatter` | `.cuga/formatters/formatter_<name>.md` |

Shared frontmatter fields across all 5 types: `id`, `name`, `description`, `enabled`, `priority`, `type`, `triggers` (shape varies by type — see below).

### Mode: Playbook

```yaml
---
description: Brief description of what this playbook does
enabled: true
id: playbook_<unique_id>
name: <Playbook Name>
priority: 50
triggers:
  natural_language:
  - keyword 1
  - keyword phrase
  target: intent
  threshold: 0.5
type: playbook
---
## <Title>
### Overview
### Parameters
- **parameter_name** (required/optional): description
### Steps
#### 1. <Step Name> — constraints: MUST / SHOULD / MAY
### Examples
### Troubleshooting
### Best Practices
```

### Mode: Intent Guard

```yaml
---
description: Description of what intents this guard blocks
enabled: true
id: guard_<unique_id>
name: <Guard Name>
priority: 90        # 90-100 recommended: guards should win priority ties
triggers:
  natural_language:
  - blocked intent 1
  target: intent
  threshold: 0.7     # strict matching for guards
type: intent_guard
intent_examples:
- 5+ example phrases of the blocked intent, for matching
response:
  response_type: natural_language   # natural_language | json | template
  content: |
    Custom message explaining why this is blocked + alternatives.
allow_override: false   # true = user can bypass, false = enforced
---
```

### Mode: Tool Guide

```yaml
---
description: Enhanced guidance for specific tools
enabled: true
id: guide_<unique_id>
name: <Guide Name>
priority: 50
triggers:
  tool_match:
  - tool_name_1
  target: tools
type: tool_guide
target_tools: [tool_name_1, tool_name_2]   # or target_apps: [app_name] for all tools in an app
guide_content: |
  ## When to Use / Best Practices / Common Pitfalls / Parameter Guidelines / Examples / Related Tools
prepend: false   # true = insert before the tool's own description, false = after
---
```

### Mode: Tool Approval

```yaml
---
description: Require approval for sensitive operations
enabled: true
id: approval_<unique_id>
name: <Approval Policy Name>
priority: 100   # highest priority recommended
triggers:
  tool_match: [sensitive_tool_1]
  target: tools
type: tool_approval
required_tools: [sensitive_tool_1]     # or required_apps: [app_name]
approval_message: |
  Explain why approval is required and what will happen.
show_code_preview: true
auto_approve_after: null   # null = never auto-approve (recommended); or seconds e.g. 30/60/300
---
```

### Mode: Output Formatter

```yaml
---
description: Format output in a specific structure
enabled: true
id: formatter_<unique_id>
name: <Formatter Name>
priority: 50
triggers:
  natural_language: [format keyword 1]
  target: agent_response   # must be agent_response for formatters
  threshold: 0.6
type: output_formatter
format_type: json   # json | markdown | table | csv | custom
format_config: |
  {"schema": {"type": "object", "properties": {"field1": {"type": "string"}}, "required": ["field1"]}}
---
```

### Testing

```bash
uv run cuga start competition --local
```

Trigger the policy with matching keywords/intent and confirm the expected block/guidance/approval/format behavior shows up.

### Reference

Full worked templates: `docs/starterkit/.cursor/{intent_guard,playbook,tool_guide,tool_approval,output_formatter}.md` in the cuga-agent repo. Policy data models: `src/cuga/backend/cuga_graph/policy/models.py`. SDK docs: https://docs.cuga.dev/docs/sdk/policies/

## cuga-build-agent

_Use when the user wants to write Python code that creates or invokes a CugaAgent or CugaSupervisor, e.g. "build an agent with cuga", "how do I call agent.invoke", "set up a multi-agent supervisor"._

## Building with the CugaAgent SDK

### Single agent

```python
from cuga import CugaAgent
from langchain_core.tools import tool
import asyncio

@tool
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together"""
    return a + b

agent = CugaAgent(tools=[add_numbers])

async def main():
    result = await agent.invoke("What is 5 + 3?")
    print(result.answer)

asyncio.run(main())
```

Key points:
- Tools are plain LangChain `@tool`-decorated functions (or an OpenAPI/MCP provider — see `cuga-build-tool`).
- `await agent.invoke(message, thread_id=...)` — `thread_id` isolates conversation state per user/session; omit it for a one-off call.
- `agent.stream()` gives real-time execution events instead of a single final result.
- `agent.policies` is the entry point for attaching Intent Guard / Playbook / Tool Approval / Tool Guide / Output Formatter policies programmatically — see `cuga-author-policy`.
- Knowledge/RAG is on by default (`enable_knowledge=True`) — see `cuga-knowledge-rag`; pass `enable_knowledge=False` to turn it off.
- The underlying LangGraph graph is reachable for advanced use cases (custom nodes, inspecting state) if the simple API isn't enough.

### Multi-agent (CugaSupervisor)

```python
from cuga import CugaAgent, CugaSupervisor
from langchain_core.tools import tool
import asyncio

@tool
def get_customers(limit: int = 10) -> str:
    """Fetch top customers from CRM."""
    return "Alice ($250k); Bob ($180k)"

@tool
def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"Email sent to {to}"

async def main():
    crm_agent = CugaAgent(tools=[get_customers])
    crm_agent.description = "CRM and customer data"

    email_agent = CugaAgent(tools=[send_email])
    email_agent.description = "Sending emails and notifications"

    supervisor = CugaSupervisor(agents={"crm": crm_agent, "email": email_agent})
    result = await supervisor.invoke("Get our top customer and email them a thank-you")
    print(result.answer)

asyncio.run(main())
```

- Each sub-agent needs a `.description` — the supervisor uses it to decide who handles what.
- Mix local `CugaAgent`s with remote agents via A2A: pass an `"agent_name": {"type": "external", "description": "...", "config": {"a2a_protocol": {...}}}` entry in `agents=`.
- Pass data between sub-agents with `variables=["var_name"]`.
- `CugaSupervisor.from_yaml("path/to/config.yaml")` loads agents from a config file instead of constructing them in code.
- Try it live first: `uv run cuga start demo_supervisor` (see `cuga-install-and-launch`).

### Reference

Full SDK docs: https://docs.cuga.dev/docs/sdk/cuga_agent/ and https://docs.cuga.dev/docs/sdk/cuga_supervisor

## cuga-build-cuga-skill

_Use when the user wants a running CugaAgent to load a new capability at runtime via cuga's own skill system, e.g. "add a skill to my agent", "create a .cuga/skills SKILL.md", "how does load_skill work"._

## Authoring a cuga runtime skill

This is different from the IDE-assistant skill you're reading right now. **cuga's own agents** can discover and load `SKILL.md` files at runtime via a `load_skill` tool — this skill teaches you how to author one of those.

### Where skills live

Controlled by `[skills] root` in `settings.toml` (or `DYNACONF_SKILLS__ROOT`):

| `root` value | Directory | Notes |
|---|---|---|
| `cuga` (default) | `.cuga/skills/` | cuga's own convention, avoids colliding with `.agents/skills/` written by other tools |
| `agents` | `.agents/skills/` | shared convention used by e.g. `npx skills` |
| `global_agents` | `~/.config/agents/skills/` | global, not project-scoped |
| `global_cuga` | `~/.config/cuga/skills/` | legacy global path |

Skills are discovered recursively (`**/SKILL.md`) under whichever root is active.

### SKILL.md shape (cuga runtime contract)

Required frontmatter: **`name`** and **`description`** (shown to the agent in its available-skills list — write a description the agent can use to decide when to load it). Optional **`requirements`**: a pip/npm package or list of packages the skill needs installed. Everything below the frontmatter is arbitrary markdown — the full instruction text handed back by `load_skill` when the agent loads this skill.

```markdown
---
name: my-skill
description: Use when the user asks to <do X>. Loads instructions for <Y>.
requirements: some-pip-package
---

## My Skill

Step-by-step instructions, code snippets, examples — whatever the agent
needs in its context to actually perform the task.
```

Name validation: no path separators or `..` in `name` — cuga sanitizes/rejects unsafe or Jinja2-template-injected values in `name`/`description` for prompt-injection safety.

### Try it

```bash
uv run cuga start demo_skills
```

Runs with `[advanced_features] sandbox_mode = "native"` by default. For sandboxed execution instead: `uv sync --extra opensandbox` then use the `opensandbox` mode, or `uv sync --group sandbox` + `uv run cuga start demo --sandbox` with `[skills]` enabled for Docker/Podman isolation.

### Installing a ready-made skill

Anthropic publishes ready-made skill folders (e.g. `pptx`) that follow the same `SKILL.md` convention:

```bash
npx skills add https://github.com/anthropics/skills --skill pptx -a universal
```

This drops it under `.agents/skills/pptx/SKILL.md`. To use cuga's default layout instead, copy/symlink it into `.cuga/skills/`. Add `-g` to install globally. Restart `uv run cuga start demo_skills` (or your app) afterward so skills are rescanned — there's no hot-reload.

### Don't confuse this with policies

A runtime skill is instructional content the agent chooses to load. A policy (playbook, intent guard, etc. — see `cuga-author-policy`) is a rule the runtime *enforces* on the agent regardless of whether it "chooses" to. Use a skill for optional know-how, a policy for a constraint or workflow that must always apply.

## cuga-build-tool

_Use when the user wants to give a cuga agent a new capability via a Python function, REST/OpenAPI service, or MCP server, e.g. "add a tool", "connect this API to cuga", "register an MCP server"._

## Registering tools with cuga

cuga supports three tool integration types:

| Type | Best for | Configured via | Loading |
|---|---|---|---|
| **LangChain** | Python functions, rapid prototyping | direct import, pass to `CugaAgent(tools=[...])` | Runtime |
| **OpenAPI** | REST APIs, existing services | `mcp_servers.yaml` | Build-time |
| **MCP** | Custom protocols, complex integrations | `mcp_servers.yaml` | Build-time |

### LangChain tool (fastest path)

```python
from langchain_core.tools import tool

@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by ID and return its status."""
    ...

agent = CugaAgent(tools=[lookup_order])
```

This is a runtime-loaded tool: no config file, no restart needed, just pass it into the `CugaAgent` constructor. Write a clear docstring — it becomes the tool description the agent's reasoning engine sees.

### OpenAPI / MCP (registry-based)

For REST APIs or MCP servers, add an entry to `mcp_servers.yaml` (path: `src/cuga/backend/tools_env/registry/config/mcp_servers.yaml` in a cuga-agent checkout). These are build-time: the tool registry (`cuga start registry`, or bundled into any `demo_*`/`manager` service) loads this config at startup, so changes need a restart to take effect.

See the registry's own README for the exact config schema and worked examples: `src/cuga/backend/tools_env/registry/README.md`, plus `docs/examples/cuga_with_runtime_tools/README.md` for a full walkthrough combining different tool types with MCP.

### Which to reach for

- One-off Python function, prototyping, or logic that lives in your app already → LangChain tool.
- An existing REST API you don't want to hand-wrap → OpenAPI entry in `mcp_servers.yaml`.
- A third-party or custom MCP server → MCP entry in `mcp_servers.yaml`.

### Enhancing how the agent uses a tool

Once a tool exists, you can shape *how* the agent uses it without touching its code — that's a `tool_guide` or `tool_approval` policy (require confirmation before a sensitive tool runs, or append extra usage guidance/examples to its description). See `cuga-author-policy`.

## cuga-debug-trajectory

_Use when a cuga agent run misbehaved (wrong tool called, silently blocked, wrong output format) and the user wants to inspect what actually happened._

## Debugging a cuga agent run

### Trajectory viewer

Every run is logged to a trajectory data directory (`<cuga log dir>/trajectory_data`). Inspect it visually:

```bash
cuga viz
```

Launches a web dashboard for browsing execution trajectories: what the reasoning engine decided at each step, which tools it called with what arguments, and what came back. Start here before reading raw logs — it's usually faster to spot the wrong branch/tool call visually than to grep JSON.

### Environment sanity check

```bash
cuga doctor
```

Run this first if the agent won't even start, or behaves inconsistently across environments — it validates the environment/dependency setup.

### `cuga status`

```bash
cuga status <service|all>
```

Confirms whether the service you expect to be running actually is, before chasing a bug that's really just "nothing is listening on that port."

### Common failure patterns

- **Tool "not found" or never called** — check the tool actually got passed to `CugaAgent(tools=[...])`, or (for OpenAPI/MCP tools) that the registry service (`cuga start registry`, or the registry bundled in a `demo_*`/`manager` service) picked up your `mcp_servers.yaml` change — those are build-time, not hot-reloaded.
- **Request silently redirected/blocked with no obvious reason** — an `intent_guard` policy may be matching. Check `.cuga/guards/` (or policies added via `agent.policies.add_intent_guard`) for a guard whose `triggers`/`intent_examples` overlap the query, and check `priority` — guards default to high priority (90-100) and are checked before other policies. See `cuga-author-policy`.
- **Response in the wrong shape** — an `output_formatter` policy may be firing (or not firing when expected) based on its `triggers.natural_language` keywords and `threshold`. Lower/raise the threshold or adjust keywords.
- **Tool paused waiting on nothing** — a `tool_approval` policy is likely gating that tool; check `.cuga/approvals/` for `required_tools`/`required_apps` matches and `auto_approve_after`.
- **`thread_id` confusion** — state (conversation history, session-scoped knowledge) is isolated per `thread_id`. If a run seems to have "forgotten" something from a previous call, confirm the same `thread_id` was passed both times.
- **Multi-agent: wrong sub-agent picked** — the `CugaSupervisor` routes based on each sub-agent's `.description`. Vague or overlapping descriptions between sub-agents cause misrouting — make them distinct and specific.

### Policy-level test coverage

If you're debugging policy interaction bugs specifically, the policy engine's own integration tests are a good reference for expected behavior: `src/cuga/backend/cuga_graph/policy/tests/` in a cuga-agent checkout (covers intent guard blocking/priority resolution, playbook guidance injection, and more).

## cuga-getting-started

_Use when the user wants to install, launch, or build something with the cuga agent framework (pip package `cuga`), or asks "how do I use cuga" / "how do I build an agent with cuga" — routes to the right cuga-harness-kit skill instead of guessing._

## Getting started with cuga

cuga (`uv add cuga`, [github.com/cuga-project/cuga-agent](https://github.com/cuga-project/cuga-agent)) is an agent orchestration framework: a reasoning engine + pluggable tools (OpenAPI/MCP/LangChain) + a policy system + optional RAG knowledge base + multi-agent supervision, built on LangGraph.

This skill is the entry point. Read the description of the skill that matches what the user is doing before improvising:

| User is trying to... | Use skill |
|---|---|
| Install cuga or start the demo UI | `cuga-install-and-launch` |
| Write Python code that creates/invokes a `CugaAgent` or `CugaSupervisor` | `cuga-build-agent` |
| Author a new **runtime** skill the agent itself can load (`.cuga/skills/<name>/SKILL.md`) | `cuga-build-cuga-skill` |
| Register a Python function, OpenAPI spec, or MCP server as a callable tool | `cuga-build-tool` |
| Add a policy: block an intent, add a playbook, require approval, enhance a tool description, or reshape output | `cuga-author-policy` |
| Ingest/search documents (RAG) | `cuga-knowledge-rag` |
| An agent run misbehaved and you need to inspect why | `cuga-debug-trajectory` |

### Two different meanings of "skill"

Don't conflate these — they are unrelated mechanisms that happen to share the `SKILL.md` filename convention:

1. **This skill** (and its siblings) is an *IDE-assistant* skill — it teaches you, the coding assistant, how to work with cuga.
2. **cuga's own runtime skills** (`.cuga/skills/<name>/SKILL.md`) are loaded by a running `CugaAgent` via a `load_skill` tool, and are what the *agent itself* can call at runtime. See `cuga-build-cuga-skill` when the user wants one of these.

### Ground truth over memory

cuga's CLI and SDK surface can change between versions. If something in these skills looks stale (a flag, a command, a class name), verify against the installed package before trusting the skill text: `python -c "import cuga; print(cuga.__file__)"`, `cuga --help`, or the repo at `src/cuga/` if working inside a checkout.

## cuga-install-and-launch

_Use when the user wants to install cuga, start the demo/web UI, or asks what `cuga start` does or which service/mode to run._

## Installing and launching cuga

### Install

For a new app project, initialize with `uv` and add `cuga` as a dependency so it is recorded in `pyproject.toml`:

```bash
uv init my-cuga-app
cd my-cuga-app
uv add cuga
```

For an existing project that already has a `pyproject.toml`, run `uv add cuga` from the project root. If the user only wants a quick install inside an already-active virtualenv, `uv pip install cuga` is acceptable.

(`cuga` requires Python >= 3.10, < 3.14.) For working inside a checkout of the [cuga-agent](https://github.com/cuga-project/cuga-agent) repo itself instead of the published package:

```bash
git clone https://github.com/cuga-project/cuga-agent.git
cd cuga-agent
uv venv --python=3.12 && source .venv/bin/activate
uv sync
```

Set LLM API keys before starting anything. Use a project-local `.env`; see `docs/cuga-env-api-keys.md` for provider-specific `AGENT_SETTING_CONFIG`, `MODEL_NAME`, and API-key examples.

### Launch

```bash
uv run cuga start <service>
```

Valid `<service>` values: `demo`, `demo_skills`, `demo_crm`, `demo_docs`, `demo_health`, `demo_knowledge`, `demo_supervisor`, `travel_agent`, `manager`, `registry`, `appworld`.

- `demo` / `demo_crm` / `demo_supervisor` / `demo_knowledge` / `demo_docs` — launch the chat web UI at `https://localhost:7860` plus a tool registry service on port 8001, each preset wiring up different sample tools (CRM+email, multi-agent supervisor, RAG knowledge, docs) so you can try the matching capability immediately.
- `demo_skills` — same UI, with cuga's own runtime skill-loading enabled (see `cuga-build-cuga-skill`).
- `manager` — a draft/publish UI: edit agent config (tools, MCP servers, LLM, policies) as a draft, try it, then publish a versioned config for production chat.
- `registry` — just the tool registry service (OpenAPI/MCP config), no chat UI.
- `appworld` / `travel_agent` — specific benchmark/example scenarios.

Other useful commands:
- `cuga stop <service>` — stop a running service.
- `cuga status` — show what's currently running.
- `cuga doctor` — environment/dependency sanity check.
- `cuga viz` — trajectory viewer dashboard (see `cuga-debug-trajectory`).

There is no `cuga policy` or `cuga knowledge` CLI subcommand — policies and knowledge are managed through the Python SDK (`agent.policies.*`, `agent.knowledge.*`, see `cuga-author-policy` / `cuga-knowledge-rag`) or through the `manager` web UI.

### Next step

Once the UI is up, point the user at `cuga-build-agent` for writing their own Python code against the SDK, or have them just chat with the demo agent directly in the browser.

## cuga-knowledge-rag

_Use when the user wants a cuga agent to ingest, search, or answer questions from documents (PDF/DOCX/XLSX/PPTX/HTML/Markdown/images) - RAG / knowledge base features._

## Knowledge base (RAG)

cuga has a built-in knowledge base: local vector store + **Docling** for parsing/normalizing documents before chunking and embedding, so ingestion stays self-contained with no external document service.

Knowledge is **enabled by default** (`enable_knowledge=True`); the SDK auto-injects knowledge tools/awareness so the agent knows what's available and how to search it.

### Try it

```bash
uv run cuga start demo_knowledge
```

Full walkthrough with sample docs: `docs/examples/knowledge_demo/` in a cuga-agent checkout.

### Programmatic use

```python
from cuga import CugaAgent
import asyncio

agent = CugaAgent(enable_knowledge=True)

async def main():
    await agent.knowledge.ingest("/path/to/quarterly_report.pdf")

    result = await agent.invoke("What does the report say about Q4 revenue?")
    print(result.answer)  # agent searches the knowledge base automatically

    results = await agent.knowledge.search("Q4 revenue figures")
    for r in results:
        print(f"{r['filename']} (page {r['page']}): {r['text'][:100]}")

    docs = await agent.knowledge.list_documents()
    await agent.aclose()

asyncio.run(main())
```

### Scoping

```python
## Session-scoped: temporary, tied to one conversation thread
await agent.knowledge.ingest("/path/to/file.pdf", scope="session", thread_id="user-session-123")
results = await agent.knowledge.search("query", scope="session", thread_id="user-session-123")

## Agent-scoped (default): permanent, shared across conversations
await agent.knowledge.ingest("/path/to/file.pdf", scope="agent")
```

Use `session` scope for per-conversation uploads that shouldn't leak between users; use `agent` scope for a shared reference corpus.

### Disabling

```python
agent = CugaAgent(tools=[my_tools], enable_knowledge=False)
```

### Supported types & tuning

PDF, DOCX, XLSX, PPTX, HTML, Markdown, images, and more (via Docling). Embedding provider (`fastembed` default/local, `huggingface`, `openai`, `ollama`, `openrouter`) plus model/batch/concurrency are set under `[knowledge.embeddings]` in `settings.toml` or via `--embeddings-*` CLI flags. Switching provider/model invalidates existing vectors (different dimensionality) — the manage UI (`cuga start manager`) surfaces a "re-index recommended" banner when that happens.

Full provider matrix: https://docs.cuga.dev/docs/sdk/knowledge/
<!-- cuga-harness-kit:end -->
