from flask import Blueprint, Response, current_app, jsonify, request
from app import manifest, source
from app.errors import InputError
from . import service

routes = Blueprint('archive', __name__)


@routes.post('/api/runs')
def run():
    if 'source' in request.files:
        document = request.files['source'].read()
    else:
        params = request.get_json(silent=True)
        if not isinstance(params, dict) or not isinstance(params.get('sample'), str):
            raise InputError('BAD_SOURCE')
        document = source.sample(params['sample'])
    bundle, reused = service.record(current_app.config['DB_PATH'], document)
    return jsonify(bundle | {'reused': reused}), 200 if reused else 201


@routes.post('/api/runs/<run_id>/rerun')
def rerun(run_id):
    path = current_app.config['DB_PATH']
    raw, _ = service.get(path, run_id)
    bundle, reused = service.record(path, raw, refresh=True)
    return jsonify(bundle | {'reused': reused})


@routes.get('/api/runs/<run_id>')
def detail(run_id):
    path = current_app.config['DB_PATH']
    bundle = service.get(path, run_id)[1]
    current_id, current, chain = service.current_version(path, run_id)
    return jsonify(bundle | {'corrections': {'chain': chain, 'current_id': current_id,
                                             'is_current': current_id == run_id,
                                             'current_correction_id': current['id'] if current else None}})


@routes.get('/api/runs/<run_id>/export')
def export_run(run_id):
    raw, bundle = service.get(current_app.config['DB_PATH'], run_id)
    document = bundle | {'original_json': raw.decode('utf-8')}
    return Response(manifest.encode(document), mimetype='application/json',
                    headers={'Content-Disposition': f'attachment; filename="rink-{run_id[:12]}.json"'})


def _correction_payload():
    if 'source' in request.files:
        document = request.files['source'].read()
        form = request.form
        return document, form.get('reason', ''), form.get('operator', ''), form.get('idempotency_key', '')
    params = request.get_json(silent=True)
    if not isinstance(params, dict) or not isinstance(params.get('source'), str):
        raise InputError('BAD_CORRECTION')
    try:
        document = params['source'].encode('utf-8')
    except UnicodeEncodeError as exc:
        raise InputError('BAD_CORRECTION') from exc
    return (document, params.get('reason', ''), params.get('operator', ''),
            params.get('idempotency_key', ''))


@routes.post('/api/runs/<run_id>/corrections')
def create_correction(run_id):
    path = current_app.config['DB_PATH']
    document, reason, operator, idem = _correction_payload()
    entry, existed = service.correct(path, run_id, document, reason, operator, idem)
    # 同一更正重试或复核两次：返回同一份凭证，不新增第二份。
    return jsonify(entry), 200 if existed else 201


@routes.post('/api/corrections/<correction_id>/review')
def review_correction(correction_id):
    entry = service.review(current_app.config['DB_PATH'], correction_id)
    return jsonify(entry)


@routes.get('/api/corrections/<correction_id>')
def correction_detail(correction_id):
    return jsonify(service.correction_record(current_app.config['DB_PATH'], correction_id))


@routes.get('/api/corrections/<correction_id>/export')
def export_correction(correction_id):
    path = current_app.config['DB_PATH']
    entry, basis_raw = service.correction_basis(path, correction_id)
    original_raw, original_bundle = service.get(path, entry['original_id'])
    corrected_raw, corrected_bundle = service.get(path, entry['corrected_id'])
    document = {
        'correction': entry,
        'original_batch': {
            'run_id': entry['original_id'],
            'manifest': original_bundle['manifest'],
            'original_json': original_raw.decode('utf-8'),
        },
        'corrected_batch': {
            'run_id': entry['corrected_id'],
            'manifest': corrected_bundle['manifest'],
            'original_json': corrected_raw.decode('utf-8'),
        },
        'basis_json': basis_raw.decode('utf-8-sig'),
    }
    return Response(
        manifest.encode(document), mimetype='application/json',
        headers={'Content-Disposition': f'attachment; filename="correction-{correction_id[:12]}.json"'})
