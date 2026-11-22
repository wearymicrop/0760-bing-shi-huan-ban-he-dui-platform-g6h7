import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from app.manifest import digest
from app.modules.archive import service
from app.source import sample
from app.store import corrections_for, find, history


def _night():
    return json.loads(sample('night').decode('utf-8-sig'))


def _corrected_document():
    """修复 0042 改单午夜后结束时间误录：00:20 -> 00:10。"""
    data = _night()
    for row in data['events']:
        if row.get('booking') == '0042' and row.get('revision') == 2 and row.get('action') == 'move':
            row['end'] = '2026-11-21T00:10:00+08:00'
    return json.dumps(data, ensure_ascii=False).encode('utf-8')


def _second_correction(document):
    """在第一份更正基础上再修一处：0057 一格的一分钟预订延长到 3 分钟。"""
    data = json.loads(document.decode('utf-8'))
    for row in data['events']:
        if row.get('booking') == '0057':
            row['end'] = '2026-11-20T20:37:00+08:00'
    return json.dumps(data, ensure_ascii=False).encode('utf-8')


def test_correction_keeps_original_batch_immutable(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    before_raw, before_bundle = service.get(path, original['id'])

    entry, existed = service.correct(path, original['id'], _corrected_document(),
                                     '0042 时券误录', '夜班组', 'fix-1')
    assert not existed and entry['status'] == 'pending'

    after_raw, after_bundle = service.get(path, original['id'])
    # 原批次的源字节与封存结果都没有被改写或覆盖。
    assert after_raw == before_raw
    assert after_bundle == before_bundle
    assert find(path, original['id'])[0] == before_raw


def test_correction_links_to_original_and_seals_new_batch(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    entry, _ = service.correct(path, original['id'], _corrected_document(), '理由', '人', 'k')
    assert entry['original_id'] == original['id']
    assert entry['supersedes_id'] == original['id']
    assert entry['corrected_id'] != original['id']
    # 更正后批次作为独立封存批次可独立取回，且结果确有变化。
    corrected_raw, corrected = service.get(path, entry['corrected_id'])
    assert digest(corrected_raw) == entry['manifest']['basis_sha256']
    assert corrected['manifest']['result_sha256'] == entry['manifest']['corrected_result_sha256']
    assert corrected['views']['balance']['tally']['booked'] == 330


def test_impact_describes_tally_booking_and_source_changes(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    entry, _ = service.correct(path, original['id'], _corrected_document(), '理由', '人', 'k')
    impact = entry['impact']
    fields = {row['field']: row for row in impact['tally']}
    assert fields['booked']['delta'] == -10 and fields['cash']['delta'] == -10
    booking = next(row for row in impact['bookings'] if row['key'] == '0042')
    assert booking['kind'] == 'changed' and booking['changes']['booked'] == -10
    source = next(row for row in impact['sources'] if row['key'] == '0042')
    assert source['kind'] == 'modified' and source['before'] != source['after']
    assert impact['summary']['source_rows_changed'] == 1


def test_retry_same_correction_does_not_create_second(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    doc = _corrected_document()
    first, existed_first = service.correct(path, original['id'], doc, '理由', '人', 'same-key')
    second, existed_second = service.correct(path, original['id'], doc, '理由', '人', 'same-key')
    assert not existed_first and existed_second
    assert first['id'] == second['id']
    assert len(corrections_for(path, original['id'])) == 1
    # 归档批次也只多出更正后那一份，原批次仍在。
    assert len(history(path)) == 2


def test_concurrent_correction_publishes_single_entry(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    doc = _corrected_document()
    with ThreadPoolExecutor(max_workers=4) as workers:
        rows = list(workers.map(
            lambda _: service.correct(path, original['id'], doc, '理由', '人', 'race-key'),
            range(4)))
    ids = {entry['id'] for entry, _ in rows}
    assert len(ids) == 1
    assert sum(not existed for _, existed in rows) == 1
    assert len(corrections_for(path, original['id'])) == 1


def test_review_is_idempotent_and_promotes_current_version(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    entry, _ = service.correct(path, original['id'], _corrected_document(), '理由', '人', 'k')

    # 复核前当前可用版本仍是原批次。
    current_id, current, _ = service.current_version(path, original['id'])
    assert current_id == original['id'] and current is None

    reviewed = service.review(path, entry['id'])
    assert reviewed['status'] == 'effective' and reviewed['reviewed_at']
    reviewed_again = service.review(path, entry['id'])
    assert reviewed_again['id'] == entry['id'] and reviewed_again['status'] == 'effective'
    assert len(corrections_for(path, original['id'])) == 1

    current_id, current, _ = service.current_version(path, original['id'])
    assert current_id == entry['corrected_id'] and current['id'] == entry['id']


def test_pending_correction_does_not_become_current(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    service.correct(path, original['id'], _corrected_document(), '理由', '人', 'k')
    current_id, current, chain = service.current_version(path, original['id'])
    assert current_id == original['id'] and current is None and len(chain) == 1


def test_chain_walks_multiple_corrections_to_latest_effective(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    first_doc = _corrected_document()
    first, _ = service.correct(path, original['id'], first_doc, '第一处', '人', 'k1')
    second_doc = _second_correction(first_doc)
    second, _ = service.correct(path, original['id'], second_doc, '第二处', '人', 'k2')
    assert second['supersedes_id'] == first['id']

    # 只有第一份生效时，当前可用是第一份更正后版本。
    service.review(path, first['id'])
    current_id, current, chain = service.current_version(path, original['id'])
    assert current_id == first['corrected_id'] and len(chain) == 2

    # 第二份复核后，当前可用推进到最新版本，原批次与第一份都仍可追溯。
    service.review(path, second['id'])
    current_id, current, _ = service.current_version(path, original['id'])
    assert current_id == second['corrected_id'] and current['id'] == second['id']
    assert service.get(path, original['id'])[1]['id'] == original['id']
    assert service.get(path, first['corrected_id'])[0] == first_doc


def test_lineage_resolves_when_viewing_corrected_batch(tmp_path):
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    entry, _ = service.correct(path, original['id'], _corrected_document(), '理由', '人', 'k')
    service.review(path, entry['id'])
    original_id, chain = service.lineage(path, entry['corrected_id'])
    assert original_id == original['id'] and len(chain) == 1
    current_id, _, _ = service.current_version(path, entry['corrected_id'])
    assert current_id == entry['corrected_id']


def test_identical_document_is_rejected_as_no_change(tmp_path):
    from app.errors import InputError
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    try:
        service.correct(path, original['id'], sample('night'), '无变化', '人', 'k')
    except InputError as exc:
        assert exc.code == 'CORRECTION_NO_CHANGE'
    else:
        raise AssertionError('应当拒绝无变化更正')
    assert corrections_for(path, original['id']) == []


def test_missing_reason_rejected(tmp_path):
    from app.errors import InputError
    path = tmp_path / 'shifts.db'
    original, _ = service.record(path, sample('night'))
    try:
        service.correct(path, original['id'], _corrected_document(), '   ', '人', 'k')
    except InputError as exc:
        assert exc.code == 'BAD_CORRECTION'
    else:
        raise AssertionError('应当拒绝缺少理由的更正')


def test_review_unknown_correction_is_404(tmp_path):
    from app.errors import InputError
    path = tmp_path / 'shifts.db'
    service.record(path, sample('night'))
    try:
        service.review(path, 'not-there')
    except InputError as exc:
        assert exc.code == 'CORRECTION_MISSING' and exc.status == 404
    else:
        raise AssertionError('应当返回 404')


def test_export_document_links_original_and_correction(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    payload = {'source': _corrected_document().decode(), 'reason': '0042 误录',
               'operator': '夜班组', 'idempotency_key': 'fix-1'}
    correction = client.post(f"/api/runs/{created['id']}/corrections", json=payload).json
    export = client.get(f"/api/corrections/{correction['id']}/export")
    assert export.status_code == 200 and 'attachment' in export.headers['Content-Disposition']
    document = json.loads(export.data)
    assert document['correction']['original_id'] == created['id']
    assert document['original_batch']['run_id'] == created['id']
    assert document['corrected_batch']['run_id'] == correction['corrected_id']
    # 更正凭证中保留原批次源文件、更正依据与影响，三方可相互核对。
    assert digest(document['original_batch']['original_json'].encode()) == created['manifest']['input_sha256']
    assert document['basis_json']
    assert document['correction']['impact']['summary']['bookings_changed'] == 1


def test_api_retry_and_review_are_idempotent(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    payload = {'source': _corrected_document().decode(), 'reason': '误录', 'idempotency_key': 'idem'}
    first = client.post(f"/api/runs/{created['id']}/corrections", json=payload)
    second = client.post(f"/api/runs/{created['id']}/corrections", json=payload)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json['id'] == second.json['id']

    review_one = client.post(f"/api/corrections/{first.json['id']}/review")
    review_two = client.post(f"/api/corrections/{first.json['id']}/review")
    assert review_one.status_code == review_two.status_code == 200
    assert review_one.json == review_two.json and review_one.json['status'] == 'effective'

    detail = client.get(f"/api/runs/{created['id']}").json
    assert detail['corrections']['is_current'] is False
    assert detail['corrections']['current_id'] == first.json['corrected_id']


def test_correction_accepts_multipart_form(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    response = client.post(
        f"/api/runs/{created['id']}/corrections",
        data={'source': (BytesIO(_corrected_document()), 'fix.json'),
              'reason': '表单上传更正', 'operator': '财务', 'idempotency_key': 'form-1'},
        content_type='multipart/form-data')
    assert response.status_code == 201 and response.json['reason'] == '表单上传更正'


def test_archive_page_shows_chain_and_banners(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    correction = client.post(
        f"/api/runs/{created['id']}/corrections",
        json={'source': _corrected_document().decode(), 'reason': '误录更正',
              'idempotency_key': 'page-1'}).json
    client.post(f"/api/corrections/{correction['id']}/review")

    original_page = client.get(f"/desk/archive?run={created['id']}").data.decode()
    assert '已被更正' in original_page and '更正台账' in original_page
    assert correction['id'][:12] in original_page

    corrected_page = client.get(f"/desk/balance?run={correction['corrected_id']}").data.decode()
    assert '更正生效' in corrected_page and created['id'][:12] in corrected_page


def test_bad_correction_payload_returns_domain_error(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    response = client.post(f"/api/runs/{created['id']}/corrections", json={'reason': '缺源文件'})
    assert response.status_code == 422 and response.json['code'] == 'BAD_CORRECTION'


def test_invalid_basis_is_rejected_without_publishing(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    response = client.post(
        f"/api/runs/{created['id']}/corrections",
        json={'source': '{"windows":[{}]}', 'reason': '坏依据'})
    assert response.status_code == 422 and response.json['code'] == 'BAD_SOURCE'
    # 解析失败不新增任何更正记录，原批次仍是唯一当前版本。
    detail = client.get(f"/api/runs/{created['id']}").json
    assert detail['corrections']['chain'] == []
    assert detail['corrections']['is_current'] is True


def test_unknown_original_batch_is_404(client):
    response = client.post('/api/runs/not-found/corrections',
                           json={'source': '{}', 'reason': 'x'})
    assert response.status_code == 404 and response.json['code'] == 'RUN_MISSING'


def test_bom_basis_and_auto_idempotency_key(client):
    created = client.post('/api/runs', json={'sample': 'night'}).json
    data = _night()
    for row in data['events']:
        if row.get('booking') == '0042' and row.get('revision') == 2:
            row['end'] = '2026-11-21T00:10:00+08:00'
    document = json.dumps(data, ensure_ascii=False)
    bom_document = '﻿' + document
    bom_response = client.post(
        f"/api/runs/{created['id']}/corrections", json={'source': bom_document, 'reason': 'BOM 更正'})
    assert bom_response.status_code == 201

    # 不传幂等键时按依据与理由自动生成，重复提交仍只保留一份。
    first = client.post(f"/api/runs/{created['id']}/corrections",
                        json={'source': document, 'reason': '自动键'})
    second = client.post(f"/api/runs/{created['id']}/corrections",
                         json={'source': document, 'reason': '自动键'})
    assert first.status_code == 201 and second.status_code == 200
    assert first.json['id'] == second.json['id']

    # 理由不同即视为不同更正，依据相同也应另立一份更正记录（更正批次本身复用）。
    other = client.post(f"/api/runs/{created['id']}/corrections",
                        json={'source': document, 'reason': '不同理由'})
    assert other.status_code == 201 and other.json['id'] != first.json['id']
    assert other.json['corrected_id'] == first.json['corrected_id']
