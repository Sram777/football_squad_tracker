"""Local football player attendance tracker and Player of the Match voting app."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "payment_tracker_data.json"
INPUT_FILE = ROOT / "input.txt"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))
MAX_REQUEST_SIZE = 1_000_000
NUMBERED_LINE = re.compile(r"^\s*(\d+)(?:\s*[.)\-:]\s*|\s+)(.+?)\s*$")
EMOJI_CHARACTERS = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF"
    "\u2600-\u27BF\uFE0E\uFE0F\u200D\u20E3\u2060\u200B"
    "\U0001F3FB-\U0001F3FF]+"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def clean_text(text: str) -> str:
    return EMOJI_CHARACTERS.sub("", text)


def parse_players(text: str) -> list[str]:
    """Extract unique player names from numbered lines, preserving their order."""
    players: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = NUMBERED_LINE.match(line)
        if not match:
            continue
        name = clean_text(match.group(2)).strip()
        key = name.casefold()
        if name and key not in seen:
            players.append(name)
            seen.add(key)
    return players


def initial_state() -> dict:
    source_text = clean_text(INPUT_FILE.read_text(encoding="utf-8-sig")) if INPUT_FILE.exists() else ""
    return {
        "source_text": source_text,
        "players": parse_players(source_text),
        "checked": [],
        "ballots": {},
        "legacy_votes": {},
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return initial_state()

    with STATE_FILE.open("r", encoding="utf-8") as state_file:
        state = json.load(state_file)
    if not isinstance(state, dict) or not isinstance(state.get("players"), list) or not isinstance(state.get("checked"), list) or not isinstance(state.get("source_text"), str):
        raise ValueError(f"Invalid tracker data in {STATE_FILE}")

    source_text = clean_text(state["source_text"])
    players = list(dict.fromkeys(clean_text(name).strip() for name in state["players"] if isinstance(name, str)))
    valid_keys = {name.strip().casefold() for name in players if name}
    old_votes = state.get("votes", {})
    ballots = state.get("ballots", {})
    legacy_votes = state.get("legacy_votes", old_votes)
    if not isinstance(old_votes, dict) or not isinstance(ballots, dict) or not isinstance(legacy_votes, dict):
        raise ValueError(f"Invalid tracker vote data in {STATE_FILE}")
    state["source_text"] = source_text
    state["players"] = [name for name in players if name]
    state["checked"] = list(dict.fromkeys(clean_text(key).strip().casefold() for key in state["checked"] if isinstance(key, str)))
    state["ballots"] = {
        voter: clean_text(player).strip().casefold()
        for voter, player in ballots.items()
        if isinstance(voter, str) and isinstance(player, str) and clean_text(player).strip().casefold() in valid_keys
    }
    state["legacy_votes"] = {
        clean_text(player).strip().casefold(): count
        for player, count in legacy_votes.items()
        if isinstance(player, str) and clean_text(player).strip().casefold() in valid_keys and isinstance(count, int) and count > 0
    }
    state.pop("votes", None)
    return state


def save_state(state: dict) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=ROOT,
            prefix=".payment_tracker_",
            suffix=".tmp",
            delete=False,
        ) as state_file:
            temporary_path = state_file.name
            json.dump(state, state_file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, STATE_FILE)
    except OSError:
        logging.exception("Could not save tracker data")
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise


STATE_LOCK = threading.RLock()
STATE = load_state()


def player_key(name: str) -> str:
    return name.strip().casefold()


def public_state(voter_id: str = "") -> dict:
    votes = dict(STATE["legacy_votes"])
    for voted_for in STATE["ballots"].values():
        votes[voted_for] = votes.get(voted_for, 0) + 1
    checked = set(STATE["checked"])
    return {
        "source_text": STATE["source_text"],
        "players": [
            {
                "name": name,
                "checked": player_key(name) in checked,
                "votes": votes.get(player_key(name), 0),
                "your_vote": STATE["ballots"].get(voter_id) == player_key(name),
            }
            for name in STATE["players"]
        ],
    }


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#100d24">
  <title>Matchday | Player tracker</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #100d24;
      --panel: #1b1738;
      --panel-raised: #28224c;
      --line: #484071;
      --text: #f5f0ff;
      --muted: #b9afd8;
      --green: #b5ff5c;
      --green-dark: #17152a;
      --gold: #ffe66d;
      --red: #ff7892;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        linear-gradient(#ffffff06 1px, transparent 1px),
        linear-gradient(90deg, #ffffff06 1px, transparent 1px),
        radial-gradient(ellipse at 85% 0%, #392c6a 0, transparent 38rem),
        var(--bg);
      background-size: 4px 4px, 4px 4px, auto, auto;
      font: 16px/1.5 "Courier New", "Lucida Console", monospace;
    }
    button, textarea { font: inherit; }
    .wrap { width: min(1050px, calc(100% - 32px)); margin: 0 auto; padding: 38px 0 56px; }
    .topline { display: flex; align-items: center; gap: 10px; color: var(--green); font-size: .78rem; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }
    .ball { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 18px #b6f36988; }
    h1 { margin: 12px 0 5px; font-size: clamp(2.2rem, 6vw, 4rem); letter-spacing: -.055em; line-height: 1.05; }
    .intro { margin: 0; color: var(--muted); }
    .layout { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .75fr); gap: 20px; margin-top: 30px; align-items: start; }
    .panel { overflow: hidden; border: 2px solid var(--line); border-radius: 4px; background: #1b1738f2; box-shadow: 5px 5px 0 #070612, 0 0 22px #8f67ff14; }
    .panel-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 20px 22px; border-bottom: 1px solid var(--line); }
    h2 { margin: 0; font-size: 1.08rem; letter-spacing: .025em; text-transform: uppercase; }
    .count { color: var(--muted); font-size: .88rem; white-space: nowrap; }
    .paste-content { padding: 20px 22px 22px; }
    .helper { margin: 0 0 12px; color: var(--muted); font-size: .9rem; }
    textarea { display: block; width: 100%; min-height: 190px; resize: vertical; padding: 14px 15px; border: 2px solid var(--line); border-radius: 3px; outline: none; color: var(--text); background: #100d24; font: .9rem/1.65 "Courier New", "Lucida Console", monospace; }
    textarea:focus { border-color: var(--green); box-shadow: 0 0 0 3px #b6f32618; }
    .paste-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 14px; }
    .primary { border: 2px solid var(--green); border-radius: 3px; padding: 11px 16px; color: var(--green-dark); background: var(--green); font-weight: 800; text-transform: uppercase; letter-spacing: .035em; cursor: pointer; transition: transform .15s, filter .15s; box-shadow: 3px 3px 0 #0b0918; }
    .primary:hover { transform: translate(-1px, -1px); filter: brightness(1.05); }
    .message { min-height: 1.4em; color: var(--muted); font-size: .86rem; }
    .message.error { color: var(--red); }
    .roster { padding: 0 14px 12px; }
    .player { display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 12px; padding: 13px 9px; border-bottom: 1px solid #ffffff0d; }
    .player:last-child { border-bottom: 0; }
    .player-name { overflow-wrap: anywhere; font-weight: 650; }
    .player-tools { display: flex; align-items: center; gap: 18px; }
    .attendance { display: inline-flex; min-height: 44px; align-items: center; gap: 8px; color: var(--muted); font-size: .9rem; cursor: pointer; }
    .attendance input { width: 22px; height: 22px; margin: 0; accent-color: var(--green); cursor: pointer; }
    .vote { display: inline-flex; min-width: 62px; min-height: 44px; align-items: center; justify-content: center; gap: 7px; padding: 7px 10px; border: 2px solid transparent; border-radius: 3px; color: var(--gold); background: transparent; cursor: pointer; }
    .vote:hover, .vote.selected { border-color: var(--gold); background: #ffd16614; box-shadow: 0 0 12px #ffd16626; }
    .trophy { font-size: 1.12rem; }
    .empty { padding: 24px 12px 29px; color: var(--muted); text-align: center; }
    .motm { position: relative; padding: 23px; }
    .motm-label { display: flex; align-items: center; gap: 8px; color: var(--gold); font-size: .76rem; font-weight: 850; letter-spacing: .12em; text-transform: uppercase; }
    .motm-name { margin: 24px 0 6px; font-size: clamp(1.65rem, 4vw, 2.45rem); letter-spacing: -.045em; line-height: 1.12; overflow-wrap: anywhere; }
    .motm-subtitle { margin: 0; color: var(--muted); }
    .motm-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 28px; padding-top: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: .85rem; }
    .vote-total { color: var(--gold); font-weight: 750; white-space: nowrap; }
    .legend { padding: 0 23px 20px; color: var(--muted); font-size: .82rem; }
    @media (max-width: 760px) {
      .wrap { padding-top: 27px; }
      .layout { grid-template-columns: 1fr; }
      .motm-panel { grid-row: 1; }
      .motm-name { margin-top: 16px; }
      .motm-foot { margin-top: 18px; }
    }
    @media (max-width: 440px) {
      .wrap { width: min(100% - 22px, 1050px); }
      .panel-head, .paste-content { padding-left: 16px; padding-right: 16px; }
      .roster { padding-left: 9px; padding-right: 9px; }
      .player-tools { gap: 10px; }
      .player { gap: 6px; padding: 10px 5px; }
      .attendance { gap: 5px; font-size: .84rem; }
      .vote { min-width: 52px; }
      .paste-content { padding-top: 16px; padding-bottom: 18px; }
      textarea { min-height: 145px; font-size: 16px; }
      .primary { width: 100%; min-height: 48px; }
      .paste-actions { gap: 8px; }
      .message { width: 100%; }
      .motm { padding: 19px; }
      .motm-name { font-size: 1.8rem; }
      .panel { border-radius: 4px; }
    }
  </style>
</head>
<body>
  <main class="wrap">
    <div class="topline"><span class="ball"></span> Matchday tools</div>
    <h1>Player tracker</h1>
    <p class="intro">Keep track of who has paid and vote for your Player of the Match.</p>

    <div class="layout">
      <section class="panel" aria-labelledby="roster-title">
        <div class="panel-head">
          <h2 id="roster-title">Squad &amp; payments</h2>
          <span class="count" id="player-count">Loading...</span>
        </div>
        <div class="paste-content">
          <p class="helper">Paste your player list below.</p>
          <textarea id="player-input" aria-label="Paste numbered player list" placeholder="1. Alex&#10;2. Jordan"></textarea>
          <div class="paste-actions">
            <button type="button" class="primary" id="update-roster">Update player list</button>
            <span class="message" id="message" role="status" aria-live="polite"></span>
          </div>
        </div>
        <div class="panel-head">
          <h2>Players</h2>
          <span class="count">Tap a trophy to vote</span>
        </div>
        <div class="roster" id="roster"></div>
      </section>

      <aside class="panel motm-panel" aria-live="polite" aria-labelledby="motm-title">
        <div class="motm">
          <div class="motm-label"><span aria-hidden="true">🏆</span> Matchday award</div>
          <h2 id="motm-title" class="motm-name">Waiting for players</h2>
          <p class="motm-subtitle" id="motm-subtitle">Add a squad to get started.</p>
          <div class="motm-foot">
            <span>Player of the Match</span>
            <span class="vote-total" id="motm-votes"></span>
          </div>
        </div>
        <div class="legend">One vote per browser. Tap another player to change your vote.</div>
      </aside>
    </div>
  </main>
  <script>
    const rosterEl = document.getElementById("roster");
    const inputEl = document.getElementById("player-input");
    const messageEl = document.getElementById("message");
    const voterId = getVoterId();
    let players = [];
    let currentSourceText = null;

    function getVoterId() {
      const key = "matchday-voter-id";
      let id = localStorage.getItem(key);
      if (!id) {
        id = crypto.randomUUID();
        localStorage.setItem(key, id);
      }
      return id;
    }

    async function request(url, options = {}) {
      const response = await fetch(url, options);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Something went wrong.");
      return result;
    }

    function setMessage(text, isError = false) {
      messageEl.textContent = text;
      messageEl.classList.toggle("error", isError);
    }

    async function refresh() {
      const data = await request(`/api/state?voter_id=${encodeURIComponent(voterId)}`);
      const playersChanged = JSON.stringify(players) !== JSON.stringify(data.players);
      players = data.players;
      currentSourceText = data.source_text;
      if (document.activeElement !== inputEl) inputEl.value = data.source_text;
      if (playersChanged) render();
      if (messageEl.classList.contains("error")) setMessage("");
    }

    function render() {
      document.getElementById("player-count").textContent =
        `${players.length} ${players.length === 1 ? "player" : "players"}`;
      rosterEl.replaceChildren();

      if (!players.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "No players yet. Paste a numbered list above to build your squad.";
        rosterEl.append(empty);
      }

      for (const player of players) {
        const row = document.createElement("div");
        row.className = "player";
        const name = document.createElement("span");
        name.className = "player-name";
        name.textContent = player.name;

        const tools = document.createElement("div");
        tools.className = "player-tools";
        const label = document.createElement("label");
        label.className = "attendance";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = player.checked;
        checkbox.setAttribute("aria-label", `Mark ${player.name} as paid`);
        checkbox.addEventListener("change", () => updateAttendance(player.name, checkbox.checked));
        label.append(checkbox, document.createTextNode("Paid"));

        const vote = document.createElement("button");
        vote.type = "button";
        vote.className = `vote${player.your_vote ? " selected" : ""}`;
        vote.setAttribute("aria-pressed", String(player.your_vote));
        vote.setAttribute("aria-label", `${player.your_vote ? "Your vote for" : "Vote for"} ${player.name}; ${player.votes} ${player.votes === 1 ? "vote" : "votes"}`);
        const trophy = document.createElement("span");
        trophy.className = "trophy";
        trophy.setAttribute("aria-hidden", "true");
        trophy.textContent = "🏆";
        const count = document.createElement("span");
        count.textContent = String(player.votes);
        vote.append(trophy, count);
        vote.addEventListener("click", () => castVote(player.name));

        tools.append(label, vote);
        row.append(name, tools);
        rosterEl.append(row);
      }

      renderWinner();
    }

    function renderWinner() {
      const title = document.getElementById("motm-title");
      const subtitle = document.getElementById("motm-subtitle");
      const total = document.getElementById("motm-votes");
      const highest = Math.max(0, ...players.map(player => player.votes));
      const winners = players.filter(player => player.votes === highest && highest > 0);

      if (!players.length) {
        title.textContent = "Waiting for players";
        subtitle.textContent = "Add a squad to get started.";
        total.textContent = "";
      } else if (!highest) {
        title.textContent = "Your pick?";
        subtitle.textContent = "Cast the first trophy vote.";
        total.textContent = "0 votes";
      } else if (winners.length > 1) {
        title.textContent = "It's a tie!";
        subtitle.textContent = winners.map(player => player.name).join(" · ");
        total.textContent = `${highest} ${highest === 1 ? "vote each" : "votes each"}`;
      } else {
        title.textContent = winners[0].name;
        subtitle.textContent = "Leading the matchday vote";
        total.textContent = `${highest} ${highest === 1 ? "vote" : "votes"}`;
      }
    }

    async function updateAttendance(name, checked) {
      try {
        const data = await request("/api/attendance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, checked })
        });
        players = data.players;
        currentSourceText = data.source_text;
        render();
        setMessage("");
      } catch (error) {
        setMessage(error.message, true);
        await refresh();
      }
    }

    async function castVote(name) {
      try {
        const data = await request("/api/vote", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, voter_id: voterId })
        });
        players = data.players;
        render();
        setMessage("");
      } catch (error) {
        setMessage(error.message, true);
      }
    }

    document.getElementById("update-roster").addEventListener("click", async () => {
      try {
        const data = await request("/api/players", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: inputEl.value, voter_id: voterId })
        });
        players = data.players;
        render();
        setMessage(`${players.length} ${players.length === 1 ? "player" : "players"} loaded.`);
      } catch (error) {
        setMessage(error.message, true);
      }
    });

    refresh().catch(error => setMessage(error.message, true));
    window.setInterval(() => refresh().catch(error => setMessage(error.message, true)), 5000);
  </script>
</body>
</html>
"""


class TrackerHandler(BaseHTTPRequestHandler):
    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request length.") from exc
        if content_length <= 0 or content_length > MAX_REQUEST_SIZE:
            raise ValueError("Request must contain data under 1 MB.")
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request must contain valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def do_HEAD(self) -> None:
      parsed = urlparse(self.path)

      if parsed.path == "/":
          body = PAGE.encode("utf-8")
          content_type = "text/html; charset=utf-8"
          status = 200
      elif parsed.path == "/api/state":
          voter_id = parse_qs(parsed.query).get("voter_id", [""])[0]
          body = json.dumps(public_state(voter_id), ensure_ascii=False).encode("utf-8")
          content_type = "application/json; charset=utf-8"
          status = 200
      else:
          body = json.dumps({"error": "Not found."}).encode("utf-8")
          content_type = "application/json; charset=utf-8"
          status = 404

      self.send_response(status)
      self.send_header("Content-Type", content_type)
      self.send_header("Content-Length", str(len(body)))
      if parsed.path.startswith("/api/"):
          self.send_header("Cache-Control", "no-store")
      self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/state":
            voter_id = parse_qs(parsed.query).get("voter_id", [""])[0]
            with STATE_LOCK:
                self.send_json(200, public_state(voter_id))
            return
        self.send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        global STATE
        try:
            payload = self.read_json()
            with STATE_LOCK:
                updated_state = copy.deepcopy(STATE)
                if self.path == "/api/players":
                    text = payload.get("text")
                    if not isinstance(text, str):
                        raise ValueError("Player list must be text.")
                    text = clean_text(text)
                    names = parse_players(text)
                    updated_state["source_text"] = text
                    updated_state["players"] = names
                    updated_state["checked"] = []
                    updated_state["legacy_votes"] = {}
                    updated_state["ballots"] = {}
                elif self.path == "/api/attendance":
                    name = payload.get("name")
                    checked = payload.get("checked")
                    if not isinstance(name, str) or not isinstance(checked, bool):
                        raise ValueError("A player name and attendance status are required.")
                    key = player_key(name)
                    if key not in {player_key(player) for player in updated_state["players"]}:
                        raise ValueError("That player is no longer in the list. Refresh and try again.")
                    if checked and key not in updated_state["checked"]:
                        updated_state["checked"].append(key)
                    elif not checked:
                        updated_state["checked"] = [item for item in updated_state["checked"] if item != key]
                elif self.path == "/api/vote":
                    name = payload.get("name")
                    voter_id = payload.get("voter_id")
                    if not isinstance(name, str) or not isinstance(voter_id, str) or not voter_id or len(voter_id) > 128:
                        raise ValueError("A player name and valid browser ID are required.")
                    key = player_key(name)
                    if key not in {player_key(player) for player in updated_state["players"]}:
                        raise ValueError("That player is no longer in the list. Refresh and try again.")
                    if updated_state["ballots"].get(voter_id) == key:
                        del updated_state["ballots"][voter_id]
                    else:
                        updated_state["ballots"][voter_id] = key
                else:
                    self.send_json(404, {"error": "Not found."})
                    return

                save_state(updated_state)
                STATE = updated_state
                self.send_json(200, public_state(payload.get("voter_id", "")))
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except OSError:
            self.send_json(500, {"error": "Could not save the tracker data. Check that the app folder is writable."})

    def log_message(self, format_string: str, *args) -> None:
        logging.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), TrackerHandler)
    url = f"http://{HOST}:{PORT}"
    logging.info("Football player tracker running at %s", url)
    logging.info("Press Ctrl+C to stop the app.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Stopping football player tracker.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()