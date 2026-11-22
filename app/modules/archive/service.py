from datetime import datetime, timezone
import json
from time import monotonic_ns
from app import manifest, pipeline, store
from app.config import VERSION
from app.errors import InputError
from . import corrections as impact


def record(path, raw, refresh=False):
    run_id = manifest.identify(raw)
    previous = store.find(path, run_id)
    if previous and not refresh:
        return json.loads(previous[1]), True
    started = monotonic_ns()
    views = pipeline.process(raw)
    result_hash = manifest.digest(manifest.encode(views).encode())
    if previous:
        saved = json.loads(previous[1])
        if result_hash != saved['manifest']['result_sha256']:
            raise RuntimeError('同版算式复算异常')
        return saved, True
    created = datetime.now(timezone.utc).isoformat()
    bundle = {'id': run_id, 'manifest': {'input_sha256': manifest.digest(raw), 'result_sha256': result_hash,
              'version': VERSION, 'created_at': created, 'cutoff': views['changes']['cutoff'],
              'elapsed_ms': (monotonic_ns() - started) // 1_000_000}, 'views': views}
    saved, reused = store.save(path, run_id, raw, manifest.encode(bundle), created)
    return json.loads(saved), reused


def get(path, run_id):
    stored = store.find(path, run_id)
    if not stored:
        raise InputError('RUN_MISSING', 404)
    return stored[0], json.loads(stored[1])


def _now():
    return datetime.now(timezone.utc).isoformat()


def _chain(path, original_id):
    """按时间顺序返回原批次之后的更正链（不含原批次本身）。"""
    return [json.loads(row) for row in store.corrections_for(path, original_id)]


def lineage(path, run_id):
    """返回任意批次（原批次或某份更正后批次）所属的原批次号与完整更正链。"""
    get(path, run_id)  # 批次存在性校验，否则 404
    linked = store.find_correction_by_corrected(path, run_id)
    original_id = json.loads(linked)['original_id'] if linked else run_id
    return original_id, _chain(path, original_id)


def current_version(path, run_id):
    """沿更正链找到当前可用版本：原批次或最近一份生效更正。"""
    original_id, chain = lineage(path, run_id)
    effective = [entry for entry in chain if entry['status'] == 'effective']
    if not effective:
        return original_id, None, chain
    latest = effective[-1]
    return latest['corrected_id'], latest, chain


def correct(path, original_id, corrected_raw, reason, operator, idempotency_key):
    """登记一份更正。原批次与更正依据均原样留存，重试不新增第二份。"""
    original_raw, original_bundle = get(path, original_id)
    if not reason or not reason.strip():
        raise InputError('BAD_CORRECTION')
    reason = reason.strip()[:300]
    operator = (operator or '').strip()[:60]
    idempotency_key = (idempotency_key or '').strip()[:120]
    if not idempotency_key:
        idempotency_key = manifest.digest(original_id.encode() + b'\0' + corrected_raw
                                          + b'\0' + reason.encode())
    chain = _chain(path, original_id)
    supersedes_id = chain[-1]['id'] if chain else original_id
    corrected_bundle, _ = record(path, corrected_raw)
    if corrected_bundle['id'] == original_id:
        raise InputError('CORRECTION_NO_CHANGE')
    chain = _chain(path, original_id)
    existing = next((entry for entry in chain if entry['idempotency_key'] == idempotency_key), None)
    if existing:
        return existing, True
    effect = impact.build(original_bundle, corrected_bundle,
                          json.loads(original_raw.decode('utf-8-sig')),
                          json.loads(corrected_raw.decode('utf-8-sig')))
    created = _now()
    correction_id = manifest.digest(
        (original_id + idempotency_key + created).encode())
    entry = {
        'id': correction_id,
        'original_id': original_id,
        'supersedes_id': supersedes_id,
        'corrected_id': corrected_bundle['id'],
        'idempotency_key': idempotency_key,
        'reason': reason,
        'operator': operator,
        'status': 'pending',
        'created_at': created,
        'reviewed_at': None,
        'manifest': {
            'basis_sha256': manifest.digest(corrected_raw),
            'original_input_sha256': original_bundle['manifest']['input_sha256'],
            'corrected_input_sha256': corrected_bundle['manifest']['input_sha256'],
            'original_result_sha256': original_bundle['manifest']['result_sha256'],
            'corrected_result_sha256': corrected_bundle['manifest']['result_sha256'],
            'version': VERSION,
        },
        'impact': effect,
    }
    stored, existed = store.insert_correction(path, {
        'id': correction_id, 'original_id': original_id, 'supersedes_id': supersedes_id,
        'idempotency_key': idempotency_key, 'reason': reason, 'operator': operator,
        'basis': corrected_raw, 'record': manifest.encode(entry), 'status': 'pending',
        'created_at': created, 'reviewed_at': None})
    if existed:
        # 并发下唯一索引拦截了重复更正：返回已落库的那一份，不新增第二份。
        return json.loads(stored), True
    return json.loads(stored), False


def review(path, correction_id):
    existing = store.get_correction(path, correction_id)
    if existing is None:
        raise InputError('CORRECTION_MISSING', 404)
    entry = json.loads(existing)
    if entry['status'] != 'effective':
        entry['status'] = 'effective'
        entry['reviewed_at'] = _now()
    recorded = store.review_correction(path, correction_id, entry['reviewed_at'],
                                       manifest.encode(entry))
    return json.loads(recorded)


def correction_record(path, correction_id):
    recorded = store.get_correction(path, correction_id)
    if recorded is None:
        raise InputError('CORRECTION_MISSING', 404)
    return json.loads(recorded)


def correction_basis(path, correction_id):
    entry = correction_record(path, correction_id)
    # 更正依据（更正后的源文件）作为独立封存批次保存，可直接取回原字节。
    raw, _ = get(path, entry['corrected_id'])
    return entry, raw


def register_view(path, bundle):
    runs = store.history(path)
    original_id, chain = lineage(path, bundle['id'])
    current_id, current, _ = current_version(path, bundle['id'])
    return {'rows': [{'key': run_id[:12], 'created': created, 'run_id': run_id} for run_id, created in runs],
            'issues': [], 'metrics': {'已归档': len(runs), '输入字节': len(get(path, bundle['id'])[0]),
                                      '更正记录': len(chain)},
            'manifest': bundle['manifest'],
            'corrections': {'chain': chain, 'current_id': current_id,
                            'is_current': current_id == bundle['id'],
                            'current_correction_id': current['id'] if current else None,
                            'original_id': original_id}}
