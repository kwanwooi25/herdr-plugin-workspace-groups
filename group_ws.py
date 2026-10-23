#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from typing import Any

PLUGIN_ID = "workspace-groups"
METADATA_SOURCE = f"plugin:{PLUGIN_ID}"
STATE_VERSION = 1
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

GROUP_PALETTE = (
    ("blue", "Blue", "#89b4fa", 111, curses.COLOR_BLUE),
    ("green", "Green", "#a6e3a1", 151, curses.COLOR_GREEN),
    ("peach", "Peach", "#fab387", 216, curses.COLOR_YELLOW),
    ("mauve", "Mauve", "#cba6f7", 183, curses.COLOR_MAGENTA),
    ("yellow", "Yellow", "#f9e2af", 223, curses.COLOR_YELLOW),
    ("teal", "Teal", "#94e2d5", 116, curses.COLOR_CYAN),
    ("red", "Red", "#f38ba8", 211, curses.COLOR_RED),
)
GROUP_COLOR_KEYS = tuple(color[0] for color in GROUP_PALETTE)
GROUP_COLOR_PAIR_BASE = 10


class PluginError(RuntimeError):
    pass


class HerdrClient:
    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
        if not self.socket_path:
            config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            self.socket_path = str(config_home / "herdr" / "herdr.sock")

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = f"workspace-groups:{os.getpid()}:{method}"
        payload = json.dumps(
            {"id": request_id, "method": method, "params": params or {}},
            separators=(",", ":"),
        ).encode() + b"\n"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            try:
                connection.connect(self.socket_path)
                connection.sendall(payload)
                response = self._read_line(connection)
            except OSError as error:
                raise PluginError(f"cannot reach Herdr at {self.socket_path}: {error}") from error

        try:
            decoded = json.loads(response)
        except json.JSONDecodeError as error:
            raise PluginError("Herdr returned invalid JSON") from error
        if decoded.get("id") != request_id:
            raise PluginError("Herdr returned a response for another request")
        if "error" in decoded:
            details = decoded["error"]
            message = details.get("message") if isinstance(details, dict) else str(details)
            raise PluginError(f"{method} failed: {message}")
        result = decoded.get("result")
        if not isinstance(result, dict):
            raise PluginError(f"{method} returned no result")
        return result

    @staticmethod
    def _read_line(connection: socket.socket) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                raise PluginError("Herdr closed the socket before replying")
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunks.append(chunk[:newline])
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise PluginError("Herdr response exceeded 8 MiB")

    def list_workspaces(self) -> list[dict[str, Any]]:
        result = self.request("workspace.list")
        workspaces = result.get("workspaces")
        if not isinstance(workspaces, list):
            raise PluginError("workspace.list returned no workspace list")
        return [workspace for workspace in workspaces if isinstance(workspace, dict)]

    def move_all(self, workspace_ids: list[str]) -> None:
        self.request("workspace.move_block", {"workspace_ids": workspace_ids})

    def report_group(self, workspace_id: str, tokens: dict[str, str | None]) -> None:
        self.request(
            "workspace.report_metadata",
            {
                "workspace_id": workspace_id,
                "source": METADATA_SOURCE,
                "tokens": tokens,
            },
        )


class StateStore:
    def __init__(self, socket_path: str) -> None:
        configured = os.environ.get("HERDR_PLUGIN_STATE_DIR")
        if configured:
            root = Path(configured)
        else:
            state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
            root = state_home / "herdr" / "plugins" / PLUGIN_ID
        self.path = root / "groups.json"
        self.session_key = hashlib.sha256(socket_path.encode()).hexdigest()[:16]

    def load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, json.JSONDecodeError) as error:
            raise PluginError(f"cannot read {self.path}: {error}") from error

        if not isinstance(raw, dict) or raw.get("version", STATE_VERSION) != STATE_VERSION:
            raise PluginError(f"unsupported state file: {self.path}")
        sessions = raw.setdefault("sessions", {})
        if not isinstance(sessions, dict):
            raise PluginError(f"invalid state file: {self.path}")
        session = sessions.setdefault(
            self.session_key, {"groups": [], "assignments": {}, "colors": {}}
        )
        groups = session.get("groups")
        assignments = session.get("assignments")
        colors = session.setdefault("colors", {})
        if not isinstance(groups, list) or not all(isinstance(item, str) for item in groups):
            raise PluginError(f"invalid group list in {self.path}")
        if not isinstance(assignments, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in assignments.items()
        ):
            raise PluginError(f"invalid assignments in {self.path}")
        if not isinstance(colors, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in colors.items()
        ):
            raise PluginError(f"invalid group colors in {self.path}")
        raw["version"] = STATE_VERSION
        return raw

    def session(self, state: dict[str, Any]) -> dict[str, Any]:
        return state["sessions"][self.session_key]

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as handle:
                handle.write(serialized)
                temporary = Path(handle.name)
            temporary.replace(self.path)
        except OSError as error:
            raise PluginError(f"cannot write {self.path}: {error}") from error


def next_group_color(groups: list[str], colors: dict[str, str]) -> str:
    used = {colors.get(group) for group in groups}
    for color in GROUP_COLOR_KEYS:
        if color not in used:
            return color
    return GROUP_COLOR_KEYS[len(groups) % len(GROUP_COLOR_KEYS)]


def normalize_session(session: dict[str, Any], live_ids: set[str]) -> bool:
    changed = False
    groups: list[str] = session["groups"]
    assignments: dict[str, str] = session["assignments"]
    colors: dict[str, str] = session.setdefault("colors", {})

    cleaned_groups: list[str] = []
    seen: set[str] = set()
    for group in groups:
        group = group.strip()
        if group and group not in seen:
            cleaned_groups.append(group)
            seen.add(group)
    if cleaned_groups != groups:
        session["groups"] = cleaned_groups
        groups = cleaned_groups
        changed = True

    cleaned_assignments = {
        workspace_id: group
        for workspace_id, group in assignments.items()
        if workspace_id in live_ids and group in seen
    }
    if cleaned_assignments != assignments:
        session["assignments"] = cleaned_assignments
        changed = True

    cleaned_colors: dict[str, str] = {}
    for group in groups:
        color = colors.get(group)
        if color not in GROUP_COLOR_KEYS:
            color = next_group_color(list(cleaned_colors), cleaned_colors)
        cleaned_colors[group] = color
    if cleaned_colors != colors:
        session["colors"] = cleaned_colors
        changed = True
    return changed


def workspace_id(workspace: dict[str, Any]) -> str:
    return str(workspace.get("workspace_id", ""))


def is_linked_worktree(workspace: dict[str, Any]) -> bool:
    worktree = workspace.get("worktree")
    return isinstance(worktree, dict) and worktree.get("is_linked_worktree") is True


def worktree_parent_map(workspaces: list[dict[str, Any]]) -> dict[str, str]:
    primary_by_repo: dict[str, str] = {}
    for workspace in workspaces:
        worktree = workspace.get("worktree")
        if not isinstance(worktree, dict) or is_linked_worktree(workspace):
            continue
        repo_key = worktree.get("repo_key")
        if isinstance(repo_key, str) and repo_key:
            primary_by_repo.setdefault(repo_key, workspace_id(workspace))

    parents: dict[str, str] = {}
    for workspace in workspaces:
        worktree = workspace.get("worktree")
        if not isinstance(worktree, dict) or not is_linked_worktree(workspace):
            continue
        repo_key = worktree.get("repo_key")
        parent_id = primary_by_repo.get(repo_key) if isinstance(repo_key, str) else None
        if parent_id:
            parents[workspace_id(workspace)] = parent_id
    return parents


def attach_worktrees(
    primary_order: list[dict[str, Any]], workspaces: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    parents = worktree_parent_map(workspaces)
    children: dict[str, list[dict[str, Any]]] = {}
    orphans: list[dict[str, Any]] = []
    for workspace in workspaces:
        if not is_linked_worktree(workspace):
            continue
        parent_id = parents.get(workspace_id(workspace))
        if parent_id:
            children.setdefault(parent_id, []).append(workspace)
        else:
            orphans.append(workspace)
    ordered: list[dict[str, Any]] = []
    for workspace in primary_order:
        ordered.append(workspace)
        ordered.extend(children.get(workspace_id(workspace), []))
    ordered.extend(orphans)
    return ordered


def drop_linked_worktree_assignments(
    session: dict[str, Any], workspaces: list[dict[str, Any]]
) -> bool:
    linked_ids = {workspace_id(workspace) for workspace in workspaces if is_linked_worktree(workspace)}
    assignments: dict[str, str] = session["assignments"]
    cleaned = {
        workspace_id_value: group
        for workspace_id_value, group in assignments.items()
        if workspace_id_value not in linked_ids
    }
    if cleaned == assignments:
        return False
    session["assignments"] = cleaned
    used_groups = set(cleaned.values())
    session["groups"] = [group for group in session["groups"] if group in used_groups]
    colors = session.get("colors", {})
    if isinstance(colors, dict):
        session["colors"] = {
            group: color for group, color in colors.items() if group in used_groups
        }
    return True


def ordered_workspaces(
    workspaces: list[dict[str, Any]], groups: list[str], assignments: dict[str, str]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {group: [] for group in groups}
    ungrouped: list[dict[str, Any]] = []
    for workspace in workspaces:
        if is_linked_worktree(workspace):
            continue
        group = assignments.get(workspace_id(workspace))
        if group in grouped:
            grouped[group].append(workspace)
        else:
            ungrouped.append(workspace)
    primary_order = [workspace for group in groups for workspace in grouped[group]] + ungrouped
    return attach_worktrees(primary_order, workspaces)

def move_workspace_in_section(
    workspaces: list[dict[str, Any]],
    assignments: dict[str, str],
    selected_workspace_id: str,
    direction: int,
) -> tuple[list[dict[str, Any]], bool]:
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    primary = [workspace for workspace in workspaces if not is_linked_worktree(workspace)]
    section = assignments.get(selected_workspace_id)
    section_indices = [
        index
        for index, workspace in enumerate(primary)
        if assignments.get(workspace_id(workspace)) == section
    ]
    selected_position = next(
        (
            position
            for position, index in enumerate(section_indices)
            if workspace_id(primary[index]) == selected_workspace_id
        ),
        None,
    )
    if selected_position is None:
        return workspaces, False
    target_position = selected_position + direction
    if target_position < 0 or target_position >= len(section_indices):
        return workspaces, False
    reordered_primary = list(primary)
    source_index = section_indices[selected_position]
    target_index = section_indices[target_position]
    reordered_primary[source_index], reordered_primary[target_index] = (
        reordered_primary[target_index],
        reordered_primary[source_index],
    )
    return attach_worktrees(reordered_primary, workspaces), True


def group_edges(
    workspaces: list[dict[str, Any]], assignments: dict[str, str]
) -> dict[str, str]:
    members: dict[str, list[str]] = {}
    for workspace in workspaces:
        if is_linked_worktree(workspace):
            continue
        workspace_id_value = workspace_id(workspace)
        group = assignments.get(workspace_id_value)
        if group:
            members.setdefault(group, []).append(workspace_id_value)

    edges: dict[str, str] = {}
    for group, workspace_ids in members.items():
        if len(workspace_ids) == 1:
            edges[workspace_ids[0]] = f"◆ {group}"
            continue
        for index, workspace_id_value in enumerate(workspace_ids):
            if index == 0:
                edges[workspace_id_value] = f"▾ {group}"
            elif index == len(workspace_ids) - 1:
                edges[workspace_id_value] = "└─"
            else:
                edges[workspace_id_value] = "├─"
    return edges

def sidebar_group_tokens(
    workspaces: list[dict[str, Any]],
    groups: list[str],
    assignments: dict[str, str],
    colors: dict[str, str],
) -> dict[str, dict[str, str | None]]:
    edges = group_edges(workspaces, assignments)
    tokens: dict[str, dict[str, str | None]] = {}
    for workspace in workspaces:
        workspace_tokens = {
            "group": None,
            "group_color": None,
            "group_edge": None,
            "group_badge": None,
            "group_header": None,
            "group_tree": None,
            "ungrouped_header": None,
            "ungrouped_tree": None,
            "ungrouped_badge": None,
        }
        workspace_tokens.update({f"group_badge_{color}": None for color in GROUP_COLOR_KEYS})
        tokens[workspace_id(workspace)] = workspace_tokens

    for group in groups:
        color = colors.get(group, GROUP_COLOR_KEYS[0])
        members = [
            workspace_id(workspace)
            for workspace in workspaces
            if not is_linked_worktree(workspace)
            and assignments.get(workspace_id(workspace)) == group
        ]
        for index, workspace_id_value in enumerate(members):
            tokens[workspace_id_value]["group"] = group
            tokens[workspace_id_value]["group_color"] = color
            tokens[workspace_id_value]["group_edge"] = edges.get(workspace_id_value)
            tokens[workspace_id_value]["group_badge"] = f"[{group}]"
            tokens[workspace_id_value][f"group_badge_{color}"] = f"[{group}]"
            tokens[workspace_id_value]["group_header"] = (
                f"━━ {group} · {len(members)} ━━━━━━━━━━" if index == 0 else None
            )
            tokens[workspace_id_value]["group_tree"] = (
                "└─" if index == len(members) - 1 else "├─"
            )

    ungrouped = [
        workspace_id(workspace)
        for workspace in workspaces
        if not is_linked_worktree(workspace) and workspace_id(workspace) not in assignments
    ]
    for index, workspace_id_value in enumerate(ungrouped):
        tokens[workspace_id_value]["ungrouped_header"] = (
            f"━━ Ungrouped · {len(ungrouped)} ━━━━━" if index == 0 else None
        )
        tokens[workspace_id_value]["ungrouped_badge"] = "[—]"
        tokens[workspace_id_value]["ungrouped_tree"] = (
            "└─" if index == len(ungrouped) - 1 else "├─"
        )

    for child_id, parent_id in worktree_parent_map(workspaces).items():
        group = assignments.get(parent_id)
        tokens[child_id]["group"] = group
        tokens[child_id]["group_color"] = colors.get(group) if group else None
    return tokens

def sync(client: HerdrClient, store: StateStore, quiet: bool = False) -> list[dict[str, Any]]:
    workspaces = client.list_workspaces()
    state = store.load()
    session = store.session(state)
    live_ids = {workspace_id(workspace) for workspace in workspaces}
    state_changed = normalize_session(session, live_ids)
    state_changed = drop_linked_worktree_assignments(session, workspaces) or state_changed

    groups: list[str] = session["groups"]
    assignments: dict[str, str] = session["assignments"]
    colors: dict[str, str] = session["colors"]
    ordered = ordered_workspaces(workspaces, groups, assignments)
    current_ids = [workspace_id(workspace) for workspace in workspaces]
    ordered_ids = [workspace_id(workspace) for workspace in ordered]
    if ordered_ids != current_ids:
        client.move_all(ordered_ids)

    metadata = sidebar_group_tokens(ordered, groups, assignments, colors)
    for workspace in ordered:
        workspace_id_value = workspace_id(workspace)
        client.report_group(workspace_id_value, metadata[workspace_id_value])

    if state_changed:
        store.save(state)
    if not quiet:
        assigned = sum(
            1
            for workspace in ordered
            if not is_linked_worktree(workspace) and workspace_id(workspace) in assignments
        )
        print(f"Synced {assigned} grouped workspace(s) across {len(groups)} group(s).")
    return ordered


def open_manager() -> None:
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", PLUGIN_ID)
    completed = subprocess.run(
        [herdr, "plugin", "pane", "open", "--plugin", plugin_id, "--entrypoint", "manager"],
        check=False,
    )
    if completed.returncode:
        raise PluginError(f"Herdr could not open the group manager (exit {completed.returncode})")


def clipped(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


class GroupManager:
    def __init__(self, client: HerdrClient, store: StateStore) -> None:
        self.client = client
        self.store = store
        self.state = store.load()
        self.session = store.session(self.state)
        self.workspaces: list[dict[str, Any]] = []
        self.selected = 0
        self.scroll = 0
        self.message = ""
        self.row_targets: dict[int, int] = {}
        self.button_targets: list[tuple[int, int, int, str]] = []
        self.refresh()

    @property
    def groups(self) -> list[str]:
        return self.session["groups"]

    @property
    def assignments(self) -> dict[str, str]:
        return self.session["assignments"]

    @property
    def colors(self) -> dict[str, str]:
        return self.session["colors"]

    def group_color(self, group: str) -> str:
        color = self.colors.get(group, GROUP_COLOR_KEYS[0])
        return color if color in GROUP_COLOR_KEYS else GROUP_COLOR_KEYS[0]

    @staticmethod
    def color_style(color: str) -> int:
        color_index = GROUP_COLOR_KEYS.index(color)
        return curses.color_pair(GROUP_COLOR_PAIR_BASE + color_index) | curses.A_BOLD

    def group_style(self, group: str) -> int:
        return self.color_style(self.group_color(group))

    def refresh(self) -> None:
        selected_id = self.selected_id()
        self.workspaces = self.client.list_workspaces()
        live_ids = {workspace_id(workspace) for workspace in self.workspaces}
        state_changed = normalize_session(self.session, live_ids)
        state_changed = drop_linked_worktree_assignments(self.session, self.workspaces) or state_changed
        if state_changed:
            self.store.save(self.state)
        self.workspaces = ordered_workspaces(self.workspaces, self.groups, self.assignments)
        self.restore_selection(selected_id)

    def selectable_indices(self) -> list[int]:
        return [
            index
            for index, workspace in enumerate(self.workspaces)
            if not is_linked_worktree(workspace)
        ]

    def restore_selection(self, selected_workspace_id: str | None) -> None:
        parents = worktree_parent_map(self.workspaces)
        selected_workspace_id = parents.get(selected_workspace_id or "", selected_workspace_id)
        selectable = self.selectable_indices()
        if not selectable:
            self.selected = 0
            return
        if selected_workspace_id:
            for index in selectable:
                if workspace_id(self.workspaces[index]) == selected_workspace_id:
                    self.selected = index
                    return
        self.selected = selectable[0]

    def move_selection(self, direction: int) -> None:
        selectable = self.selectable_indices()
        if not selectable:
            return
        try:
            position = selectable.index(self.selected)
        except ValueError:
            self.selected = selectable[0]
            return
        target = max(0, min(len(selectable) - 1, position + direction))
        self.selected = selectable[target]

    def selected_id(self) -> str | None:
        if not self.workspaces or self.selected >= len(self.workspaces):
            return None
        workspace = self.workspaces[self.selected]
        selected_workspace_id = workspace_id(workspace)
        if is_linked_worktree(workspace):
            return worktree_parent_map(self.workspaces).get(selected_workspace_id)
        return selected_workspace_id

    def selected_group(self) -> str | None:
        workspace_id = self.selected_id()
        return self.assignments.get(workspace_id) if workspace_id else None

    def persist_and_sync(self) -> None:
        selected_id = self.selected_id()
        self.store.save(self.state)
        self.workspaces = sync(self.client, self.store, quiet=True)
        self.restore_selection(selected_id)
        self.message = "Saved and synced"

    def assign(self, group: str) -> None:
        workspace_id = self.selected_id()
        group = " ".join(group.split())
        if not workspace_id or not group:
            return
        if len(group) > 32:
            self.message = "Group names are limited to 32 characters"
            return
        if group not in self.groups:
            self.colors[group] = next_group_color(self.groups, self.colors)
            self.groups.append(group)
        self.assignments[workspace_id] = group
        self.persist_and_sync()

    def rename_group(self, name: str) -> None:
        group = self.selected_group()
        if not group:
            self.message = "The selected workspace is not grouped"
            return
        name = " ".join(name.split())
        if not name:
            self.message = "Group names cannot be empty"
            return
        if len(name) > 32:
            self.message = "Group names are limited to 32 characters"
            return
        if name == group:
            self.message = "Group name unchanged"
            return
        if name in self.groups:
            self.message = f"Group already exists: {name}"
            return
        self.groups[self.groups.index(group)] = name
        for workspace_id, assigned in self.assignments.items():
            if assigned == group:
                self.assignments[workspace_id] = name
        self.colors[name] = self.colors.pop(group)
        self.persist_and_sync()
        self.message = f"Renamed group: {group} → {name}"

    def request_group_rename(self, screen: curses.window) -> None:
        group = self.selected_group()
        if not group:
            self.message = "The selected workspace is not grouped"
            return
        name = self.text_prompt(screen, "Rename group", initial=group, submit="rename")
        if name is not None:
            self.rename_group(name)

    def ungroup(self) -> None:
        workspace_id = self.selected_id()
        if workspace_id and workspace_id in self.assignments:
            group = self.assignments.pop(workspace_id)
            if group not in self.assignments.values():
                self.groups.remove(group)
                self.colors.pop(group, None)
            self.persist_and_sync()
        else:
            self.message = "The selected workspace is already ungrouped"

    def move_group(self, direction: int) -> None:
        group = self.selected_group()
        if not group:
            self.message = "The selected workspace is not grouped"
            return
        index = self.groups.index(group)
        target = index + direction
        if target < 0 or target >= len(self.groups):
            self.message = "The group is already at that edge"
            return
        self.groups[index], self.groups[target] = self.groups[target], self.groups[index]
        self.persist_and_sync()

    def set_group_color(self, color: str) -> None:
        group = self.selected_group()
        if not group:
            self.message = "The selected workspace is not grouped"
            return
        if color not in GROUP_COLOR_KEYS:
            self.message = "Unknown group color"
            return
        self.colors[group] = color
        self.persist_and_sync()
        self.message = f"{group} color: {color}"

    def move_workspace(self, direction: int) -> None:
        workspace_id = self.selected_id()
        if not workspace_id:
            return
        reordered, changed = move_workspace_in_section(
            self.workspaces, self.assignments, workspace_id, direction
        )
        if not changed:
            self.message = "The workspace is already at that edge"
            return
        self.client.move_all(
            [str(workspace.get("workspace_id", "")) for workspace in reordered]
        )
        self.workspaces = sync(self.client, self.store, quiet=True)
        self.restore_selection(workspace_id)
        self.message = "Workspace order updated"

    def delete_group(self) -> None:
        group = self.selected_group()
        if not group:
            self.message = "The selected workspace is not grouped"
            return
        self.session["groups"] = [item for item in self.groups if item != group]
        self.session["assignments"] = {
            workspace_id: assigned
            for workspace_id, assigned in self.assignments.items()
            if assigned != group
        }
        self.colors.pop(group, None)
        self.persist_and_sync()
        self.message = f"Deleted group: {group}"

    def run(self, screen: curses.window) -> None:
        curses.curs_set(0)
        screen.keypad(True)
        try:
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_GREEN, -1)
            curses.init_pair(5, curses.COLOR_RED, -1)
            color_count = getattr(curses, "COLORS", 0)
            for index, (_, _, _, xterm_color, fallback_color) in enumerate(GROUP_PALETTE):
                foreground = xterm_color if color_count > xterm_color else fallback_color
                curses.init_pair(GROUP_COLOR_PAIR_BASE + index, foreground, -1)
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(150)
        except curses.error:
            pass

        while True:
            self.draw(screen)
            key = screen.get_wch()
            self.message = ""
            if key in ("q", "Q", "\x1b"):
                return
            if key == curses.KEY_MOUSE:
                if self.handle_mouse(screen):
                    return
            elif key in (curses.KEY_UP, "k"):
                self.move_selection(-1)
            elif key in (curses.KEY_DOWN, "j"):
                self.move_selection(1)
            elif key == "K":
                self.move_workspace(-1)
            elif key == "J":
                self.move_workspace(1)
            elif key in ("h", "["):
                self.move_group(-1)
            elif key in ("l", "]"):
                self.move_group(1)
            elif key in ("g", "G", "\n", curses.KEY_ENTER):
                self.pick_group(screen)
            elif key in ("c", "C"):
                self.pick_color(screen)
            elif key in ("n", "N"):
                self.request_group_rename(screen)
            elif key in ("u", "U", curses.KEY_BACKSPACE, "\x7f"):
                self.ungroup()
            elif key in ("x", "X"):
                self.request_group_delete(screen)
            elif key in ("r", "R"):
                self.refresh()
                sync(self.client, self.store, quiet=True)
                self.message = "Refreshed"
            elif key == "?":
                self.help(screen)

    def handle_mouse(self, screen: curses.window) -> bool:
        try:
            _, mouse_x, mouse_y, _, state = curses.getmouse()
        except curses.error:
            return False

        wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
        wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
        if wheel_up and state & wheel_up:
            self.move_selection(-1)
            return False
        if wheel_down and state & wheel_down:
            self.move_selection(1)
            return False

        for target_y, start_x, end_x, action in self.button_targets:
            if mouse_y == target_y and start_x <= mouse_x < end_x:
                return self.handle_action(screen, action)

        if mouse_y in self.row_targets:
            index = self.row_targets[mouse_y]
            already_selected = index == self.selected
            self.selected = index
            double_click = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
            right_click = getattr(curses, "BUTTON3_CLICKED", 0)
            if (double_click and state & double_click) or (right_click and state & right_click):
                self.pick_group(screen)
            elif already_selected and state & getattr(curses, "BUTTON1_CLICKED", 0):
                self.message = "Double-click or use Assign to change the group"
        return False

    def handle_action(self, screen: curses.window, action: str) -> bool:
        if action == "assign":
            self.pick_group(screen)
        elif action == "rename":
            self.request_group_rename(screen)
        elif action == "color":
            self.pick_color(screen)
        elif action == "ungroup":
            self.ungroup()
        elif action == "move_up":
            self.move_workspace(-1)
        elif action == "move_down":
            self.move_workspace(1)
        elif action == "previous":
            self.move_group(-1)
        elif action == "next":
            self.move_group(1)
        elif action == "delete":
            self.request_group_delete(screen)
        elif action == "refresh":
            self.refresh()
            sync(self.client, self.store, quiet=True)
            self.message = "Refreshed"
        elif action == "keys":
            self.help(screen)
        elif action == "close":
            return True
        return False

    def request_group_delete(self, screen: curses.window) -> None:
        group = self.selected_group()
        if group and self.confirm(screen, f"Delete '{group}' and ungroup its workspaces?"):
            self.delete_group()
        elif not group:
            self.message = "The selected workspace is not grouped"

    def workspace_sections(self) -> list[tuple[str | None, list[tuple[int, dict[str, Any]]]]]:
        sections: list[tuple[str | None, list[tuple[int, dict[str, Any]]]]] = []
        for group in self.groups:
            members = [
                (index, workspace)
                for index, workspace in enumerate(self.workspaces)
                if not is_linked_worktree(workspace)
                and self.assignments.get(workspace_id(workspace)) == group
            ]
            if members:
                sections.append((group, members))
        ungrouped = [
            (index, workspace)
            for index, workspace in enumerate(self.workspaces)
            if not is_linked_worktree(workspace)
            and workspace_id(workspace) not in self.assignments
        ]
        if ungrouped:
            sections.append((None, ungrouped))
        return sections

    def render_rows(self, panel_width: int) -> list[tuple[str, int | None, int]]:
        rows: list[tuple[str, int | None, int]] = []
        parent_map = worktree_parent_map(self.workspaces)
        children: dict[str, list[dict[str, Any]]] = {}
        for workspace in self.workspaces:
            parent_id = parent_map.get(workspace_id(workspace))
            if parent_id:
                children.setdefault(parent_id, []).append(workspace)

        for section_index, (group, members) in enumerate(self.workspace_sections()):
            if section_index:
                rows.append(("", None, 0))
            title = group if group else "Ungrouped"
            token = f" {title} · {len(members)} "
            top = "╭─" + token
            top += "─" * max(0, panel_width - len(top) - 1) + "╮"
            header_style = self.group_style(group) if group else curses.A_DIM
            rows.append((top[:panel_width], None, header_style))
            for workspace_index, workspace in members:
                rows.append(
                    (
                        self.workspace_line(workspace, panel_width),
                        workspace_index,
                        self.workspace_style(workspace_index, workspace),
                    )
                )
                for child in children.get(workspace_id(workspace), []):
                    rows.append(
                        (
                            self.worktree_line(child, panel_width),
                            None,
                            self.worktree_style(child),
                        )
                    )
            rows.append(("╰" + "─" * max(0, panel_width - 2) + "╯", None, header_style))
        return rows

    @staticmethod
    def workspace_line(workspace: dict[str, Any], panel_width: int) -> str:
        inner_width = max(1, panel_width - 2)
        label = str(workspace.get("label") or workspace.get("workspace_id") or "")
        state = str(workspace.get("agent_status") or "unknown")
        icon = {"working": "●", "blocked": "!", "done": "✓", "idle": "○"}.get(state, "·")
        worktree = workspace.get("worktree")
        repo = str(worktree.get("repo_name") or "") if isinstance(worktree, dict) else ""
        state_width = 8
        label_width = max(12, min(38, inner_width - state_width - len(repo) - 7))
        content = f" {icon} {label:<{label_width}.{label_width}}  {state:<{state_width}.{state_width}}"
        if repo:
            content += f"  {repo}"
        return "│" + clipped(content, inner_width).ljust(inner_width) + "│"

    @staticmethod
    def worktree_line(workspace: dict[str, Any], panel_width: int) -> str:
        inner_width = max(1, panel_width - 2)
        label = str(workspace.get("label") or workspace_id(workspace))
        state = str(workspace.get("agent_status") or "unknown")
        label_width = max(12, min(42, inner_width - 27))
        content = f"   ↳ {label:<{label_width}.{label_width}}  {state:<8.8}  inherits parent"
        return "│" + clipped(content, inner_width).ljust(inner_width) + "│"

    @staticmethod
    def worktree_style(workspace: dict[str, Any]) -> int:
        state = str(workspace.get("agent_status") or "unknown")
        if state == "working":
            return curses.color_pair(4)
        if state == "blocked":
            return curses.color_pair(5) | curses.A_BOLD
        return curses.A_DIM

    def workspace_style(self, workspace_index: int, workspace: dict[str, Any]) -> int:
        if workspace_index == self.selected:
            return curses.color_pair(2) | curses.A_BOLD
        state = str(workspace.get("agent_status") or "unknown")
        if state == "working":
            return curses.color_pair(4)
        if state == "blocked":
            return curses.color_pair(5) | curses.A_BOLD
        return 0

    def draw(self, screen: curses.window) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        self.row_targets = {}
        self.button_targets = []
        if height < 14 or width < 56:
            screen.addnstr(0, 0, "Popup is too small. Resize Herdr and reopen.", max(1, width - 1))
            screen.refresh()
            return

        screen.addnstr(0, 2, " Workspace Groups ", width - 4, curses.color_pair(1) | curses.A_BOLD)
        screen.hline(1, 0, curses.ACS_HLINE, width)
        screen.addnstr(
            2,
            2,
            "j/k select · J/K move · g assign · n rename · c color · h/l order · ? help",
            width - 4,
            curses.A_BOLD,
        )

        panel_left = 2
        panel_width = width - 4
        content_top = 4
        content_bottom = height - 4
        content_height = content_bottom - content_top
        rows = self.render_rows(panel_width)
        selected_line = next(
            (line for line, (_, workspace_index, _) in enumerate(rows) if workspace_index == self.selected),
            0,
        )
        if selected_line < self.scroll:
            self.scroll = max(0, selected_line - 1)
        elif selected_line >= self.scroll + content_height:
            self.scroll = max(0, selected_line - content_height + 2)
        self.scroll = min(self.scroll, max(0, len(rows) - content_height))

        for offset, (text, workspace_index, attributes) in enumerate(
            rows[self.scroll : self.scroll + content_height]
        ):
            row = content_top + offset
            screen.addnstr(row, panel_left, clipped(text, panel_width), panel_width, attributes)
            if workspace_index is not None:
                self.row_targets[row] = workspace_index

        screen.hline(height - 4, 0, curses.ACS_HLINE, width)
        self.draw_buttons(screen, height - 3, width)
        screen.hline(height - 2, 0, curses.ACS_HLINE, width)
        current = self.selected_group() or "Ungrouped"
        primary_count = sum(1 for workspace in self.workspaces if not is_linked_worktree(workspace))
        worktree_count = len(self.workspaces) - primary_count
        footer = self.message or (
            f"Selected: {current} · {primary_count} workspaces · {worktree_count} inherited worktrees"
            " · Mouse: click selects, double/right-click assigns"
        )
        screen.refresh()

    def draw_buttons(self, screen: curses.window, row: int, width: int) -> None:
        utilities = [("keys", "? Help"), ("close", "q Close")]
        utility_texts = [(action, f" [{label}]") for action, label in utilities]
        utility_width = sum(len(text) for _, text in utility_texts) + len(utility_texts)
        utility_x = max(2, width - utility_width - 2)
        x = utility_x
        for action, text in utility_texts:
            screen.addstr(row, x, text, curses.A_DIM)
            self.button_targets.append((row, x, x + len(text), action))
            x += len(text) + 1

        actions = [
            ("assign", "g Group"),
            ("rename", "n Rename"),
            ("color", "c Color"),
            ("ungroup", "u Remove"),
            ("move_up", "K Up"),
            ("move_down", "J Down"),
            ("previous", "h Group◀"),
            ("next", "l Group▶"),
            ("delete", "x Delete"),
            ("refresh", "r Refresh"),
        ]
        x = 2
        for action, label in actions:
            text = f"[{label}]"
            if x + len(text) + 1 > utility_x:
                break
            screen.addstr(row, x, text, curses.A_BOLD if action == "assign" else curses.A_DIM)
            self.button_targets.append((row, x, x + len(text), action))
            x += len(text) + 1

    def pick_group(self, screen: curses.window) -> None:
        current = self.selected_group()
        options: list[tuple[str, str | None]] = []
        for group in self.groups:
            count = sum(1 for assigned in self.assignments.values() if assigned == group)
            marker = "●" if group == current else " "
            options.append((f"{marker} {group}  ({count})", group))
        options.append(("+ Create new group…", "__new__"))
        if current:
            options.append(("  Remove from group", None))

        selected = next((index for index, (_, value) in enumerate(options) if value == current), 0)
        picker_scroll = 0
        while True:
            self.draw(screen)
            height, width = screen.getmaxyx()
            box_width = min(max(42, max(len(label) for label, _ in options) + 8), width - 4)
            box_height = min(len(options) + 6, height - 2)
            top = max(1, (height - box_height) // 2)
            left = max(2, (width - box_width) // 2)
            option_rows: dict[int, int] = {}
            option_capacity = max(1, box_height - 4)
            if selected < picker_scroll:
                picker_scroll = selected
            elif selected >= picker_scroll + option_capacity:
                picker_scroll = selected - option_capacity + 1

            screen.addstr(top, left, "╭" + "─" * (box_width - 2) + "╮", curses.color_pair(1))
            title = " Assign workspace to a group "
            screen.addnstr(top, left + 2, title, box_width - 4, curses.color_pair(1) | curses.A_BOLD)
            visible_options = range(picker_scroll, min(len(options), picker_scroll + option_capacity))
            for visible_index, option_index in enumerate(visible_options):
                row = top + 2 + visible_index
                label = options[option_index][0]
                text = "│ " + clipped(label, box_width - 4).ljust(box_width - 4) + " │"
                style = curses.color_pair(2) | curses.A_BOLD if option_index == selected else 0
                screen.addnstr(row, left, text, box_width, style)
                option_rows[row] = option_index
            footer_row = top + box_height - 2
            screen.addnstr(
                footer_row,
                left,
                "│ ↑/↓ choose · click or Enter · Esc cancel".ljust(box_width - 1) + "│",
                box_width,
                curses.A_DIM,
            )
            screen.addstr(top + box_height - 1, left, "╰" + "─" * (box_width - 2) + "╯", curses.color_pair(1))
            screen.refresh()

            key = screen.get_wch()
            if key in ("\x1b", "q", "Q"):
                return
            if key in (curses.KEY_UP, "k"):
                selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, "j"):
                selected = (selected + 1) % len(options)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if self.apply_group_choice(screen, options[selected][1]):
                    return
            elif key in ("n", "N"):
                if self.apply_group_choice(screen, "__new__"):
                    return
            elif key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, state = curses.getmouse()
                except curses.error:
                    continue
                if mouse_y in option_rows and left <= mouse_x < left + box_width:
                    selected = option_rows[mouse_y]
                    if state & (
                        getattr(curses, "BUTTON1_CLICKED", 0)
                        | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
                    ):
                        if self.apply_group_choice(screen, options[selected][1]):
                            return

    def pick_color(self, screen: curses.window) -> None:
        group = self.selected_group()
        if not group:
            self.message = "The selected workspace is not grouped"
            return

        current = self.group_color(group)
        selected = GROUP_COLOR_KEYS.index(current)
        option_rows: dict[int, int] = {}
        while True:
            self.draw(screen)
            height, width = screen.getmaxyx()
            box_width = min(38, width - 4)
            box_height = len(GROUP_PALETTE) + 5
            top = max(1, (height - box_height) // 2)
            left = max(2, (width - box_width) // 2)
            option_rows.clear()

            screen.addstr(top, left, "╭" + "─" * (box_width - 2) + "╮", self.group_style(group))
            title = f" Color for {group} "
            screen.addnstr(top, left + 2, title, box_width - 4, self.group_style(group))
            for index, (color, label, _, _, _) in enumerate(GROUP_PALETTE):
                row = top + 2 + index
                marker = "›" if index == selected else " "
                text = f"│ {marker} ● {label}".ljust(box_width - 1) + "│"
                style = self.color_style(color)
                if index == selected:
                    style |= curses.A_REVERSE
                screen.addnstr(row, left, text, box_width, style)
                option_rows[row] = index
            footer_row = top + box_height - 2
            screen.addnstr(
                footer_row,
                left,
                "│ j/k choose · Enter apply · Esc".ljust(box_width - 1) + "│",
                box_width,
                curses.A_DIM,
            )
            screen.addstr(
                top + box_height - 1,
                left,
                "╰" + "─" * (box_width - 2) + "╯",
                self.group_style(group),
            )
            screen.refresh()

            key = screen.get_wch()
            if key in ("\x1b", "q", "Q"):
                return
            if key in (curses.KEY_UP, "k"):
                selected = (selected - 1) % len(GROUP_PALETTE)
            elif key in (curses.KEY_DOWN, "j"):
                selected = (selected + 1) % len(GROUP_PALETTE)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.set_group_color(GROUP_PALETTE[selected][0])
                return
            elif key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, state = curses.getmouse()
                except curses.error:
                    continue
                if mouse_y in option_rows and left <= mouse_x < left + box_width:
                    selected = option_rows[mouse_y]
                    if state & (
                        getattr(curses, "BUTTON1_CLICKED", 0)
                        | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
                    ):
                        self.set_group_color(GROUP_PALETTE[selected][0])
                        return

    def apply_group_choice(self, screen: curses.window, choice: str | None) -> bool:
        if choice == "__new__":
            group = self.text_prompt(screen, "New group name")
            if group:
                self.assign(group)
                return True
            return False
        if choice is None:
            self.ungroup()
            return True
        self.assign(choice)
        return True

    def text_prompt(
        self, screen: curses.window, label: str, initial: str = "", submit: str = "create"
    ) -> str | None:
        value = initial
        curses.curs_set(1)
        try:
            while True:
                self.draw(screen)
                height, width = screen.getmaxyx()
                box_width = min(58, width - 4)
                top = max(1, height // 2 - 3)
                left = max(2, (width - box_width) // 2)
                screen.addstr(top, left, "╭" + "─" * (box_width - 2) + "╮", curses.color_pair(1))
                screen.addnstr(top, left + 2, f" {label} ", box_width - 4, curses.color_pair(1) | curses.A_BOLD)
                field = clipped(value, box_width - 6).ljust(box_width - 6)
                screen.addnstr(top + 2, left, f"│  {field}  │", box_width, curses.color_pair(2))
                hint = f"Enter {submit} · Esc cancel"
                screen.addnstr(top + 4, left, ("│ " + hint).ljust(box_width - 1) + "│", box_width, curses.A_DIM)
                screen.addstr(top + 5, left, "╰" + "─" * (box_width - 2) + "╯", curses.color_pair(1))
                screen.move(top + 2, min(left + box_width - 3, left + 3 + len(value)))
                screen.refresh()
                key = screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value.strip()
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    value = value[:-1]
                elif isinstance(key, str) and key.isprintable() and len(value) < 32:
                    value += key
        finally:
            curses.curs_set(0)

    def confirm(self, screen: curses.window, message: str) -> bool:
        selected = False
        while True:
            self.draw(screen)
            height, width = screen.getmaxyx()
            box_width = min(max(50, len(message) + 6), width - 4)
            top = max(1, height // 2 - 2)
            left = max(2, (width - box_width) // 2)
            screen.addstr(top, left, "╭" + "─" * (box_width - 2) + "╮", curses.color_pair(3))
            screen.addnstr(top + 1, left, ("│  " + message).ljust(box_width - 1) + "│", box_width)
            no_text = "[ Cancel ]"
            yes_text = "[ Delete ]"
            no_x = left + 3
            yes_x = left + box_width - len(yes_text) - 3
            screen.addstr(top + 3, no_x, no_text, curses.color_pair(2) if not selected else curses.A_DIM)
            screen.addstr(top + 3, yes_x, yes_text, curses.color_pair(2) if selected else curses.A_DIM)
            screen.addstr(top + 4, left, "╰" + "─" * (box_width - 2) + "╯", curses.color_pair(3))
            screen.refresh()
            key = screen.get_wch()
            if key in ("\x1b", "n", "N", "q", "Q"):
                return False
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT, "\t", "h", "l"):
                selected = not selected
            elif key in ("\n", "\r", curses.KEY_ENTER, "y", "Y"):
                return selected or key in ("y", "Y")
            elif key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, state = curses.getmouse()
                except curses.error:
                    continue
                if state & getattr(curses, "BUTTON1_CLICKED", 0) and mouse_y == top + 3:
                    if no_x <= mouse_x < no_x + len(no_text):
                        return False
                    if yes_x <= mouse_x < yes_x + len(yes_text):
                        return True

    @staticmethod
    def help(screen: curses.window) -> None:
        height, width = screen.getmaxyx()
        lines = [
            "Keyboard controls",
            "",
            "Navigation",
            "j / ↓       Select next parent workspace",
            "k / ↑       Select previous parent workspace",
            "",
            "Ordering",
            "J           Move workspace down inside its group",
            "K           Move workspace up inside its group",
            "h / [       Move the selected group earlier",
            "l / ]       Move the selected group later",
            "",
            "Grouping",
            "Enter / g   Choose an existing group or create a new one",
            "n           Rename the selected group",
            "c           Choose the selected group's color",
            "u           Remove the workspace from its group",
            "x           Delete the selected group",
            "",
            "General",
            "r           Refresh workspace state",
            "?           Open this keyboard menu",
            "q / Esc     Close",
            "",
            "Worktrees inherit their parent workspace group and cannot be selected.",
            "Mouse (secondary): click a parent row or the key-labeled controls.",
            "Press any key or click to return.",
        ]
        screen.erase()
        top = max(0, (height - len(lines)) // 2)
        for offset, line in enumerate(lines[:height]):
            left = max(0, (width - len(line)) // 2)
            screen.addnstr(top + offset, left, clipped(line, width - 1), width - 1)
        screen.refresh()
        screen.get_wch()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group Herdr workspaces")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("open", help="Open the popup manager")
    sync_parser = subparsers.add_parser("sync", help="Reorder workspaces and refresh metadata")
    sync_parser.add_argument("--quiet", action="store_true")
    subparsers.add_parser("manage", help="Run the popup manager")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "open":
            open_manager()
            return 0

        client = HerdrClient()
        store = StateStore(client.socket_path)
        if args.command == "sync":
            sync(client, store, quiet=args.quiet)
        elif args.command == "manage":
            manager = GroupManager(client, store)
            curses.wrapper(manager.run)
        return 0
    except (PluginError, curses.error) as error:
        print(f"workspace-groups: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
