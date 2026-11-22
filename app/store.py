from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3


@contextmanager
def connection(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute('''CREATE TABLE IF NOT EXISTS shifts(
            id TEXT PRIMARY KEY, source BLOB NOT NULL, bundle TEXT NOT NULL,
            created TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS corrections(
            id TEXT PRIMARY KEY,
            original_id TEXT NOT NULL,
            supersedes_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            operator TEXT NOT NULL,
            basis BLOB NOT NULL,
            record TEXT NOT NULL,
            status TEXT NOT NULL,
            created TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(original_id, idempotency_key))''')
        with db:
            yield db
    finally:
        db.close()


def find(path, run_id):
    with connection(path) as db:
        return db.execute('SELECT source,bundle FROM shifts WHERE id=?', (run_id,)).fetchone()


def save(path, run_id, source, bundle, created):
    with connection(path) as db:
        changed = db.execute('INSERT OR IGNORE INTO shifts VALUES (?,?,?,?)',
                             (run_id, source, bundle, created)).rowcount
        row = db.execute('SELECT bundle FROM shifts WHERE id=?', (run_id,)).fetchone()
    return row[0], not bool(changed)


def history(path):
    with connection(path) as db:
        return db.execute('SELECT id,created FROM shifts ORDER BY created DESC,id LIMIT 50').fetchall()


def insert_correction(path, entry):
    """Append a correction. Returns (record, existed). Never overwrites a prior row."""
    with connection(path) as db:
        changed = db.execute(
            '''INSERT OR IGNORE INTO corrections
               (id,original_id,supersedes_id,idempotency_key,reason,operator,basis,record,status,created,reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (entry['id'], entry['original_id'], entry['supersedes_id'], entry['idempotency_key'],
             entry['reason'], entry['operator'], entry['basis'], entry['record'], entry['status'],
             entry['created_at'], entry.get('reviewed_at'))).rowcount
        row = db.execute('SELECT record FROM corrections WHERE original_id=? AND idempotency_key=?',
                         (entry['original_id'], entry['idempotency_key'])).fetchone()
    return row[0], not bool(changed)


def get_correction(path, correction_id):
    with connection(path) as db:
        row = db.execute('SELECT record FROM corrections WHERE id=?', (correction_id,)).fetchone()
    return row[0] if row else None


def corrections_for(path, original_id):
    with connection(path) as db:
        rows = db.execute(
            'SELECT record FROM corrections WHERE original_id=? ORDER BY created ASC,id ASC',
            (original_id,)).fetchall()
    return [row[0] for row in rows]


def find_correction_by_corrected(path, corrected_id):
    with connection(path) as db:
        rows = db.execute('SELECT record FROM corrections').fetchall()
    for (record,) in rows:
        if json.loads(record).get('corrected_id') == corrected_id:
            return record
    return None


def review_correction(path, correction_id, reviewed_at, updated_record=None):
    """Mark a correction reviewed. Idempotent: a second review changes nothing."""
    with connection(path) as db:
        current = db.execute('SELECT record,status FROM corrections WHERE id=?',
                             (correction_id,)).fetchone()
        if not current:
            return None
        if current[1] != 'effective':
            db.execute('UPDATE corrections SET status=?, reviewed_at=?, record=? WHERE id=?',
                       ('effective', reviewed_at, updated_record, correction_id))
        row = db.execute('SELECT record FROM corrections WHERE id=?', (correction_id,)).fetchone()
    return row[0]
