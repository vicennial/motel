"""The provider registry and everything that reads it: config, desk, setup, doctor and the installer."""

import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import BASE_TOML, SERVER, Fixture
from motel import desk, installer
from motel.config import Config, Paths
from motel.errors import ConfigError, NotSetUp, Refused, Usage
from motel.providers import registry
from motel.providers.fake import FakeProvider
from motel.rooms import Caller

TOKEN = "t" * 40


def toml(provider="hetzner", extra=""):
    return BASE_TOML.replace('provider = "hetzner"', f'provider = "{provider}"') + extra


def fixture_for(provider):
    """A Fixture whose config and setup receipt both name ``provider``."""
    fx = Fixture(toml=toml(provider))
    receipt = json.loads(Path(fx.paths.receipt_file).read_text())
    receipt["provider"] = provider
    Path(fx.paths.receipt_file).write_text(json.dumps(receipt))
    return fx


class RegistryTest(unittest.TestCase):
    def test_every_cloud_is_listed_and_loadable(self):
        self.assertEqual(set(registry.names()), {"hetzner", "digitalocean", "linode", "vultr", "scaleway", "aws", "gcp", "azure"})
        for name in registry.names():
            with self.subTest(name):
                cls = registry.get(name).load_class()
                self.assertEqual(cls.name, name)
                self.assertIn(cls.currency, ("EUR", "USD"))
                self.assertIsInstance(cls.formats_on_create, bool)

    def test_the_table_matches_the_provider_modules(self):
        from motel.providers import vultr
        self.assertEqual(registry.VULTR_BLOCK_TYPES, vultr.BLOCK_TYPES)
        self.assertEqual(installer.CABIN_USER_VERBS, __import__("motel.cabins.desk", fromlist=["x"]).USER_VERBS)

    def test_raw_clouds_say_so(self):
        raw = {n for n in registry.names() if registry.get(n).load_class().formats_on_create is False}
        self.assertEqual(raw, set(registry.names()) - {"hetzner"})

    def test_build_passes_the_rate_and_block_type(self):
        cfg = Config.from_dict({"host": {"provider": "vultr", "allow_users": ["alice"]}, "provider": {"block_type": "storage_opt"},
                                "rooms": {"eur_per_usd": 0.9}})
        provider = registry.build(cfg, Paths(root="/nonexistent"), TOKEN)
        self.assertEqual((provider.block_type, str(provider.eur_per_usd)), ("storage_opt", "0.9"))
        do = Config.from_dict({"host": {"provider": "digitalocean", "allow_users": ["alice"]}, "rooms": {"eur_per_usd": 0.5}})
        self.assertEqual(str(registry.build(do, Paths(root="/nonexistent"), TOKEN).eur_per_usd), "0.5")

    def test_azure_gets_sku_and_identity_and_needs_no_token(self):
        cfg = Config.from_dict({"host": {"provider": "azure", "allow_users": ["alice"]},
                                "provider": {"sku": "Premium_LRS", "client_id": "0a1b2c3d-0000-0000-0000-000000000000"}})
        provider = registry.build(cfg, Paths(root="/nonexistent"), None)
        self.assertEqual(provider.sku, "Premium_LRS")
        self.assertEqual(provider.identity_hint, {"client_id": "0a1b2c3d-0000-0000-0000-000000000000"})

    def test_aws_reads_the_credentials_file_only_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(root=tmp)
            cfg = Config.from_dict({"host": {"provider": "aws", "allow_users": ["alice"]}})
            self.assertIsNone(registry.build(cfg, paths, None)._creds_source)
            os.makedirs(paths.credentials_dir)
            path = os.path.join(paths.credentials_dir, "aws.credentials")
            Path(path).write_text("aws_access_key_id = AKIAEXAMPLE\naws_secret_access_key = abc\n")
            os.chmod(path, 0o600)
            if os.geteuid() == 0:
                self.assertIsNotNone(registry.build(cfg, paths, None)._creds_source)


class ConfigTest(unittest.TestCase):
    def test_every_provider_name_is_accepted(self):
        for name in registry.provider_names():
            with self.subTest(name):
                self.assertEqual(Config.from_dict({"host": {"provider": name, "allow_users": ["alice"]}}).provider, name)

    def test_ids_with_dashes_dots_and_underscores(self):
        ids = ["9f2c1d4e-8b3a-4c5d-9e6f-0123456789ab", "i-0abc123def456", "vm-name_1.eu", "1000001"]
        for value in ids:
            with self.subTest(value):
                cfg = Config.from_dict({"host": {"provider": "vultr", "allow_users": ["alice"], "server_id": value,
                                                 "never_touch": [value, 7]}})
                self.assertEqual(cfg.server_id, value)
                self.assertEqual(cfg.never_touch, {value, "7"})

    def test_ids_that_could_escape_are_refused(self):
        for bad in ("../x", "a/b", "-x", ".x", "a b", "x" * 81, "", "a;b"):
            with self.subTest(bad), self.assertRaises(ConfigError):
                Config.from_dict({"host": {"provider": "vultr", "allow_users": ["alice"], "server_id": bad}})
            with self.subTest(bad, table="never_touch"), self.assertRaises(ConfigError):
                Config.from_dict({"host": {"provider": "vultr", "allow_users": ["alice"], "never_touch": [bad]}})

    def test_rate_and_block_type(self):
        base = {"host": {"provider": "vultr", "allow_users": ["alice"]}}
        self.assertEqual(Config.from_dict(base).eur_per_usd, 1.0)
        self.assertEqual(Config.from_dict({**base, "rooms": {"eur_per_usd": 0.92}}).eur_per_usd, 0.92)
        for bad in ({"rooms": {"eur_per_usd": 0}}, {"rooms": {"eur_per_usd": "1"}}, {"provider": {"block_type": "ssd"}},
                    {"provider": {"nonsense": "x"}}):
            with self.subTest(bad), self.assertRaises(ConfigError):
                Config.from_dict({**base, **bad})

    def test_a_provider_option_must_fit_the_chosen_cloud(self):
        with self.assertRaises(ConfigError) as ctx:
            Config.from_dict({"host": {"provider": "hetzner", "allow_users": ["alice"]}, "provider": {"block_type": "high_perf"}})
        self.assertIn("does not apply", str(ctx.exception))

    def test_the_cabins_table_goes_through_the_cabins_module(self):
        host = {"provider": "hetzner", "allow_users": ["alice"]}
        cfg = Config.from_dict({"host": host, "cabins": {"enabled": True, "types": ["cpx32"], "max_eur_per_hour": 0.2}})
        self.assertTrue(cfg.cabins.enabled)
        self.assertEqual((cfg.cabins.types, cfg.cabins.allow_users), (("cpx32",), ("alice",)))
        self.assertIsNone(Config.from_dict({"host": host}).cabins)

    def test_a_bad_cabins_table_never_stops_the_rooms(self):
        """One typo in [cabins] used to make every rooms verb, and rooms housekeeping, refuse to start."""
        host = {"provider": "hetzner", "allow_users": ["alice"]}
        for bad in ({"enabled": True}, {"nonsense": 1}, {"idle_timeout": "5m"}, "yes", {"enabled": False, "typo_key": 1}):
            with self.subTest(bad):
                cfg = Config.from_dict({"host": host, "cabins": bad})
                self.assertIsNone(cfg.cabins)
                self.assertIn("cabins", cfg.cabins_problem)
                self.assertEqual(cfg.provider, "hetzner")

    def test_ids_up_to_80_characters_for_azure(self):
        name = "a" * 80
        self.assertEqual(Config.from_dict({"host": {"provider": "azure", "allow_users": ["alice"], "server_id": name}}).server_id, name)
        from motel.config import same_id
        self.assertTrue(same_id("My-VM", "my-vm"))
        self.assertFalse(same_id("my-vm", "my-vm2"))


class LenientCabinsTest(unittest.TestCase):
    def test_rooms_check_in_and_reap_with_a_broken_cabins_table(self):
        fx = Fixture(toml=BASE_TOML + '\n[cabins]\nenabled = true\ntypo_key = 1\n')
        self.addCleanup(fx.close)
        self.assertEqual(fx.checkin(10)["floor"][:2], "f-")
        code, out, err = fx.desk("reap", "--apply", "--json", caller=Caller(0, 0, "root", operator=True))
        self.assertEqual(code, 0, out + err)

    def test_the_doctor_reports_the_cabins_problem_and_the_rest_still_runs(self):
        fx = Fixture(toml=BASE_TOML + '\n[cabins]\nenabled = true\ntypo_key = 1\n')
        self.addCleanup(fx.close)
        _, out, _ = fx.desk("doctor", "--json")
        checks = {c["check"]: c for c in json.loads(out)["checks"]}
        self.assertFalse(checks["cabins config valid"]["ok"])
        self.assertIn("typo_key", checks["cabins config valid"]["detail"])
        self.assertTrue(checks["config valid"]["ok"])


class CaseInsensitiveIdsTest(unittest.TestCase):
    def test_the_never_touch_guard_ignores_case(self):
        from motel.errors import Guard
        from motel.guard import check_owned
        from motel.providers.base import Volume
        vol = Volume(id="MyDisk", name="motel-f-1", size_gb=10, server_id="vm", labels={"managed-by": "motel", "role": "floor",
                                                                                           "host": "vm", "floor": "f-1"})
        floor = {"id": "f-1", "volume_id": "MyDisk", "state": "active"}
        check_owned(vol, floor, action="mount", server_id="vm", never_touch=set())
        with self.assertRaises(Guard) as ctx:
            check_owned(vol, floor, action="mount", server_id="vm", never_touch={"mydisk"})
        self.assertIn("never-touch", ctx.exception.message)

    def test_the_configured_server_matches_the_receipt_in_any_case(self):
        fx = Fixture(toml=toml("azure").replace('provider = "azure"', 'provider = "azure"\nserver_id = "MY-VM"'))
        self.addCleanup(fx.close)
        receipt = json.loads(Path(fx.paths.receipt_file).read_text())
        receipt.update(provider="azure", server_id="my-vm")
        Path(fx.paths.receipt_file).write_text(json.dumps(receipt))
        ctx = desk.build_ctx(fx.paths, lambda cfg, paths: fx.provider, fx.sysops, fx.clock)
        self.assertEqual(ctx.receipt.server_id, "my-vm")


class DeskProviderTest(unittest.TestCase):
    def test_default_provider_reads_the_token_file_of_the_chosen_cloud(self):
        for name in ("digitalocean", "linode", "vultr", "scaleway", "hetzner"):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                paths = Paths(root=tmp)
                os.makedirs(paths.credentials_dir)
                cfg = Config.from_dict({"host": {"provider": name, "allow_users": ["alice"]}})
                with self.assertRaises(NotSetUp) as ctx:
                    desk.default_provider(cfg, paths)
                self.assertIn(f"{name}.token", ctx.exception.message)
                Path(paths.token_file(name)).write_text(TOKEN + "\n")
                self.assertEqual(desk.default_provider(cfg, paths).name, name)

    def test_instance_clouds_need_no_token_file(self):
        for name in ("aws", "gcp", "azure"):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                cfg = Config.from_dict({"host": {"provider": name, "allow_users": ["alice"]}})
                self.assertEqual(desk.default_provider(cfg, Paths(root=tmp)).name, name)

    def test_the_setup_verb_lists_every_cloud(self):
        for name in registry.provider_names():
            args = desk.build_parser().parse_args(["setup", name])
            self.assertEqual(args.provider, name)
        with self.assertRaises(Usage):
            desk.build_parser().parse_args(["setup", "nimbus"])


class SetupCloudTest(unittest.TestCase):
    def run_setup(self, fx, name, **hooks):
        out, err = io.StringIO(), io.StringIO()
        code = desk.run(["setup", name, "--json"], paths=fx.paths, caller=Caller(0, 0, "root"), sysops=fx.sysops, clock=fx.clock,
                        out=out, err=err, **hooks)
        return code, out.getvalue(), err.getvalue()

    def fixture(self, provider):
        fx = Fixture(toml=toml(provider), receipt=False)
        self.addCleanup(fx.close)
        cloud = FakeProvider(server_id=SERVER, location="eu-1")
        cloud.add(11, 100, name="data-volume", server_id=SERVER)
        return fx, cloud

    def test_a_token_cloud_prompts_for_its_own_token_and_stores_it_under_its_own_name(self):
        fx, cloud = self.fixture("vultr")
        asked = []
        code, out, err = self.run_setup(fx, "vultr", provider_factory=lambda token: cloud,
                                        token_reader=lambda: asked.append(1) or TOKEN)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(Path(fx.paths.token_file("vultr")).read_text().strip(), TOKEN)
        self.assertFalse(os.path.exists(fx.paths.token_file("hetzner")))
        self.assertEqual(stat.S_IMODE(os.stat(fx.paths.token_file("vultr")).st_mode), 0o600)
        self.assertEqual(json.loads(Path(fx.paths.receipt_file).read_text())["provider"], "vultr")
        self.assertNotIn(TOKEN, out)

    def test_an_instance_cloud_reads_and_writes_no_token(self):
        fx, cloud = self.fixture("azure")
        tokens = []
        code, out, err = self.run_setup(fx, "azure", provider_factory=lambda token: tokens.append(token) or cloud,
                                        token_reader=lambda: self.fail("must not ask for a token"))
        self.assertEqual(code, 0, out + err)
        self.assertEqual(tokens, [None])
        self.assertFalse(any(name.endswith(".token") for name in os.listdir(fx.paths.credentials_dir)) if os.path.isdir(fx.paths.credentials_dir) else False)
        receipt = json.loads(Path(fx.paths.receipt_file).read_text())
        self.assertEqual([v["id"] for v in receipt["never_touch"]], ["11"])  # existing disks are frozen

    def test_an_instance_cloud_that_cannot_see_this_vm_is_refused(self):
        fx, cloud = self.fixture("azure")
        cloud.server_id = "someone-else"
        cloud.whoami = lambda: __import__("motel.providers.base", fromlist=["x"]).HostIdentity(SERVER, "eu-1", "azure")
        code, out, _ = self.run_setup(fx, "azure", provider_factory=lambda token: cloud)
        self.assertEqual(code, Refused.code)
        self.assertFalse(os.path.exists(fx.paths.receipt_file))

    def test_token_file_is_refused_for_an_instance_cloud(self):
        fx, cloud = self.fixture("gcp")
        out, err = io.StringIO(), io.StringIO()
        code = desk.run(["setup", "gcp", "--token-file", "/x", "--json"], paths=fx.paths, caller=Caller(0, 0, "root"),
                        sysops=fx.sysops, clock=fx.clock, out=out, err=err, provider_factory=lambda token: cloud)
        self.assertEqual(code, Usage.code)

    def test_the_config_must_name_the_cloud_being_set_up(self):
        fx, cloud = self.fixture("linode")
        code, _, _ = self.run_setup(fx, "vultr", provider_factory=lambda token: cloud, token_reader=lambda: TOKEN)
        self.assertEqual(code, Refused.code)


class DoctorTest(unittest.TestCase):
    def test_the_token_check_and_the_visibility_warning_run_for_every_cloud(self):
        for name in ("hetzner", "vultr", "scaleway"):
            with self.subTest(name):
                fx = fixture_for(name)
                self.addCleanup(fx.close)
                os.makedirs(fx.paths.credentials_dir, exist_ok=True)
                Path(fx.paths.token_file(name)).write_text(TOKEN)
                fx.provider.add(5, 10)
                code, out, _ = fx.desk("doctor", "--json")
                checks = {c["check"]: c for c in json.loads(out)["checks"]}
                self.assertEqual(checks["token file root-owned 0600"]["detail"], fx.paths.token_file(name))
                self.assertEqual(checks["what else the token can see"]["level"], "warn")

    def test_an_instance_cloud_has_no_token_file_check(self):
        fx = fixture_for("azure")
        self.addCleanup(fx.close)
        _, out, _ = fx.desk("doctor", "--json")
        checks = {c["check"]: c for c in json.loads(out)["checks"]}
        self.assertNotIn("token file root-owned 0600", checks)
        self.assertIn("what else the token can see", checks)

    def test_the_local_provider_has_neither(self):
        fx = fixture_for("local")
        self.addCleanup(fx.close)
        _, out, _ = fx.desk("doctor", "--json")
        checks = {c["check"] for c in json.loads(out)["checks"]}
        self.assertNotIn("token file root-owned 0600", checks)
        self.assertNotIn("what else the token can see", checks)


if __name__ == "__main__":
    unittest.main()
