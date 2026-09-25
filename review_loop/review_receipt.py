"""Host-owned, root-base-only review generation and exact-ID receipt.

A stacked generation cannot be associated here: #23's parent-chain resolver is
not part of this branch. Never infer parent association from head SHA alone.
"""
import json
import re
import sqlite3
import time

from . import gh

SHA = re.compile(r'[0-9a-f]{40}\Z')


class ReceiptDenied(ValueError):
    pass


def generation_for(pr, loop, number, head):
    """Resolve an unstacked generation; reject unknown or retargeted bases."""
    if not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] != number or pr.get('state') != 'open' or pr.get('draft') is not False:
        raise ReceiptDenied('PR identity/state unknown')
    base, child = pr.get('base'), pr.get('head')
    if not isinstance(base, dict) or not isinstance(child, dict):
        raise ReceiptDenied('incomplete generation')
    ref, base_sha = base.get('ref'), base.get('sha')
    if (ref != loop.get('base') or not isinstance(base_sha, str) or not SHA.fullmatch(base_sha)
            or (base.get('repo') or {}).get('full_name') != loop.get('repo')
            or child.get('sha') != head or not SHA.fullmatch(head)
            or (child.get('repo') or {}).get('full_name') != loop.get('repo')):
        raise ReceiptDenied('stale or unresolvable generation')
    # This branch knows only the configured root base. In particular it does not
    # identify an open PR whose head is the base ref; stacked releases need #23.
    return json.dumps({'head': head, 'base_ref': ref, 'base_sha': base_sha,
                       'parents': [], 'parent_chain_verified': False},
                      sort_keys=True, separators=(',', ':'))


class ReceiptLedger:
    """The worker's own SQLite run claim, never a caller-supplied receipt."""
    def __init__(self, db, run_id, generation):
        self.db, self.run_id, self.generation = db, run_id, generation
        if not db or not run_id or not generation:
            raise ReceiptDenied('unclaimed generation')

    def _connect(self):
        con = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        con.execute('PRAGMA busy_timeout=10000')
        con.execute('PRAGMA synchronous=FULL')
        return con

    def claim(self, principal_id):
        if type(principal_id) is not int or principal_id <= 0:
            raise ReceiptDenied('invalid reviewer principal')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT generation,seat,state FROM runs WHERE id=?',
                              (self.run_id,)).fetchone()
            if row != (self.generation, 'reviewer', 'running'):
                raise ReceiptDenied('generation not owned by running reviewer')
            con.execute('INSERT INTO review_receipts(run_id,state,generation,principal_id,created) '
                        'VALUES(?,?,?,?,?)', (self.run_id, 'claimed', self.generation,
                                            principal_id, time.time()))
            con.execute('COMMIT')

    def confirm(self, review_id, verdict, principal_id):
        if type(review_id) is not int or review_id <= 0:
            raise ReceiptDenied('invalid review ID')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            changed = con.execute('UPDATE review_receipts SET state=?,review_id=?,verdict=?,confirmed=? '
                'WHERE run_id=? AND state=? AND generation=? AND principal_id=?',
                ('confirmed', review_id, verdict, time.time(), self.run_id, 'claimed',
                 self.generation, principal_id)).rowcount
            if changed != 1:
                raise ReceiptDenied('receipt claim unavailable')
            con.execute('COMMIT')


def submit(loop, scope, ledger, verdict, body):
    """Claim before POST, read exact returned ID, then re-resolve generation."""
    from . import broker
    expected = {'APPROVE': 'APPROVED', 'REQUEST_CHANGES': 'CHANGES_REQUESTED',
                'COMMENT': 'COMMENTED'}
    if verdict not in expected or not isinstance(body, str) or not body.strip():
        raise ReceiptDenied('invalid verdict')
    login = broker.authorize(loop, repo=scope.repo, number=scope.number, head=scope.head,
                             role='reviewer', branch=scope.branch, operation='review')
    path = f'/repos/{scope.repo}/pulls/{scope.number}'
    reader = loop['read_token']
    if generation_for(gh.api(loop, path, login=reader), loop, scope.number, scope.head) != ledger.generation:
        raise ReceiptDenied('generation changed before POST')
    principal = gh.api(loop, '/user', login=login)
    if not isinstance(principal, dict) or type(principal.get('id')) is not int or principal['id'] <= 0 or str(principal.get('login', '')).casefold() != login.casefold():
        raise ReceiptDenied('reviewer identity changed')
    principal_id = principal['id']
    ledger.claim(principal_id)  # No retry after this durable point, even if POST throws.
    endpoint = path + '/reviews'
    response = gh.api(loop, endpoint, method='POST', login=login,
                      body={'commit_id': scope.head, 'event': verdict, 'body': body})
    review_id = response.get('id') if isinstance(response, dict) else None
    if type(review_id) is not int or review_id <= 0:
        raise ReceiptDenied('POST acknowledgement lacks exact review ID')
    readback = gh.api(loop, endpoint + '/' + str(review_id), login=reader)
    if (not isinstance(readback, dict) or type(readback.get('id')) is not int
            or readback['id'] != review_id or readback.get('state') != expected[verdict]
            or readback.get('commit_id') != scope.head or
            not isinstance(readback.get('user'), dict) or
            type(readback['user'].get('id')) is not int or
            readback['user']['id'] != principal_id or
            str(readback['user'].get('login', '')).casefold() != login.casefold()):
        raise ReceiptDenied('exact-ID readback mismatch')
    if generation_for(gh.api(loop, path, login=reader), loop, scope.number, scope.head) != ledger.generation:
        raise ReceiptDenied('generation changed after POST')
    ledger.confirm(review_id, expected[verdict], principal_id)
    broker._audit(loop, scope.repo, scope.number, scope.head, scope.branch,
                  'reviewer', 'review', login)
    return {'id': review_id}
