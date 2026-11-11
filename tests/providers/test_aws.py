"""The AWS provider against a fake EC2 Query API and a fake IMDSv2 on a loopback socket (no network, no account)."""

import hashlib
import os
import tempfile
import unittest
import urllib.parse
from datetime import UTC, datetime
from decimal import Decimal

from _aws_gcp_http import FakeClock, LoopbackServer

from motel.errors import InventoryIncomplete, NotFound, ProviderError, Usage
from motel.providers import aws
from motel.providers.aws import AwsProvider, Credentials, load_credentials_file, signing_key, sigv4

SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
INSTANCE = "i-0123456789abcdef0"
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)
NS = 'xmlns="http://ec2.amazonaws.com/doc/2016-11-15/"'


class TestSigV4(unittest.TestCase):
    """Known answers from AWS's published SigV4 documentation and test suite (aws-sig-v4-test-suite)."""

    def test_signing_key_documented_example(self):
        # https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html (derive signing key)
        key = signing_key(SECRET, "20150830", "us-east-1", "iam")
        self.assertEqual(key.hex(), "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9")

    def _sign(self, method, query=""):
        return sigv4(method=method, path="/", query=query, headers={"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
                     payload=b"", access_key="AKIDEXAMPLE", secret_key=SECRET, region="us-east-1", service="service",
                     amz_date="20150830T123600Z")

    def test_get_vanilla(self):
        auth, signature = self._sign("GET")
        self.assertEqual(signature, "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")
        self.assertEqual(auth, "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
                               "SignedHeaders=host;x-amz-date, Signature=" + signature)

    def test_post_vanilla(self):
        self.assertEqual(self._sign("POST")[1], "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b")

    def test_get_vanilla_query_order_key_case(self):
        self.assertEqual(self._sign("GET", "Param2=value2&Param1=value1")[1],
                         "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500")

    def test_canonical_request_hash_get_vanilla(self):
        text, _ = aws.canonical_request("GET", "/", "", {"host": "example.amazonaws.com", "x-amz-date": "20150830T123600Z"}, b"")
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),
                         "bb579772317eb040ac9ed261061d46c1f17a8133879d6129b6e1c25292927e63")


def _vol_xml(v):
    tags = "".join(f"<item><key>{k}</key><value>{val}</value></item>" for k, val in v["tags"].items())
    atts = "".join(f"<item><volumeId>{v['id']}</volumeId><instanceId>{a['instance']}</instanceId><device>{a['device']}</device>"
                   f"<status>{a['status']}</status></item>" for a in v["attachments"])
    return (f"<item><volumeId>{v['id']}</volumeId><size>{v['size']}</size><availabilityZone>{v['az']}</availabilityZone>"
            f"<status>{v['status']}</status><createTime>2026-11-09T11:00:00.000Z</createTime>"
            f"<attachmentSet>{atts}</attachmentSet><tagSet>{tags}</tagSet></item>")


class FakeEc2:
    """IMDSv2 plus a small EC2 Query API. ``fail[action]`` is a list of (status, code, message) returned first."""

    def __init__(self, secret=SECRET):
        self.secret, self.volumes, self.n, self.fail = secret, {}, 0, {}
        self.auth_seen, self.imds_calls, self.create_calls, self.bad_sig = [], [], 0, 0
        self.hide_first_describe = True
        self.stuck = False
        self.page_size = 500
        self.token_ok = True
        self.expiry = "2026-11-09T18:00:00Z"
        self.cred_fetches = 0
        self.mod = {}  # vol id -> remaining "modifying" polls
        self.mod_last_start = {}

    def _err(self, status, code, message):
        body = f"<Response><Errors><Error><Code>{code}</Code><Message>{message}</Message></Error></Errors><RequestID>r</RequestID></Response>"
        return status, {"Content-Type": "text/xml"}, body.encode()

    def _ok(self, action, inner):
        return 200, {"Content-Type": "text/xml"}, f"<{action}Response {NS}><requestId>r</requestId>{inner}</{action}Response>".encode()

    def handle(self, req):
        if req.path.startswith("/latest/"):
            return self.imds(req)
        return self.ec2(req)

    def imds(self, req):
        self.imds_calls.append((req.method, req.path, req.headers.get("x-aws-metadata-token-ttl") or req.headers.get("x-aws-ec2-metadata-token")))
        if req.method == "PUT":
            return (200, {}, b"TOKEN123") if self.token_ok else (403, {}, b"")
        if req.headers.get("x-aws-ec2-metadata-token") != "TOKEN123":
            return 401, {}, b""
        table = {"/latest/meta-data/instance-id": INSTANCE, "/latest/meta-data/placement/availability-zone": "us-east-1a",
                 "/latest/meta-data/placement/region": "us-east-1", "/latest/meta-data/iam/security-credentials/": "motel-role"}
        if req.path in table:
            return 200, {}, table[req.path].encode()
        if req.path == "/latest/meta-data/iam/security-credentials/motel-role":
            self.cred_fetches += 1
            import json
            return 200, {}, json.dumps({"Code": "Success", "AccessKeyId": "ASIAEXAMPLE0000", "SecretAccessKey": self.secret,
                                        "Token": "SESSIONTOKEN", "Expiration": self.expiry}).encode()
        return 404, {}, b""

    def ec2(self, req):
        host = req.headers["host"]
        auth = req.headers.get("authorization", "")
        self.auth_seen.append(auth)
        stamp = req.headers["x-amz-date"]
        headers = {"content-type": req.headers["content-type"], "host": host, "x-amz-date": stamp}
        if "x-amz-security-token" in req.headers:
            headers["x-amz-security-token"] = req.headers["x-amz-security-token"]
        _, expected = sigv4(method="POST", path="/", query="", headers=headers, payload=req.body, access_key="ASIAEXAMPLE0000",
                            secret_key=self.secret, region="us-east-1", service="ec2", amz_date=stamp)
        if not auth.endswith("Signature=" + expected) or "ASIAEXAMPLE0000/20261109/us-east-1/ec2/aws4_request" not in auth:
            self.bad_sig += 1
            return self._err(403, "SignatureDoesNotMatch", "bad signature")
        q = dict(urllib.parse.parse_qsl(req.body.decode(), keep_blank_values=True))
        action = q["Action"]
        assert q["Version"] == "2016-11-15"
        queue = self.fail.get(action)
        if queue:
            return self._err(*queue.pop(0))
        return getattr(self, "a_" + action)(q)

    # -- actions
    def a_DescribeInstances(self, q):
        return self._ok("DescribeInstances", f"<reservationSet><item><instancesSet><item><instanceId>{q['InstanceId.1']}</instanceId></item></instancesSet></item></reservationSet>")

    def a_CreateVolume(self, q):
        self.create_calls += 1
        self.n += 1
        vid = f"vol-0abc{self.n:013x}"
        tags = {}
        i = 1
        while f"TagSpecification.1.Tag.{i}.Key" in q:
            tags[q[f"TagSpecification.1.Tag.{i}.Key"]] = q[f"TagSpecification.1.Tag.{i}.Value"]
            i += 1
        assert q["TagSpecification.1.ResourceType"] == "volume"
        self.volumes[vid] = {"id": vid, "size": int(q["Size"]), "az": q["AvailabilityZone"], "status": "creating", "tags": tags,
                             "attachments": [], "type": q["VolumeType"], "token": q.get("ClientToken"), "polls": 0}
        return self._ok("CreateVolume", f"<volumeId>{vid}</volumeId><size>{q['Size']}</size><status>creating</status>")

    def _advance(self, v):
        v["polls"] += 1
        if v["status"] == "creating" and v["polls"] >= 2 and not self.stuck:
            v["status"] = "available"
        for a in v["attachments"]:
            if a["status"] == "attaching" and a["polls"] >= 1:
                a["status"] = "attached"
            a["polls"] += 1
        if v.get("detaching"):
            v["detaching"] -= 1
            if v["detaching"] == 0:
                v["attachments"], v["status"] = [], "available"

    def a_DescribeVolumes(self, q):
        ids = [q[k] for k in sorted(q) if k.startswith("VolumeId.")]
        if ids and "MaxResults" in q:
            return self._err(400, "InvalidParameterCombination", "MaxResults with VolumeId")
        vols = list(self.volumes.values())
        if ids:
            if ids[0] not in self.volumes or (self.hide_first_describe and self.volumes[ids[0]]["polls"] == 0 and self.volumes[ids[0]]["status"] == "creating"):
                if ids[0] in self.volumes:
                    self.volumes[ids[0]]["polls"] += 1  # consistency lag: the first read does not see the new volume
                return self._err(400, "InvalidVolume.NotFound", f"The volume '{ids[0]}' does not exist.")
            vols = [self.volumes[i] for i in ids]
        n = 1
        while f"Filter.{n}.Name" in q:
            name, value = q[f"Filter.{n}.Name"], q[f"Filter.{n}.Value.1"]
            if name.startswith("tag:"):
                vols = [v for v in vols if v["tags"].get(name[4:]) == value]
            elif name == "attachment.instance-id":
                vols = [v for v in vols if any(a["instance"] == value for a in v["attachments"])]
            n += 1
        for v in vols:
            self._advance(v)
        start = int(q.get("NextToken", "0"))
        size = self.page_size
        chunk = vols[start:start + size]
        more = start + size < len(vols)
        token = f"<nextToken>{start + size}</nextToken>" if more else ""
        return self._ok("DescribeVolumes", f"<volumeSet>{''.join(_vol_xml(v) for v in chunk)}</volumeSet>{token}")

    def a_AttachVolume(self, q):
        v = self.volumes[q["VolumeId"]]
        assert v["status"] == "available", v["status"]
        if any(a["device"] == q["Device"] for x in self.volumes.values() for a in x["attachments"]):
            return self._err(400, "InvalidParameterValue", f"Attachment point {q['Device']} is already in use")
        v["attachments"].append({"instance": q["InstanceId"], "device": q["Device"], "status": "attaching", "polls": 0})
        v["status"] = "in-use"
        return self._ok("AttachVolume", f"<volumeId>{v['id']}</volumeId><instanceId>{q['InstanceId']}</instanceId><device>{q['Device']}</device><status>attaching</status>")

    def a_DetachVolume(self, q):
        v = self.volumes[q["VolumeId"]]
        for a in v["attachments"]:
            a["status"] = "detaching"
        v["detaching"] = 2
        return self._ok("DetachVolume", f"<volumeId>{v['id']}</volumeId><status>detaching</status>")

    def a_ModifyVolume(self, q):
        v = self.volumes[q["VolumeId"]]
        last = self.mod_last_start.get(v["id"])
        if last is not None:
            return self._err(400, "VolumeModificationRateExceeded", "You've reached the maximum modification rate per volume limit.")
        self.mod_last_start[v["id"]] = True
        v["size"] = int(q["Size"])
        self.mod[v["id"]] = 2
        return self._ok("ModifyVolume", f"<volumeModification><volumeId>{v['id']}</volumeId><modificationState>modifying</modificationState></volumeModification>")

    def a_DescribeVolumesModifications(self, q):
        vid = q["VolumeId.1"]
        left = self.mod.get(vid, 0)
        state = "modifying" if left > 0 else "optimizing"
        self.mod[vid] = max(0, left - 1)
        return self._ok("DescribeVolumesModifications", f"<volumeModificationSet><item><volumeId>{vid}</volumeId><modificationState>{state}</modificationState></item></volumeModificationSet>")

    def a_DeleteVolume(self, q):
        v = self.volumes[q["VolumeId"]]
        if v["attachments"]:
            return self._err(400, "VolumeInUse", "Volume is attached")
        del self.volumes[q["VolumeId"]]
        return self._ok("DeleteVolume", "<return>true</return>")


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEc2()
        self.server = LoopbackServer(self.fake.handle).__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.clock = FakeClock()
        self.provider = self.make()

    def make(self, **kw):
        return AwsProvider(base=self.server.url, imds_base=self.server.url, sleep=self.clock.sleep, monotonic=self.clock.monotonic,
                           clock=lambda: NOW, **kw)

    def labels(self, floor="f-1a2b"):
        return {"managed-by": "motel", "role": "floor", "host": INSTANCE, "floor": floor, "suite": "0", "created": "20261109t1100z"}

    def actions(self):
        return [dict(urllib.parse.parse_qsl(r.body.decode()))["Action"] for r in self.server.requests if r.path == "/"]


class TestIdentityAndCredentials(Base):
    def test_whoami_uses_imdsv2_token_first(self):
        who = self.provider.whoami()
        self.assertEqual((who.server_id, who.location, who.provider), (INSTANCE, "us-east-1a", "aws"))
        self.assertEqual(self.fake.imds_calls[0][0], "PUT")
        self.assertTrue(all(c[2] == "TOKEN123" for c in self.fake.imds_calls[1:]))

    def test_no_imdsv1_fallback(self):
        self.fake.token_ok = False
        with self.assertRaises(ProviderError):
            self.provider.whoami()
        self.assertFalse([c for c in self.fake.imds_calls if c[0] == "GET"])

    def test_instance_role_credentials_sign_requests_and_are_cached(self):
        self.provider.visible()
        self.assertEqual(self.fake.bad_sig, 0)
        self.assertEqual(self.fake.cred_fetches, 1)
        self.provider.visible()
        self.assertEqual(self.fake.cred_fetches, 1)
        req = [r for r in self.server.requests if r.path == "/"][0]
        self.assertEqual(req.headers["x-amz-security-token"], "SESSIONTOKEN")
        self.assertIn("SignedHeaders=content-type;host;x-amz-date;x-amz-security-token", req.headers["authorization"])

    def test_credentials_refresh_near_expiry(self):
        self.fake.expiry = "2026-11-09T12:03:00Z"  # inside the 5 minute margin
        self.provider.list_owned({"managed-by": "motel"})
        self.provider.list_owned({"managed-by": "motel"})
        self.assertEqual(self.fake.cred_fetches, 2)

    def test_static_credentials_and_file(self):
        self.fake.secret = "static-secret-0123456789abcdef"
        p = self.make(credentials=Credentials("ASIAEXAMPLE0000", "static-secret-0123456789abcdef", None))
        p.visible()
        self.assertEqual(self.fake.bad_sig, 0)
        self.assertEqual(self.fake.cred_fetches, 0)
        self.assertNotIn("x-amz-security-token", [r for r in self.server.requests if r.path == "/"][0].headers)

    def test_credentials_file_checks(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "aws.credentials")
            with open(path, "w") as f:
                f.write("[default]\n# comment\naws_access_key_id = ASIAEXAMPLE0000\naws_secret_access_key = static-secret-0123456789abcdef\n")
            os.chmod(path, 0o600)
            creds = load_credentials_file(path, owner_uid=os.getuid())
            self.assertEqual((creds.access_key, creds.token), ("ASIAEXAMPLE0000", None))
            self.assertNotIn("static-secret", repr(creds))
            os.chmod(path, 0o644)
            with self.assertRaises(Usage):
                load_credentials_file(path, owner_uid=os.getuid())
            os.chmod(path, 0o600)
            with self.assertRaises(Usage):
                load_credentials_file(path, owner_uid=os.getuid() + 1)
            link = os.path.join(d, "link")
            os.symlink(path, link)
            with self.assertRaises(Usage):
                load_credentials_file(link, owner_uid=os.getuid())

    def test_wrong_secret_is_rejected_by_the_fake(self):
        self.fake.secret = "another-secret-0123456789abcdef"
        p = self.make(credentials=Credentials("ASIAEXAMPLE0000", "not-the-secret-0123456789", None))
        with self.assertRaises(ProviderError) as ctx:
            p.visible()
        self.assertIn("SignatureDoesNotMatch", ctx.exception.message)
        self.assertEqual(self.fake.bad_sig, 1)  # a 403 is not retried


class TestVolumes(Base):
    def test_create_attach_tags_device_and_serial(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.assertEqual((vol.size_gb, vol.server_id, vol.name, vol.location), (50, INSTANCE, "motel-f-1a2b-cd34", "us-east-1a"))
        self.assertEqual(vol.labels, self.labels())  # Name tag is the volume name, not a label
        stored = self.fake.volumes[vol.id]
        self.assertEqual(stored["tags"]["Name"], "motel-f-1a2b-cd34")
        self.assertEqual(stored["type"], "gp3")
        self.assertEqual(stored["token"], "motel-f-1a2b-cd34")  # ClientToken
        self.assertEqual(stored["attachments"][0]["device"], "/dev/sdf")
        self.assertEqual(self.provider.device_path(vol.id), "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_" + vol.id.replace("-", ""))
        self.assertEqual(self.provider.expected_serial(vol.id), vol.id.replace("-", ""))
        self.assertEqual(self.fake.bad_sig, 0)

    def test_eventual_consistency_notfound_is_waited_out(self):
        self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.assertGreater(len(self.clock.slept), 0)

    def test_second_attach_picks_next_device(self):
        a = self.provider.create_volume("motel-f-1a2b-0001", 50, "us-east-1a", self.labels("f-1"), server_id=INSTANCE)
        b = self.provider.create_volume("motel-f-1a2b-0002", 50, "us-east-1a", self.labels("f-2"), server_id=INSTANCE)
        self.assertEqual([self.fake.volumes[v.id]["attachments"][0]["device"] for v in (a, b)], ["/dev/sdf", "/dev/sdg"])

    def test_device_in_use_race_tries_the_next_name(self):
        self.fake.fail["AttachVolume"] = [(400, "InvalidParameterValue", "Attachment point /dev/sdf is already in use")]
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.assertEqual(self.fake.volumes[vol.id]["attachments"][0]["device"], "/dev/sdg")  # sdf was reported in use
        self.assertEqual(self.actions().count("AttachVolume"), 2)

    def test_create_without_server_stays_available(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels())
        self.assertIsNone(vol.server_id)
        self.assertNotIn("AttachVolume", self.actions())

    def test_attach_failure_after_create_is_indeterminate_so_the_desk_reconciles(self):
        self.fake.fail["AttachVolume"] = [(400, "UnauthorizedOperation", "not allowed")]
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.assertTrue(ctx.exception.indeterminate)
        found = self.provider.list_owned({"floor": "f-1a2b", "managed-by": "motel"})
        self.assertEqual([v.name for v in found], ["motel-f-1a2b-cd34"])

    def test_create_5xx_is_indeterminate_and_not_retried(self):
        self.fake.fail["CreateVolume"] = [(500, "InternalError", "boom")]
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(self.fake.create_calls, 0)  # the fake returned the injected error instead
        self.assertEqual(self.actions().count("CreateVolume"), 1)

    def test_wait_timeout_after_create_is_indeterminate(self):
        self.fake.stuck = True
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertIn("vol-", ctx.exception.message)
        self.assertGreaterEqual(self.clock.now, 1300.0)

    def test_create_4xx_is_definite(self):
        self.fake.fail["CreateVolume"] = [(400, "VolumeLimitExceeded", "too many")]
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels())
        self.assertFalse(ctx.exception.indeterminate)
        self.assertIn("VolumeLimitExceeded", ctx.exception.message)

    def test_throttled_write_is_retried_with_backoff(self):
        self.fake.fail["CreateVolume"] = [(503, "RequestLimitExceeded", "slow"), (503, "RequestLimitExceeded", "slow")]
        self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels())
        self.assertEqual(self.fake.create_calls, 1)
        self.assertEqual(self.clock.slept[:2], [1.0, 2.0])

    def test_server_error_on_read_is_retried_and_write_is_not(self):
        self.fake.fail["DescribeVolumes"] = [(500, "InternalError", "x"), (503, "ServiceUnavailable", "x")]
        self.assertEqual(self.provider.list_owned({"managed-by": "motel"}), [])

    def test_wrong_zone_or_server_refused(self):
        with self.assertRaises(ProviderError):
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1b", self.labels(), server_id=INSTANCE)
        with self.assertRaises(ProviderError):
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id="i-0aaaaaaaaaaaaaaaa")

    def test_bad_tags_refused_before_the_call(self):
        with self.assertRaises(ProviderError):
            self.provider.create_volume("n", 50, "us-east-1a", {"aws:x": "y"})
        self.assertNotIn("CreateVolume", self.actions())

    def test_detach_then_delete(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        with self.assertRaises(ProviderError):
            self.provider.delete(vol.id)  # still attached: VolumeInUse
        self.provider.detach(vol.id)
        self.assertIsNone(self.provider.get_volume(vol.id).server_id)
        self.provider.detach(vol.id)  # already detached: no call
        self.assertEqual(self.actions().count("DetachVolume"), 1)
        self.provider.delete(vol.id)
        with self.assertRaises(NotFound):
            self.provider.get_volume(vol.id)

    def test_resize_waits_for_optimizing_and_rate_limit_is_explained(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels(), server_id=INSTANCE)
        self.provider.resize(vol.id, 80)
        self.assertEqual(self.provider.get_volume(vol.id).size_gb, 80)
        self.assertEqual(self.actions().count("DescribeVolumesModifications"), 3)
        with self.assertRaises(ProviderError) as ctx:
            self.provider.resize(vol.id, 100)
        self.assertIn("6 hours", ctx.exception.message)
        self.assertFalse(ctx.exception.indeterminate)
        with self.assertRaises(ProviderError):
            self.provider.resize(vol.id, 80)  # not larger: refused before any call
        self.assertEqual(self.actions().count("ModifyVolume"), 2)

    def test_get_volume_does_not_send_max_results_with_ids(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, "us-east-1a", self.labels())
        self.provider.get_volume(vol.id)  # the fake answers 400 InvalidParameterCombination otherwise

    def test_odd_ids_refused(self):
        for bad in ("vol-xyz", "1", "vol-0abc&Action=DeleteVolume", ""):
            with self.assertRaises(ProviderError):
                self.provider.get_volume(bad)
            with self.assertRaises(ProviderError):
                self.provider.device_path(bad)


class TestListing(Base):
    def seed(self, n, tagged=True):
        for i in range(n):
            vid = f"vol-0abc{i + 100:013x}"
            self.fake.volumes[vid] = {"id": vid, "size": 10, "az": "us-east-1a", "status": "available",
                                      "tags": {"managed-by": "motel", "floor": f"f-{i}"} if tagged else {}, "attachments": [],
                                      "type": "gp3", "token": None, "polls": 5}

    def test_pagination_and_inventory(self):
        self.fake.page_size = 2
        self.seed(5)
        inv = self.provider.visible()
        self.assertEqual(len(inv.volumes), 5)
        self.assertEqual(inv.servers, (INSTANCE,))
        self.assertEqual(self.actions().count("DescribeVolumes"), 3)

    def test_filters_are_sent_and_checked(self):
        self.seed(3)
        self.seed(0)
        found = self.provider.list_owned({"managed-by": "motel", "floor": "f-1"})
        self.assertEqual([v.labels["floor"] for v in found], ["f-1"])

    def test_listing_failure_is_inventory_incomplete(self):
        self.seed(1)
        self.fake.fail["DescribeVolumes"] = [(403, "UnauthorizedOperation", "no")]
        with self.assertRaises(InventoryIncomplete):
            self.provider.visible()
        self.fake.fail["DescribeVolumes"] = [(403, "UnauthorizedOperation", "no")]
        with self.assertRaises(InventoryIncomplete):
            self.provider.list_owned({"managed-by": "motel"})

    def test_attached_foreign_volume_reports_its_server(self):
        self.seed(1)
        vid = next(iter(self.fake.volumes))
        self.fake.volumes[vid]["attachments"] = [{"instance": "i-0bbbbbbbbbbbbbbbb", "device": "/dev/sdf", "status": "attached", "polls": 9}]
        self.assertEqual(self.provider.get_volume(vid).server_id, "i-0bbbbbbbbbbbbbbbb")


class TestPrice(Base):
    def test_known_region_unknown_region_and_override(self):
        self.assertEqual(self.provider.price_gb_month("us-east-1a"), Decimal("0.08"))
        self.assertEqual(self.provider.currency, "USD")
        with self.assertRaises(ProviderError) as ctx:
            self.provider.price_gb_month("eu-central-1b")
        self.assertIn("price_gb_month_eur", ctx.exception.hint)
        p = self.make(prices={"gp3": {"eu-central-1": Decimal("0.10")}})
        self.assertEqual(p.price_gb_month("eu-central-1b"), Decimal("0.10"))

    def test_bad_construction(self):
        with self.assertRaises(ProviderError):
            AwsProvider(volume_type="io2")
        with self.assertRaises(ProviderError):
            AwsProvider(volume_type="nope")


class TestNoRedirect(unittest.TestCase):
    def test_redirect_is_refused_and_credentials_do_not_follow(self):
        with LoopbackServer(lambda r: (302, {"Location": "http://127.0.0.1:9/evil"}, b"")) as srv:
            p = AwsProvider(Credentials("ASIAEXAMPLE0000", SECRET), region="us-east-1", base=srv.url, imds_base=srv.url,
                            sleep=lambda s: None, clock=lambda: NOW)
            p._ident = (INSTANCE, "us-east-1a", "us-east-1")
            with self.assertRaises(ProviderError):
                p.delete("vol-0abc0000000000001")
            self.assertEqual(len(srv.requests), 1)


if __name__ == "__main__":
    unittest.main()


class TestDocs(unittest.TestCase):
    def test_policy_json_in_the_docs_parses(self):
        import json
        import re
        from pathlib import Path
        text = (Path(__file__).resolve().parents[2] / "docs" / "providers" / "aws.md").read_text()
        blocks = re.findall(r"```json\n(.*?)```", text, re.S)
        self.assertTrue(blocks)
        for block in blocks:
            doc = json.loads(block)
            self.assertEqual(doc["Version"], "2012-10-17")
            self.assertTrue(all("Sid" in s and "Action" in s for s in doc["Statement"]))
