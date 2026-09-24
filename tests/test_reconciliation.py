#!/usr/bin/env python3
"""Failure injection across profile-bound routes, GitHub hooks, and loop config."""
import json
import pathlib
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t
from review_loop import cli, config, gh, routes


def main():
    t.HOST = t.start_sink()
    t.DATA['host'] = t.HOST
    import os
    os.environ.update(t.env())
    t.reset(prs={})
    t.make_loop('reconcile', 'acme/reconcile', 'reviewer-profile', 'fixer-profile')
    config_path = t.LOOPS_DIR / 'reconcile.json'
    before = config_path.read_bytes()
    originals = {name: routes.route(name) for name in ('reconcile-review', 'reconcile-fix')}
    hooks = {1: routes.url_for_profile('reconcile-review', 'reviewer-profile', t.HOST),
             2: routes.url_for_profile('reconcile-fix', 'fixer-profile', t.HOST)}
    old_hooks = hooks.copy()
    form = {'reviewer_profile': 'vex', 'fixer_profile': 'drey',
            'reviewer_login': t.REVIEWER, 'fixer_login': t.FIXER}
    apply = t.parser_for(form).parse_args(['apply', '--loop', 'reconcile'])

    def api(loop, path, method='GET', body=None, login=None):
        if path.endswith('/hooks?per_page=100'):
            return [{'id': key, 'config': {'url': url}} for key, url in hooks.items()]
        key = int(path.rsplit('/', 1)[-1])
        if method == 'PATCH':
            hooks[key] = body['config']['url']
        return {'id': key, 'config': {'url': hooks[key]}}

    def assert_old(label, rc):
        assert rc == 2, (label, rc)
        assert config_path.read_bytes() == before, label + ' config'
        assert {name: routes.route(name) for name in originals} == originals, label + ' routes'
        assert hooks == old_hooks, label + ' hooks'
        print('PASS', label)

    with mock.patch.object(gh, 'api', side_effect=api):
        rc, out = t.run_cli(apply)
    assert rc == 0, out
    assert hooks[1].endswith('/p/vex/webhooks/reconcile-review')
    assert hooks[2].endswith('/p/drey/webhooks/reconcile-fix')
    assert config.seat_profile(config.load_id('reconcile'), 'reviewer') == 'vex'
    print('PASS both hook URLs and routes updated with config')
    config_path.write_bytes(before)
    routes.restore_entries(originals)
    hooks.update(old_hooks)

    actual_new_route = routes.new_route
    count = 0
    def second_route(*args, **kw):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('second route failed')
        return actual_new_route(*args, **kw)
    with mock.patch.object(gh, 'api', side_effect=api), mock.patch.object(routes, 'new_route', side_effect=second_route):
        rc, out = t.run_cli(apply)
    assert_old('second route failure rolls back first', rc)

    actual_route = routes.route
    def stale(name):
        if name == 'reconcile-fix' and routes.all_routes()[name]['profile'] == 'drey':
            return originals[name]
        return actual_route(name)
    with mock.patch.object(gh, 'api', side_effect=api), mock.patch.object(routes, 'route', side_effect=stale):
        rc, out = t.run_cli(apply)
    assert_old('route readback mismatch rolls back', rc)

    def fail_hook(loop, path, method='GET', body=None, login=None):
        if method == 'PATCH' and path.endswith('/hooks/2') and body['config']['url'] != old_hooks[2]:
            return None
        return api(loop, path, method, body, login)
    with mock.patch.object(gh, 'api', side_effect=fail_hook):
        rc, out = t.run_cli(apply)
    assert_old('second hook failure rolls back first', rc)

    with mock.patch.object(gh, 'api', return_value=None):
        rc, out = t.run_cli(apply)
    assert_old('unreadable hook listing fails closed', rc)

    # Publication must not truncate the installed config when the write fails.
    original_replace = cli.os.replace
    def fail_publish(src, dst):
        if pathlib.Path(dst) == config_path:
            raise OSError('disk full during config publication')
        return original_replace(src, dst)
    with mock.patch.object(gh, 'api', side_effect=api), \
            mock.patch.object(cli.os, 'replace', side_effect=fail_publish):
        rc, out = t.run_cli(apply)
    assert_old('config publication failure preserves prior bytes', rc)
    assert 'config unchanged' in out, out

    # An existing hook for our route at an unexpected profile must not be ignored.
    hooks[1] = routes.url_for_profile('reconcile-review', 'tuck', t.HOST)
    with mock.patch.object(gh, 'api', side_effect=api):
        rc, out = t.run_cli(apply)
    assert rc == 2 and 'hook' in out.lower(), (rc, out)
    assert config_path.read_bytes() == before
    assert {name: routes.route(name) for name in originals} == originals
    assert hooks[1].endswith('/p/tuck/webhooks/reconcile-review')
    hooks.update(old_hooks)
    print('PASS unexpected installed hook profile fails closed')

    def malformed(loop, path, method='GET', body=None, login=None):
        if path.endswith('/hooks?per_page=100'):
            return [{'id': 1, 'config': 'bad'}]
        return api(loop, path, method, body, login)
    with mock.patch.object(gh, 'api', side_effect=malformed):
        rc, out = t.run_cli(apply)
    assert_old('malformed hook listing fails closed without exception', rc)

    empty = t.HOME / 'profiles' / 'empty-profile'
    empty.mkdir(parents=True, exist_ok=True)
    assert not config.profile_exists('empty-profile')
    print('PASS empty profile directory is not an installed profile')

    init = t.parser_for({'reviewer_profile': 'vex', 'fixer_profile': 'drey'}).parse_args([
        'init', '--repo', 'acme/reconcile', '--id', 'reconcile', '--host', t.HOST,
        '--reviewer', t.REVIEWER, '--fixer', t.FIXER,
        '--token', f'{t.REVIEWER}={t.SEAT_PATS[0]}', '--token', f'{t.FIXER}={t.SEAT_PATS[1]}'])
    count = 0
    with mock.patch.object(routes, 'new_route', side_effect=second_route):
        rc, out = t.run_cli(init)
    assert_old('init retry restores preexisting config and routes', rc)

    new_init = t.parser_for({'reviewer_profile': 'vex', 'fixer_profile': 'drey'}).parse_args([
        'init', '--repo', 'acme/newloop', '--id', 'newloop', '--host', t.HOST,
        '--reviewer', t.REVIEWER, '--fixer', t.FIXER,
        '--token', f'{t.REVIEWER}={t.SEAT_PATS[0]}',
        '--token', f'{t.FIXER}={t.SEAT_PATS[1]}', '--hooks'])
    created = {}
    def partial_create(loop, path, method='GET', body=None, login=None):
        if method == 'POST':
            if created:
                return None
            created[100] = body['config']['url']
            return {'id': 100}
        if method == 'DELETE':
            created.pop(int(path.rsplit('/', 1)[-1]), None)
            return None
        if path.endswith('/hooks?per_page=100'):
            return [{'id': key, 'config': {'url': url}} for key, url in created.items()]
        return {'id': 100, 'config': {'url': created[100]}} if 100 in created else None
    with mock.patch.object(gh, 'api', side_effect=partial_create):
        rc, out = t.run_cli(new_init)
    assert rc == 2, (rc, out)
    assert not created and not (t.LOOPS_DIR / 'newloop.json').exists(), (created, out)
    assert not routes.route('newloop-review') and not routes.route('newloop-fix')
    print('PASS init second hook failure removes first hook and local artifacts')

    multi = t.parser_for({'reviewer_profile': 'vex', 'fixer_profile': 'drey',
                          'reviewer_login': t.REVIEWER}).parse_args([
        'init', '--repo', 'acme/multi', '--host', t.HOST,
        '--reviewer', 'backup-reviewer', '--reviewer', t.REVIEWER, '--fixer', t.FIXER,
        '--token', f'{t.REVIEWER}={t.SEAT_PATS[0]}', '--token', f'{t.FIXER}={t.SEAT_PATS[1]}'])
    rc, out = t.run_cli(multi)
    assert rc == 0, out
    assert config.load_id('multi')['reviewer_seat'] == t.REVIEWER
    print('PASS configured eligible reviewer selected from multi-login allowlist')

    # A stale route must be repaired even when the form and loop config already agree.
    stable_form = {'reviewer_profile': 'reviewer-profile', 'fixer_profile': 'fixer-profile',
                   'reviewer_login': t.REVIEWER, 'fixer_login': t.FIXER}
    unchanged_apply = t.parser_for(stable_form).parse_args(['apply', '--loop', 'reconcile'])
    routes.new_route('reconcile-review', profile='vex', prompt=cli.prompts.REVIEWER,
                     events=['pull_request'], script='gate_reviewer.py', host=t.HOST,
                     deliver='discord')
    old_config = config_path.read_bytes()
    old_fixer = routes.route('reconcile-fix')
    stale_route = routes.route('reconcile-review')
    hooks[1] = routes.url_for_profile('reconcile-review', 'vex', t.HOST)
    stale_hooks = hooks.copy()
    with mock.patch.object(gh, 'api', side_effect=api):
        rc, out = t.run_cli(t.parser_for(stable_form).parse_args(
            ['apply', '--loop', 'reconcile', '--dry-run']))
    assert rc == 0 and 'profile vex → reviewer-profile' in out, (rc, out)
    assert routes.route('reconcile-review') == stale_route and hooks == stale_hooks

    # Reviewer probe: unchanged settings must update both the stale route and its GitHub hook.
    with mock.patch.object(gh, 'api', return_value=None):
        rc, out = t.run_cli(unchanged_apply)
    assert rc == 2 and 'hook listing' in out, (rc, out)
    assert routes.route('reconcile-review') == stale_route and hooks == stale_hooks
    assert config_path.read_bytes() == old_config
    print('PASS unchanged settings refuse route repair on untrusted hook listing')

    def fail_repair(loop, path, method='GET', body=None, login=None):
        if method == 'PATCH' and path.endswith('/hooks/1') and body['config']['url'] != stale_hooks[1]:
            hooks[1] = body['config']['url']  # Simulate a lost response after GitHub applied it.
            return None
        return api(loop, path, method, body, login)
    with mock.patch.object(gh, 'api', side_effect=fail_repair):
        rc, out = t.run_cli(unchanged_apply)
    assert rc == 2 and 'reconciliation FAILED' in out, (rc, out)
    assert routes.route('reconcile-review') == stale_route and hooks == stale_hooks
    assert config_path.read_bytes() == old_config
    print('PASS unchanged settings roll back route and hook after ambiguous hook PATCH')

    hooks[1] = routes.url_for_profile('reconcile-review', 'tuck', t.HOST)
    with mock.patch.object(gh, 'api', side_effect=api):
        rc, out = t.run_cli(unchanged_apply)
    assert rc == 2 and 'unexpected URL' in out, (rc, out)
    assert routes.route('reconcile-review') == stale_route and config_path.read_bytes() == old_config
    hooks.update(stale_hooks)
    print('PASS unchanged settings refuse unknown hook destination')

    with mock.patch.object(gh, 'api', side_effect=api):
        rc, out = t.run_cli(unchanged_apply)
    assert rc == 0 and routes.route('reconcile-review')['profile'] == 'reviewer-profile', (rc, out)
    assert hooks == old_hooks and routes.route('reconcile-fix') == old_fixer
    assert config_path.read_bytes() == old_config
    print('PASS unchanged settings reconcile stale owned route and hook atomically')

    # A stale profile is not permission to overwrite a route with another gate's script.
    foreign = routes.route('reconcile-review')
    routes.new_route('reconcile-review', profile='vex', prompt=cli.prompts.FIXER,
                     events=['pull_request_review'], script='gate_fixer.py', host=t.HOST,
                     deliver='discord')
    foreign_snapshot = routes.route('reconcile-review')
    with mock.patch.object(gh, 'api', side_effect=api):
        rc, out = t.run_cli(unchanged_apply)
    assert rc == 2 and 'belongs to something else' in out, (rc, out)
    assert routes.route('reconcile-review') == foreign_snapshot
    assert config_path.read_bytes() == old_config and hooks == old_hooks
    routes.restore_entries({'reconcile-review': foreign})
    print('PASS unchanged-settings route repair refuses foreign gate ownership')

    # The configured fixer is the selected login, not merely one of the allowed fixers.
    fixer_form = {'reviewer_profile': 'vex', 'fixer_profile': 'drey',
                  'reviewer_login': t.REVIEWER, 'fixer_login': t.FIXER}
    fixer_args = ['init', '--repo', 'acme/multifix', '--host', t.HOST,
                  '--reviewer', t.REVIEWER, '--fixer', 'backup-fixer', '--fixer', t.FIXER,
                  '--token', f'{t.REVIEWER}={t.SEAT_PATS[0]}',
                  '--token', f'{t.FIXER}={t.SEAT_PATS[1]}']
    rc, out = t.run_cli(t.parser_for(fixer_form).parse_args(fixer_args))
    assert rc == 0 and config.load_id('multifix')['seats']['fixer']['login'] == t.FIXER, (rc, out)
    print('PASS configured eligible fixer selected from multi-login allowlist')

    ineligible = {**fixer_form, 'fixer_login': 'not-allowed'}
    rc, out = t.run_cli(t.parser_for(ineligible).parse_args(
        [*fixer_args, '--id', 'badfix']))
    assert rc == 2 and 'fixer' in out.lower(), (rc, out)
    assert not (t.LOOPS_DIR / 'badfix.json').exists()
    assert not routes.route('badfix-review') and not routes.route('badfix-fix')
    print('PASS ineligible configured fixer fails before config and route writes')
    print('19/19 reconciliation cases pass')


if __name__ == '__main__':
    main()
