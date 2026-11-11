"""The banner, the "start here" list and the bare-`motel` first-run behaviour."""

import io
import os
import unittest
from unittest import mock

from motel import cli
from motel.onboard import banner

PLAIN_BANNER = """\
#   #   ###   #####  #####  #
## ##  #   #    #    #      #
# # #  #   #    #    #      #
#   #  #   #    #    ####   #
#   #  #   #    #    #      #
#   #  #   #    #    #      #
#   #   ###     #    #####  #####
   disk and servers by the hour, for agents"""
START_COMMANDS = ["motel try", "sudo uvx --from motel motel install", "motel doctor", "motel checkin 50G --for 6h", "motel cabin checkin", "motel mcp"]


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM")}
    env.update(extra)
    return mock.patch.dict(os.environ, env, clear=True)


class BannerTest(unittest.TestCase):
    def test_plain_banner_is_the_brand_ascii(self):
        self.assertEqual(banner.banner(color=False), PLAIN_BANNER)

    def test_color_banner_uses_palette_and_resets(self):
        text = banner.banner(color=True)
        self.assertIn(banner.PINK, text)
        self.assertIn(banner.TEAL, text)
        self.assertTrue(text.rstrip().endswith(banner.RESET))

    def test_start_here_has_six_commands_in_order(self):
        lines = banner.start_here(color=False).splitlines()
        self.assertEqual(lines[0], "start here")
        self.assertEqual(len(lines) - 1, 6)
        for line, command in zip(lines[1:], START_COMMANDS, strict=True):
            self.assertTrue(line.strip().startswith(command), line)

    def test_expiry_line(self):
        self.assertIn("checkout time reached", banner.expiry_line("job-7f3a"))
        self.assertNotIn("\033", banner.expiry_line("job-7f3a", color=False))
        self.assertIn("\033", banner.expiry_line("job-7f3a", color=True))


class ColorRulesTest(unittest.TestCase):
    def test_no_color_on_a_non_tty(self):
        with clean_env():
            self.assertFalse(banner.want_color(io.StringIO()))

    def test_color_on_a_tty(self):
        with clean_env():
            self.assertTrue(banner.want_color(FakeTty()))

    def test_no_color_env_wins_even_when_empty_is_unset(self):
        with clean_env(NO_COLOR="1"):
            self.assertFalse(banner.want_color(FakeTty()))

    def test_flag_and_dumb_terminal(self):
        with clean_env():
            self.assertFalse(banner.want_color(FakeTty(), no_color=True))
        with clean_env(TERM="dumb"):
            self.assertFalse(banner.want_color(FakeTty()))


class FirstRunTest(unittest.TestCase):
    def test_non_tty_prints_nothing(self):
        out = io.StringIO()
        self.assertFalse(banner.first_run(out))
        self.assertEqual(out.getvalue(), "")

    def test_tty_prints_banner_and_start_here(self):
        out = FakeTty()
        with clean_env():
            self.assertTrue(banner.first_run(out))
        text = out.getvalue()
        self.assertIn("disk and servers by the hour, for agents", text)
        for command in START_COMMANDS:
            self.assertIn(command, text)
        self.assertIn("\033", text)

    def test_tty_with_no_color_has_no_escape_codes(self):
        out = FakeTty()
        with clean_env(NO_COLOR="1"):
            banner.first_run(out)
        self.assertNotIn("\033", out.getvalue())
        self.assertIn(PLAIN_BANNER, out.getvalue())


class BareMotelTest(unittest.TestCase):
    """Bare `motel` keeps exit 2 and its message everywhere except a terminal."""

    def test_non_tty_is_unchanged(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main([], out=out, err=err), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "motel: no verb given. Try: motel --help\n")

    def test_tty_shows_the_banner_and_still_exits_2(self):
        out, err = FakeTty(), io.StringIO()
        with clean_env(NO_COLOR="1"):
            self.assertEqual(cli.main([], out=out, err=err), 2)
        self.assertIn("motel try", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_a_verb_on_a_tty_does_not_print_the_banner(self):
        out, err = FakeTty(), io.StringIO()
        self.assertEqual(cli.main(["--version"], out=out, err=err), 0)
        self.assertEqual(out.getvalue().strip().split()[0], "motel")
        self.assertNotIn("start here", out.getvalue())


if __name__ == "__main__":
    unittest.main()
