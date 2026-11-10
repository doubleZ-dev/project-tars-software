"""Persist conservative reservations. A crashed request never refunds itself."""
import sqlite3
from pathlib import Path


class Allowance:
    RESERVE = 20000

    def __init__(self, path, free_tokens, *, renew=False):
        if free_tokens < 100000: raise ValueError('verify_current_free_tokens_first')
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS allowance(id INTEGER PRIMARY KEY CHECK(id=1), available INTEGER)')
        with self.db:
            if renew:
                self.db.execute('DELETE FROM allowance')
            self.db.execute('INSERT OR IGNORE INTO allowance VALUES(1,?)', (free_tokens-50000,))
            # A lower current console balance is always respected; a higher one
            # only resets the ledger through explicit local operator renewal.
            self.db.execute('UPDATE allowance SET available=min(available,?)', (free_tokens-50000,))

    def reserve(self):
        with self.db:
            row = self.db.execute('UPDATE allowance SET available=available-? WHERE id=1 AND available>=?',
                                  (self.RESERVE, self.RESERVE))
            if row.rowcount != 1: raise RuntimeError('free_allowance_reserve_exhausted')

    def settle(self, actual):
        if not isinstance(actual, int) or actual <= 0: raise ValueError('usage_unavailable')
        with self.db:
            self.db.execute('UPDATE allowance SET available=available+? WHERE id=1', (self.RESERVE-actual,))

    def close(self): self.db.close()
