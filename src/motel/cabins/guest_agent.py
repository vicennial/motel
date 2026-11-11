#!/usr/bin/python3 -I
"""motel-guest: admission gate, activity, readiness and managed jobs on a cabin (standard library only).

  gate (forced command of every cabin key) | run B64JSON | sample (root timer) | expire (root timer) | seal (root)
  | ctl (forced command of cabinctl: activity | ready | seal | set-expiry LABEL)

Gate and seal share a lock: a session admitted before a seal makes it refuse, one arriving after is refused.
Activity is any managed job, admitted session, login-session process or running container; an unreadable
source, unknown sample or sampling gap restarts the verified idle interval.
"""
import base64
import calendar
import fcntl
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

SCHEMA = 'motel-guest/v1'
ROOT = '/var/lib/motel-guest'
LEASE_FILE = '/etc/motel-cabin/lease.json'
HEARTBEAT_S = 60
JOB_STALE_S = 180
SAMPLE_GAP_S = 150
SAMPLE_MAX_AGE_S = 180
IDLE_S = 1800            # floor; the lease file may only raise it
POWEROFF_MARGIN_S = 120   # after expiry + grace: the reaper normally deletes the cabin before this
GUEST = '/usr/local/bin/motel-guest'
WORK_DIR = 'work'      # ~/work: where `motel cabin run` syncs the checkout


BOOT_ERROR_RE = re.compile(r'(E: |Err:|W: |dpkg: error|failed: |tour: |sudo: )')

class Guest:
    def __init__(self, root=ROOT, proc='/proc', lease_file=LEASE_FILE, now=time.time, user='cabin',
                 uid_of=lambda name: pwd.getpwnam(name).pw_uid, docker=None, poweroff=None, pid=os.getpid,
                 seal_runner=None, security_probe=None):
        self.root, self.proc, self.lease_file, self.now = root, proc, lease_file, now
        self.user, self.uid_of, self.pid = user, uid_of, pid
        self.docker = docker if docker is not None else self._docker
        self.poweroff = poweroff or (lambda: subprocess.run(['/usr/bin/systemctl', 'poweroff'], check=False))
        self.seal_runner = seal_runner or self._seal_via_sudo
        self.security_probe = security_probe or self._security_via_sudo
        self.jobs = os.path.join(root, 'jobs')
        self.sessions = os.path.join(root, 'sessions')
        self.gate_dir = os.path.join(root, 'gate')

    def _read(self, path, default=None):
        try:
            with open(path, encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def _write(self, path, doc, mode=0o644):
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(doc, handle, sort_keys=True)
        os.chmod(tmp, mode)
        os.replace(tmp, path)

    def _docker(self):
        if not shutil.which('docker'):
            return 0
        proc = subprocess.run(['docker', 'ps', '-q'], capture_output=True, timeout=20, check=False)
        if proc.returncode != 0:
            raise OSError('docker query failed')
        return len(proc.stdout.split())

    def _alive(self, pid):
        return isinstance(pid, int) and os.path.exists(os.path.join(self.proc, str(pid)))

    def _admission_lock(self):
        fd = os.open(os.path.join(self.gate_dir, 'admission.lock'), os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def sealed(self):
        return self._read(os.path.join(self.gate_dir, 'sealed.json'))

    def last_admission(self):
        doc = self._read(os.path.join(self.sessions, 'last-admission.json')) or {}
        value = doc.get('at')
        return value if isinstance(value, (int, float)) else None

    def idle_threshold(self):
        value = (self._read(self.lease_file) or {}).get('idle_s')
        return max(IDLE_S, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else IDLE_S

    def expiry(self):
        doc = self._read(os.path.join(self.root, 'ctl', 'expiry.json')) or {}
        label = doc.get('expires_label')
        try:
            if label:
                return calendar.timegm(time.strptime(label, '%Y%m%dT%H%M%SZ'))
            lease = self._read(self.lease_file) or {}
            if lease.get('expires'):
                return calendar.timegm(time.strptime(lease['expires'], '%Y-%m-%dT%H:%M:%SZ'))
        except (ValueError, TypeError):
            return None
        return None

    def session_processes(self):
        uid = str(self.uid_of(self.user))
        found = []
        for name in os.listdir(self.proc):
            if not name.isdigit():
                continue
            try:
                with open(os.path.join(self.proc, name, 'loginuid'), encoding='ascii') as handle:
                    if handle.read().strip() == uid:
                        found.append(int(name))
            except OSError:
                continue
        return found

    def _live_records(self, directory, fresh_s=None):
        out = []
        now = self.now()
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return out
        for name in names:
            if not name.endswith('.json') or name.startswith('last-'):
                continue
            path = os.path.join(directory, name)
            doc = self._read(path)
            try:
                fresh = fresh_s is None or now - os.stat(path).st_mtime < fresh_s
            except OSError:
                continue
            if doc and fresh and self._alive(doc.get('pid')):
                out.append(doc.get('id', name))
        return out

    def current(self, docker=True, jobs=True):
        reasons, unknown = [], []
        try:
            if jobs and self._live_records(self.jobs, JOB_STALE_S):
                reasons.append('managed_job')
        except OSError:
            unknown.append('jobs')
        try:
            if self._live_records(self.sessions):
                reasons.append('admitted_session')
        except OSError:
            unknown.append('sessions')
        try:
            if self.session_processes():
                reasons.append('ssh_session_or_process')
        except (OSError, KeyError):
            unknown.append('processes')
        if docker:
            try:
                if self.docker():
                    reasons.append('containers')
            except (OSError, subprocess.SubprocessError):
                unknown.append('containers')
        return reasons, unknown

    def sample(self):
        now = self.now()
        prev = self._read(os.path.join(self.root, 'activity.json'), {}) or {}
        reasons, unknown = self.current(docker=True)
        prev_at = prev.get('sampled_at')
        gap = not isinstance(prev_at, (int, float)) or now - prev_at > SAMPLE_GAP_S or now < prev_at
        admitted = self.last_admission()
        if reasons:
            last_active, since = now, None
        elif unknown:
            last_active, since = prev.get('last_active', now), None
        else:
            last_active = prev.get('last_active', now)
            since = prev.get('verified_idle_since')
            if gap or since is None or (admitted is not None and isinstance(prev_at, (int, float))
                                        and admitted >= prev_at):
                since = now
        doc = {'schema': SCHEMA, 'sampled_at': now, 'active': bool(reasons), 'reasons': reasons,
               'unknown': unknown, 'last_active': last_active, 'verified_idle_since': since}
        self._write(os.path.join(self.root, 'activity.json'), doc)
        return doc

    def hard_expiry(self):
        expires = self.expiry()
        if expires is None:
            return None
        lease = self._read(self.lease_file) or {}
        grace = lease.get('grace_s', 600)
        grace = int(grace) if isinstance(grace, (int, float)) and not isinstance(grace, bool) else 600
        return expires + grace + POWEROFF_MARGIN_S

    def expire_check(self):
        limit = self.hard_expiry()
        if limit is not None and self.now() > limit:
            self.poweroff()
            return {'schema': SCHEMA, 'expired': True, 'powered_off': True}
        return {'schema': SCHEMA, 'expired': False, 'powered_off': False}

    def activity(self):
        now = self.now()
        doc = self._read(os.path.join(self.root, 'activity.json'))
        if not doc or not isinstance(doc.get('sampled_at'), (int, float)):
            return {'schema': SCHEMA, 'sample_age_s': None, 'active': None, 'unknown': ['sampler']}
        reasons, unknown = self.current(docker=False, jobs=False)
        reasons = sorted(set(doc.get('reasons') or []) | set(reasons))
        unknown = sorted(set(doc.get('unknown') or []) | set(unknown))
        since = doc.get('verified_idle_since')
        admitted = self.last_admission()
        if since is None or (admitted is not None and admitted >= since):
            idle = 0.0
        else:
            idle = max(0.0, now - since)
        out = {'schema': SCHEMA, 'sample_age_s': round(now - doc['sampled_at'], 1), 'active': bool(reasons),
               'reasons': reasons, 'unknown': unknown, 'idle_seconds': 0.0 if reasons else round(idle, 1)}
        if since is None and not reasons and not unknown:
            out['unknown'] = ['idle_interval']
        sealed = self.sealed()
        if sealed:
            out['sealed_at'] = sealed.get('sealed_at')
        return out

    def gate(self, original, exec_fn=os.execv, shell='/bin/bash', err=None):
        err = err or sys.stderr
        try:
            fd = self._admission_lock()
        except OSError:
            print('motel-guest: admission gate unavailable; session refused', file=err)
            return 75
        try:
            if self.sealed() is not None:
                print('motel-guest: this lease was sealed for idle cleanup; new sessions are refused', file=err)
                return 75
            now, pid = self.now(), self.pid()
            os.makedirs(self.sessions, mode=0o755, exist_ok=True)
            self._write(os.path.join(self.sessions, f'{pid}.json'),
                        {'id': f'session-{pid}', 'pid': pid, 'admitted_at': now})
            self._write(os.path.join(self.sessions, 'last-admission.json'), {'at': now})
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        try:
            os.makedirs(os.path.join(os.path.expanduser('~'), WORK_DIR), mode=0o755, exist_ok=True)
        except OSError:
            pass
        if original:
            exec_fn(shell, [shell, '-c', original])
        else:
            exec_fn(shell, ['-' + os.path.basename(shell)])
        return 0

    def seal(self):
        fd = self._admission_lock()
        try:
            existing = self.sealed()
            if existing is not None:
                return dict(existing, schema=SCHEMA, sealed=True, already=True)
            now = self.now()
            problems = []
            doc = self._read(os.path.join(self.root, 'activity.json')) or {}
            sampled = doc.get('sampled_at')
            since = doc.get('verified_idle_since')
            if not isinstance(sampled, (int, float)) or now - sampled > SAMPLE_MAX_AGE_S or now < sampled:
                problems.append('activity sample missing or stale')
            if not isinstance(since, (int, float)):
                problems.append('no continuous verified idle interval')
            elif now - since < self.idle_threshold():
                problems.append(f'verified idle shorter than {self.idle_threshold() // 60} min')
            reasons, unknown = self.current(docker=True, jobs=True)
            if reasons:
                problems.append('busy: ' + ','.join(reasons))
            if unknown:
                problems.append('unknown: ' + ','.join(unknown))
            admitted = self.last_admission()
            if admitted is not None and isinstance(since, (int, float)) and admitted >= since:
                problems.append('session admitted during the idle interval')
            if problems:
                return {'schema': SCHEMA, 'sealed': False, 'reason': '; '.join(problems)}
            sealed = {'sealed_at': now, 'verified_idle_seconds': round(now - since, 1)}
            self._write(os.path.join(self.gate_dir, 'sealed.json'), sealed)
            return dict(sealed, schema=SCHEMA, sealed=True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _seal_via_sudo(self):
        proc = subprocess.run(['/usr/bin/sudo', '-n', GUEST, 'seal'], capture_output=True, timeout=60, check=False)
        try:
            doc = json.loads(proc.stdout)
        except ValueError:
            doc = None
        if proc.returncode != 0 or not isinstance(doc, dict):
            return {'schema': SCHEMA, 'sealed': False, 'reason': 'unknown: seal helper failed'}
        return doc

    def listeners(self):
        out = []
        for name, version in (('tcp', 4), ('tcp6', 6)):
            try:
                with open(os.path.join(self.proc, 'net', name), encoding='ascii') as handle:
                    rows = handle.read().splitlines()[1:]
            except OSError:
                continue
            for row in rows:
                fields = row.split()
                if len(fields) < 4 or fields[3] != '0A':
                    continue
                addr_hex, port_hex = fields[1].split(':')
                raw = bytes.fromhex(addr_hex)
                if version == 4:
                    addr = '.'.join(str(b) for b in raw[::-1])
                else:
                    words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
                    addr = str(ipaddress.IPv6Address(b''.join(words)))
                out.append({'addr': addr, 'port': int(port_hex, 16)})
        return out

    def _security_via_sudo(self):
        proc = subprocess.run(['/usr/bin/sudo', '-n', GUEST, 'inspect'], capture_output=True,
                              text=True, input=os.environ.get('SSH_CONNECTION', ''), timeout=20, check=True)
        return json.loads(proc.stdout)

    @staticmethod
    def trusted(paths):
        return all(os.stat(p).st_uid == 0 and not os.stat(p).st_mode & 0o022 for p in paths)

    def tour_forward_port(self, config='/etc'):
        d = self._read(config + '/motel-cabin/tour.json')
        p = isinstance(d, dict) and d.get('profile') == 'tour-guest' and not d.get('own_login') and d.get('concierge_port')
        return p if type(p) is int and 1024 <= p <= 65535 else None

    def inspect_security(self, run=subprocess.run, config='/etc', connection=None):
        result = {'nft': 'missing', 'forwarding_disabled': False, 'gate': 'missing', 'forward_port': None}

        def output(argv):
            return run(argv, capture_output=True, text=True, timeout=10, check=True).stdout

        try:
            actual = json.loads(output(['/usr/sbin/nft', '-j', '-s', 'list', 'table', 'inet', 'cabin']))
            expected = self._read(os.path.join(config, 'motel-cabin', 'expected-nft.json'))
            def clean(value):
                if isinstance(value, dict):
                    return {key: clean(item) for key, item in value.items() if key != 'handle'}
                if isinstance(value, list):
                    return [clean(item) for item in value]
                return value
            def rules(doc):
                return [clean(item) for item in doc['nftables'] if 'metainfo' not in item]
            if expected and rules(actual) and rules(actual) == rules(expected):
                result['nft'] = 'loaded'
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            pass
        try:
            remote, remote_port, local, local_port = (connection or '').split()
            remote, local = str(ipaddress.IPv4Address(remote)), str(ipaddress.IPv4Address(local))
            if not (remote_port.isdigit() and local_port.isdigit()
                    and 0 < int(remote_port) < 65536 and int(local_port) == 22):
                raise ValueError('invalid SSH connection context')
            context = f'user=cabin,host={remote},addr={remote},laddr={local},lport={local_port}'
            ssh = set(output(['/usr/sbin/sshd', '-T', '-C', context]).splitlines())
            strict = {'allowtcpforwarding no', 'allowagentforwarding no', 'allowstreamlocalforwarding no',
                      'x11forwarding no', 'permittunnel no', 'gatewayports no'}
            result['forwarding_disabled'] = strict <= ssh
            port = self.tour_forward_port(config)
            opts, fwd = 'restrict,pty', None
            if port:
                opts += f',port-forwarding,permitlisten="127.0.0.1:{port}"'
                tcp = {x for x in ssh if x.startswith(('allowtcpforwarding ', 'permitlisten '))}
                if strict - {'allowtcpforwarding no'} <= ssh and tcp == {'allowtcpforwarding remote', f'permitlisten 127.0.0.1:{port}'}:
                    fwd = port
            key = os.path.join(config, 'ssh', 'cabin-keys', 'cabin')
            with open(key, encoding='utf-8') as handle:
                lines = handle.read().splitlines()
            paths = (key, os.path.join(config, 'ssh', 'cabin-keys', 'cabinctl'), os.path.dirname(key))
            trusted = self.trusted(paths)
            sudoers = os.path.join(config, 'sudoers.d', 'cabinctl')
            output(['/usr/sbin/visudo', '-cqf', sudoers])
            allowed = {line.split(' ', 1)[1] for line in ssh if line.startswith('allowusers ')}
            if (allowed == {'cabin', 'cabinctl'} and trusted and len(lines) == 1 and lines[0].startswith(
                    opts + ',command="/usr/local/bin/motel-guest gate" ssh-ed25519 ')
                    and {'authorizedkeysfile /etc/ssh/cabin-keys/%u', 'authorizedkeyscommand none',
                         'passwordauthentication no', 'kbdinteractiveauthentication no',
                         'permitrootlogin no', 'forcecommand none',
                         'authenticationmethods publickey', 'usedns no'} <= ssh):
                result['gate'] = 'enforced'
                result['forward_port'] = fwd
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return result

    def boot(self, log='/var/log/cabin-bootstrap.log', status='/run/cloud-init/status.json', stage_file=None):
        """Where an unfinished bootstrap is: its current stage, cloud-init's verdict, the last line it printed. The stage comes
        from the small file each stage writes (a chatty stage such as apt would push its marker out of the log tail); the
        last 'stage:' marker in the tail is the fallback."""
        try:
            with open(log, 'rb') as handle:
                handle.seek(max(0, handle.seek(0, 2) - 16384))
                lines = [x for x in (y.strip() for y in handle.read().decode('ascii', 'replace').splitlines()) if x]
        except OSError:
            lines = []
        stages = [x[7:] for x in lines if x.startswith('stage: ')]
        try:
            with open(stage_file or os.path.join(self.root, 'boot-stage'), 'rb') as handle:
                named = handle.read(64).decode('ascii', 'replace').strip()
        except OSError:
            named = ''
        if re.fullmatch(r'[a-z][a-z0-9-]{0,47}', named):
            stages = [named]
        v1 = (self._read(status) or {}).get('v1')
        v1 = v1 if isinstance(v1, dict) else {}
        parts = [x for x in v1.values() if isinstance(x, dict)] + [v1]
        final = v1.get('modules-final')
        verdict = 'error' if any(x.get('errors') for x in parts) else 'done' if isinstance(final, dict) and final.get('finished') \
            else 'running' if v1 else 'unknown'
        clean = lambda x, n: ''.join(c if ' ' <= c <= '~' else '?' for c in x)[:n]
        out = {'stage': stages[-1][:48] if stages else None, 'cloud_init': verdict,
               'last': clean(lines[-1], 120) if lines else None}
        # apt and the bootstrap name their own failures; the last few such lines say more than the last line alone.
        errors = [clean(x, 160) for x in lines if BOOT_ERROR_RE.match(x)][-3:]
        if errors:
            out['errors'] = errors
        return out

    def ready(self):
        doc = self._read(os.path.join(self.root, 'ready.json'), {}) or {}
        try:
            security = self.security_probe()
            if not isinstance(security, dict):
                security = {}
        except (OSError, ValueError, subprocess.SubprocessError):
            security = {}
        reply = {'schema': SCHEMA, 'bootstrap': doc.get('bootstrap', 'pending'), 'tailnet_ip': doc.get('tailnet_ip'),
                 'nft': security.get('nft', 'missing'), 'forwarding_disabled': security.get('forwarding_disabled', False),
                 'gate': security.get('gate', 'missing'), 'disk': doc.get('disk', 'none'),
                 'forward_port': security.get('forward_port'),
                 'tcp_listeners': self.listeners()}
        if reply['bootstrap'] != 'done':
            reply['boot'] = self.boot()
        return reply

    def set_expiry(self, label):
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', label or ''):
            raise SystemExit('invalid expiry label')
        self._write(os.path.join(self.root, 'ctl', 'expiry.json'), {'expires_label': label})
        return {'schema': SCHEMA, 'expires_label': label}

    def ctl(self, command):
        words = (command or '').split()
        if words == ['activity']:
            return self.activity()
        if words == ['ready']:
            return self.ready()
        if words == ['seal']:
            return self.seal_runner()
        if len(words) == 2 and words[0] == 'set-expiry':
            return self.set_expiry(words[1])
        raise SystemExit('motel-guest: command not allowed')

    def run(self, encoded, popen=subprocess.Popen):
        try:
            payload = json.loads(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError):
            print('motel-guest: unreadable command', file=sys.stderr)
            return 64
        cwd = None
        argv = payload
        if isinstance(payload, dict):
            argv, cwd = payload.get('argv'), payload.get('cwd')
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) and '\0' not in a for a in argv)):
            print('motel-guest: command must be a non-empty argument list', file=sys.stderr)
            return 64
        if cwd is not None:
            parts = cwd.split('/') if isinstance(cwd, str) else ['..']
            if not cwd or cwd.startswith('/') or '..' in parts or '\0' in cwd:
                print('motel-guest: working directory must be a relative path without ..', file=sys.stderr)
                return 64
            cwd = os.path.join(os.path.expanduser('~'), cwd)
            try:
                os.makedirs(cwd, mode=0o755, exist_ok=True)
            except OSError as exc:
                print(f'motel-guest: cannot create the working directory: {exc.strerror}', file=sys.stderr)
                return 73
        expires = self.expiry()
        if expires is not None and self.now() >= expires:
            print('motel-guest: lease expired; new jobs are refused', file=sys.stderr)
            return 75
        if self.sealed() is not None:
            print('motel-guest: lease sealed for idle cleanup; new jobs are refused', file=sys.stderr)
            return 75
        os.makedirs(self.jobs, mode=0o700, exist_ok=True)
        job = f'{int(self.now())}-{os.getpid()}'
        path = os.path.join(self.jobs, job + '.json')
        try:
            child = popen(argv, **({'cwd': cwd} if cwd else {}))
        except OSError as exc:
            print(f'motel-guest: cannot start command: {exc.strerror}', file=sys.stderr)
            return 127
        self._write(path, {'id': job, 'pid': child.pid, 'started': self.now()}, mode=0o600)
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_S):
                try:
                    os.utime(path)
                except OSError:
                    pass
        threading.Thread(target=beat, daemon=True).start()
        forward = {}
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                forward[sig] = signal.signal(sig, lambda s, _f: child.send_signal(s))
            except ValueError:
                pass
        try:
            rc = child.wait()
        finally:
            stop.set()
            for sig, old in forward.items():
                signal.signal(sig, old)
            try:
                os.unlink(path)
            except OSError:
                pass
        return rc if rc >= 0 else 128 - rc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    guest = Guest()
    if argv[:1] == ['run'] and len(argv) == 2:
        return guest.run(argv[1])
    if argv == ['gate']:
        return guest.gate(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    if argv == ['sample']:
        guest.sample()
        return 0
    if argv == ['expire']:
        guest.expire_check()
        return 0
    if argv == ['inspect'] and os.geteuid() == 0:
        print(json.dumps(guest.inspect_security(connection=sys.stdin.read(256)), sort_keys=True))
        return 0
    if argv == ['seal'] and os.geteuid() == 0:
        print(json.dumps(guest.seal(), sort_keys=True))
        return 0
    if argv == ['ctl']:
        print(json.dumps(guest.ctl(os.environ.get('SSH_ORIGINAL_COMMAND', '')), sort_keys=True))
        return 0
    print('usage: motel-guest gate | run B64JSON | sample | expire | seal | ctl', file=sys.stderr)
    return 64


if __name__ == '__main__':
    sys.exit(main())
