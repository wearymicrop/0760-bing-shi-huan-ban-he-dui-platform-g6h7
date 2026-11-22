"""更正影响计算：只比对两个已封存视图，不改动任何一方的原始记录。"""

TALLY_FIELDS = ('booked', 'debit', 'cash', 'waived', 'disputed', 'difference', 'voucher_difference')


def _index(rows, key):
    return {row[key]: row for row in rows}


def _delta_fields(before, after, fields):
    return {field: after.get(field, 0) - before.get(field, 0)
            for field in fields if after.get(field, 0) != before.get(field, 0)}


def tally_impact(before, after):
    old = before['balance']['tally']
    new = after['balance']['tally']
    return [{'field': field, 'label': field, 'before': old.get(field, 0), 'after': new.get(field, 0),
             'delta': new.get(field, 0) - old.get(field, 0)}
            for field in TALLY_FIELDS if old.get(field, 0) != new.get(field, 0)]


def booking_impact(before, after):
    fields = ('booked', 'debit', 'cash', 'waived', 'disputed', 'difference')
    old = _index(before['balance']['rows'], 'key')
    new = _index(after['balance']['rows'], 'key')
    rows = []
    for key in sorted(set(old) | set(new)):
        left, right = old.get(key), new.get(key)
        if left is None:
            rows.append({'key': key, 'kind': 'added', 'rink': right.get('rink', ''),
                         'changes': _delta_fields({f: 0 for f in fields}, right, fields)})
        elif right is None:
            rows.append({'key': key, 'kind': 'removed', 'rink': left.get('rink', ''),
                         'changes': _delta_fields(left, {f: 0 for f in fields}, fields)})
        else:
            changes = _delta_fields(left, right, fields)
            if changes:
                rows.append({'key': key, 'kind': 'changed', 'rink': right.get('rink', left.get('rink', '')),
                             'changes': changes})
    return rows


def voucher_impact(before, after):
    fields = ('allowed', 'used', 'remaining')
    old = _index(before['debit']['vouchers'], 'key')
    new = _index(after['debit']['vouchers'], 'key')
    rows = []
    for key in sorted(set(old) | set(new)):
        left, right = old.get(key), new.get(key)
        if left is None:
            rows.append({'key': key, 'kind': 'added', 'owner': right.get('owner', ''),
                         'changes': _delta_fields({f: 0 for f in fields}, right, fields)})
        elif right is None:
            rows.append({'key': key, 'kind': 'removed', 'owner': left.get('owner', ''),
                         'changes': _delta_fields(left, {f: 0 for f in fields}, fields)})
        else:
            changes = _delta_fields(left, right, fields)
            if changes:
                rows.append({'key': key, 'kind': 'changed',
                             'owner': right.get('owner', left.get('owner', '')), 'changes': changes})
    return rows


def issue_impact(before, after):
    def signature(issue):
        return (issue.get('code', ''), issue.get('key', ''), issue.get('line', 0))

    old = {signature(x): x for x in before['balance']['issues']}
    new = {signature(x): x for x in after['balance']['issues']}
    removed = [{'code': k[0], 'key': k[1], 'line': k[2]} for k in sorted(set(old) - set(new))]
    added = [{'code': k[0], 'key': k[1], 'line': k[2]} for k in sorted(set(new) - set(old))]
    return {'added': added, 'removed': removed}


def source_impact(before_raw, after_raw):
    """逐段比对源记录，保留双方源行号，说明更正依据落在了哪些输入上。

    以业务身份键（订单+修订号、权益号、冰面+窗口）配对；同身份但内容不同记为 modified，
    同时保留原行与更正后行，确保误录与更正依据都可追溯。
    """
    sections = ('windows', 'passes', 'events')
    report = []
    for section in sections:
        old_rows = before_raw.get(section, [])
        new_rows = after_raw.get(section, [])
        old_index = {}
        for index, row in enumerate(old_rows):
            if isinstance(row, dict):
                old_index.setdefault(_identity_key(section, row), []).append((index + 1, row))
        new_index = {}
        for index, row in enumerate(new_rows):
            if isinstance(row, dict):
                new_index.setdefault(_identity_key(section, row), []).append((index + 1, row))
        for key in sorted(set(old_index) | set(new_index)):
            old_entries = old_index.get(key, [])
            new_entries = new_index.get(key, [])
            if not old_entries:
                for line, row in new_entries:
                    report.append({'section': section, 'kind': 'added', 'line': line,
                                   'key': _identity(section, row), 'raw': row, 'before': None, 'after': row})
                continue
            if not new_entries:
                for line, row in old_entries:
                    report.append({'section': section, 'kind': 'removed', 'line': line,
                                   'key': _identity(section, row), 'raw': row, 'before': row, 'after': None})
                continue
            for (old_line, old_row), (new_line, new_row) in zip(old_entries, new_entries):
                if old_row != new_row:
                    report.append({'section': section, 'kind': 'modified', 'line': new_line,
                                   'original_line': old_line, 'key': _identity(section, new_row),
                                   'raw': new_row, 'before': old_row, 'after': new_row})
    return report


def _identity_key(section, row):
    if section == 'events':
        return (row.get('booking'), row.get('revision'))
    if section == 'passes':
        return (row.get('pass'),)
    return (row.get('rink'), row.get('start'), row.get('end'))


def _identity(section, row):
    if section == 'events':
        return row.get('booking', '')
    if section == 'passes':
        return row.get('pass', '')
    return row.get('rink', '')


def build(before_bundle, after_bundle, before_raw, after_raw):
    before = before_bundle['views']
    after = after_bundle['views']
    tally = tally_impact(before, after)
    bookings = booking_impact(before, after)
    vouchers = voucher_impact(before, after)
    issues = issue_impact(before, after)
    sources = source_impact(before_raw, after_raw)
    return {
        'tally': tally,
        'bookings': bookings,
        'vouchers': vouchers,
        'issues': issues,
        'sources': sources,
        'summary': {
            'tally_fields': len(tally),
            'bookings_changed': len(bookings),
            'vouchers_changed': len(vouchers),
            'issues_added': len(issues['added']),
            'issues_removed': len(issues['removed']),
            'source_rows_changed': len(sources),
            'balanced_before': before['balance']['balanced'],
            'balanced_after': after['balance']['balanced'],
        },
    }
