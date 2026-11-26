# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/src/vitrina.py" дата="2026-10-22">
# @что: витрина «Канцеляриста» — загрузка PDF, лента карточек, расхождения красным,
#       цитаты-источники у каждого поля, кнопки человека. Один HTML, без сборки.
# </@паспорт>

PAGE = """<!doctype html><html lang=ru><meta charset=utf-8>
<title>Канцелярист · витрина</title>
<style>
body{font-family:system-ui;max-width:880px;margin:32px auto;padding:0 16px;background:#101314;color:#e9e3d5}
h1{font-size:1.4rem} small{color:#889}
#drop{border:2px dashed #334;border-radius:10px;padding:22px;text-align:center;cursor:pointer;background:#161a1c}
table{width:100%;border-collapse:collapse;margin-top:14px}
td,th{padding:8px 10px;border-bottom:1px solid #232a2d;text-align:left;font-size:.93rem}
tr{cursor:pointer} tr:hover{background:#1a2022}
.card{background:#161a1c;border-radius:10px;padding:16px;margin-top:14px;display:none}
.k{color:#889;font-size:.85rem}
.mis{background:#3a1416;border-left:4px solid #c0392b;padding:8px 12px;border-radius:6px;margin:6px 0}
.ok{background:#12301f;border-left:4px solid #1e8e5a;padding:8px 12px;border-radius:6px;margin:6px 0}
cite{display:block;color:#7a9;font-size:.8rem;margin-top:2px;white-space:pre-wrap}
.draft{background:#1c2123;padding:12px;border-radius:8px;white-space:pre-wrap;margin-top:8px}
button{padding:9px 14px;border-radius:8px;border:1px solid #334;background:#12684f;color:#fff;font-weight:600;cursor:pointer;margin-right:8px}
button.warn{background:#7a3324}
.tag{font-size:.75rem;border:1px solid #334;border-radius:6px;padding:2px 8px;margin-left:6px}
</style>
<h1>«Канцелярист» — агент входящих документов</h1>
<div id=drop>Перетащи сюда PDF или нажми, чтобы выбрать файл 📄</div>
<input type=file id=file accept=.pdf hidden>
<div id=feed></div>
<div class=card id=card></div>
<small>Каркас к хакатону «ИИкузница»: тип → реквизиты с цитатами → сверка с реестром → черновик → решение человека. Расхождения красным; каждое утверждение с источником.</small>
<script>
const drop=document.getElementById('drop'),file=document.getElementById('file'),
card=document.getElementById('card'),feed=document.getElementById('feed');
let cur=null;
drop.onclick=()=>file.click();
drop.ondragover=e=>{e.preventDefault();drop.style.borderColor='#12684f'};
drop.ondragleave=()=>drop.style.borderColor='#334';
drop.ondrop=e=>{e.preventDefault();if(e.dataTransfer.files[0])send(e.dataTransfer.files[0])};
file.onchange=()=>{if(file.files[0])send(file.files[0])};

function send(f){
  drop.innerHTML='…разбираю «'+f.name+'»';
  const fd=new FormData();fd.append('file',f);
  fetch('/upload',{method:'POST',body:fd}).then(r=>r.json()).then(c=>{
    drop.innerHTML='Перетащи сюда PDF или нажми, чтобы выбрать файл 📄';
    open_card(c.id);load();
  }).catch(e=>drop.innerHTML='Ошибка: '+e);
}

function load(){
  fetch('/api/docs').then(r=>r.json()).then(rows=>{
    feed.innerHTML='<table><tr><th>файл</th><th>тип</th><th>контрагент</th><th>сумма</th><th>⚠ расхождения</th><th>статус</th></tr>'+
      rows.map(r=>`<tr onclick="open_card('${r.id}')"><td>${r.filename}</td><td>${r.doc_type}</td>`+
      `<td>${r.контрагент||'—'}</td><td>${r.сумма||'—'}</td>`+
      `<td>${r.расхождений?('⚠ '+r.расхождений):'—'}</td><td>${r.status}</td></tr>`).join('')+'</table>';
  });
}

function open_card(id){
  cur=id;
  fetch('/api/docs/'+id).then(r=>r.json()).then(c=>{
    let h=`<b>${c.filename}</b> — тип «${c.doc_type}» (уверенность ${c.confidence}) <span class=tag>${c.status}</span>`;
    for(const[k,v]of Object.entries(c.fields||{})){
      h+=`<p style=margin:6px 0><span class=k>${k}:</span> <b>${v.value}</b>`+
         `<cite>источник: ${c.filename}, стр. ${v.страница}: «${v.цитата}»</cite></p>`;
    }
    if((c.mismatches||[]).length){
      h+='<h3>⚠ Расхождения с реестром</h3>';
      for(const m of c.mismatches)
        h+=`<div class=mis><b>${m.поле}</b> (${m.важность}): в документе <b>${m.в_документе}</b>, в реестре <b>${m.в_реестре}</b>`+
           `<div>${m.пояснение}</div></div>`;
    } else if(c.fields&&Object.keys(c.fields).length){
      h+='<div class=ok>Расхождений с реестром нет — реквизиты совпадают.</div>';
    }
    h+=`<h3>Черновик ответа</h3><div class=draft>${c.draft.text}</div>`;
    if((c.draft.citations||[]).length)
      h+='<span class=k>источники черновика:</span><cite>'+c.draft.citations.join('<br>')+'</cite>';
    h+='<p style=margin-top:12px>'+
       '<button data-a="approve">✓ Согласовать</button>'+
       '<button data-a="correct">✎ Исправить</button>'+
       '<button class=warn data-a="escalate">☞ Эскалировать человеку</button></p>';
    card.innerHTML=h;card.style.display='block';card.scrollIntoView({behavior:'smooth'});
    card.querySelectorAll('button[data-a]').forEach(b=>b.onclick=()=>dec(b.dataset.a));
  });
}
function dec(action){
  if(!cur)return;
  const note=action==='correct'?prompt('Что исправить? (заметка в журнал)')||'':'';
  fetch('/api/docs/'+cur+'/decision',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:action,note:note})})
  .then(r=>r.json()).then(c=>{open_card(cur);load();});
}
window.open_card=open_card;
load();
</script>"""
