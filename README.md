# Workspace Groups for Herdr

A keyboard-first Herdr plugin that keeps related workspaces together, adds colored group badges to the sidebar, and makes linked Git worktrees inherit their parent workspace group.

```text
spaces
· [Work] platform
  └─ feat/cache
· [Work] api
· [Personal] notes
  └─ feat/search
· [—] dotfiles
```

## Requirements

- Herdr 0.9.1 or newer
- Python 3.10 or newer
- Linux or macOS

The plugin has no third-party Python dependencies and makes no network requests.

## Install

Install the latest version directly from GitHub:

```bash
herdr plugin install kwanwooi25/herdr-plugin-workspace-groups
```

Configure colored sidebar badges once:

```bash
herdr plugin action invoke workspace-groups.setup-sidebar
```

The setup action safely appends `[ui.sidebar.spaces]` only when that table does not already exist, then reloads Herdr and refreshes group metadata. If you already customize that table, it refuses to overwrite it; print the required TOML and merge the token entries into your existing rows:

```bash
herdr plugin action invoke workspace-groups.sidebar-config
herdr server reload-config
herdr plugin action invoke workspace-groups.sync
```

To install an exact release:

```bash
herdr plugin install kwanwooi25/herdr-plugin-workspace-groups --ref v0.6.0
```

Herdr v1 does not have a separate plugin update command. Re-run `plugin install` to replace a GitHub-managed checkout with the latest version.

## Open the manager

```bash
herdr plugin pane open --plugin workspace-groups --entrypoint manager
```

Or add a keybinding to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+f"
type = "plugin_action"
command = "workspace-groups.manage"
description = "manage workspace groups"
```

Then reload the running server:

```bash
herdr server reload-config
```

## Controls

| Key | Action |
|---|---|
| `j` / `k` | Select the next or previous parent workspace |
| `J` / `K` | Move a workspace within its current group |
| `g` / `Enter` | Assign to an existing or new group |
| `u` | Remove from the current group |
| `c` | Choose the group color |
| `h` / `l` | Move the selected group earlier or later |
| `x` | Delete the selected group |
| `r` | Refresh workspace state |
| `?` | Open keyboard help |
| `q` / `Esc` | Close |

Mouse input remains available as a secondary path: click parent rows and key-labeled controls, or double/right-click a parent row to assign it.

## Behavior

- Group members remain contiguous in the Herdr sidebar.
- Workspace order is preserved inside each group unless you move it explicitly.
- Linked Git worktrees appear immediately after their parent workspace.
- A linked worktree inherits its parent group and color; it cannot be grouped or reordered independently.
- Group colors are selected from Blue, Green, Peach, Mauve, Yellow, Teal, and Red.
- Existing Herdr sessions store independent group assignments.
- Startup and workspace lifecycle hooks keep ordering and metadata synchronized.

Herdr's current plugin API cannot insert collapsible native section headers into the sidebar. This plugin therefore represents groups with colored badges and contiguous ordering while keeping Herdr's native branch and Git-status rows.

## Data and security

The plugin reads workspace state from the local Herdr socket and stores only group names, assignments, ordering, and color choices under Herdr's plugin state directory. It does not inspect pane contents, collect credentials, or contact external services.

A Herdr plugin runs as your user and is not sandboxed. Review `herdr-plugin.toml`, `group_ws.py`, and `configure_sidebar.py` before installation, or pin a trusted release with `--ref`.

## Development

Link a local checkout:

```bash
herdr plugin link /path/to/herdr-plugin-workspace-groups
```

Run the regression suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_group_ws.py
```

Project files:

```text
herdr-plugin.toml     Plugin actions, events, startup hook, and manager pane
group_ws.py           State, ordering, metadata sync, and curses manager
configure_sidebar.py  Safe one-time sidebar configuration helper
test_group_ws.py      Ordering, inheritance, metadata, and setup regressions
```

## Uninstall

```bash
herdr plugin uninstall workspace-groups
```

Herdr keeps plugin config and state directories when replacing or reinstalling plugins. Remove those directories manually only if you also want to discard saved groups.

## License

No license file is included. Public availability does not grant redistribution or modification rights.
