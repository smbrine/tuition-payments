import csv
import io
import sqlite3
import tempfile
import unittest
import json
import queue
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from app import Ledger, handler, kopecks


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(':memory:')
        self.ident = self.ledger.charge('ST-001', 'EDU-001', '60000', '2026-11-15')

    def tearDown(self):
        self.ledger.db.close()

    def test_partial_and_full_payment(self):
        self.ledger.pay(self.ident, '20000', '2026-11-01', 'P-1')
        self.assertEqual(self.ledger.rows('2026-11-10')[0]['balance'], 4000000)
        self.assertEqual(self.ledger.rows('2026-11-10')[0]['status'], 'Частично оплачено')
        self.ledger.pay(self.ident, '40000', '2026-11-02', 'P-2')
        self.assertEqual(self.ledger.rows('2026-11-30')[0]['status'], 'Оплачено')

    def test_duplicate_reference_does_not_change_balance(self):
        self.ledger.pay(self.ident, '100', '2026-11-01', 'P-1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.pay(self.ident, '100', '2026-11-01', 'P-1')
        self.assertEqual(self.ledger.rows()[0]['paid'], 10000)

    def test_overpayment_rolls_back(self):
        with self.assertRaises(ValueError):
            self.ledger.pay(self.ident, '60000.01', '2026-11-01', 'P-1')
        self.assertEqual(self.ledger.rows()[0]['paid'], 0)

    def test_money_validation(self):
        self.assertEqual(kopecks('0.01'), 1)
        for value in ['0', '-1', '1.001', 'NaN', 'Infinity', 'abc', '100000001']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                kopecks(value)

    def test_due_date_boundary(self):
        self.assertEqual(self.ledger.rows('2026-11-15')[0]['status'], 'Ожидает оплаты')
        self.assertEqual(self.ledger.rows('2026-11-16')[0]['status'], 'Просрочено')

    def test_missing_charge_and_invalid_date(self):
        with self.assertRaises(ValueError):
            self.ledger.pay(999, '100', '2026-11-01', 'P-1')
        with self.assertRaises(ValueError):
            self.ledger.charge('ST-002', 'EDU-002', '100', '2026-02-30')

    def test_export_and_backup(self):
        self.ledger.pay(self.ident, '20000', '2026-11-01', 'P-1')
        rows = list(csv.reader(io.StringIO(self.ledger.export()), delimiter=';'))
        self.assertEqual(rows[1][3:6], ['60000.00', '20000.00', '40000.00'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup.db'
            self.ledger.backup(path)
            restored = Ledger(path)
            self.assertEqual(restored.rows()[0]['balance'], 4000000)
            restored.db.close()

    def test_invalid_identifiers_and_duplicate_contract(self):
        for student, contract in [(None, 'EDU-002'), ('ST-002', None), (' ', 'EDU-002')]:
            with self.subTest(student=student, contract=contract), self.assertRaises(ValueError):
                self.ledger.charge(student, contract, '100', '2026-12-01')
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.charge('ST-002', 'EDU-001', '100', '2026-12-01')
        self.assertEqual(len(self.ledger.rows()), 1)

    def test_csv_identifiers_cannot_be_formulas(self):
        self.ledger.charge('=1+1', '@SUM(A1)', '100', '2026-12-01')
        rows = list(csv.reader(io.StringIO(self.ledger.export()), delimiter=';'))
        self.assertEqual(rows[2][1:3], ["'=1+1", "'@SUM(A1)"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        ready = queue.Queue()

        def serve():
            ledger = Ledger(':memory:')
            server = HTTPServer(('127.0.0.1', 0), handler(ledger))
            ready.put(server)
            try:
                server.serve_forever(poll_interval=0.01)
            finally:
                server.server_close()
                ledger.db.close()

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        self.server = ready.get(timeout=5)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)

    def post(self, path, data, origin=None):
        request = Request(self.base + path, data=json.dumps(data).encode(),
                          headers={'Content-Type': 'application/json',
                                   'Origin': origin or self.base})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_create_pay_and_read(self):
        self.assertEqual(self.post('/api/charges', dict(student='ST-003', contract='EDU-003',
                             amount='1000', due='2026-12-01'))[0], 201)
        self.assertEqual(self.post('/api/payments', dict(charge_id=1, amount='400',
                             paid='2026-11-27', reference='P-003'))[0], 201)
        with urlopen(self.base + '/api/charges', timeout=5) as response:
            self.assertEqual(json.load(response)[0]['balance'], 60000)
        with urlopen(self.base + '/', timeout=5) as response:
            self.assertIn('Учет оплаты обучения', response.read().decode())
        with urlopen(self.base + '/export.csv', timeout=5) as response:
            self.assertIn('EDU-003', response.read().decode())

    def test_invalid_body_and_cross_origin(self):
        self.assertEqual(self.post('/api/charges', [])[0], 400)
        self.assertEqual(self.post('/api/charges', {}, 'http://example.com')[0], 403)
        request = Request(self.base + '/api/charges', data=b'{broken',
                          headers={'Origin': self.base})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == '__main__':
    unittest.main()
