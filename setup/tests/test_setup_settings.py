"""settings.json: the hook merge, the status-line chain and the http allow-list.

Every case runs against a temporary HOME. Nothing here reaches the developer's own
~/.claude, and the install path is driven through the Environment seam so no launchctl,
no security and no socket is ever touched.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from _harness import TempHome, setup


class SplitHookMatcher(unittest.TestCase):
    """Ownership is decided per ENTRY, never per matcher group."""

    def _ours(self, url_tail="/v1/hook"):
        return {"type": "command", "command": f"curl -X POST http://127.0.0.1:9999{url_tail}"}

    def test_an_unshared_matcher_is_handed_back_by_identity(self):
        matcher = {"hooks": [self._ours()]}
        split = setup.split_hook_matcher(matcher)
        self.assertIs(split.ours, matcher)
        self.assertIsNone(split.foreign)
        self.assertEqual(split.our_count, 1)

    def test_a_foreign_matcher_is_handed_back_by_identity(self):
        matcher = {"matcher": "Bash", "hooks": [{"type": "command", "command": "notify-me"}]}
        split = setup.split_hook_matcher(matcher)
        self.assertIsNone(split.ours)
        self.assertIs(split.foreign, matcher)

    def test_a_shared_matcher_splits_and_both_halves_keep_the_other_keys(self):
        matcher = {
            "matcher": "Bash",
            "hooks": [self._ours(), {"type": "command", "command": "notify-me"}],
        }
        split = setup.split_hook_matcher(matcher)
        self.assertEqual(split.ours["matcher"], "Bash")
        self.assertEqual(split.foreign["matcher"], "Bash")
        self.assertEqual(split.foreign["hooks"], [{"type": "command", "command": "notify-me"}])
        self.assertEqual((split.our_count, split.foreign_count), (1, 1))

    def test_an_http_entry_is_matched_on_its_url(self):
        matcher = {"hooks": [{"type": "http", "url": "http://127.0.0.1:9999/v1/hook/stop"}]}
        self.assertEqual(setup.split_hook_matcher(matcher).our_count, 1)

    def test_a_matcher_with_no_hooks_list_survives_as_foreign(self):
        matcher = {"matcher": "Bash"}
        split = setup.split_hook_matcher(matcher)
        self.assertIs(split.foreign, matcher)
        self.assertEqual(split.our_count, 0)


class MergeHookFragment(unittest.TestCase):
    FRAGMENT = {
        "SessionStart": [
            {"hooks": [{"type": "command", "command": f"curl {setup.HOOK_MARKER} || exit 0"}]}
        ],
        "Stop": [{"hooks": [{"type": "http", "url": f"http://{setup.HOOK_MARKER}/stop"}]}],
    }

    def test_a_second_merge_is_a_fixed_point(self):
        once, _ = setup.merge_hook_fragment({}, self.FRAGMENT)
        twice, _ = setup.merge_hook_fragment(once, self.FRAGMENT)
        self.assertEqual(once, twice)

    def test_our_stale_entry_from_an_older_port_is_replaced_not_duplicated(self):
        stale = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"curl {setup.HOOK_MARKER}/old"}]}
                ]
            }
        }
        merged, _ = setup.merge_hook_fragment(stale, self.FRAGMENT)
        entries = [e for m in merged["hooks"]["SessionStart"] for e in m["hooks"]]
        self.assertEqual(entries, self.FRAGMENT["SessionStart"][0]["hooks"])

    def test_a_foreign_matcher_group_and_a_shared_one_both_keep_their_entries(self):
        prior = {
            "hooks": {
                "SessionStart": [
                    {"matcher": "own", "hooks": [{"type": "command", "command": "theirs-own"}]},
                    {
                        "matcher": "shared",
                        "hooks": [
                            {"type": "command", "command": f"curl {setup.HOOK_MARKER}/old"},
                            {"type": "command", "command": "theirs-shared"},
                        ],
                    },
                ]
            }
        }
        merged, _ = setup.merge_hook_fragment(prior, self.FRAGMENT)
        commands = [e.get("command") for m in merged["hooks"]["SessionStart"] for e in m["hooks"]]
        self.assertIn("theirs-own", commands)
        self.assertIn("theirs-shared", commands)
        self.assertEqual(sum(1 for c in commands if setup.HOOK_MARKER in (c or "")), 1)

    def test_unrelated_events_and_top_level_keys_are_untouched(self):
        prior = {"model": "opus", "hooks": {"PreToolUse": [{"hooks": [{"command": "x"}]}]}}
        merged, _ = setup.merge_hook_fragment(prior, self.FRAGMENT)
        self.assertEqual(merged["model"], "opus")
        self.assertEqual(merged["hooks"]["PreToolUse"], prior["hooks"]["PreToolUse"])

    def test_the_input_document_is_not_mutated(self):
        prior = {"hooks": {}}
        setup.merge_hook_fragment(prior, self.FRAGMENT)
        self.assertEqual(prior, {"hooks": {}})


class RemoveHookEntries(unittest.TestCase):
    def test_only_our_entries_go_and_an_emptied_group_and_event_go_with_them(self):
        settings = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": f"curl {setup.HOOK_MARKER}"}]}
                ],
                "Notification": [
                    {
                        "hooks": [
                            {"type": "command", "command": f"curl {setup.HOOK_MARKER}"},
                            {"type": "command", "command": "theirs"},
                        ]
                    }
                ],
            }
        }
        pruned, removed = setup.remove_hook_entries(settings)
        self.assertEqual(removed, 2)
        self.assertNotIn("SessionStart", pruned["hooks"])
        self.assertEqual(
            pruned["hooks"]["Notification"],
            [{"hooks": [{"type": "command", "command": "theirs"}]}],
        )

    def test_an_emptied_hooks_object_is_dropped_entirely(self):
        settings = {
            "model": "opus",
            "hooks": {
                "Stop": [{"hooks": [{"type": "http", "url": f"http://{setup.HOOK_MARKER}/stop"}]}]
            },
        }
        pruned, removed = setup.remove_hook_entries(settings)
        self.assertEqual(removed, 1)
        self.assertEqual(pruned, {"model": "opus"})


class MalformedHookEvents(TempHome):
    """A hooks value that is not a list of matcher groups is somebody else's shape.

    Iterating a string explodes it into characters and a dict into its keys, and the
    event comes back rewritten as nonsense. Left alone and reported instead.
    """

    FRAGMENT = {"SessionStart": [{"hooks": [{"type": "command", "command": f"curl {setup.HOOK_MARKER}"}]}]}

    def test_a_string_or_a_dict_event_survives_the_merge_byte_identical(self):
        for value in ("nope", {"hooks": "also nope"}, 7):
            with self.subTest(value=value):
                prior = {"hooks": {"SessionStart": value, "Stop": []}}
                merged, _count = setup.merge_hook_fragment(prior, self.FRAGMENT)
                self.assertEqual(merged["hooks"]["SessionStart"], value)

    def test_the_removal_leaves_it_alone_too(self):
        prior = {"hooks": {"SessionStart": "nope"}}
        pruned, removed = setup.remove_hook_entries(prior)
        self.assertEqual(pruned, prior)
        self.assertEqual(removed, 0)

    def test_the_count_ignores_it_rather_than_counting_characters(self):
        self.assertEqual(setup.hook_events({"hooks": {"SessionStart": "curl 127.0.0.1:9999/v1/hook"}}), [])

    def test_the_install_says_which_event_it_refused_to_touch(self):
        self.write_settings({"hooks": {"SessionStart": "nope"}})
        self.assertEqual(setup.main(["install", "--yes"], env=self.env()), 0)
        doc = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["hooks"]["SessionStart"], "nope")
        self.assertIn("SessionStart", self.output)
        self.assertIn("not a list", self.output)


class EmptyHooksObject(unittest.TestCase):
    def test_a_pre_existing_empty_hooks_object_is_not_ours_to_delete(self):
        # A run that removes nothing of ours must write nothing at all.
        settings = {"model": "opus", "hooks": {}}
        pruned, removed = setup.remove_hook_entries(settings)
        self.assertEqual(pruned, settings)
        self.assertEqual(removed, 0)

    def test_a_hooks_object_we_emptied_still_goes(self):
        settings = {
            "hooks": {"Stop": [{"hooks": [{"type": "http", "url": f"http://{setup.HOOK_MARKER}/stop"}]}]}
        }
        pruned, removed = setup.remove_hook_entries(settings)
        self.assertEqual(removed, 1)
        self.assertEqual(pruned, {})


class AllowedHttpHookUrls(unittest.TestCase):
    """The allow-list is a switch: creating it blocks every http hook we did not name."""

    def test_an_absent_key_stays_absent(self):
        plan = setup.allowed_hook_urls_plan({"model": "opus"})
        self.assertEqual(plan.action, "absent")
        self.assertNotIn("allowedHttpHookUrls", plan.settings)

    def test_a_present_key_gains_both_patterns_and_keeps_the_operators_own(self):
        plan = setup.allowed_hook_urls_plan({"allowedHttpHookUrls": ["http://example/*"]})
        self.assertEqual(plan.action, "added")
        self.assertEqual(
            plan.settings["allowedHttpHookUrls"],
            ["http://example/*", *setup.ALLOWED_HOOK_PATTERNS],
        )

    def test_an_empty_list_is_still_a_set_key_and_gains_both_patterns(self):
        plan = setup.allowed_hook_urls_plan({"allowedHttpHookUrls": []})
        self.assertEqual(plan.settings["allowedHttpHookUrls"], list(setup.ALLOWED_HOOK_PATTERNS))

    def test_a_key_that_already_admits_us_is_left_alone(self):
        prior = {"allowedHttpHookUrls": list(setup.ALLOWED_HOOK_PATTERNS)}
        plan = setup.allowed_hook_urls_plan(prior)
        self.assertEqual(plan.action, "present")
        self.assertEqual(plan.settings, prior)

    def test_presence_tests_the_type_not_the_key(self):
        # An explicit null is NOT a set allow-list: writing the two patterns over it
        # would switch the allowlist on and block every other http hook there is.
        plan = setup.allowed_hook_urls_plan({"allowedHttpHookUrls": None})
        self.assertEqual(plan.action, "absent")
        self.assertIsNone(plan.settings["allowedHttpHookUrls"])

    def test_a_non_list_value_is_left_exactly_as_it_is(self):
        # A string would otherwise be exploded into characters by the membership test.
        for value in ("http://example/*", {"a": 1}, 7):
            with self.subTest(value=value):
                plan = setup.allowed_hook_urls_plan({"allowedHttpHookUrls": value})
                self.assertEqual(plan.action, "not-a-list")
                self.assertIn("not a list", plan.reason)
                self.assertEqual(plan.settings["allowedHttpHookUrls"], value)


class AllowedHttpHookUrlsRemoval(unittest.TestCase):
    def test_ours_go_when_something_of_the_operators_remains(self):
        plan = setup.allowed_hook_urls_removal_plan(
            {"allowedHttpHookUrls": ["http://example/*", *setup.ALLOWED_HOOK_PATTERNS]}
        )
        self.assertEqual(plan.action, "removed")
        self.assertEqual(plan.settings["allowedHttpHookUrls"], ["http://example/*"])

    def test_the_whole_key_goes_rather_than_be_left_empty(self):
        # An empty list is not "no allow-list": it admits nothing, so every http hook
        # the operator has - not just ours - would stop being called.
        plan = setup.allowed_hook_urls_removal_plan(
            {"allowedHttpHookUrls": list(setup.ALLOWED_HOOK_PATTERNS)}
        )
        self.assertEqual(plan.action, "key-removed")
        self.assertNotIn("allowedHttpHookUrls", plan.settings)

    def test_an_absent_key_is_a_no_op(self):
        self.assertEqual(setup.allowed_hook_urls_removal_plan({}).action, "absent")

    def test_a_null_is_absent_and_a_non_list_is_left_alone(self):
        self.assertEqual(setup.allowed_hook_urls_removal_plan({"allowedHttpHookUrls": None}).action, "absent")
        for value in ("http://example/*", {"a": 1}):
            with self.subTest(value=value):
                plan = setup.allowed_hook_urls_removal_plan({"allowedHttpHookUrls": value})
                self.assertEqual(plan.action, "not-a-list")
                self.assertEqual(plan.settings["allowedHttpHookUrls"], value)


class StatusLine(unittest.TestCase):
    def test_ours_is_recognised_by_the_script_name(self):
        self.assertTrue(setup.statusline_is_ours("/usr/bin/python3 /r/hooks/sidecrab_statusline.py"))
        self.assertFalse(setup.statusline_is_ours("starship prompt"))
        self.assertFalse(setup.statusline_is_ours(""))

    def test_the_command_quotes_only_what_needs_it(self):
        self.assertEqual(
            setup.statusline_command("/opt/py", "/repo/hooks/sidecrab_statusline.py"),
            "/opt/py /repo/hooks/sidecrab_statusline.py",
        )
        self.assertEqual(
            setup.statusline_command("/opt/py", "/my repo/hooks/sidecrab_statusline.py"),
            "/opt/py '/my repo/hooks/sidecrab_statusline.py'",
        )

    def test_restore_never_writes_over_a_line_that_is_not_ours(self):
        rows = [
            # current command, ours?, saved present, saved, expected action
            ("starship", False, True, {"command": "old"}, "preserve-foreign"),
            ("ours", True, True, {"command": "old"}, "restore"),
            ("ours", True, True, None, "remove"),
            ("ours", True, False, None, "remove"),
            ("", False, True, {"command": "old"}, "restore"),
            ("", False, True, None, "none"),
            ("", False, False, None, "none"),
        ]
        for current, ours, present, saved, expected in rows:
            with self.subTest(current=current, saved=saved, present=present):
                decision = setup.statusline_restore_decision(current, ours, present, saved)
                self.assertEqual(decision.action, expected)


class InstallWritesSettings(TempHome):
    """The whole settings.json leg of an install, against a temporary HOME."""

    def install(self, *args):
        return setup.main(["install", "--yes", *args], env=self.env())

    def test_the_seven_events_land_and_the_file_is_backed_up_first(self):
        path = self.write_settings({"model": "opus"})
        original = path.read_text(encoding="utf-8")
        self.assertEqual(self.install(), 0)

        doc = self.read_settings()
        self.assertEqual(len(setup.hook_events(doc)), 7)
        self.assertEqual(doc["model"], "opus")
        self.assertEqual(self.backups(path), ["settings.json.sidecrab-bak-20260904-112233"])
        self.assertEqual(
            (path.parent / "settings.json.sidecrab-bak-20260904-112233").read_text(encoding="utf-8"),
            original,
        )

    def test_a_second_run_changes_nothing_and_backs_up_nothing(self):
        path = self.write_settings({"model": "opus"})
        self.install()
        after_first = path.read_text(encoding="utf-8")
        self.clock = self.clock.replace(hour=12)
        self.install()

        self.assertEqual(path.read_text(encoding="utf-8"), after_first)
        self.assertEqual(self.backups(path), ["settings.json.sidecrab-bak-20260904-112233"])

    def test_a_first_ever_settings_file_is_created_with_no_backup(self):
        path = self.home / ".claude" / "settings.json"
        self.assertEqual(self.install(), 0)
        self.assertEqual(len(setup.hook_events(self.read_settings())), 7)
        self.assertEqual(self.backups(path), [])

    def test_a_hand_merged_foreign_hook_survives_the_install(self):
        self.write_settings(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {"type": "command", "command": "curl 127.0.0.1:9999/v1/hook/old"},
                                {"type": "command", "command": "mine --please-keep"},
                            ]
                        }
                    ]
                }
            }
        )
        self.install()
        commands = [
            e.get("command")
            for m in self.read_settings()["hooks"]["SessionStart"]
            for e in m["hooks"]
        ]
        self.assertIn("mine --please-keep", commands)
        self.assertNotIn("curl 127.0.0.1:9999/v1/hook/old", commands)

    def test_the_prior_status_line_is_saved_once_and_its_padding_carried(self):
        self.write_settings({"statusLine": {"type": "command", "command": "starship", "padding": 1}})
        self.install()

        doc = self.read_settings()
        self.assertTrue(setup.statusline_is_ours(doc["statusLine"]["command"]))
        self.assertEqual(doc["statusLine"]["padding"], 1)
        chain = json.loads((self.home / ".sidecrab" / "statusline-chain.json").read_text("utf-8"))
        self.assertEqual(chain["statusLine"]["command"], "starship")

        # A second install must not capture OUR command as the prior: that builds a loop.
        self.install()
        chain = json.loads((self.home / ".sidecrab" / "statusline-chain.json").read_text("utf-8"))
        self.assertEqual(chain["statusLine"]["command"], "starship")

    def test_an_empty_slot_is_recorded_as_a_null_prior(self):
        self.write_settings({})
        self.install()
        chain = json.loads((self.home / ".sidecrab" / "statusline-chain.json").read_text("utf-8"))
        self.assertIsNone(chain["statusLine"])

    def test_an_absent_allow_list_is_never_created(self):
        self.write_settings({"model": "opus"})
        self.install()
        self.assertNotIn("allowedHttpHookUrls", self.read_settings())

    def test_a_present_allow_list_gains_both_patterns(self):
        self.write_settings({"allowedHttpHookUrls": ["http://example/*"]})
        self.install()
        self.assertEqual(
            self.read_settings()["allowedHttpHookUrls"],
            ["http://example/*", *setup.ALLOWED_HOOK_PATTERNS],
        )

    def test_a_malformed_settings_file_aborts_the_whole_install(self):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")

        code = self.install()

        self.assertEqual(code, 1)
        self.assertIn(str(path), self.output)
        # Nothing written anywhere, and not one launchctl call.
        self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")
        self.assertEqual(self.backups(path), [])
        self.assertFalse((self.home / ".sidecrab").exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        self.assertEqual(self.runner.calls, [])

    def test_a_settings_file_that_is_not_an_object_aborts_too(self):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(self.install(), 1)
        self.assertEqual(self.runner.calls, [])


class PanelApprovals(TempHome):
    """config.json's panelApprovals.enabled - a security posture, never a silent default."""

    def install(self, *args, **kwargs):
        return setup.main(["install", *args], env=self.env(**kwargs))

    def config(self):
        return json.loads((self.home / ".sidecrab" / "config.json").read_text(encoding="utf-8"))

    def write_config(self, doc):
        path = self.home / ".sidecrab" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def test_an_absent_key_is_written_false(self):
        self.install("--yes")
        self.assertIs(self.config()["panelApprovals"]["enabled"], False)

    def test_an_operators_true_is_never_reverted_by_a_plain_re_run(self):
        path = self.write_config({"panelApprovals": {"enabled": True}, "quiet": {"from": "22:00"}})
        before = path.read_text(encoding="utf-8")
        self.install("--yes")
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.backups(path), [])

    def test_with_approvals_turns_it_on_and_states_the_guarantees(self):
        self.install("--yes", "--with-approvals")
        self.assertIs(self.config()["panelApprovals"]["enabled"], True)
        for guarantee in ("55", "never auto-allow", "pairing code"):
            self.assertIn(guarantee, self.output.lower().replace("auto-allows", "auto-allow"))

    def test_no_approvals_turns_it_off_over_an_operators_true(self):
        self.write_config({"panelApprovals": {"enabled": True}})
        self.install("--yes", "--no-approvals")
        self.assertIs(self.config()["panelApprovals"]["enabled"], False)

    def test_every_other_key_survives_and_the_write_is_atomic(self):
        path = self.write_config({"quiet": {"from": "22:00"}, "toast": {"thresholdSec": 90}})
        self.install("--yes", "--with-approvals")
        doc = self.config()
        self.assertEqual(doc["quiet"], {"from": "22:00"})
        self.assertEqual(doc["toast"], {"thresholdSec": 90})
        self.assertEqual(self.backups(path), ["config.json.sidecrab-bak-20260904-112233"])
        self.assertEqual(list(path.parent.glob("*.sidecrab-tmp-*")), [])

    def test_a_tty_that_answers_nothing_leaves_approvals_off(self):
        self.install(ask=lambda prompt: "")
        self.assertIs(self.config()["panelApprovals"]["enabled"], False)

    def test_a_tty_that_answers_yes_turns_them_on(self):
        self.install(ask=lambda prompt: "y")
        self.assertIs(self.config()["panelApprovals"]["enabled"], True)


class AtomicWrite(TempHome):
    """The replace must not silently widen a file the operator narrowed."""

    def writer(self):
        return setup.Writer(lambda: self.clock)

    def test_the_targets_mode_survives_the_replace_and_the_backup_keeps_it(self):
        path = self.home / "secret.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        path.chmod(0o600)

        writer = self.writer()
        backup = writer.write_json(path, {"a": 2})

        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_a_first_ever_write_is_not_world_readable(self):
        path = self.home / "new.json"
        self.writer().write_json(path, {"a": 1})
        self.assertEqual(path.stat().st_mode & 0o777 & 0o077, 0)

    def test_the_bytes_are_flushed_and_fsynced_before_the_replace(self):
        seen = []
        real_fsync = os.fsync

        def fsync(fd):
            seen.append(fd)
            return real_fsync(fd)

        path = self.home / "durable.json"
        with mock.patch.object(os, "fsync", fsync):
            self.writer().write_json(path, {"a": 1})
        self.assertTrue(seen, "the temp file was replaced over the target without an fsync")

    def test_an_unwritable_directory_is_a_refusal_not_a_traceback(self):
        blocked = self.home / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        self.addCleanup(blocked.chmod, 0o700)

        with self.assertRaises(setup.SetupError) as caught:
            self.writer().write_json(blocked / "x.json", {"a": 1})
        self.assertIn(str(blocked / "x.json"), str(caught.exception))

    def test_the_command_layer_turns_that_into_an_exit_code(self):
        settings = self.home / ".claude"
        settings.mkdir(parents=True)
        (settings / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
        settings.chmod(0o500)
        self.addCleanup(settings.chmod, 0o700)

        self.assertEqual(setup.main(["install", "--yes"], env=self.env()), 1)
        self.assertIn("ERROR", self.output)


class Uninstall(TempHome):
    """Take back what SideCrab wrote, and nothing else."""

    def install(self, *args):
        return setup.main(["install", "--yes", *args], env=self.env())

    def uninstall(self, *args):
        return setup.main(["uninstall", "--yes", *args], env=self.env())

    def read_settings_doc(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def test_only_our_entries_go_and_a_hand_merged_hook_stays(self):
        self.write_settings(
            {
                "model": "opus",
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "mine --please-keep"}]}
                    ],
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "unrelated"}]}],
                },
            }
        )
        self.install()
        self.assertEqual(self.uninstall(), 0)

        doc = self.read_settings_doc()
        self.assertEqual(doc["model"], "opus")
        self.assertEqual(setup.hook_events(doc), [])
        commands = [e["command"] for m in doc["hooks"]["SessionStart"] for e in m["hooks"]]
        self.assertEqual(commands, ["mine --please-keep"])
        self.assertEqual(doc["hooks"]["PreToolUse"], [{"hooks": [{"type": "command", "command": "unrelated"}]}])

    def test_the_prior_status_line_comes_back_and_the_chain_file_goes(self):
        self.write_settings({"statusLine": {"type": "command", "command": "starship", "padding": 2}})
        self.install()
        self.uninstall()

        doc = self.read_settings_doc()
        self.assertEqual(doc["statusLine"], {"type": "command", "command": "starship", "padding": 2})
        self.assertFalse((self.home / ".sidecrab" / "statusline-chain.json").exists())

    def test_an_empty_slot_goes_back_to_empty(self):
        self.write_settings({"model": "opus"})
        self.install()
        self.uninstall()
        self.assertNotIn("statusLine", self.read_settings_doc())

    def test_a_status_line_installed_after_us_is_left_exactly_as_it_is(self):
        self.write_settings({"statusLine": {"type": "command", "command": "starship"}})
        self.install()
        doc = self.read_settings_doc()
        doc["statusLine"] = {"type": "command", "command": "powerline"}
        (self.home / ".claude" / "settings.json").write_text(json.dumps(doc, indent=2), "utf-8")

        self.uninstall()
        self.assertEqual(self.read_settings_doc()["statusLine"]["command"], "powerline")
        self.assertIn("not SideCrab", self.output)

    def test_our_allow_list_patterns_go_when_the_operators_own_remain(self):
        self.write_settings({"allowedHttpHookUrls": ["http://example/*"]})
        self.install()
        self.uninstall()
        self.assertEqual(self.read_settings_doc()["allowedHttpHookUrls"], ["http://example/*"])

    def test_an_allow_list_that_would_be_emptied_loses_the_key_instead(self):
        self.write_settings({"allowedHttpHookUrls": list(setup.ALLOWED_HOOK_PATTERNS)})
        self.uninstall()
        self.assertNotIn("allowedHttpHookUrls", self.read_settings_doc())
        self.assertIn("blocked", self.output.lower())

    def test_settings_are_backed_up_before_the_removal(self):
        path = self.write_settings({"model": "opus"})
        self.install()
        self.clock = self.clock.replace(hour=13)
        self.uninstall()
        self.assertIn("settings.json.sidecrab-bak-20260904-132233", self.backups(path))

    def test_a_run_with_nothing_of_ours_to_remove_writes_nothing(self):
        path = self.write_settings({"model": "opus"})
        before = path.read_text(encoding="utf-8")
        self.assertEqual(self.uninstall(), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.backups(path), [])

    def test_purge_removes_the_sidecrab_directory_and_a_plain_run_does_not(self):
        self.install()
        (self.home / ".sidecrab" / "panel-token").write_text("ABCDE23456", encoding="utf-8")
        self.uninstall()
        self.assertTrue((self.home / ".sidecrab" / "config.json").exists())

        self.uninstall("--purge")
        self.assertFalse((self.home / ".sidecrab").exists())


if __name__ == "__main__":
    unittest.main()
