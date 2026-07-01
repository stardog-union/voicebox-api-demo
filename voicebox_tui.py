"""Voicebox public API streaming demo: a Textual chat TUI.

A conversation transcript over the Launchpad Voicebox public API,
with Stardog enhancements: every answer carries collapsible Reasoning, the
SPARQL it ran (Queries Executed), the grounded Results table, and Sources.

Run:
    export VBX_APP_TOKEN=...                          # required
    export VOICEBOX_API_URL=http://localhost:8080     # optional, this is the default
    export VBX_CLIENT_ID=demo-client                  # optional
    uv run voicebox-tui
"""

from __future__ import annotations

import csv
import io
import json
import os
import pathlib
from datetime import datetime

import httpx
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Static,
)


def _load_dotenv() -> None:
    """Load a .env from the working directory (real env vars take precedence)."""
    env_path = pathlib.Path(".env")
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

API_URL = os.environ.get("VOICEBOX_API_URL", "http://localhost:8080").rstrip("/")
ASK_PATH = "/api/v1/voicebox/stream/ask"
APP_INFO_PATH = "/api/v1/app"
APP_TOKEN = os.environ.get("VBX_APP_TOKEN", "")
CLIENT_ID = os.environ.get("VBX_CLIENT_ID", "demo-client")

# Stardog brand palette: green accent on charcoal, light-gray text.
STARDOG_GREEN = "#90e270"
MUTED_GRAY = "#8a8f98"
AMBER = "#e2c770"

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

STARDOG_THEME = Theme(
    name="stardog",
    primary=STARDOG_GREEN,
    secondary=MUTED_GRAY,
    accent=STARDOG_GREEN,
    success=STARDOG_GREEN,
    warning=AMBER,
    error="#e57373",
    foreground="#d6d6d6",
    background="#1b1b1b",
    surface="#242424",
    panel="#2c2c2c",
    dark=True,
)


def reasoning_steps(snapshot: dict) -> list[dict]:
    """The reasoning steps carried by a stream snapshot."""
    for action in snapshot.get("actions", []):
        if action.get("type") == "reasoning":
            try:
                return json.loads(action.get("value") or "[]")
            except json.JSONDecodeError:
                return []
    return []


def render_reasoning(steps: list[dict]) -> str:
    """Render the reasoning trace: one line per tool call, plus the agent's
    narration in full. The widget word-wraps, so nothing is truncated."""
    lines: list[str] = []
    for step in steps:
        if step.get("tool"):
            label = (
                step.get("summary") or step.get("output_text_friendly") or ""
            ).strip()
            if label:
                lines.append(f"[{STARDOG_GREEN}]▸[/] [dim]{escape(label)}[/]")
            continue
        text = (step.get("output_text") or "").strip()
        if (
            not text
            or text.startswith("Data ID:")
            or text.lower().startswith("no data found")
        ):
            continue
        # Keep the narration intact; indent every line to align under the step.
        narration = "\n".join(f"  {escape(line)}" for line in text.splitlines())
        lines.append(f"[italic]{narration}[/]")
    return "\n".join(lines)


def collect_sparql(snapshot: dict) -> list[str]:
    return [
        a["value"].strip()
        for a in snapshot.get("actions", [])
        if a.get("type") == "sparql" and a.get("value")
    ]


def dedupe_sparql(queries: list[str]) -> list[str]:
    """SPARQL queries in run order, with exact duplicates removed."""
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def collect_csv(snapshot: dict) -> tuple[list[str], list[list[str]]]:
    """Parse the first ``csv`` action into (columns, rows)."""
    for a in snapshot.get("actions", []):
        if a.get("type") == "csv" and a.get("value"):
            reader = list(csv.reader(io.StringIO(a["value"])))
            if not reader:
                return [], []
            return reader[0], reader[1:]
    return [], []


def _localname(iri: str) -> str:
    """Short label for an IRI: the segment after the last #, / or :."""
    for sep in ("#", "/"):
        if sep in iri:
            iri = iri.rsplit(sep, 1)[-1]
    return iri.rsplit(":", 1)[-1] if ":" in iri else iri


def collect_sources(snapshot: dict) -> list[dict]:
    """The graphs an answer was grounded in, with the entity types touched."""
    out: list[dict] = []
    seen: set[str] = set()
    for a in snapshot.get("actions", []):
        if a.get("type") != "provenance" or not a.get("value"):
            continue
        try:
            data = json.loads(a["value"])
        except json.JSONDecodeError:
            continue
        for graph in data.get("graphs", []):
            name = graph.get("name") or _localname(graph.get("iri", ""))
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(
                {
                    "name": name,
                    "graph_type": graph.get("graph_type") or "",
                    "iri": graph.get("iri") or "",
                    "description": graph.get("description") or "",
                    "types": [_localname(t) for t in graph.get("types", [])],
                }
            )
    return out


def format_app_info(app: dict) -> str:
    """One labeled status line for the connected Voicebox App."""
    reasoning = f"[{STARDOG_GREEN}]on[/]" if app.get("reasoning") else "[dim]off[/]"
    return (
        f"[{STARDOG_GREEN}]●[/] [b]{escape(str(app.get('name', '?')))}[/]"
        f"     [dim]database[/]  {escape(str(app.get('database', '?')))}"
        f"     [dim]model[/]  {escape(str(app.get('model', '?')))}"
        f"     [dim]reasoning[/]  {reasoning}"
    )


def sources_view(sources: list[dict]) -> Table:
    """A metadata table for the graphs an answer was grounded in."""
    table = Table(
        show_header=True,
        header_style=f"bold {STARDOG_GREEN}",
        border_style=MUTED_GRAY,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Graph", style="bold", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Entity types", style=STARDOG_GREEN)
    table.add_column("IRI", overflow="fold", style="dim")
    has_description = any(s["description"] for s in sources)
    if has_description:
        table.add_column("Description")
    for s in sources:
        row = [
            s["name"],
            s["graph_type"] or "—",
            ", ".join(s["types"]) or "—",
            s["iri"] or "—",
        ]
        if has_description:
            row.append(s["description"] or "—")
        table.add_row(*row)
    return table


def sparql_view(queries: list[str]):
    """Rich renderable: each query in its own titled panel, last highlighted."""
    if not queries:
        return "[dim]No SPARQL was run for this answer.[/]"

    def panel(query: str, title: str, *, accent: bool) -> Panel:
        return Panel(
            Syntax(query, "sparql", theme="monokai", word_wrap=True),
            title=title,
            title_align="left",
            border_style=STARDOG_GREEN if accent else MUTED_GRAY,
            padding=(0, 1),
        )

    if len(queries) == 1:
        return panel(queries[0], "SPARQL", accent=True)
    blocks = []
    for i, query in enumerate(queries, start=1):
        is_latest = i == len(queries)
        title = f"Query {i} · latest" if is_latest else f"Query {i}"
        blocks.append(panel(query, title, accent=is_latest))
    return Group(*blocks)


class UserTurn(Vertical):
    """One question from the user."""

    def __init__(self, text: str) -> None:
        super().__init__(classes="turn user-turn")
        self._text = text
        self._ts = datetime.now().strftime("%H:%M")

    def compose(self) -> ComposeResult:
        yield Label(f"You  [dim]{self._ts}[/]", classes="speaker you")
        yield Static(self._text, classes="user-msg")


class VoiceboxTurn(Vertical):
    """One answer from Voicebox: live reasoning, then answer + collapsible
    Reasoning / Queries Executed / Results / Sources."""

    def __init__(self) -> None:
        super().__init__(classes="turn voicebox-turn")
        self._ts = datetime.now().strftime("%H:%M")
        self._spin_index = 0
        self._spin_timer = None

    def compose(self) -> ComposeResult:
        yield Label(f"Voicebox  [dim]{self._ts}[/]", classes="speaker vbx")
        yield Static(f"{SPINNER[0]} thinking…", classes="thinking")
        yield Markdown(classes="answer")
        with Collapsible(title="Reasoning", collapsed=False, classes="reasoning-col"):
            yield Static(classes="reasoning-log")
        with Collapsible(
            title="Queries Executed",
            collapsed=True,
            classes="queries-col detail hidden",
        ):
            yield Static(classes="queries")
        with Collapsible(
            title="Results", collapsed=True, classes="results-col detail hidden"
        ):
            yield DataTable(classes="results")
        with Collapsible(
            title="Sources", collapsed=True, classes="sources-col detail hidden"
        ):
            yield Static(classes="sources")

    def on_mount(self) -> None:
        self._spin_timer = self.set_interval(0.08, self._spin)

    def _spin(self) -> None:
        self._spin_index = (self._spin_index + 1) % len(SPINNER)
        self.query_one(".thinking", Static).update(
            f"[{STARDOG_GREEN}]{SPINNER[self._spin_index]}[/] thinking…"
        )

    def _stop_spin(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def set_reasoning(self, steps: list[dict]) -> None:
        self.query_one(".reasoning-log", Static).update(render_reasoning(steps))

    def show_error(self, message: str) -> None:
        self._stop_spin()
        self.query_one(".thinking", Static).update(f"[#e57373]✗ {escape(message)}[/]")

    def finalize(self, snapshot: dict, queries: list[str]) -> None:
        self._stop_spin()
        self.query_one(".thinking", Static).display = False
        self.query_one(".answer", Markdown).update(
            snapshot.get("result") or "_(no answer returned)_"
        )

        reasoning = self.query_one(".reasoning-col", Collapsible)
        reasoning.collapsed = True

        if queries:
            self.query_one(".queries", Static).update(sparql_view(queries))
            col = self.query_one(".queries-col", Collapsible)
            col.title = f"Queries Executed ({len(queries)})"
            col.remove_class("hidden")

        columns, rows = collect_csv(snapshot)
        if columns:
            table = self.query_one(".results", DataTable)
            table.add_columns(*columns)
            for row in rows:
                table.add_row(*row)
            col = self.query_one(".results-col", Collapsible)
            col.title = f"Results ({len(rows)} rows)"
            col.remove_class("hidden")

        sources = collect_sources(snapshot)
        if sources:
            self.query_one(".sources", Static).update(sources_view(sources))
            col = self.query_one(".sources-col", Collapsible)
            plural = "s" if len(sources) != 1 else ""
            col.title = f"Sources ({len(sources)} graph{plural})"
            col.remove_class("hidden")


class ConnectionModal(ModalScreen[dict]):
    """Update the API URL and/or app token mid-session."""

    CSS = """
    ConnectionModal { align: center middle; }
    #conn-dialog {
        width: 76; height: auto; padding: 1 2;
        background: $surface; border: round $accent;
    }
    #conn-dialog Input { margin: 0 0 1 0; }
    #conn-dialog .hint { color: $text-muted; text-style: italic; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, api_url: str, client_id: str) -> None:
        super().__init__()
        self._api_url = api_url
        self._client_id = client_id

    def compose(self) -> ComposeResult:
        with Vertical(id="conn-dialog"):
            yield Label("Connection settings")
            url = Input(value=self._api_url, id="url-input")
            url.border_title = "API URL"
            yield url
            client = Input(value=self._client_id, id="client-input")
            client.border_title = "Client ID"
            yield client
            token = Input(
                password=True,
                placeholder="leave blank to keep current token",
                id="token-input",
            )
            token.border_title = "App token"
            yield token
            yield Label("Enter to apply · Esc to cancel", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#url-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(
            {
                "api_url": self.query_one("#url-input", Input).value.strip(),
                "client_id": self.query_one("#client-input", Input).value.strip(),
                "app_token": self.query_one("#token-input", Input).value.strip(),
            }
        )

    def action_cancel(self) -> None:
        self.dismiss({})


class VoiceboxDemo(App):
    TITLE = "Voicebox"
    CSS = """
    Screen { background: $background; }

    #appinfo {
        dock: top; height: 1; padding: 0 2;
        background: $panel; color: $foreground;
    }
    #transcript { padding: 1 2 0 2; }

    #composer { dock: bottom; height: auto; background: $surface; }
    #conversation { height: 1; margin: 1 3 0 3; text-style: bold; }
    #query { margin: 0 2 1 2; border: round $accent; }

    .turn { height: auto; margin: 0 0 1 0; }
    .voicebox-turn { border-left: outer $accent; padding: 0 0 0 1; }
    .speaker { text-style: bold; margin: 0 0 0 1; }
    .speaker.you { color: $secondary; }
    .speaker.vbx { color: $accent; }
    .user-msg { padding: 0 0 0 2; border-left: outer $secondary; }
    .answer { height: auto; padding: 0 0 0 1; }
    .thinking { color: $accent; padding: 0 0 0 1; }

    .reasoning-log { padding: 0 1; height: auto; }
    .queries { height: auto; padding: 0 1; }
    .results-col DataTable { height: auto; max-height: 16; }
    .detail.hidden { display: none; }
    .sources { height: auto; padding: 0 1; }

    Collapsible { border: none; padding: 0 0 0 1; }
    Collapsible > Contents { padding: 0 0 0 1; }
    """
    BINDINGS = [
        ("ctrl+n", "new_conversation", "New conversation"),
        ("ctrl+t", "edit_connection", "Connection"),
        ("ctrl+c", "quit", "Quit"),
    ]

    api_url: str = API_URL
    app_token: str = APP_TOKEN
    client_id: str = CLIENT_ID
    conversation_id: str | None = None
    turn_count: int = 0
    # The stream reports a conversation-wide running total of SPARQL actions, so
    # each turn's snapshot repeats earlier turns' queries. Track how many we've
    # already attributed to prior turns; a turn shows only the entries past this.
    _sparql_offset: int = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="appinfo")
        yield VerticalScroll(id="transcript")
        with Vertical(id="composer"):
            yield Label("", id="conversation")
            yield Input(placeholder="Ask Voicebox a question…", id="query")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(STARDOG_THEME)
        self.theme = "stardog"
        if not self.app_token:
            self._toast(
                "[b red]No app token. Press Ctrl+T to set one (or add it to .env).[/]"
            )
            self.query_one("#appinfo", Static).update(
                "[dim]not connected - press Ctrl+T to set a token[/]"
            )
        else:
            self.sub_title = self.api_url
            self.load_app_info()
        self._update_conversation_label()
        self.query_one("#query", Input).focus()

    def action_edit_connection(self) -> None:
        def applied(result: dict | None) -> None:
            url = (result or {}).get("api_url", "").rstrip("/")
            client = (result or {}).get("client_id", "")
            token = (result or {}).get("app_token", "")
            changed = False
            if url and url != self.api_url:
                self.api_url = url
                changed = True
            if client and client != self.client_id:
                self.client_id = client
                changed = True
            if token:
                self.app_token = token
                changed = True
            if not changed:
                return
            self.action_new_conversation()
            self.sub_title = self.api_url
            self.load_app_info()
            self._toast("[#90e270]Connection updated - started a new conversation.[/]")

        self.push_screen(ConnectionModal(self.api_url, self.client_id), applied)

    @work
    async def load_app_info(self) -> None:
        """Show what the token's Voicebox App is wired to (GET /api/v1/app)."""
        info = self.query_one("#appinfo", Static)
        info.update(f"[dim]connecting to {escape(self.api_url)}…[/]")
        headers = {
            "Authorization": f"bearer {self.app_token}",
            "X-Client-Id": self.client_id,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.api_url}{APP_INFO_PATH}", headers=headers
                )
        except httpx.HTTPError:
            info.update(f"[#e57373]● could not reach {escape(self.api_url)}[/]")
            return
        if resp.status_code != 200 or not resp.json():
            info.update(f"[#e57373]● app info unavailable (HTTP {resp.status_code})[/]")
            return
        info.update(format_app_info(resp.json()))

    def _update_conversation_label(self) -> None:
        """Update the conversation status line and input prompt."""
        label = self.query_one("#conversation", Label)
        query = self.query_one("#query", Input)
        if self.conversation_id:
            label.update(
                f"[{STARDOG_GREEN}]● Continuing this conversation[/]  "
                f"[dim]{self.conversation_id} · turn {self.turn_count} · "
                f"Ctrl+N for a new one[/]"
            )
            query.border_title = "Follow-up question (same conversation)"
            query.placeholder = (
                "Ask a follow-up - it keeps context · Ctrl+N to start over…"
            )
        else:
            label.update(
                f"[{MUTED_GRAY}]○ New conversation[/]  "
                f"[dim]· your next question starts a fresh thread[/]"
            )
            query.border_title = "New conversation"
            query.placeholder = "Ask a question to start a new conversation…"

    def action_new_conversation(self) -> None:
        self.conversation_id = None
        self.turn_count = 0
        self._sparql_offset = 0
        self.query_one("#transcript", VerticalScroll).remove_children()
        self._update_conversation_label()

    def _toast(self, message: str) -> None:
        self.query_one("#conversation", Label).update(message)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        if not self.app_token:
            self._toast("[b red]No app token - press Ctrl+T to set one.[/]")
            return
        self.turn_count += 1
        self._update_conversation_label()
        event.input.value = ""

        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.mount(UserTurn(question))
        turn = VoiceboxTurn()
        await transcript.mount(turn)
        transcript.scroll_end(animate=False)
        self.ask(question, turn)

    @work(exclusive=True)
    async def ask(self, question: str, turn: VoiceboxTurn) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        headers = {
            "Authorization": f"bearer {self.app_token}",
            "X-Client-Id": self.client_id,
            "Content-Type": "application/json",
        }
        body: dict = {"query": question}
        if self.conversation_id:
            body["conversation_id"] = self.conversation_id
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST", f"{self.api_url}{ASK_PATH}", headers=headers, json=body
                ) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread()).decode("utf-8", "replace")
                        turn.show_error(f"HTTP {resp.status_code}: {detail[:160]}")
                        return
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            snapshot = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if snapshot.get("conversation_id"):
                            self.conversation_id = snapshot["conversation_id"]
                            self._update_conversation_label()
                        turn.set_reasoning(reasoning_steps(snapshot))
                        if not snapshot.get("pending", True):
                            # actions carry a conversation-wide running total of
                            # SPARQL; this turn's queries are the ones past what
                            # earlier turns already showed.
                            all_sparql = collect_sparql(snapshot)
                            queries = dedupe_sparql(all_sparql[self._sparql_offset :])
                            self._sparql_offset = len(all_sparql)
                            turn.finalize(snapshot, queries)
                        transcript.scroll_end(animate=False)
        except httpx.HTTPError as exc:
            turn.show_error(f"Request failed: {exc}")


def main() -> None:
    VoiceboxDemo().run()


if __name__ == "__main__":
    main()
