from pathlib import Path
from copy import deepcopy
import tempfile
import tomllib

import unittest
from unittest.mock import Mock, patch

from configure_sidebar import ConfigError, apply_config

from group_ws import (
    GroupManager,
    StateStore,
    drop_linked_worktree_assignments,
    group_edges,
    move_workspace_in_section,
    normalize_session,
    ordered_workspaces,
    sidebar_group_tokens,
)


class OrderingTests(unittest.TestCase):
    def test_groups_are_contiguous_and_keep_relative_workspace_order(self):
        workspaces = [
            {"workspace_id": "w1"},
            {"workspace_id": "w2"},
            {"workspace_id": "w3"},
            {"workspace_id": "w4"},
        ]
        assignments = {"w1": "personal", "w3": "personal", "w4": "work"}

        ordered = ordered_workspaces(workspaces, ["work", "personal"], assignments)

        self.assertEqual([item["workspace_id"] for item in ordered], ["w4", "w1", "w3", "w2"])

    def test_normalize_removes_closed_and_unknown_assignments(self):
        session = {
            "groups": ["work", "work", "", " personal "],
            "assignments": {"w1": "work", "w2": "gone", "w3": "personal"},
            "colors": {"work": "green", "gone": "red", "personal": "invalid"},
        }

        changed = normalize_session(session, {"w1", "w2"})

        self.assertTrue(changed)
        self.assertEqual(session["groups"], ["work", "personal"])
        self.assertEqual(session["assignments"], {"w1": "work"})
        self.assertEqual(session["colors"], {"work": "green", "personal": "blue"})

    def test_group_edges_mark_boundaries(self):
        workspaces = [
            {"workspace_id": "w1"},
            {"workspace_id": "w2"},
            {"workspace_id": "w3"},
            {"workspace_id": "w4"},
        ]
        assignments = {"w1": "work", "w2": "work", "w3": "work", "w4": "solo"}

        self.assertEqual(
            group_edges(workspaces, assignments),
            {"w1": "▾ work", "w2": "├─", "w3": "└─", "w4": "◆ solo"},
        )

    def test_sidebar_tokens_create_group_and_ungrouped_headers(self):
        workspaces = [
            {"workspace_id": "w1"},
            {"workspace_id": "w2"},
            {"workspace_id": "w3"},
            {"workspace_id": "w4"},
        ]
        assignments = {"w1": "work", "w2": "work"}

        tokens = sidebar_group_tokens(workspaces, ["work"], assignments, {"work": "green"})

        self.assertEqual(tokens["w1"]["group_header"], "━━ work · 2 ━━━━━━━━━━")
        self.assertEqual(tokens["w1"]["group_tree"], "├─")
        self.assertEqual(tokens["w1"]["group_badge"], "[work]")
        self.assertEqual(tokens["w1"]["group_badge_green"], "[work]")
        self.assertEqual(tokens["w2"]["group_badge_green"], "[work]")
        self.assertIsNone(tokens["w1"]["group_badge_blue"])
        self.assertEqual(tokens["w3"]["ungrouped_badge"], "[—]")
        self.assertEqual(tokens["w4"]["ungrouped_badge"], "[—]")
        self.assertIsNone(tokens["w2"]["group_header"])
        self.assertEqual(tokens["w2"]["group_tree"], "└─")
        self.assertEqual(tokens["w3"]["ungrouped_header"], "━━ Ungrouped · 2 ━━━━━")
        self.assertEqual(tokens["w3"]["ungrouped_tree"], "├─")
        self.assertIsNone(tokens["w4"]["ungrouped_header"])
        self.assertEqual(tokens["w4"]["ungrouped_tree"], "└─")

    def test_workspace_order_moves_only_inside_selected_section(self):
        workspaces = [
            {"workspace_id": "w1"},
            {"workspace_id": "w2"},
            {"workspace_id": "w3"},
            {"workspace_id": "w4"},
            {"workspace_id": "w5"},
        ]
        assignments = {"w1": "work", "w2": "work", "w3": "personal"}

        reordered, changed = move_workspace_in_section(workspaces, assignments, "w2", -1)

        self.assertTrue(changed)
        self.assertEqual(
            [workspace["workspace_id"] for workspace in reordered],
            ["w2", "w1", "w3", "w4", "w5"],
        )
        unchanged, changed = move_workspace_in_section(reordered, assignments, "w2", -1)
        self.assertFalse(changed)
        self.assertIs(unchanged, reordered)

        reordered, changed = move_workspace_in_section(reordered, assignments, "w5", -1)
        self.assertTrue(changed)
        self.assertEqual(
            [workspace["workspace_id"] for workspace in reordered],
            ["w2", "w1", "w3", "w5", "w4"],
        )

    def test_worktree_inherits_parent_group_and_stays_with_parent(self):
        parent = {
            "workspace_id": "w1",
            "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": False},
        }
        child = {
            "workspace_id": "wc",
            "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True},
        }
        other = {"workspace_id": "w2"}
        workspaces = [parent, other, child]
        assignments = {"w1": "work", "w2": "work", "wc": "forbidden"}

        ordered = ordered_workspaces(workspaces, ["work"], assignments)
        tokens = sidebar_group_tokens(ordered, ["work"], assignments, {"work": "peach"})

        self.assertEqual(
            [workspace["workspace_id"] for workspace in ordered],
            ["w1", "wc", "w2"],
        )
        self.assertEqual(tokens["wc"]["group"], "work")
        self.assertEqual(tokens["wc"]["group_color"], "peach")
        self.assertIsNone(tokens["wc"]["group_badge_peach"])
        self.assertIsNone(tokens["wc"]["ungrouped_badge"])

        reordered, changed = move_workspace_in_section(ordered, assignments, "w2", -1)
        self.assertTrue(changed)
        self.assertEqual(
            [workspace["workspace_id"] for workspace in reordered],
            ["w2", "w1", "wc"],
        )

    def test_linked_worktree_assignments_are_removed(self):
        workspaces = [
            {
                "workspace_id": "w1",
                "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": False},
            },
            {
                "workspace_id": "wc",
                "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True},
            },
        ]
        session = {
            "groups": ["work", "forbidden"],
            "assignments": {"w1": "work", "wc": "forbidden"},
            "colors": {"work": "green", "forbidden": "red"},
        }

        changed = drop_linked_worktree_assignments(session, workspaces)

        self.assertTrue(changed)
        self.assertEqual(session["groups"], ["work"])
        self.assertEqual(session["assignments"], {"w1": "work"})
        self.assertEqual(session["colors"], {"work": "green"})


    def test_manager_navigation_skips_inherited_worktrees(self):
        manager = GroupManager.__new__(GroupManager)
        manager.workspaces = [
            {
                "workspace_id": "w1",
                "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": False},
            },
            {
                "workspace_id": "wc",
                "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True},
            },
            {"workspace_id": "w2"},
        ]
        manager.selected = 0

        manager.move_selection(1)
        self.assertEqual(manager.selected, 2)
        manager.move_selection(-1)
        self.assertEqual(manager.selected, 0)

        manager.selected = 1
        self.assertEqual(manager.selected_id(), "w1")

class GroupRenameTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict("os.environ", {"HERDR_PLUGIN_STATE_DIR": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = StateStore("/tmp/herdr-rename-test.sock")
        self.state = {
            "version": 1,
            "sessions": {
                self.store.session_key: {
                    "groups": ["Personal", "Work", "Later"],
                    "assignments": {"p": "Personal", "w1": "Work", "w2": "Work", "l": "Later"},
                    "colors": {"Personal": "blue", "Work": "green", "Later": "red"},
                },
                "other-session": {
                    "groups": ["Work"],
                    "assignments": {"other": "Work"},
                    "colors": {"Work": "peach"},
                },
            },
        }
        self.store.save(self.state)
        self.workspaces = [
            {"workspace_id": "p"},
            {"workspace_id": "w1", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": False}},
            {"workspace_id": "wc", "worktree": {"repo_key": "/repo/.git", "is_linked_worktree": True}},
            {"workspace_id": "w2"},
            {"workspace_id": "l"},
            {"workspace_id": "ungrouped"},
        ]
        self.client = Mock()
        self.client.list_workspaces.return_value = self.workspaces
        self.manager = GroupManager(self.client, self.store)
        self.manager.selected = 1

    def test_rename_persists_members_order_color_and_session_isolation(self):
        self.manager.rename_group("  Client   Work  ")

        saved = self.store.load()
        session = self.store.session(saved)
        self.assertEqual(session["groups"], ["Personal", "Client Work", "Later"])
        self.assertEqual(
            session["assignments"],
            {"p": "Personal", "w1": "Client Work", "w2": "Client Work", "l": "Later"},
        )
        self.assertEqual(session["colors"], {"Personal": "blue", "Client Work": "green", "Later": "red"})
        self.assertEqual(saved["sessions"]["other-session"], self.state["sessions"]["other-session"])
        self.assertEqual(self.manager.selected_id(), "w1")
        self.assertEqual(self.manager.workspaces, self.workspaces)
        self.client.move_all.assert_not_called()
        metadata = {call.args[0]: call.args[1] for call in self.client.report_group.call_args_list}
        for workspace_id in ("w1", "w2", "wc"):
            self.assertEqual(metadata[workspace_id]["group"], "Client Work")
            self.assertEqual(metadata[workspace_id]["group_color"], "green")
        self.assertEqual(metadata["w1"]["group_badge_green"], "[Client Work]")
        self.assertEqual(metadata["w2"]["group_badge_green"], "[Client Work]")
        reopened = GroupManager(self.client, self.store)
        reopened.selected = 1
        self.assertEqual(reopened.selected_group(), "Client Work")

    def test_invalid_or_unchanged_names_leave_state_and_sidebar_untouched(self):
        cases = [
            ("", "cannot be empty"),
            (" \t\n ", "cannot be empty"),
            ("x" * 33, "32 characters"),
            (" Personal ", "already exists"),
            (" Work ", "unchanged"),
        ]
        original = deepcopy(self.manager.state)
        for name, message in cases:
            with self.subTest(name=name):
                self.manager.rename_group(name)
                self.assertIn(message, self.manager.message)
                self.assertEqual(self.manager.state, original)
                self.assertEqual(self.store.load(), original)
        self.client.report_group.assert_not_called()

    def test_ungrouped_workspace_cannot_rename_a_group(self):
        self.manager.selected = 5
        self.manager.rename_group("New")
        self.assertIn("not grouped", self.manager.message)
        self.assertEqual(self.store.load(), self.state)
        self.client.report_group.assert_not_called()

    def test_rename_accepts_unicode_name_at_length_limit(self):
        name = "项" * 32
        self.manager.rename_group(name)
        self.assertEqual(self.store.session(self.store.load())["assignments"]["w2"], name)

    def test_cancelled_rename_leaves_state_untouched(self):
        with patch.object(self.manager, "text_prompt", return_value=None) as prompt:
            self.manager.request_group_rename(Mock())
        self.assertEqual(prompt.call_args.kwargs, {"initial": "Work", "submit": "rename"})
        self.assertEqual(self.store.load(), self.state)
        self.client.report_group.assert_not_called()

    def test_rename_button_updates_the_entire_group(self):
        with patch.object(self.manager, "text_prompt", return_value="Projects"):
            self.assertFalse(self.manager.handle_action(Mock(), "rename"))
        self.assertEqual(self.store.session(self.store.load())["assignments"]["w2"], "Projects")

    def test_rename_prompt_prefills_name_and_supports_editing_and_cancel(self):
        for keys, expected in [(["\x7f", "!", "\n"], "Wor!"), (["\x1b"], None)]:
            with self.subTest(keys=keys):
                screen = Mock()
                screen.getmaxyx.return_value = (30, 100)
                screen.get_wch.side_effect = keys
                with patch.object(self.manager, "draw"), patch("group_ws.curses.curs_set"), patch(
                    "group_ws.curses.color_pair", return_value=0
                ):
                    result = self.manager.text_prompt(screen, "Rename group", initial="Work", submit="rename")
                self.assertEqual(result, expected)
                rendered = [str(call.args[2]) for call in screen.addnstr.call_args_list]
                self.assertTrue(any("Work" in text for text in rendered))
                self.assertTrue(any("Enter rename" in text for text in rendered))


class SidebarConfigurationTests(unittest.TestCase):
    def test_apply_adds_parseable_sidebar_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[theme]\nname = "catppuccin"\n', encoding="utf-8")

            self.assertTrue(apply_config(path))
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            first_row = parsed["ui"]["sidebar"]["spaces"]["rows"][0]
            tokens = {
                item["token"]
                for item in first_row
                if isinstance(item, dict) and "token" in item
            }
            self.assertIn("$group_badge_blue", tokens)
            self.assertIn("$group_badge_red", tokens)
            self.assertIn("$ungrouped_badge", tokens)
            self.assertFalse(apply_config(path))

    def test_apply_preserves_custom_sidebar_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            original = '[ui.sidebar.spaces]\nrows = [["workspace"]]\n'
            path.write_text(original, encoding="utf-8")

            with self.assertRaises(ConfigError):
                apply_config(path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
