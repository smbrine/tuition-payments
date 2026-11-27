"""Учебный локальный реестр начислений и оплат. Python 3.10+, без зависимостей."""
import argparse
import csv
import io
import json
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse


def kopecks(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal('100000000'):
            raise ValueError('Сумма должна быть больше нуля и не более 100 млн рублей')
        if amount * 100 != (amount * 100).to_integral_value():
            raise ValueError('Допускается не более двух знаков после запятой')
        return int(amount * 100)
    except InvalidOperation as exc:
        raise ValueError('Некорректная сумма') from exc


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS charges (
                id INTEGER PRIMARY KEY, student TEXT NOT NULL,
                contract TEXT NOT NULL UNIQUE, amount INTEGER NOT NULL CHECK(amount>0),
                due TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY, charge_id INTEGER NOT NULL REFERENCES charges(id),
                amount INTEGER NOT NULL CHECK(amount>0), paid TEXT NOT NULL,
                reference TEXT NOT NULL UNIQUE);
            CREATE INDEX IF NOT EXISTS payment_charge ON payments(charge_id);
            CREATE INDEX IF NOT EXISTS charge_due ON charges(due);
        ''')

    def charge(self, student, contract, amount, due):
        if not isinstance(student, str) or not isinstance(contract, str):
            raise ValueError('Код студента и договор должны быть строками')
        student, contract = student.strip(), contract.strip()
        if not 1 <= len(student) <= 100 or not 1 <= len(contract) <= 50:
            raise ValueError('Укажите код студента и номер договора допустимой длины')
        due = date.fromisoformat(due).isoformat()
        with self.db:
            c = self.db.execute('INSERT INTO charges(student,contract,amount,due) VALUES(?,?,?,?)',
                                (student, contract, kopecks(amount), due))
            return c.lastrowid

    def pay(self, charge_id, amount, paid, reference):
        cents = kopecks(amount)
        paid = date.fromisoformat(paid).isoformat()
        if not isinstance(reference, str):
            raise ValueError('Номер платежа должен быть строкой')
        reference = reference.strip()
        if not 1 <= len(reference) <= 100:
            raise ValueError('Укажите уникальный номер платежа до 100 символов')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            charge = self.db.execute('SELECT amount FROM charges WHERE id=?', (charge_id,)).fetchone()
            if charge is None:
                raise ValueError('Начисление не найдено')
            total = self.db.execute('SELECT COALESCE(SUM(amount),0) FROM payments WHERE charge_id=?',
                                    (charge_id,)).fetchone()[0]
            if cents > charge['amount'] - total:
                raise ValueError('Платеж превышает остаток задолженности')
            self.db.execute('INSERT INTO payments(charge_id,amount,paid,reference) VALUES(?,?,?,?)',
                            (charge_id, cents, paid, reference))

    def rows(self, as_of=None):
        today = date.fromisoformat(as_of) if as_of else date.today()
        result = []
        for row in self.db.execute('''SELECT c.*,COALESCE(SUM(p.amount),0) AS paid
                                      FROM charges c LEFT JOIN payments p ON c.id=p.charge_id
                                      GROUP BY c.id ORDER BY c.due,c.id'''):
            r = dict(row)
            r['balance'] = r['amount'] - r['paid']
            r['status'] = ('Оплачено' if r['balance'] == 0 else
                           'Просрочено' if date.fromisoformat(r['due']) < today else
                           'Частично оплачено' if r['paid'] else 'Ожидает оплаты')
            result.append(r)
        return result

    def export(self):
        out = io.StringIO(newline='')
        w = csv.writer(out, delimiter=';')
        w.writerow(['ID', 'Код студента', 'Договор', 'Начислено', 'Оплачено', 'Остаток', 'Срок', 'Статус'])
        for r in self.rows():
            # Spreadsheet programs must treat user-supplied identifiers as text.
            def csv_text(value):
                return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
            w.writerow([r['id'], csv_text(r['student']), csv_text(r['contract']), f"{r['amount']/100:.2f}",
                        f"{r['paid']/100:.2f}", f"{r['balance']/100:.2f}", r['due'], r['status']])
        return out.getvalue()

    def backup(self, path):
        with sqlite3.connect(str(path)) as target:
            self.db.backup(target)


def handler(ledger):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, content, mime='application/json; charset=utf-8'):
            payload = content.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/charges':
                self.respond(200, json.dumps(ledger.rows(), ensure_ascii=False))
            elif path == '/export.csv':
                self.respond(200, '\ufeff' + ledger.export(), 'text/csv; charset=utf-8')
            elif path in ['/', '/app.js', '/style.css']:
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
                self.respond(200, Path(__file__).with_name(name).read_text(encoding='utf-8'), mime+'; charset=utf-8')
            else:
                self.respond(404, '{}')

        def do_POST(self):
            # Browser mutations must originate from this local interface.
            expected = 'http://' + self.headers.get('Host', '')
            if self.headers.get('Origin') != expected:
                self.respond(403, json.dumps({'error': 'Недопустимый источник запроса'}))
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 8192:
                    raise ValueError('Недопустимый размер запроса')
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError('Ожидается JSON-объект')
                if self.path == '/api/charges':
                    ledger.charge(data['student'], data['contract'], data['amount'], data['due'])
                elif self.path == '/api/payments':
                    ledger.pay(int(data['charge_id']), data['amount'], data['paid'], data['reference'])
                else:
                    self.respond(404, '{}')
                    return
                self.respond(201, '{"ok":true}')
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, sqlite3.IntegrityError) as exc:
                message = 'Договор или номер платежа уже существует' if isinstance(exc, sqlite3.IntegrityError) else str(exc)
                self.respond(400, json.dumps({'error': message}, ensure_ascii=False))

        def log_message(self, *args):
            pass
    return Handler


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='tuition.db')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--backup', metavar='PATH')
    args = parser.parse_args()
    ledger = Ledger(args.db)
    if args.demo and not ledger.rows():
        demo_id = ledger.charge('ST-001', 'EDU-2026-001', '60000', '2026-11-15')
        ledger.charge('ST-002', 'EDU-2026-002', '45000', '2026-12-15')
        ledger.pay(demo_id, '20000', '2026-11-01', 'DEMO-001')
    if args.backup:
        ledger.backup(args.backup)
    else:
        print(f'Учебный стенд: http://127.0.0.1:{args.port} (Ctrl+C для остановки)')
        HTTPServer(('127.0.0.1', args.port), handler(ledger)).serve_forever()
