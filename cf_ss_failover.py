#!/usr/bin/env python3
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

STATE_FILE = Path(os.getenv('STATE_FILE', '/opt/ss-failover/state.json'))
LOG_FILE = Path(os.getenv('LOG_FILE', '/opt/ss-failover/failover.log'))

FORWARD_PORT = int(os.getenv('FORWARD_PORT', '10001'))
CHECK_TIMEOUT = float(os.getenv('CHECK_TIMEOUT', '3'))
CHECK_INTERVAL = float(os.getenv('CHECK_INTERVAL', '1'))
RECOVER_INTERVAL = float(os.getenv('RECOVER_INTERVAL', '2'))
BACKUP_CHECK_INTERVAL = float(os.getenv('BACKUP_CHECK_INTERVAL', str(RECOVER_INTERVAL)))
FAIL_THRESHOLD = int(os.getenv('FAIL_THRESHOLD', '3'))
RECOVER_THRESHOLD = int(os.getenv('RECOVER_THRESHOLD', '5'))

BACKUP_HOST = os.getenv('BACKUP_HOST', os.getenv('BACKUP_IP', '127.0.0.1'))
BACKUP_PORT = int(os.getenv('BACKUP_PORT', str(FORWARD_PORT)))


def parse_backups():
    raw = os.getenv('BACKUP_LIST', '').strip()
    items = []
    if raw:
        for part in raw.split(','):
            part = part.strip()
            if not part:
                continue
            if ':' not in part:
                raise RuntimeError(f'invalid BACKUP_LIST item: {part}')
            host, port = part.rsplit(':', 1)
            items.append([host.strip(), int(port)])
    if not items:
        items.append([BACKUP_HOST, BACKUP_PORT])
    return items


BACKUPS = parse_backups()
BACKUP_HOST, BACKUP_PORT = BACKUPS[0]
PRIMARY_STABLE_COUNT = int(os.getenv('PRIMARY_STABLE_COUNT', '3'))

NFT_FAMILY = os.getenv('NFT_FAMILY', 'ip')
NFT_TABLE = os.getenv('NFT_TABLE', 'nat')
NFT_CHAIN = os.getenv('NFT_CHAIN', 'prerouting')
NFT_POSTROUTING_CHAIN = os.getenv('NFT_POSTROUTING_CHAIN', 'postrouting')

RULE_PATTERNS = {
    'tcp': re.compile(rf'tcp dport {FORWARD_PORT}\b.*?dnat to ([0-9.]+):(\d+)'),
    'udp': re.compile(rf'udp dport {FORWARD_PORT}\b.*?dnat to ([0-9.]+):(\d+)'),
}
POSTROUTING_PATTERNS = {
    'tcp': re.compile(r'ip daddr ([0-9.]+) tcp dport (\d+) .*?masquerade'),
    'udp': re.compile(r'ip daddr ([0-9.]+) udp dport (\d+) .*?masquerade'),
}
HANDLE_RE = re.compile(r'handle (\d+)$')


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def default_state():
    return {
        'active': 'primary',
        'fail_count': 0,
        'recover_count': 0,
        'last_change': 0,
        'primary_target': None,
        'primary_seen_count': 0,
        'backup_index': 0,
    }


def load_state():
    state = default_state()
    if not STATE_FILE.exists():
        return state
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            state.update(data)
    except Exception:
        pass
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding='utf-8')


def tcp_ok(host, port, timeout=None):
    if timeout is None:
        timeout = CHECK_TIMEOUT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        sock.close()


def run_nft(*args, check=True):
    return subprocess.run(['nft', *args], text=True, capture_output=True, timeout=5, check=check)


def list_chain(chain):
    return run_nft('-a', 'list', 'chain', NFT_FAMILY, NFT_TABLE, chain).stdout


def parse_forward_rules(text):
    rules = {'tcp': None, 'udp': None}
    for line in text.splitlines():
        stripped = line.strip()
        handle_match = HANDLE_RE.search(stripped)
        handle = handle_match.group(1) if handle_match else None
        for proto in ('tcp', 'udp'):
            if rules[proto] is not None:
                continue
            if f'{proto} dport {FORWARD_PORT}' not in stripped or 'dnat to' not in stripped:
                continue
            match = RULE_PATTERNS[proto].search(stripped)
            if match:
                rules[proto] = {
                    'host': match.group(1),
                    'port': int(match.group(2)),
                    'line': stripped,
                    'handle': handle,
                }
    return rules


def parse_postrouting_rules(text):
    seen = {'tcp': [], 'udp': []}
    for line in text.splitlines():
        stripped = line.strip()
        handle_match = HANDLE_RE.search(stripped)
        handle = handle_match.group(1) if handle_match else None
        for proto in ('tcp', 'udp'):
            match = POSTROUTING_PATTERNS[proto].search(stripped)
            if match:
                seen[proto].append({
                    'host': match.group(1),
                    'port': int(match.group(2)),
                    'line': stripped,
                    'handle': handle,
                })
    return seen


def get_current_rules():
    try:
        return parse_forward_rules(list_chain(NFT_CHAIN))
    except Exception as e:
        log(f'nft rule read failed: {e}')
        return {'tcp': None, 'udp': None}


def get_postrouting_rules():
    try:
        return parse_postrouting_rules(list_chain(NFT_POSTROUTING_CHAIN))
    except Exception as e:
        log(f'postrouting read failed: {e}')
        return {'tcp': [], 'udp': []}


def add_postrouting_rule(proto, host, port):
    run_nft(
        'add', 'rule', NFT_FAMILY, NFT_TABLE, NFT_POSTROUTING_CHAIN,
        'ip', 'daddr', host, proto, 'dport', str(port), 'masquerade',
        check=False,
    )


def ensure_postrouting_unique(host, port):
    rules = get_postrouting_rules()
    for proto in ('tcp', 'udp'):
        matches = [r for r in rules[proto] if r['host'] == host and r['port'] == port]
        if not matches:
            add_postrouting_rule(proto, host, port)
            continue
        keep = matches[0]['handle']
        for dup in matches[1:]:
            if dup['handle']:
                run_nft('delete', 'rule', NFT_FAMILY, NFT_TABLE, NFT_POSTROUTING_CHAIN, 'handle', str(dup['handle']), check=False)
                log(f'removed duplicate postrouting {proto} {host}:{port} handle={dup["handle"]}')


def add_forward_rule(proto, host, port):
    run_nft(
        'insert', 'rule', NFT_FAMILY, NFT_TABLE, NFT_CHAIN,
        proto, 'dport', str(FORWARD_PORT), 'counter', 'dnat', 'to', f'{host}:{port}',
    )


def delete_rule(chain, handle):
    if not handle:
        raise RuntimeError('missing nft rule handle')
    run_nft('delete', 'rule', NFT_FAMILY, NFT_TABLE, chain, 'handle', str(handle))


def switch_target(new_host, new_port):
    rules = get_current_rules()
    tcp_rule = rules['tcp']
    udp_rule = rules['udp']
    if not tcp_rule:
        raise RuntimeError(f'tcp dport {FORWARD_PORT} rule not found')

    add_forward_rule('tcp', new_host, new_port)
    delete_rule(NFT_CHAIN, tcp_rule['handle'])

    if udp_rule:
        add_forward_rule('udp', new_host, new_port)
        delete_rule(NFT_CHAIN, udp_rule['handle'])

    ensure_postrouting_unique(new_host, new_port)


def refresh_primary_target(state, current_host, current_port):
    current = [current_host, current_port]
    if current in BACKUPS:
        state['primary_seen_count'] = 0
        return
    prev = state.get('primary_target')
    if prev == current:
        state['primary_seen_count'] = min(state.get('primary_seen_count', 0) + 1, PRIMARY_STABLE_COUNT)
    else:
        state['primary_target'] = current
        state['primary_seen_count'] = 1
        log(f'learn primary target={current_host}:{current_port}')


def health_ok(host, port, label):
    ok = tcp_ok(host, port, CHECK_TIMEOUT)
    log(f'{label}->{host}:{port} ' + ('OK' if ok else 'FAIL'))
    return ok


def run_once(state):
    rules = get_current_rules()
    tcp_rule = rules['tcp']
    udp_rule = rules['udp']
    if not tcp_rule:
        raise RuntimeError(f'nft rule missing for tcp dport {FORWARD_PORT}')
    if not udp_rule:
        log(f'warning: udp dport {FORWARD_PORT} rule missing')

    current_host = tcp_rule['host']
    current_port = tcp_rule['port']
    refresh_primary_target(state, current_host, current_port)

    primary = state.get('primary_target')
    if not primary:
        raise RuntimeError('primary target not learned yet')
    primary_host, primary_port = primary

    manual_hold = state.get('manual_hold')
    if manual_hold == 'backup':
        idx = int(state.get('backup_index', 0))
        idx = max(0, min(idx, len(BACKUPS) - 1))
        hold_host, hold_port = BACKUPS[idx]
        if [current_host, current_port] != [hold_host, hold_port]:
            switch_target(hold_host, hold_port)
            current_host, current_port = hold_host, hold_port
            log(f'=== MANUAL HOLD BACKUP#{idx + 1} {hold_host}:{hold_port} ===')
        state['active'] = f'backup:{idx}'
        state['fail_count'] = 0
        state['recover_count'] = 0
        health_ok(primary_host, primary_port, 'primary-probe')
        ensure_postrouting_unique(hold_host, hold_port)
        log('manual hold: backup active, auto recovery disabled')
        save_state(state)
        return

    current_backup_index = next((i for i, b in enumerate(BACKUPS) if b == [current_host, current_port]), None)
    active_is_backup = current_backup_index is not None
    if active_is_backup:
        state['active'] = f'backup:{current_backup_index}'
        state['backup_index'] = current_backup_index
    else:
        state['active'] = 'primary'

    if not active_is_backup:
        ok = health_ok(current_host, current_port, 'primary')
        state['recover_count'] = 0
        if ok:
            state['fail_count'] = 0
            ensure_postrouting_unique(current_host, current_port)
        else:
            state['fail_count'] = state.get('fail_count', 0) + 1
            log(f"fail_count={state['fail_count']}/{FAIL_THRESHOLD}")
            if state['fail_count'] >= FAIL_THRESHOLD:
                backup_host, backup_port = BACKUPS[0]
                switch_target(backup_host, backup_port)
                state['active'] = 'backup:0'
                state['backup_index'] = 0
                state['fail_count'] = 0
                state['recover_count'] = 0
                state['last_change'] = int(time.time())
                log(f'=== SWITCH NFT -> BACKUP#1 {backup_host}:{backup_port} ===')
    else:
        # Prefer primary recovery. Never let backup rotation starve recover_count.
        primary_ok = health_ok(primary_host, primary_port, 'primary-probe')
        state['fail_count'] = 0
        if primary_ok:
            state['recover_count'] = state.get('recover_count', 0) + 1
            log(f"recover_count={state['recover_count']}/{RECOVER_THRESHOLD}")
            if state['recover_count'] >= RECOVER_THRESHOLD:
                switch_target(primary_host, primary_port)
                state['active'] = 'primary'
                state['recover_count'] = 0
                state['last_change'] = int(time.time())
                ensure_postrouting_unique(primary_host, primary_port)
                log(f'=== SWITCH NFT -> PRIMARY {primary_host}:{primary_port} ===')
                save_state(state)
                return
            # Primary recovering but threshold not met: stay/rotate backup without clearing recover_count
            backup_host, backup_port = BACKUPS[current_backup_index or 0]
            backup_ok = health_ok(backup_host, backup_port, f'backup#{(current_backup_index or 0) + 1}')
            ensure_postrouting_unique(backup_host, backup_port)
            if not backup_ok and len(BACKUPS) > 1:
                next_idx = ((current_backup_index or 0) + 1) % len(BACKUPS)
                next_host, next_port = BACKUPS[next_idx]
                switch_target(next_host, next_port)
                state['active'] = f'backup:{next_idx}'
                state['backup_index'] = next_idx
                state['last_change'] = int(time.time())
                ensure_postrouting_unique(next_host, next_port)
                log(
                    f'=== SWITCH NFT -> BACKUP#{next_idx + 1} {next_host}:{next_port} '
                    f'(primary recovering, keep recover_count={state["recover_count"]}) ==='
                )
        else:
            state['recover_count'] = 0
            log('backup active, primary unhealthy')
            backup_host, backup_port = BACKUPS[current_backup_index or 0]
            backup_ok = health_ok(backup_host, backup_port, f'backup#{(current_backup_index or 0) + 1}')
            ensure_postrouting_unique(backup_host, backup_port)
            if not backup_ok and len(BACKUPS) > 1:
                next_idx = ((current_backup_index or 0) + 1) % len(BACKUPS)
                next_host, next_port = BACKUPS[next_idx]
                switch_target(next_host, next_port)
                state['active'] = f'backup:{next_idx}'
                state['backup_index'] = next_idx
                state['last_change'] = int(time.time())
                ensure_postrouting_unique(next_host, next_port)
                log(f'=== SWITCH NFT -> BACKUP#{next_idx + 1} {next_host}:{next_port} ===')

    save_state(state)


def main():
    if '--once' in sys.argv:
        run_once(load_state())
        return

    log(f'START port={FORWARD_PORT} backups=' + ','.join(f'{h}:{p}' for h, p in BACKUPS))
    log(f'  fail: {CHECK_INTERVAL}s x{FAIL_THRESHOLD} | backup-check: {BACKUP_CHECK_INTERVAL}s | recover: {RECOVER_INTERVAL}s x{RECOVER_THRESHOLD}')
    while True:
        try:
            state = load_state()
            run_once(state)
            active = str(state.get('active') or '')
            interval = BACKUP_CHECK_INTERVAL if active.startswith('backup') else CHECK_INTERVAL
        except Exception as e:
            log(f'unhandled error: {e}')
            interval = CHECK_INTERVAL
        time.sleep(interval)


if __name__ == '__main__':
    main()
