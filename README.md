# Voicebox API Demo (TUI)

A chat TUI for the Launchpad Voicebox **public API** streaming
endpoint (`POST /api/v1/voicebox/stream/ask`), with Stardog enhancements a plain
LLM chat can't show: the SPARQL it ran, the grounded result table, and the
source graphs.

Built for enablement demos: it makes the streaming NDJSON response tangible.

## What it shows

A scrolling **conversation transcript**. Each question and each Voicebox answer
is a message in the thread, and follow-ups keep context (multi-turn). The
endpoint streams [NDJSON](http://ndjson.org/), one *cumulative* snapshot per
line, which the TUI renders as:

- **Live reasoning**: the agent's thinking and tool calls (`fetch_data`,
  `compute_answer`, ...) stream into the in-progress message, then collapse into
  a **Reasoning** section once the answer lands.
- **Answer**: the final markdown answer.
- **Queries Executed**: the SPARQL the agent ran against Stardog (collapsible).
- **Results**: the grounded result table (collapsible).
- **Sources**: the graphs the answer was drawn from, with the entity types touched.

A status bar shows which Voicebox App the token is wired to (name, database,
model, reasoning), and the line above the input shows whether your next question
continues the conversation or starts a new one.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed.
- A running Launchpad with the Voicebox public API reachable, and a Voicebox
  **app token**.

## Configure

Nothing has to be set up front: launch the app and press **Ctrl+T** to set the
API URL, app token, and client ID at runtime.

To preconfigure instead, set these environment variables, or put them in a
`.env` in the directory you run from (the app loads it automatically):

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `VBX_APP_TOKEN` | the only one you need | (none) | Bearer token for the public API. |
| `VOICEBOX_API_URL` | no | `http://localhost:8080` | Base URL of the Launchpad API. |
| `VBX_CLIENT_ID` | no | `demo-client` | Sent as the `X-Client-Id` header. |

```bash
cp .env.example .env   # then edit in your token
```

## Run

```bash
uv run voicebox-tui
```

`uv` creates the environment from `pyproject.toml` on first run.

Keys:

- **Enter**: ask a question; follow-ups stay in the same conversation.
- **Ctrl+N**: start a new conversation.
- **Ctrl+T**: change the API URL and/or app token (starts a fresh conversation).
- **Ctrl+C**: quit.

Then ask a question about whatever knowledge graph your app token is connected to.

## Development

```bash
uv run ruff format     # format
uv run ruff check      # lint
uv run ty check        # type-check
```

## The underlying request

The TUI is a thin client over this call:

```bash
curl -N -X POST "$VOICEBOX_API_URL/api/v1/voicebox/stream/ask" \
    -H "Authorization: bearer $VBX_APP_TOKEN" \
    -H "X-Client-Id: demo-client" \
    -H "Content-Type: application/json" \
    -d '{"query": "<your question about the knowledge graph>"}'
```
