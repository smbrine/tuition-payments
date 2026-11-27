let records = [];
const money = n => (n / 100).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
function render() {
  const q = document.querySelector('#search').value.toLowerCase();
  const body = document.querySelector('#rows');
  body.replaceChildren();
  records.filter(r => `${r.student} ${r.contract}`.toLowerCase().includes(q)).forEach(r => {
    const tr = document.createElement('tr');
    [r.id,r.student,r.contract,money(r.amount),money(r.paid),money(r.balance),r.due,r.status].forEach(v => {
      const td = document.createElement('td'); td.textContent = v; tr.append(td);
    });
    body.append(tr);
  });
  document.querySelector('#summary').textContent = `Начислено: ${money(records.reduce((s,r)=>s+r.amount,0))} · Оплачено: ${money(records.reduce((s,r)=>s+r.paid,0))} · Остаток: ${money(records.reduce((s,r)=>s+r.balance,0))}`;
}
async function load() {
  const response = await fetch('/api/charges');
  if (!response.ok) throw new Error('Не удалось загрузить реестр');
  records = await response.json(); render();
}
for (const [id, url] of [['charge','/api/charges'],['payment','/api/payments']]) {
  document.getElementById(id).addEventListener('submit', async event => {
    event.preventDefault(); const form = event.currentTarget;
    try {
      const response = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Ошибка операции');
      form.reset(); await load();
      document.querySelector('#message').textContent = 'Данные сохранены';
    } catch (error) { document.querySelector('#message').textContent = error.message; }
  });
}
document.querySelector('#search').addEventListener('input', render);
load().catch(error => { document.querySelector('#message').textContent = error.message; });
