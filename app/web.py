from flask import Blueprint, current_app, jsonify, render_template, request
from app import source
from app.config import SAMPLES, VERSION
from app.errors import InputError
from app.modules.archive import service
from app.modulespec import MODULES

pages = Blueprint('pages', __name__)

KIND_LABELS = {'added': '新增', 'removed': '移除', 'changed': '变更', 'modified': '修改'}
SECTION_LABELS = {'windows': '营业窗', 'passes': '时券版本', 'events': '订单事件'}
TALLY_LABELS = {'booked': '有效预订', 'debit': '扣券', 'cash': '自付', 'waived': '豁免',
                'disputed': '争议', 'difference': '分钟差额', 'voucher_difference': '权益差额'}


def kind_label(value):
    return KIND_LABELS.get(value, value)


def section_label(value):
    return SECTION_LABELS.get(value, value)


def tally_label(value):
    return TALLY_LABELS.get(value, value)


def data(module, run_id):
    if module not in MODULES:
        raise InputError('BAD_ROW', 404)
    path = current_app.config['DB_PATH']
    bundle = service.get(path, run_id)[1] if run_id else service.record(path, source.sample('night'))[0]
    stage = service.register_view(path, bundle) if module == 'archive' else bundle['views'][module]
    return bundle, stage


def correction_context(path, bundle):
    original_id, chain = service.lineage(path, bundle['id'])
    current_id, current, _ = service.current_version(path, bundle['id'])
    # 当前查看的若是某份更正后的批次，回溯到它对应的更正凭证。
    correction_id = None
    for entry in chain:
        if entry['corrected_id'] == bundle['id']:
            correction_id = entry['id']
    return {'is_current': current_id == bundle['id'], 'current_id': current_id,
            'chain': chain, 'correction_id': correction_id, 'original_id': original_id}


@pages.get('/')
def home():
    return page('debit')


@pages.get('/desk/<module>')
def page(module):
    bundle, stage = data(module, request.args.get('run'))
    title, _, columns = MODULES[module]
    correction_info = correction_context(current_app.config['DB_PATH'], bundle)
    return render_template(f'{module}.html', module=module, title=title, columns=columns, run=bundle,
                           view=stage, tally=bundle['views']['balance']['tally'], modules=MODULES,
                           version=VERSION, samples=SAMPLES, correction_info=correction_info)


@pages.get('/api/modules/<module>/<run_id>')
def api_stage(module, run_id):
    bundle, stage = data(module, run_id)
    return jsonify({'id': bundle['id'], **stage})
