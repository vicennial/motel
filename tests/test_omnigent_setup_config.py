"""The text-level edit of an Omnigent server config: what it writes, what it leaves alone, when it refuses."""
import unittest

from motel.omnigent_setup.config_edit import Want, plan_config, render_block, scalar

URL = "http://127.0.0.1:6767"
WANT = Want(URL)

try:
    import yaml
except ImportError:          # the core has no YAML library; the round-trip checks run where one is installed
    yaml = None


def apply(text, want=WANT):
    plan = plan_config(text, want)
    return plan, plan.text


class CreateAndAppendTest(unittest.TestCase):
    def test_a_missing_file_gets_just_the_block(self):
        plan, text = apply(None)
        self.assertEqual(plan.action, "create")
        self.assertIn("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n", text)
        self.assertNotIn("motel:\n", text.replace("provider: motel", ""))       # no sub-block without flags

    def test_the_motel_sub_block_holds_only_the_flags_that_were_given(self):
        self.assertEqual(render_block(Want(URL, server_type="cpx32")),
                         "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n    server_type: cpx32\n")
        self.assertEqual(render_block(Want(URL, ttl="90m")).splitlines()[-2:], ["  motel:", "    ttl: 90m"])
        both = render_block(Want(URL, server_type="cpx32", ttl="2h"))
        self.assertTrue(both.endswith("    server_type: cpx32\n    ttl: 2h\n"))

    def test_a_file_without_a_sandbox_key_gets_the_block_appended_and_nothing_else_changes(self):
        original = "# my config\ndatabase_uri: sqlite:///x.db   # keep\nadmins:\n  - alice@example.com\n"
        plan, text = apply(original)
        self.assertEqual(plan.action, "add")
        self.assertTrue(text.startswith(original))
        self.assertIn("sandbox:\n  provider: motel\n", text[len(original):])

    def test_a_file_without_a_final_newline_is_handled(self):
        original = "admins: [alice@example.com]"
        _plan, text = apply(original)
        self.assertTrue(text.startswith(original + "\n"))

    def test_an_empty_file_and_a_comment_only_file(self):
        for original in ("", "\n", "# nothing yet\n"):
            plan, text = apply(original)
            self.assertEqual(plan.action, "add", original)
            self.assertTrue(text.startswith(original), original)
            self.assertIn("provider: motel", text)

    def test_values_that_would_break_yaml_are_refused(self):
        for want in (Want("http://x y"), Want("ftp://host"), Want(URL + "#frag"), Want(URL, server_type="CPX 32"),
                     Want(URL, ttl="2 hours"), Want(URL, ttl="0h"), Want(URL, server_type="a\nb")):
            with self.assertRaises(ValueError, msg=want):
                plan_config("", want)


class SingleProviderShapeTest(unittest.TestCase):
    MOTEL = "database_uri: x   # keep\nsandbox:\n  provider: motel   # why\n  server_url: http://127.0.0.1:6767\nother: 1\n"

    def test_a_matching_block_is_left_alone(self):
        plan, _ = apply(self.MOTEL)
        self.assertEqual((plan.action, plan.present, plan.text), ("unchanged", True, None))

    def test_a_block_with_a_different_server_url_is_kept_unless_the_flag_was_given(self):
        text = self.MOTEL.replace("6767", "7000")
        self.assertEqual(plan_config(text, WANT).action, "unchanged")
        plan = plan_config(text, Want(URL, server_url_explicit=True))
        self.assertEqual(plan.action, "update")
        self.assertIn("  server_url: http://127.0.0.1:6767\nother: 1\n", plan.text)
        self.assertIn("provider: motel   # why", plan.text)

    def test_a_missing_server_url_is_added_after_the_provider_line(self):
        text = "sandbox:\n  provider: motel\n  reaper:\n    enabled: true\n"
        plan, out = apply(text)
        self.assertEqual(plan.action, "update")
        self.assertEqual(out, "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  reaper:\n    enabled: true\n")

    def test_flags_add_a_motel_sub_block_at_the_end_of_the_block(self):
        plan, out = apply(self.MOTEL, Want(URL, server_type="cpx32", ttl="2h"))
        self.assertEqual(plan.action, "update")
        self.assertIn("  server_url: http://127.0.0.1:6767\n  motel:\n    server_type: cpx32\n    ttl: 2h\nother: 1\n", out)

    def test_flags_update_an_existing_sub_block_in_place_and_keep_its_other_keys(self):
        text = ("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n    # lease\n"
                "    ttl: 1h   # short\n    disk_gb: 20\n")
        plan, out = apply(text, Want(URL, server_type="cpx32", ttl="2h"))
        self.assertEqual(plan.action, "update")
        self.assertIn("    ttl: 2h   # short\n", out)
        self.assertIn("    # lease\n", out)
        self.assertIn("    disk_gb: 20\n", out)
        self.assertIn("    server_type: cpx32\n", out)

    def test_flags_that_already_match_change_nothing(self):
        text = "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n    ttl: '2h'\n    server_type: cpx32\n"
        self.assertEqual(plan_config(text, Want(URL, server_type="cpx32", ttl="2h")).action, "unchanged")

    def test_a_sandbox_block_without_a_provider_gets_one(self):
        text = "sandbox:\n  server_url: http://127.0.0.1:6767\n  reaper:\n    enabled: true\n"
        plan, out = apply(text)
        self.assertEqual(plan.action, "add")
        self.assertEqual(out, "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  reaper:\n    enabled: true\n")

    def test_an_empty_sandbox_key_is_filled(self):
        plan, out = apply("admins: []\nsandbox:\nother: 1\n")
        self.assertEqual(plan.action, "add")
        self.assertEqual(out, "admins: []\nsandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\nother: 1\n")

    def test_the_text_after_the_block_is_byte_identical(self):
        tail = "\n# end comment\nlater:\n  key: |\n    sandbox: not a key\n"
        plan, out = apply("sandbox:\n  provider: motel\n" + tail)
        self.assertTrue(out.endswith(tail))

    def test_indentation_of_four_spaces_is_followed(self):
        text = "sandbox:\n    provider: motel\n    reaper:\n        enabled: true\n"
        _plan, out = apply(text, Want(URL, ttl="2h"))
        self.assertIn("    provider: motel\n    server_url: http://127.0.0.1:6767\n", out)
        self.assertIn("    motel:\n      ttl: 2h\n", out)


class OtherProviderTest(unittest.TestCase):
    MODAL = "sandbox:\n  provider: modal\n  server_url: https://omni.example.com\n  modal:\n    app: x\nafter: 1\n"

    def test_a_block_for_another_provider_is_refused_with_the_block_to_paste(self):
        plan = plan_config(self.MODAL, WANT)
        self.assertEqual(plan.action, "manual")
        self.assertIn("provider modal", plan.reason)
        self.assertIn("--yes", plan.reason)
        self.assertEqual(plan.block, render_block(WANT))
        self.assertIsNone(plan.text)

    def test_with_yes_the_old_block_becomes_comments_and_the_rest_is_kept(self):
        plan = plan_config(self.MODAL, Want(URL, replace_other=True))
        self.assertEqual(plan.action, "replace")
        self.assertIn("# sandbox:\n#   provider: modal\n", plan.text)
        self.assertIn("\nsandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\nafter: 1\n", plan.text)
        self.assertTrue(plan.text.endswith("after: 1\n"))


class ProvidersListShapeTest(unittest.TestCase):
    LIST = ("name: x\nsandbox:\n  server_url: https://omni.example.com\n  providers:\n    - provider: modal\n      modal:\n"
            "        app: a   # keep\n    - provider: e2b\n  reaper:\n    enabled: true\nafter: 1\n")

    def test_motel_is_added_to_the_list_not_replacing_it(self):
        plan, out = apply(self.LIST)
        self.assertEqual(plan.action, "add")
        self.assertIn("    - provider: e2b\n    - provider: motel\n  reaper:\n", out)
        self.assertIn("        app: a   # keep\n", out)
        self.assertNotIn("    - provider: motel\n      server_url", out)     # the shared server_url is reused

    def test_the_new_entry_carries_its_own_server_url_when_there_is_no_shared_one(self):
        text = "sandbox:\n  providers:\n    - provider: modal\n"
        _plan, out = apply(text, Want(URL, server_type="cpx32"))
        self.assertEqual(out, "sandbox:\n  providers:\n    - provider: modal\n    - provider: motel\n      server_url: "
                              "http://127.0.0.1:6767\n      motel:\n        server_type: cpx32\n")

    def test_an_existing_motel_entry_is_left_alone(self):
        text = self.LIST.replace("    - provider: e2b\n", "    - provider: motel\n")
        self.assertEqual(plan_config(text, WANT).action, "unchanged")

    def test_a_flow_style_motel_entry_is_recognised(self):
        text = "sandbox:\n  server_url: http://127.0.0.1:6767\n  providers:\n    - {provider: motel}\n"
        self.assertEqual(plan_config(text, WANT).action, "unchanged")
        self.assertEqual(plan_config(text, Want(URL, ttl="2h")).action, "manual")

    def test_flags_update_an_existing_entry_in_place(self):
        text = ("sandbox:\n  server_url: http://127.0.0.1:6767\n  providers:\n    - provider: motel\n      motel:\n"
                "        ttl: 1h\n    - provider: modal\n")
        plan, out = apply(text, Want(URL, ttl="2h", server_type="cpx32"))
        self.assertEqual(plan.action, "update")
        self.assertIn("        server_type: cpx32\n        ttl: 2h\n    - provider: modal\n", out)

    def test_provider_and_providers_together_are_not_edited(self):
        text = "sandbox:\n  provider: modal\n  providers:\n    - provider: e2b\n"
        self.assertEqual(plan_config(text, WANT).action, "manual")


class NotEditedTest(unittest.TestCase):
    def assertManual(self, text, word):
        plan = plan_config(text, WANT)
        self.assertEqual(plan.action, "manual", text)
        self.assertIn(word, plan.reason)
        self.assertEqual(plan.block, render_block(WANT))

    def test_unsafe_shapes_print_the_block_instead(self):
        self.assertManual("sandbox: {provider: motel}\n", "inline")
        self.assertManual("sandbox:\n  provider: motel\r\n", "line endings")
        self.assertManual("sandbox:\n\tprovider: motel\n", "tabs")
        self.assertManual("sandbox:\n  provider: motel\nsandbox:\n  provider: modal\n", "more than one")
        self.assertManual("---\nsandbox:\n  provider: modal\n---\nb: 2\n", "documents")
        self.assertManual("{admins: [a]}\n", "flow mapping")
        self.assertManual('"sandbox":\n  provider: modal\n', "quoted")
        self.assertManual("sandbox:\n  provider: &p motel\n", "plain name")

    def test_an_inline_motel_block_is_manual_only_when_flags_must_change_it(self):
        text = "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel: {ttl: 1h}\n"
        self.assertEqual(plan_config(text, WANT).action, "unchanged")
        self.assertEqual(plan_config(text, Want(URL, ttl="2h")).action, "manual")

    def test_a_multi_line_flow_value_is_manual(self):
        text = "sandbox:\n  provider: motel\n  motel: {\n    ttl: 1h }\n"
        self.assertEqual(plan_config(text, WANT).action, "manual")

    def test_a_block_scalar_body_is_not_read_as_keys(self):
        text = ("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  host_config:\n    note: |\n"
                "      provider: modal\n      server_url: elsewhere\n")
        self.assertEqual(plan_config(text, WANT).action, "unchanged")


class ScalarTest(unittest.TestCase):
    def test_scalar_reads_plain_and_simple_quoted_values_only(self):
        self.assertEqual(scalar("motel"), "motel")
        self.assertEqual(scalar('"motel"'), "motel")
        self.assertEqual(scalar("'http://a:1'"), "http://a:1")
        for value in (None, "", "~", "null", "{a: 1}", "[a]", "|", "&x motel", '"a\\nb"'):
            self.assertIsNone(scalar(value), value)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class RoundTripWithYamlTest(unittest.TestCase):
    """Where a YAML library exists, the edited file must parse, keep every other key, and name motel."""

    CORPUS = (
        "",
        "# only a comment\n",
        "database_uri: sqlite:///x.db   # keep\nadmins:\n  - alice@example.com\n",
        "no_newline: 1",
        "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\nafter: 1\n",
        "sandbox:\n  provider: motel   # c\nafter: 1\n",
        "sandbox:\n  server_url: http://127.0.0.1:6767\n  reaper:\n    enabled: true\n",
        "sandbox:\nafter: 1\n",
        "sandbox:\n  providers:\n    - provider: modal\n      modal:\n        app: a\n    - provider: e2b\nafter: 1\n",
        "sandbox:\n  server_url: https://omni.example.com\n  providers:\n    - provider: e2b\n  reaper:\n    enabled: true\n",
        "sandbox:\n    provider: motel\n    motel:\n        ttl: 1h\n",
        "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n    disk_gb: 20\n    env: [A, B]\n",
    )
    WANTS = (WANT, Want(URL, server_type="cpx32"), Want(URL, True, "cpx32", "2h"), Want("https://omni.example.com", True))

    def test_every_edit_parses_and_keeps_the_other_keys(self):
        for text in self.CORPUS:
            for want in self.WANTS:
                plan = plan_config(text, want)
                if plan.text is None:
                    continue
                old, new = yaml.safe_load(text) or {}, yaml.safe_load(plan.text)
                self.assertEqual({k: v for k, v in old.items() if k != "sandbox"}, {k: v for k, v in new.items() if k != "sandbox"},
                                 (text, want))
                block = new["sandbox"]
                entries = block["providers"] if "providers" in block else [block]
                motel = [e for e in entries if e.get("provider") == "motel"]
                self.assertEqual(len(motel), 1, (text, want, plan.text))
                merged = {**{k: v for k, v in block.items() if k != "providers"}, **motel[0]}
                self.assertTrue(merged["server_url"].startswith("http"), (text, want))
                if want.ttl:
                    self.assertEqual(merged["motel"]["ttl"], want.ttl)
                if want.server_type:
                    self.assertEqual(merged["motel"]["server_type"], want.server_type)
                again = plan_config(plan.text, want)
                self.assertEqual(again.action, "unchanged", (text, want, plan.text))


if __name__ == "__main__":
    unittest.main()
