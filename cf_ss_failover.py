#!/usr/bin/env python3
"""gzqh: nftables DNAT failover daemon and control CLI.

Single source of truth for:
  * failover logic (``run`` / ``once``)
  * manual modes (``mode auto|hold-primary|hold-backup``)
  * primary target management (``set-primary``, ``take-port``)
  * systemd unit rendering (``render-unit``) used by install.sh

Configuration lives in the systemd unit as ``Environment=KEY=VALUE`` lines.
The CLI reads the unit file directly, so menu actions always use the same
config the daemon runs with. Environment variables override the unit file.

Compatible with Python 3.6+.
"""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time

VERSION = '2.0.0'
SERVICE_FILE = os.environ.get('GZQH_SERVICE_FILE', '/etc/systemd/system/ss-failover.service')
STATE_DIR = os.environ.get('GZQH_STATE_DIR', '/opt/ss-failover')
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
LOG_FILE = os.path.join(STATE_DIR, 'failover.log')
LOCK_FILE = os.path.join(STATE_DIR, 'lock')
PRIMARY_FILE = os.path.join(STATE_DIR, 'primary.json')  # survives state.json loss

# key -> (default, kind). Order is the order written into the unit file.
CONFIG_SPEC = [
    ('FORWARD_PORT', '10001', 'port'),
    ('BACKUP_LIST', '', 'backups'),
    ('CHECK_TIMEOUT', '0.06', 'posfloat'),
    ('CHECK_INTERVAL', '0.4', 'posfloat'),
    ('FAIL_THRESHOLD', '1', 'posint'),
    ('BACKUP_CHECK_INTERVAL', '1', 'posfloat'),
    ('RECOVER_THRESHOLD', '10', 'posint'),
    ('HEARTBEAT_INTERVAL', '60', 'posfloat'),
    ('DNS_REFRESH_INTERVAL', '300', 'posfloat'),
    ('NFT_FAMILY', 'ip', 'name'),
    ('NFT_TABLE', 'nat', 'name'),
    ('NFT_CHAIN', 'prerouting', 'name'),
    ('NFT_POSTROUTING_CHAIN', 'postrouting', 'name'),
]
CONFIG_KEYS = [k for k, _, _ in CONFIG_SPEC]
LEGACY_KEYS = ['BACKUP_HOST', 'BACKUP_IP', 'BACKUP_PORT', 'RECOVER_INTERVAL', 'PRIMARY_STABLE_COUNT']

EXIT_CONFIG = 2  # systemd: RestartPreventExitStatus=2

HOST_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')
NAME_RE = re.compile(r'^[A-Za-z0-9_-]+$')
IPV4_RE = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
HANDLE_RE = re.compile(r'#\s*handle\s+(\d+)\s*$')


class ConfigError(Exception):
    pass


class Fatal(Exception):
    pass


# ---------------------------------------------------------------- utilities

def is_ipv4(s):
    m = IPV4_RE.match(s or '')
    return bool(m) and all(0 <= int(g) <= 255 for g in m.groups())


def valid_port(v):
    try:
        n = int(str(v))
    except ValueError:
        return False
    return str(n) == str(v).strip() and 1 <= n <= 65535


def valid_posfloat(v):
    try:
        return float(v) > 0 and re.match(r'^[0-9]+(\.[0-9]+)?$', str(v).strip()) is not None
    except ValueError:
        return False


def valid_posint(v):
    return re.match(r'^[0-9]+$', str(v).strip()) is not None and int(v) >= 1


def parse_hostport(item):
    item = item.strip()
    if item.count(':') != 1:
        raise ConfigError('备用线路格式应为 host:port，收到: %r' % item)
    host, port = item.split(':', 1)
    host = host.strip()
    port = port.strip()
    if not host or not (is_ipv4(host) or (HOST_RE.match(host) and not re.match(r'^[0-9.]+$', host))):
        raise ConfigError('无效的主机: %r' % host)
    if not valid_port(port):
        raise ConfigError('无效的端口: %r' % port)
    return host, int(port)


def resolve_ipv4(host):
    if is_ipv4(host):
        return host
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return None
    for info in infos:
        return info[4][0]
    return None


def now():
    return time.time()


# ------------------------------------------------------------------ config

def read_unit_env(path=SERVICE_FILE):
    env = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('Environment='):
                    continue
                body = line[len('Environment='):].strip()
                if len(body) >= 2 and body[0] == body[-1] == '"':
                    body = body[1:-1]
                if '=' not in body:
                    continue
                k, v = body.split('=', 1)
                env[k.strip()] = v.strip()
    except (IOError, OSError):
        pass
    return env


def migrate_legacy(env):
    """Fold legacy keys into the current schema (returns a new dict)."""
    env = dict(env)
    if not env.get('BACKUP_LIST'):
        host = env.get('BACKUP_HOST') or env.get('BACKUP_IP')
        if host and host not in ('BACKUP_HOST_PLACEHOLDER',):
            env['BACKUP_LIST'] = '%s:%s' % (host, env.get('BACKUP_PORT') or env.get('FORWARD_PORT') or '10001')
    if 'BACKUP_CHECK_INTERVAL' not in env and env.get('RECOVER_INTERVAL'):
        env['BACKUP_CHECK_INTERVAL'] = env['RECOVER_INTERVAL']
    # drop placeholder / loopback-placeholder backups written by old installers
    if env.get('BACKUP_LIST'):
        items = [i.strip() for i in env['BACKUP_LIST'].split(',') if i.strip()]
        items = [i for i in items if not i.startswith('BACKUP_HOST_PLACEHOLDER')]
        env['BACKUP_LIST'] = ','.join(items)
    for k in LEGACY_KEYS:
        env.pop(k, None)
    return env


def normalize_backup_list(raw):
    items = []
    seen = set()
    for part in (raw or '').split(','):
        if not part.strip():
            continue
        host, port = parse_hostport(part)
        spec = '%s:%d' % (host, port)
        if spec in seen:
            continue
        seen.add(spec)
        items.append(spec)
    return ','.join(items)


def validate_value(key, value):
    kind = dict((k, t) for k, _, t in CONFIG_SPEC)[key]
    value = str(value).strip()
    if kind == 'port' and not valid_port(value):
        raise ConfigError('%s 必须是 1-65535 的整数，当前: %r' % (key, value))
    if kind == 'posfloat' and not valid_posfloat(value):
        raise ConfigError('%s 必须是大于 0 的数字，当前: %r' % (key, value))
    if kind == 'posint' and not valid_posint(value):
        raise ConfigError('%s 必须是大于等于 1 的整数，当前: %r' % (key, value))
    if kind == 'name' and not NAME_RE.match(value):
        raise ConfigError('%s 含非法字符: %r' % (key, value))
    if kind == 'backups':
        value = normalize_backup_list(value)
    return value


def load_config(strict=True):
    env = migrate_legacy(read_unit_env())
    for k in CONFIG_KEYS + LEGACY_KEYS:
        if k in os.environ:
            env[k] = os.environ[k]
    env = migrate_legacy(env)
    cfg = {}
    errors = []
    for key, default, _ in CONFIG_SPEC:
        raw = env.get(key, default)
        try:
            cfg[key] = validate_value(key, raw)
        except ConfigError as e:
            errors.append(str(e))
            cfg[key] = default
    if errors and strict:
        raise ConfigError('; '.join(errors))
    return Config(cfg)


class Config(object):
    def __init__(self, raw):
        self.raw = raw
        self.port = int(raw['FORWARD_PORT'])
        self.check_timeout = float(raw['CHECK_TIMEOUT'])
        self.check_interval = float(raw['CHECK_INTERVAL'])
        self.fail_threshold = int(raw['FAIL_THRESHOLD'])
        self.backup_interval = float(raw['BACKUP_CHECK_INTERVAL'])
        self.recover_threshold = int(raw['RECOVER_THRESHOLD'])
        self.heartbeat = float(raw['HEARTBEAT_INTERVAL'])
        self.dns_refresh = float(raw['DNS_REFRESH_INTERVAL'])
        self.family = raw['NFT_FAMILY']
        self.table = raw['NFT_TABLE']
        self.chain = raw['NFT_CHAIN']
        self.post_chain = raw['NFT_POSTROUTING_CHAIN']
        self.backup_specs = [s for s in raw['BACKUP_LIST'].split(',') if s]
        self.backups = []  # list of dicts: spec, host, port, ip
        self._resolved_at = 0
        self.resolve(force=True)

    def resolve(self, force=False):
        if not force and now() - self._resolved_at < self.dns_refresh:
            return
        old = dict((b['spec'], b['ip']) for b in self.backups)
        out = []
        for spec in self.backup_specs:
            host, port = parse_hostport(spec)
            ip = resolve_ipv4(host)
            if ip is None and spec in old:
                ip = old[spec]  # keep last good resolution on transient DNS failure
            out.append({'spec': spec, 'host': host, 'port': port, 'ip': ip})
        self.backups = out
        self._resolved_at = now()

    def backup_target(self, i):
        b = self.backups[i]
        return (b['ip'], b['port']) if b['ip'] else None

    def find_backup(self, target):
        for i, b in enumerate(self.backups):
            if b['ip'] and (b['ip'], b['port']) == tuple(target):
                return i
        return None

    def find_backup_spec(self, spec):
        for i, b in enumerate(self.backups):
            if b['spec'] == spec:
                return i
        return None


def render_unit(env, python_bin='/usr/bin/python3', script='/opt/cf_ss_failover.py'):
    lines = [
        '[Unit]',
        'Description=gzqh nft failover (entry port %s)' % env['FORWARD_PORT'],
        'After=network-online.target nftables.service',
        'Wants=network-online.target',
        '',
        '[Service]',
        'Type=simple',
    ]
    for key in CONFIG_KEYS:
        lines.append('Environment=%s=%s' % (key, env[key]))
    lines += [
        'ExecStart=%s %s run' % (python_bin, script),
        'Restart=always',
        'RestartSec=3',
        'RestartPreventExitStatus=%d' % EXIT_CONFIG,
        '',
        '[Install]',
        'WantedBy=multi-user.target',
        '',
    ]
    return '\n'.join(lines)


# --------------------------------------------------------------- logging

class Logger(object):
    def __init__(self, echo_all=False):
        self.echo_all = echo_all
        self._last = {}

    def write(self, msg, important=False):
        line = '[%s] %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
        if important or self.echo_all:
            print(line, flush=True)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except (IOError, OSError):
            pass

    def throttled(self, key, msg, every=60, important=True):
        t = now()
        if t - self._last.get(key, 0) >= every:
            self._last[key] = t
            self.write(msg, important)


# ----------------------------------------------------------------- state

def default_state():
    return {
        'version': 2,
        'mode': 'auto',          # auto | hold_primary | hold_backup
        'hold_backup': None,     # backup spec "host:port" when mode == hold_backup
        'primary': None,         # [ip, port]
        'active': 'unknown',     # primary | backup:<i> | orphan | unknown
        'last_applied': None,    # [ip, port] last target observed/applied by gzqh
        'fail_count': 0,
        'recover_count': 0,
        'last_change': 0,
    }


def load_state(cfg=None):
    st = default_state()
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (IOError, OSError, ValueError):
        data = {}
    if data.get('version') == 2:
        st.update(data)
    else:
        # migrate v1
        if isinstance(data.get('primary_target'), list) and len(data['primary_target']) == 2:
            st['primary'] = [data['primary_target'][0], int(data['primary_target'][1])]
        if data.get('manual_hold') == 'backup' and cfg is not None and cfg.backups:
            idx = int(data.get('backup_index') or 0)
            idx = max(0, min(idx, len(cfg.backups) - 1))
            st['mode'] = 'hold_backup'
            st['hold_backup'] = cfg.backups[idx]['spec']
        for k in ('fail_count', 'recover_count', 'last_change'):
            if isinstance(data.get(k), int):
                st[k] = data[k]
    if not st.get('primary'):
        try:
            with open(PRIMARY_FILE, encoding='utf-8') as f:
                st['primary'] = json.load(f)
        except (IOError, OSError, ValueError):
            pass
    if st['mode'] not in ('auto', 'hold_primary', 'hold_backup'):
        st['mode'] = 'auto'
    if st['primary'] is not None:
        try:
            st['primary'] = [str(st['primary'][0]), int(st['primary'][1])]
            if not is_ipv4(st['primary'][0]):
                st['primary'] = None
        except (TypeError, ValueError, IndexError):
            st['primary'] = None
    return st


def atomic_write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=True, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    if st.get('primary'):
        try:
            with open(PRIMARY_FILE, encoding='utf-8') as f:
                same = json.load(f) == st['primary']
        except (IOError, OSError, ValueError):
            same = False
        if not same:
            atomic_write_json(PRIMARY_FILE, st['primary'])
    out = dict(st)
    out['primary_target'] = st.get('primary')  # legacy readers
    atomic_write_json(STATE_FILE, out)


class Lock(object):
    def __init__(self, timeout=10):
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.fd = open(LOCK_FILE, 'a')
        deadline = now() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (IOError, OSError):
                if now() > deadline:
                    raise Fatal('等待锁超时: %s' % LOCK_FILE)
                time.sleep(0.02)

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            self.fd.close()


# ------------------------------------------------------------------- nft

def nft(args, stdin=None):
    p = subprocess.Popen(['nft'] + list(args), stdin=subprocess.PIPE if stdin is not None else None,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    try:
        out, err = p.communicate(stdin, timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise Fatal('nft 超时: %s' % ' '.join(args))
    if p.returncode != 0:
        raise Fatal('nft %s 失败: %s' % (' '.join(args) or '-f -', err.strip()))
    return out


def list_chain(cfg, chain):
    return nft(['-a', 'list', 'chain', cfg.family, cfg.table, chain])


def dport_re(proto, port):
    return re.compile(r'\b%s dport %d(?![\d-])' % (proto, port))


DNAT_RE = re.compile(r'\bdnat (?:ip )?to (\d{1,3}(?:\.\d{1,3}){3}):(\d+)(?![\d-])')


def entry_rules(cfg, text=None):
    """All DNAT rules for the entry port, in chain order."""
    if text is None:
        text = list_chain(cfg, cfg.chain)
    rules = []
    pats = {'tcp': dport_re('tcp', cfg.port), 'udp': dport_re('udp', cfg.port)}
    for line in text.splitlines():
        s = line.strip()
        h = HANDLE_RE.search(s)
        if not h:
            continue
        for proto in ('tcp', 'udp'):
            if pats[proto].search(s):
                m = DNAT_RE.search(s)
                if m:
                    rules.append({'proto': proto, 'ip': m.group(1), 'port': int(m.group(2)),
                                  'handle': h.group(1), 'line': s})
                break
    return rules


def current_target(rules):
    for r in rules:
        if r['proto'] == 'tcp':
            return (r['ip'], r['port'])
    return None


MASQ_RE = re.compile(r'\bip daddr (\d{1,3}(?:\.\d{1,3}){3}) (tcp|udp) dport (\d+)\b.*\bmasquerade\b')


def masq_rules(cfg):
    out = []
    try:
        text = list_chain(cfg, cfg.post_chain)
    except Fatal:
        return None
    for line in text.splitlines():
        s = line.strip()
        h = HANDLE_RE.search(s)
        m = MASQ_RE.search(s)
        if h and m:
            out.append({'ip': m.group(1), 'proto': m.group(2), 'port': int(m.group(3)), 'handle': h.group(1)})
    return out


def masq_batch(cfg, ip, port):
    """Batch lines that make the masquerade rules for ip:port exist exactly once."""
    rules = masq_rules(cfg)
    if rules is None:
        return []
    lines = []
    for proto in ('tcp', 'udp'):
        match = [r for r in rules if r['ip'] == ip and r['port'] == port and r['proto'] == proto]
        if not match:
            lines.append('add rule %s %s %s ip daddr %s %s dport %d counter masquerade'
                         % (cfg.family, cfg.table, cfg.post_chain, ip, proto, port))
        for dup in match[1:]:
            lines.append('delete rule %s %s %s handle %s' % (cfg.family, cfg.table, cfg.post_chain, dup['handle']))
    return lines


def apply_target(cfg, target, rules=None, with_udp=None):
    """Atomically point the entry port at target (single nft transaction)."""
    ip, port = target
    if rules is None:
        rules = entry_rules(cfg)
    if with_udp is None:
        with_udp = any(r['proto'] == 'udp' for r in rules) or not rules
    lines = []
    if with_udp:
        lines.append('insert rule %s %s %s udp dport %d counter dnat to %s:%d'
                     % (cfg.family, cfg.table, cfg.chain, cfg.port, ip, port))
    lines.append('insert rule %s %s %s tcp dport %d counter dnat to %s:%d'
                 % (cfg.family, cfg.table, cfg.chain, cfg.port, ip, port))
    for r in rules:
        lines.append('delete rule %s %s %s handle %s' % (cfg.family, cfg.table, cfg.chain, r['handle']))
    lines += masq_batch(cfg, ip, port)
    nft(['-f', '-'], stdin='\n'.join(lines) + '\n')


def ensure_clean(cfg, rules, target):
    """Remove stray duplicate entry rules (keep the first tcp/udp) and fix masquerade."""
    lines = []
    seen = set()
    for r in rules:
        key = r['proto']
        if key in seen or (r['ip'], r['port']) != tuple(target):
            lines.append('delete rule %s %s %s handle %s' % (cfg.family, cfg.table, cfg.chain, r['handle']))
        else:
            seen.add(key)
    if lines:
        # shadowed duplicates: re-apply cleanly so tcp and udp agree
        apply_target(cfg, target, rules)
        return True
    extra = masq_batch(cfg, target[0], target[1])
    if extra:
        nft(['-f', '-'], stdin='\n'.join(extra) + '\n')
    return False


def tcp_ok(ip, port, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        s.close()


# --------------------------------------------------------------- engine

class Engine(object):
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.probe_last = {}
        self.cleaned = False

    def probe(self, target, label):
        ok = tcp_ok(target[0], target[1], self.cfg.check_timeout)
        key = (label, tuple(target))
        if self.probe_last.get(key) != ok or self.log.echo_all:
            self.log.write('%s %s:%d %s' % (label, target[0], target[1], 'OK' if ok else 'FAIL'))
        self.probe_last[key] = ok
        return ok

    def switch(self, st, target, active, reason, rules):
        self.cfg_apply(target, rules)
        st['active'] = active
        st['last_applied'] = list(target)
        st['last_change'] = int(now())
        st['fail_count'] = 0
        st['recover_count'] = 0
        self.log.write('=== 切换 -> %s %s:%d (%s) ===' % (active, target[0], target[1], reason), important=True)

    def cfg_apply(self, target, rules):
        apply_target(self.cfg, target, rules)

    def healthy_backup(self, exclude=None, start=0):
        n = len(self.cfg.backups)
        for k in range(n):
            i = (start + k) % n
            if i == exclude:
                continue
            t = self.cfg.backup_target(i)
            if t and self.probe(t, 'backup#%d' % (i + 1)):
                return i
        return None

    def step(self, st):
        cfg = self.cfg
        cfg.resolve()
        rules = entry_rules(cfg)
        target = current_target(rules)
        if target is None:
            self.log.throttled('norule', '入口 tcp dport %d 没有 dnat 规则，等待规则出现（请先用 nfter 建好转发）' % cfg.port)
            st['active'] = 'unknown'
            return
        if not any(r['proto'] == 'udp' for r in rules):
            self.log.throttled('noudp', '提示: 入口 udp dport %d 没有 dnat 规则，仅切换 tcp' % cfg.port, every=3600, important=False)

        primary = tuple(st['primary']) if st['primary'] else None
        bidx = cfg.find_backup(target)
        if primary is not None and target == primary:
            where = 'primary'
        elif bidx is not None:
            where = 'backup'
        elif primary is None:
            st['primary'] = list(target)
            primary = target
            where = 'primary'
            self.log.write('记录主线: %s:%d' % target, important=True)
        elif st.get('last_applied') and tuple(st['last_applied']) == target:
            where = 'orphan'  # a backup we applied that is no longer in the list
        else:
            st['primary'] = list(target)
            primary = target
            where = 'primary'
            self.log.write('入口目标被外部修改，更新主线为 %s:%d' % target, important=True)
        st['last_applied'] = list(target)
        st['active'] = 'primary' if where == 'primary' else ('backup:%d' % bidx if where == 'backup' else 'orphan')

        if not self.cleaned:
            if ensure_clean(cfg, rules, target):
                self.log.write('已清理入口端口的重复 dnat 规则', important=True)
                rules = entry_rules(cfg)
            self.cleaned = True

        mode = st['mode']
        if mode == 'hold_primary':
            st['fail_count'] = st['recover_count'] = 0
            if where != 'primary':
                self.switch(st, primary, 'primary', '手动锁定主线', rules)
            else:
                self.probe(primary, 'primary')
            return

        if mode == 'hold_backup':
            idx = cfg.find_backup_spec(st.get('hold_backup') or '')
            if idx is None:
                self.log.write('锁定的备用 %s 已不在列表中，恢复自动模式' % st.get('hold_backup'), important=True)
                st['mode'] = 'auto'
                st['hold_backup'] = None
                mode = 'auto'
            else:
                st['fail_count'] = st['recover_count'] = 0
                t = cfg.backup_target(idx)
                if t is None:
                    self.log.throttled('holddns', '锁定的备用 %s 无法解析，保持当前线路' % st['hold_backup'])
                    return
                if target != t:
                    self.switch(st, t, 'backup:%d' % idx, '手动锁定备用#%d' % (idx + 1), rules)
                else:
                    self.probe(t, 'backup#%d' % (idx + 1))
                    if primary:
                        self.probe(primary, 'primary')
                return

        # ---- auto
        if where == 'primary':
            st['recover_count'] = 0
            if self.probe(primary, 'primary'):
                st['fail_count'] = 0
                return
            st['fail_count'] = min(st['fail_count'] + 1, cfg.fail_threshold)
            if st['fail_count'] < cfg.fail_threshold:
                self.log.write('主线失败 %d/%d' % (st['fail_count'], cfg.fail_threshold))
                return
            i = self.healthy_backup()
            if i is None:
                self.log.throttled('nobackup', '主线故障，但没有可用的备用线路，保持主线')
                return
            self.switch(st, cfg.backup_target(i), 'backup:%d' % i, '主线故障', rules)
            return

        # on a backup (or orphan)
        st['fail_count'] = 0
        if not primary:
            self.log.throttled('noprimary', '当前在备用线路上但不知道主线目标，无法自动切回；请在菜单「参数设置 -> 主线目标」设置')
        primary_ok = self.probe(primary, 'primary') if primary else False
        if primary_ok:
            st['recover_count'] += 1
            if st['recover_count'] >= cfg.recover_threshold:
                self.switch(st, primary, 'primary', '主线连续 %d 次恢复' % cfg.recover_threshold, rules)
                return
        else:
            st['recover_count'] = 0
        if where == 'backup':
            cur_ok = self.probe(target, 'backup#%d' % (bidx + 1))
            if cur_ok:
                return
            start = bidx + 1
            exclude = bidx
        else:
            start = 0
            exclude = None
        i = self.healthy_backup(exclude=exclude, start=start)
        if i is not None:
            self.switch(st, cfg.backup_target(i), 'backup:%d' % i,
                        '当前备用不可用' if where == 'backup' else '当前线路已从备用列表移除', rules)
            return
        if primary_ok:
            self.switch(st, primary, 'primary', '备用全部不可用，主线可达，提前切回', rules)
            return
        self.log.throttled('alldown', '主线与所有备用都不可用，保持当前线路')

    def interval(self, st):
        return self.cfg.check_interval if st.get('active') == 'primary' else self.cfg.backup_interval


def run_step(cfg, log, engine=None):
    engine = engine or Engine(cfg, log)
    with Lock():
        st = load_state(cfg)
        try:
            engine.step(st)
        finally:
            save_state(st)
    return engine, st


# ------------------------------------------------------------------ CLI

def cmd_run(cfg, log):
    os.makedirs(STATE_DIR, exist_ok=True)
    inst = open(os.path.join(STATE_DIR, 'daemon.lock'), 'a')
    try:
        fcntl.flock(inst, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        raise Fatal('已有一个 gzqh 守护进程在运行（systemctl status ss-failover）')
    log.write('START v%s port=%d backups=%s mode=%s' % (
        VERSION, cfg.port, ','.join(cfg.backup_specs) or '(无)', load_state(cfg)['mode']), important=True)
    log.write('参数: 主线检测 %gs x%d, 超时 %gs | 备用期间检测 %gs, 恢复 x%d' % (
        cfg.check_interval, cfg.fail_threshold, cfg.check_timeout, cfg.backup_interval, cfg.recover_threshold), important=True)
    engine = Engine(cfg, log)
    last_hb = 0
    while True:
        t0 = now()
        st = None
        try:
            engine, st = run_step(cfg, log, engine)
            interval = engine.interval(st)
        except Exception as e:  # keep the daemon alive on transient errors
            log.throttled('err:' + str(e)[:80], '错误: %s' % e)
            interval = cfg.check_interval
        if st is not None and now() - last_hb >= cfg.heartbeat:
            last_hb = now()
            p = st.get('primary')
            log.write('心跳 mode=%s active=%s primary=%s fail=%d recover=%d' % (
                st['mode'], st['active'], '%s:%s' % tuple(p) if p else '-', st['fail_count'], st['recover_count']))
        time.sleep(max(0.05, interval - (now() - t0)))


def fmt_target(t):
    return '%s:%s' % (t[0], t[1]) if t else '-'


def cmd_status(cfg, args):
    st = load_state(cfg)
    try:
        rules = entry_rules(cfg)
        target = current_target(rules)
        nft_err = None
    except Fatal as e:
        rules, target, nft_err = [], None, str(e)
    mode_name = {'auto': '自动', 'hold_primary': '锁定主线', 'hold_backup': '锁定备用'}[st['mode']]
    if st['mode'] == 'hold_backup':
        mode_name += ' (%s)' % st.get('hold_backup')
    primary = tuple(st['primary']) if st['primary'] else None
    if target is None:
        active = '无规则'
    elif primary and target == primary:
        active = '主线'
    else:
        i = cfg.find_backup(target)
        if i is not None:
            active = '备用#%d' % (i + 1)
        elif primary is None:
            active = '主线(待服务记录)'
        else:
            active = '已移除的备用'
    if '--brief' in args:
        print('%s|%s|%s|%s' % (mode_name, active, fmt_target(target), fmt_target(primary)))
        return 0
    probe = '--probe' in args
    def p(t):
        if not probe or not t:
            return ''
        return '  [%s]' % ('OK' if tcp_ok(t[0], t[1], max(cfg.check_timeout, 1.0)) else 'FAIL')
    print('模式:       %s' % mode_name)
    print('当前线路:   %s  -> %s' % (active, fmt_target(target)))
    print('主线:       %s%s' % (fmt_target(primary), p(primary)))
    if not cfg.backups:
        print('备用线路:   (未配置)')
    for i, b in enumerate(cfg.backups):
        t = (b['ip'], b['port']) if b['ip'] else None
        resolved = '' if b['host'] == b['ip'] else ('  (解析: %s)' % (b['ip'] or '失败'))
        print('备用#%-2d     %s%s%s' % (i + 1, b['spec'], resolved, p(t)))
    print('计数:       失败 %d/%d, 恢复 %d/%d' % (st['fail_count'], cfg.fail_threshold, st['recover_count'], cfg.recover_threshold))
    if st.get('last_change'):
        print('最近切换:   %s' % time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st['last_change'])))
    if nft_err:
        print('nft 读取失败: %s' % nft_err)
    print()
    print('--- 入口 %d 的 dnat 规则 ---' % cfg.port)
    for r in rules:
        print(r['line'])
    if not rules:
        print('(无)')
    return 0


def cmd_mode(cfg, log, args):
    if not args:
        raise Fatal('用法: mode auto|hold-primary|hold-backup <编号>')
    with Lock():
        st = load_state(cfg)
        m = args[0]
        if m == 'auto':
            st['mode'] = 'auto'
            st['hold_backup'] = None
        elif m == 'hold-primary':
            if not st['primary']:
                raise Fatal('还不知道主线目标，先用 set-primary 设置或让服务跑一次')
            st['mode'] = 'hold_primary'
            st['hold_backup'] = None
        elif m == 'hold-backup':
            if len(args) < 2 or not valid_posint(args[1]) or int(args[1]) > len(cfg.backups):
                raise Fatal('备用编号无效')
            b = cfg.backups[int(args[1]) - 1]
            if not b['ip']:
                raise Fatal('备用 %s 无法解析' % b['spec'])
            st['mode'] = 'hold_backup'
            st['hold_backup'] = b['spec']
        else:
            raise Fatal('未知模式: %s' % m)
        st['fail_count'] = st['recover_count'] = 0
        save_state(st)
        log.write('模式切换为 %s%s' % (st['mode'], (' ' + st['hold_backup']) if st['hold_backup'] else ''), important=True)
        Engine(cfg, log).step(st)
        save_state(st)
    return 0


def cmd_set_primary(cfg, log, args):
    if not args:
        raise Fatal('用法: set-primary IP:PORT')
    host, port = parse_hostport(args[0])
    ip = resolve_ipv4(host)
    if not ip:
        raise Fatal('无法解析 %s' % host)
    new = (ip, port)
    if cfg.find_backup(new) is not None:
        raise Fatal('%s:%d 已在备用列表中，主线不能和备用相同' % new)
    with Lock():
        st = load_state(cfg)
        rules = entry_rules(cfg)
        target = current_target(rules)
        old = tuple(st['primary']) if st['primary'] else None
        st['primary'] = list(new)
        on_primary = target is not None and (target == old or cfg.find_backup(target) is None)
        if target is not None and on_primary and target != new:
            apply_target(cfg, new, rules)
            st['last_applied'] = list(new)
            st['active'] = 'primary'
            st['last_change'] = int(now())
        st['fail_count'] = st['recover_count'] = 0
        save_state(st)
        log.write('主线设置为 %s:%d（原: %s）' % (new[0], new[1], fmt_target(old)), important=True)
    return 0


def find_port_target(cfg, port):
    text = list_chain(cfg, cfg.chain)
    single = re.compile(r'\b(tcp|udp) dport %d(?![\d-]).*?\bdnat (?:ip )?to (\d{1,3}(?:\.\d{1,3}){3}):(\d+)(?![\d-])' % port)
    range_map = re.compile(r'\b(tcp|udp) dport (\d+)-(\d+)\b.*?\bdnat (?:ip )?to (\d{1,3}(?:\.\d{1,3}){3}) ?: ?(?:tcp|udp) dport map \{(.*?)\}')
    range_same = re.compile(r'\b(tcp|udp) dport (\d+)-(\d+)\b.*?\bdnat (?:ip )?to (\d{1,3}(?:\.\d{1,3}){3})(?![\d.:])')
    cands = []
    for line in text.splitlines():
        s = line.strip()
        m = single.search(s)
        if m:
            cands.append((0 if m.group(1) == 'tcp' else 1, m.group(2), int(m.group(3)), s))
            continue
        m = range_map.search(s)
        if m:
            if int(m.group(2)) <= port <= int(m.group(3)):
                mm = re.search(r'(?:^|[,\s])%d\s*:\s*(\d+)(?=[,\s]|$)' % port, m.group(5))
                if mm:
                    cands.append((2 if m.group(1) == 'tcp' else 3, m.group(4), int(mm.group(1)), s))
            continue
        m = range_same.search(s)
        if m and int(m.group(2)) <= port <= int(m.group(3)):
            cands.append((4 if m.group(1) == 'tcp' else 5, m.group(4), port, s))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1], cands[0][2], [c[3] for c in cands]


def cmd_take_port(cfg, log, args):
    if not args or not valid_port(args[0]):
        raise Fatal('用法: take-port <新端口> [--dry-run]')
    new_port = int(args[0])
    if new_port == cfg.port:
        raise Fatal('入口端口没有变化')
    found = find_port_target(cfg, new_port)
    if not found:
        raise Fatal('没有找到端口 %d 的 nft/nfter 转发规则，请先建好转发' % new_port)
    ip, tport, lines = found
    if '--dry-run' in args:
        print(ip)
        print(tport)
        for l in lines:
            print(l)
        return 0
    with Lock():
        text = list_chain(cfg, cfg.chain)
        handles = []
        pat_old = [dport_re(p, cfg.port) for p in ('tcp', 'udp')]
        pat_new = [dport_re(p, new_port) for p in ('tcp', 'udp')]
        for line in text.splitlines():
            s = line.strip()
            h = HANDLE_RE.search(s)
            if not h or not re.search(r'\bdnat\b', s):
                continue
            if any(p.search(s) for p in pat_old + pat_new):
                handles.append(h.group(1))
        batch = []
        for proto in ('udp', 'tcp'):
            batch.append('insert rule %s %s %s %s dport %d counter dnat to %s:%d'
                         % (cfg.family, cfg.table, cfg.chain, proto, new_port, ip, tport))
        for h in handles:
            batch.append('delete rule %s %s %s handle %s' % (cfg.family, cfg.table, cfg.chain, h))
        batch += masq_batch(cfg, ip, tport)
        nft(['-f', '-'], stdin='\n'.join(batch) + '\n')
        st = load_state(cfg)
        st.update({'mode': 'auto', 'hold_backup': None, 'primary': [ip, tport], 'active': 'primary',
                   'last_applied': [ip, tport], 'fail_count': 0, 'recover_count': 0, 'last_change': int(now())})
        save_state(st)
        log.write('入口 %d -> %d，主线 %s:%d，移除规则 handle=%s' % (cfg.port, new_port, ip, tport, ','.join(handles)), important=True)
    return 0


def cmd_restore_primary(cfg, log):
    with Lock():
        st = load_state(cfg)
        if not st['primary']:
            print('没有记录主线，跳过')
            return 0
        rules = entry_rules(cfg)
        target = current_target(rules)
        st['mode'] = 'auto'
        st['hold_backup'] = None
        save_state(st)
        if target is None or target == tuple(st['primary']):
            print('入口已在主线或无规则，无需恢复')
            return 0
        apply_target(cfg, tuple(st['primary']), rules)
        log.write('卸载前已把入口切回主线 %s:%d' % tuple(st['primary']), important=True)
    return 0


def cmd_render_unit(args):
    """render-unit [--python PATH] [--script PATH] [--set KEY=VAL ...]"""
    env = migrate_legacy(read_unit_env())
    python_bin, script = '/usr/bin/python3', '/opt/cf_ss_failover.py'
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--python':
            python_bin = args[i + 1]; i += 2; continue
        if a == '--script':
            script = args[i + 1]; i += 2; continue
        if a == '--set':
            k, v = args[i + 1].split('=', 1)
            if k not in CONFIG_KEYS:
                raise ConfigError('未知配置项 %s' % k)
            env[k] = v; i += 2; continue
        raise ConfigError('未知参数 %s' % a)
    out = {}
    for key, default, _ in CONFIG_SPEC:
        out[key] = validate_value(key, env.get(key, default))
    sys.stdout.write(render_unit(out, python_bin, script))
    return 0


def cmd_validate(args):
    """validate KEY VALUE -> prints normalized value or error."""
    if len(args) != 2 or args[0] not in CONFIG_KEYS:
        raise ConfigError('用法: validate KEY VALUE')
    print(validate_value(args[0], args[1]))
    return 0


def cmd_resolve(args):
    ip = resolve_ipv4(args[0]) if args else None
    if not ip:
        return 1
    print(ip)
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else 'run'
    args = argv[2:]
    if cmd in ('--once',):
        cmd = 'once'
    try:
        if cmd == 'render-unit':
            return cmd_render_unit(args)
        if cmd == 'validate':
            return cmd_validate(args)
        if cmd == 'resolve':
            return cmd_resolve(args)
        if cmd == 'version':
            print(VERSION)
            return 0
        cfg = load_config()
        if cmd == 'check-config':
            print('配置有效')
            return 0
        if cmd == 'run':
            return cmd_run(cfg, Logger())
        if cmd == 'once':
            run_step(cfg, Logger(echo_all=True))
            return 0
        if cmd == 'status':
            return cmd_status(cfg, args)
        log = Logger()
        if cmd == 'mode':
            return cmd_mode(cfg, log, args)
        if cmd == 'set-primary':
            return cmd_set_primary(cfg, log, args)
        if cmd == 'take-port':
            return cmd_take_port(cfg, log, args)
        if cmd == 'restore-primary':
            return cmd_restore_primary(cfg, log)
        print('未知命令: %s' % cmd, file=sys.stderr)
        return 1
    except ConfigError as e:
        print('配置错误: %s' % e, file=sys.stderr)
        return EXIT_CONFIG
    except Fatal as e:
        print('错误: %s' % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main(sys.argv))
