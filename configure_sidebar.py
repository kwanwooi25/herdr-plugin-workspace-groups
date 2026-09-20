#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PLUGIN_ROOT = Path(__file__).resolve().parent
TOKENS = (
    "group_badge_blue",
    "group_badge_green",
    "group_badge_peach",
    "group_badge_mauve",
    "group_badge_yellow",
    "group_badge_teal",
    "group_badge_red",
)
SPACES_HEADER = "[ui.sidebar.spaces]"
SIDEBAR_CONFIG = """# Workspace group badges reported by the workspace-groups plugin.
[ui.sidebar.spaces]
rows = [
  [
    "state_icon",
    { token = "$group_badge_blue", fg = "#89b4fa", bold = true, dim = false },
    { token = "$group_badge_green", fg = "#a6e3a1", bold = true, dim = false },
    { token = "$group_badge_peach", fg = "#fab387", bold = true, dim = false },
    { token = "$group_badge_mauve", fg = "#cba6f7", bold = true, dim = false },
    { token = "$group_badge_yellow", fg = "#f9e2af", bold = true, dim = false },
    { token = "$group_badge_teal", fg = "#94e2d5", bold = true, dim = false },
    { token = "$group_badge_red", fg = "#f38ba8", bold = true, dim = false },
    { token = "$ungrouped_badge", fg = "#6c7086", dim = true },
    "workspace",
  ],
  [
    { token = "branch", dim = true },
    "git_status",
  ],
]
"""


class ConfigError(RuntimeError):
    pass


def config_path() -> Path:
    configured = os.environ.get("HERDR_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "herdr" / "config.toml"


def is_configured(text: str) -> bool:
    return all(f'${token}' in text for token in TOKENS) and "$ungrouped_badge" in text


def apply_config(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from error
    if is_configured(text):
        return False
    if SPACES_HEADER in text:
        raise ConfigError(
            f"{path} already defines {SPACES_HEADER}; invoke workspace-groups.sidebar-config "
            "and merge the printed badge tokens into that table without replacing your layout"
        )

    updated = text.rstrip()
    if updated:
        updated += "\n\n"
    updated += SIDEBAR_CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            handle.write(updated)
            temporary = Path(handle.name)
        temporary.replace(path)
    except OSError as error:
        raise ConfigError(f"cannot write {path}: {error}") from error
    return True


def refresh_herdr() -> None:
    herdr = os.environ.get("HERDR_BIN_PATH", "herdr")
    reload_result = subprocess.run(
        [herdr, "server", "reload-config"], check=False, text=True, capture_output=True
    )
    if reload_result.returncode != 0:
        detail = reload_result.stderr.strip() or reload_result.stdout.strip()
        print(f"Config saved; reload Herdr later with `herdr server reload-config`: {detail}")
        return
    sync_result = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "group_ws.py"), "sync", "--quiet"],
        check=False,
        text=True,
        capture_output=True,
    )
    if sync_result.returncode != 0:
        detail = sync_result.stderr.strip() or sync_result.stdout.strip()
        print(f"Config reloaded; run the plugin sync action after Herdr starts: {detail}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure Herdr sidebar group badges")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true", help="append a safe sidebar table")
    mode.add_argument("--print", dest="print_config", action="store_true", help="print TOML")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.print_config:
        print(SIDEBAR_CONFIG, end="")
        return 0
    path = config_path()
    try:
        changed = apply_config(path)
    except ConfigError as error:
        print(f"workspace-groups: {error}", file=sys.stderr)
        return 1
    if changed:
        print(f"Added workspace group badges to {path}")
    else:
        print(f"Workspace group badges are already configured in {path}")
    refresh_herdr()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
