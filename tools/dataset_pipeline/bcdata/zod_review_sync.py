"""Final review-state sync while Studio is stopped for a batch switch."""
import sqlite3

from .zod_review_migration import carry_reviews


def sync_reviews(root, db):
    counts = carry_reviews(root, db)
    path = root / 'repairs/continuous-boundaries-v4/audits.sqlite'
    if not path.exists():
        return counts
    source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        # Human corrections/rejections must keep their original group hold,
        # including when their complete labels carried forward unchanged.
        held = source.execute('SELECT m.image_id FROM zod_audit_members m JOIN frames f USING(image_id) WHERE m.repair_required=1 AND (f.action IS NOT NULL OR f.correction_json IS NOT NULL)').fetchall()
        db.executemany('UPDATE zod_audit_members SET repair_required=1 WHERE image_id=?', held)
        db.executemany('INSERT OR IGNORE INTO zod_conversion_findings VALUES(?,?,?)',
                       source.execute('SELECT * FROM zod_conversion_findings').fetchall())
        db.execute('UPDATE zod_audit_members SET repair_required=1 WHERE image_id IN (SELECT image_id FROM zod_conversion_findings)')
        db.commit()
        return counts
    finally:
        source.close()
