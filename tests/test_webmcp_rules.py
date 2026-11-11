"""Policy and recovery checks that a single fixture cannot express."""

import json
from pathlib import Path

from fine_print.cards import describe
from fine_print.engine import REGISTRY, ToolEvent
from fine_print.signals import strip_untrusted
import fine_print.rules  # noqa: F401

FIX = Path(__file__).parent / 'fixtures'


def observation(name):
    data = json.loads((FIX / f'webmcp_{name}.json').read_text(encoding='utf-8'))
    hits = [o for o in REGISTRY.run('tool', data['tool'], ToolEvent(data['tool'], data['args'], data['result']))
            if o.rule_id == data['limit_id']]
    assert len(hits) == 1
    return data, hits[0]


def test_search_cap_is_digest_with_factual_note_and_no_guessed_recovery():
    data, obs = observation('search_cap_100')
    assert obs.severity == 'digest'
    assert obs.numbers == {'asked': 100, 'shown': 20, 'cap': 20}
    assert obs.subject == 'web_search'
    assert obs.recover == ()
    assert obs.note and len(obs.note) <= 200
    line, fix = describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)
    assert '100' in line and '20' in line
    assert 'narrower' in fix
    replay = REGISTRY.run('tool', 'web_search', ToolEvent('web_search', data['args'],
                                                        strip_untrusted(data['committed']), replay=True))
    assert [o.numbers for o in replay if o.rule_id == obs.rule_id] == [obs.numbers]


def test_summary_budget_recovery_requires_saved_copy_not_another_child():
    from fine_print.engine import Call
    data, obs = observation('summary_cut')
    assert obs.severity == 'card'
    assert obs.numbers['total'] == 9000
    assert obs.numbers['shown'] < 2000
    assert obs.note is None
    assert any(r.satisfied([Call('read_file', {'path': json.loads(data['result'])['results'][0]['summary_full_path']})])
               for r in obs.recover)
    assert not any(r.satisfied([Call('delegate_task', {'tasks': [{'goal': 'Another task'}]})]) for r in obs.recover)
    _, lost = observation('summary_unstored')
    assert lost.recover == ()
    assert lost.numbers['stored'] == 0


def test_stored_copy_cap_is_not_recovered_by_reading_the_capped_file():
    from fine_print.engine import Call
    _, obs = observation('stored_over_cap')
    assert obs.numbers == {'shown': 2000000, 'total': 2500001}
    assert obs.severity == 'card'
    assert obs.note and len(obs.note) <= 200
    assert not any(r.satisfied([Call('read_file', {'path': 'huge-page.md'})]) for r in obs.recover)


def test_batch_timeout_is_digest_not_a_claim_that_completed_pages_were_lost():
    _, obs = observation('batch_timeout')
    assert obs.severity == 'digest'
    assert obs.note is None
    assert obs.numbers == {'urls': 2, 'seconds': 120}
    line, _ = describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)
    assert 'batch' in line and '120' in line
    assert 'completed' not in line


def test_web_copy_recovery_failure_is_not_cleared_by_another_line_read():
    from fine_print.engine import Call
    _, obs = observation('web_recovery_clamped')
    assert obs.severity == 'card'
    assert obs.numbers['shown'] == 2000
    assert obs.numbers['phase'] == 'line_clamp'
    assert obs.note is None
    name = obs.subject
    assert not any(r.satisfied([Call('read_file', {'path': name, 'offset': 1})]) for r in obs.recover)
    assert any(r.satisfied([Call('execute_code', {'code': f'process({name!r})'})]) for r in obs.recover)
    _, eof = observation('web_recovery_eof')
    assert eof.severity == 'digest'
    assert eof.numbers['shown'] == 0
    assert eof.numbers['phase'] == 'past_eof'


def test_browser_stderr_is_digest_and_preserves_unknown_original_length():
    _, obs = observation('browser_stderr_cut')
    assert obs.severity == 'digest'
    assert obs.numbers == {'shown': 4000, 'total_at_least': 4001}
    assert 'total' not in obs.numbers
    assert obs.note is None
    data, native = observation('browser_stderr_multimodal')
    assert native.numbers == obs.numbers
    assert isinstance(data['result'], dict)


def test_image_repeat_refusal_is_digest_and_does_not_claim_child_identity():
    _, obs = observation('image_repeat_refusal')
    assert obs.severity == 'digest'
    assert obs.numbers == {'loads': 3, 'cap': 3}
    line, fix = describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)
    assert 'subagent' not in line
    assert 'already' in fix
    assert obs.note is None


def test_session_message_caps_are_digest_and_scroll_is_not_called_unreachable():
    _, obs = observation('session_read_cut')
    assert obs.severity == 'digest'
    assert obs.numbers == {'shown': 2000, 'total': 8000, 'message_id': 7}
    line, fix = describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)
    assert 'unreachable' not in line
    assert 'around_message_id=7' in fix
    _, discovered = observation('session_discover_cut')
    assert discovered.numbers['shown'] == 4000


def test_video_helper_route_is_digest_not_a_claim_of_lost_video_bytes():
    data, obs = observation('video_helper_text')
    assert obs.severity == 'digest'
    assert obs.numbers == {'analysis_chars': len(json.loads(data['result'])['analysis'])}
    assert obs.note is None
    line, _ = describe(obs.rule_id, obs.numbers, obs.subject, lambda key, default: default)
    assert 'helper' in line
    assert 'native video' in line


def test_media_subjects_store_hosts_not_url_queries():
    for name, tool, key in [('image_repeat_refusal', 'vision_analyze', 'image_url'),
                            ('video_helper_text', 'video_analyze', 'video_url')]:
        data = json.loads((FIX / f'webmcp_{name}.json').read_text(encoding='utf-8'))
        found = [o for o in REGISTRY.run('tool', tool,
                                        ToolEvent(tool, {key: 'https://example.com/media?label=private-label'}, data['result']))
                 if o.rule_id == data['limit_id']]
        assert len(found) == 1
        assert found[0].subject == 'example.com'
