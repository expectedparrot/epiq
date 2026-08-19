"""Build a self-contained, read-only HTML snapshot of an Epiq database."""

# ruff: noqa: E501 -- the embedded HTML, CSS, and JavaScript are intentionally kept together.

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import EpiqError
from .store import Store

HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; font-src 'none'">
<title>Epiq snapshot</title><style>
:root{--ink:#111512;--muted:#6f746f;--paper:#fffff8;--line:#d9d8cf;--green:#145d49;--yellow:#fff2bd;--blue:#edf3f6;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Georgia,"Times New Roman",serif}button,input,select,table{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}.top{max-width:1480px;margin:auto;padding:30px 34px 18px;border-bottom:1px solid var(--ink)}.eyebrow,.meta,.type{font:600 .68rem/1.4 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.top h1{font-size:clamp(2rem,4vw,3.25rem);font-weight:400;letter-spacing:-.035em;margin:.25rem 0}.top p{margin:0;color:var(--muted);font-style:italic}.readonly{display:inline-block;margin-top:9px;font:600 .67rem var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--green)}.layout{max-width:1480px;margin:auto}nav{position:sticky;top:0;z-index:8;display:flex;gap:22px;overflow:auto;padding:0 34px;background:#fffff8ed;border-bottom:1px solid var(--line);backdrop-filter:blur(8px)}nav button{border:0;border-bottom:2px solid transparent;background:transparent;padding:12px 0 9px;color:var(--muted);cursor:pointer;white-space:nowrap}nav button.active{border-bottom-color:var(--ink);color:var(--ink)}main{padding:26px 34px 60px;min-width:0}.view{display:none}.view.active{display:block}.toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.toolbar input,.toolbar select,.toolbar button{border:1px solid var(--line);border-radius:3px;background:transparent;padding:7px 9px;min-width:210px}.toolbar button{min-width:auto;cursor:pointer}.stats{display:flex;gap:30px;flex-wrap:wrap;border-top:1px solid var(--ink);padding:12px 0 20px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:0}.card{border:0;border-left:1px solid var(--line);background:transparent;padding:6px 14px;text-align:left}.card:first-child{border-left:0;padding-left:0}.card b{display:block;font:400 1.6rem Georgia,serif}.card span{color:var(--muted);font-size:.75rem}.panel{margin:24px 0;border-top:1px solid var(--ink)}.panel-head{padding:9px 0;display:flex;justify-content:space-between;gap:12px}.panel-head h2{font-size:1.05rem;font-weight:400;margin:0}.scroll{overflow:auto;max-height:72vh;border-top:1px solid var(--line)}table{border-collapse:separate;border-spacing:0;width:max-content;min-width:100%;font-size:.79rem;table-layout:fixed}th,td{padding:8px 10px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);text-align:left;vertical-align:top;width:190px;min-width:72px;overflow-wrap:anywhere;white-space:normal}th{position:sticky;top:0;background:#f4f3eb;z-index:2;font-weight:650}th:first-child,td:first-child{position:sticky;left:0;background:#fffff8;z-index:1;width:210px}th:first-child{z-index:3}.resize{position:absolute;right:-4px;top:0;width:8px;height:100%;cursor:col-resize;z-index:4}.nowrap td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.inspect-cell{cursor:pointer}.inspect-cell:hover{text-decoration:underline;text-decoration-color:#a5aaa5;text-underline-offset:3px}.state{display:inline-block;border:0;border-radius:999px;padding:3px 7px;font-size:.72rem;cursor:pointer}.contested{background:var(--yellow);color:#6e5300}.notfound{background:var(--blue);color:#315a78}.unasked{background:#ecece6;color:#70746f}.schema{padding:8px 0;display:grid;gap:0}.field{border-top:1px solid var(--line);padding:8px 0}.field b{margin-right:8px}.type{color:var(--muted)}.items{display:grid;gap:0}.item{border-top:1px solid var(--line);padding:10px 0}.item p{margin:.4rem 0;color:#4f554f}.item a{color:var(--green);overflow-wrap:anywhere}.item summary{cursor:pointer}.json{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.5 var(--mono);background:#f5f4ed;padding:9px}.empty{padding:24px 0;color:var(--muted);font-style:italic}dialog{border:1px solid var(--ink);border-radius:0;box-shadow:12px 18px 50px #0003;width:min(760px,calc(100% - 28px));max-height:88vh;padding:0;background:var(--paper)}dialog::backdrop{background:#eeeee8cc}.modal-head{position:sticky;top:0;background:var(--paper);border-bottom:1px solid var(--ink);padding:13px 18px}.modal-head button{float:right;border:0;background:transparent;color:var(--ink);cursor:pointer}.modal-body{padding:18px;overflow-wrap:anywhere}.modal-body blockquote{border-left:2px solid var(--green);margin:10px 0;padding-left:12px;color:#4f5853}.lineage{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}@media(max-width:720px){.top,main{padding-left:16px;padding-right:16px}nav{padding:0 16px}}
</style></head><body>
<header class="top"><div class="eyebrow">Epiq database snapshot</div><h1 id="title"></h1><p id="subtitle"></p><span class="readonly">Read only · no database connection</span></header>
<div class="layout"><nav><button class="active" data-view="overview">Overview</button><button data-view="tables">Tables</button><button data-view="evidence">Evidence</button><button data-view="reviews">Review queues</button><button data-view="history">Event history</button></nav><main>
<section id="overview" class="view active"><div class="stats" id="stats"></div><div class="panel"><div class="panel-head"><h2>Tables</h2><span class="meta">Current projection</span></div><div class="cards" id="table-cards" style="padding:14px"></div></div><div class="panel"><div class="panel-head"><h2>Integrity</h2></div><div id="integrity" class="items"></div></div></section>
<section id="tables" class="view"><div class="toolbar"><select id="table-select"></select><input id="table-search" type="search" placeholder="Filter rows and values"><button id="wrap-toggle" type="button">Wrap: on</button></div><div class="panel"><div class="panel-head"><h2 id="table-title"></h2><span id="table-summary" class="meta"></span></div><div class="scroll"><table id="matrix"></table></div><div class="schema" id="schema"></div></div></section>
<section id="evidence" class="view"><div class="toolbar"><input id="evidence-search" type="search" placeholder="Filter evidence"></div><div class="panel"><div class="panel-head"><h2>Evidence supporting current cells</h2><span id="evidence-count" class="meta"></span></div><div class="items" id="evidence-list"></div></div></section>
<section id="reviews" class="view"><div class="panel"><div class="panel-head"><h2>Claim proposals</h2></div><div class="items" id="proposals"></div></div><div class="panel"><div class="panel-head"><h2>Field challenges</h2></div><div class="items" id="challenges"></div></div><div class="panel"><div class="panel-head"><h2>Agent jobs</h2></div><div class="items" id="jobs"></div></div></section>
<section id="history" class="view"><div class="toolbar"><input id="history-search" type="search" placeholder="Filter events, actors, and payloads"></div><div class="panel"><div class="panel-head"><h2>Append-only event history</h2><span id="history-count" class="meta"></span></div><div class="items" id="event-list"></div></div></section>
</main></div><dialog id="detail"><div class="modal-head"><button id="close">Close</button><b id="detail-title"></b></div><div class="modal-body" id="detail-body"></div></dialog>
<script>const DATA=__DATA__;
const $=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),json=v=>JSON.stringify(v,null,2),label=q=>q.definition?.label||q.name.replaceAll('_',' '),short=v=>v===null||v===undefined?'—':typeof v==='object'?json(v):String(v),matches=(value,term)=>json(value).toLowerCase().includes(term.toLowerCase()),safeUrl=v=>/^https?:\/\//i.test(v||'')?v:null;
document.title=`${DATA.overview.project.name||'Epiq'} · snapshot`;$('title').textContent=DATA.overview.project.name||'Untitled Epiq project';$('subtitle').textContent=`Captured ${DATA.generated_at} · ${DATA.tables.length} tables · ${DATA.events.length} events`;
const lineages=[];DATA.tables.forEach(t=>t.rows.forEach(r=>Object.values(r.cells).forEach(c=>(c.lineage||[]).forEach(l=>lineages.push({...l,entity:r.name,question:Object.keys(r.cells).find(k=>r.cells[k]===c),table:t.entity_kind})))));const evidence=[...new Map(lineages.map(x=>[x.evidence_id,x])).values()];const cells=DATA.tables.reduce((n,t)=>n+t.rows.length*t.questions.length,0);
$('stats').innerHTML=[[DATA.tables.length,'tables'],[DATA.tables.reduce((n,t)=>n+t.rows.length,0),'current rows'],[cells,'projected cells'],[evidence.length,'active evidence'],[DATA.events.length,'events']].map(x=>`<div class="card"><b>${x[0]}</b><span>${x[1]}</span></div>`).join('');
$('table-cards').innerHTML=DATA.tables.length?DATA.tables.map((t,i)=>`<button class="card" style="text-align:left;cursor:pointer" onclick="openTable(${i})"><b>${esc(t.entity_kind)}</b><span>${t.rows.length} rows · ${t.questions.length} fields</span></button>`).join(''):'<div class="empty">This project has no tables.</div>';
$('integrity').innerHTML=`<details class="item"><summary>${DATA.integrity.ok?'Checks passed':'Problems found'} · ${DATA.integrity.findings?.length||0} findings</summary><pre class="json">${esc(json(DATA.integrity))}</pre></details>`;
$('table-select').innerHTML=DATA.tables.map((t,i)=>`<option value="${i}">${esc(t.entity_kind)}</option>`).join('');
function cellValue(c){return c.state==='Answered'?(c.value??c.values):c.state==='Contested'?c.values:c.state==='NotFound'?'Not found':'Unasked'}
const inspectAttrs=(t,r,q)=>`data-table="${esc(t.entity_kind)}" data-row="${esc(r.entity_id)}" data-question="${esc(q.name)}"`;
function renderedCell(t,r,q){const c=r.cells[q.name],value=esc(short(cellValue(c))),attrs=inspectAttrs(t,r,q);if(c.state==='Answered')return `<td class="inspect-cell" ${attrs}>${value}</td>`;return `<td class="inspect-cell" ${attrs}><button class="state ${c.state.toLowerCase()}" type="button">${value}</button></td>`}
function enableResizing(){document.querySelectorAll('#matrix .resize').forEach(handle=>handle.onpointerdown=event=>{event.preventDefault();const th=handle.parentElement,index=[...th.parentElement.children].indexOf(th)+1,start=event.clientX,width=th.getBoundingClientRect().width;const move=e=>{const next=Math.max(72,width+e.clientX-start);document.querySelectorAll(`#matrix tr>*:nth-child(${index})`).forEach(cell=>{cell.style.width=`${next}px`;cell.style.minWidth=`${next}px`})},up=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up)};document.addEventListener('pointermove',move);document.addEventListener('pointerup',up)});}
function renderTable(){const t=DATA.tables[+$('table-select').value],term=$('table-search').value;if(!t){$('matrix').innerHTML='';$('schema').innerHTML='<div class="empty">No table selected.</div>';return}$('table-title').textContent=t.entity_kind;const rows=t.rows.filter(r=>matches(r,term)),head=(text,type='')=>`<th>${esc(text)}${type?`<br><span class="type">${esc(type)}</span>`:''}<i class="resize" title="Drag to resize"></i></th>`;$('table-summary').textContent=`${rows.length}/${t.rows.length} rows · ${t.questions.length} fields`;$('matrix').innerHTML=`<thead><tr>${head(t.entity_kind)}${t.questions.map(q=>head(label(q),q.value_type)).join('')}</tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.name)}</td>${t.questions.map(q=>renderedCell(t,r,q)).join('')}</tr>`).join('')}</tbody>`;$('schema').innerHTML=t.questions.length?t.questions.map(q=>`<details class="field"><summary><b>${esc(label(q))}</b><span class="type">${esc(q.name)} · ${esc(q.value_type)}</span></summary><pre class="json">${esc(json(q.definition))}</pre></details>`).join(''):'<div class="empty">No fields.</div>';document.querySelectorAll('.inspect-cell').forEach(cell=>cell.onclick=()=>showCell(cell.dataset.table,cell.dataset.row,cell.dataset.question));enableResizing()}
function showCell(kind,id,name){const t=DATA.tables.find(x=>x.entity_kind===kind),r=t.rows.find(x=>x.entity_id===id),q=t.questions.find(x=>x.name===name),c=r.cells[name];$('detail-title').textContent=`${r.name} · ${label(q)}`;let body=`<p><b>State:</b> ${esc(c.state)} · <b>Type:</b> ${esc(q.value_type)}</p><p><b>Value:</b></p><pre class="json">${esc(json(cellValue(c)))}</pre>`;if(c.temporal)body+=`<p><b>Temporal status:</b></p><pre class="json">${esc(json(c.temporal))}</pre>`;if(c.research)body+=`<p><b>Research outcome:</b></p><pre class="json">${esc(json(c.research))}</pre>`;if(!(c.lineage||[]).length)body+='<p>No claim lineage.</p>';for(const l of c.lineage||[]){const url=safeUrl(l.source?.url),source=url?`<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(l.source.title||url)}</a>`:esc(l.source?.title||l.source?.url||'');body+=`<div class="lineage"><b>${esc(l.claim_id||'Claim')}</b> · ${esc(l.confidence||'')}<blockquote>${esc(l.excerpt||'')}</blockquote><p>${source}</p><pre class="json">${esc(json(l))}</pre></div>`}$('detail-body').innerHTML=body;$('detail').showModal()}
function openTable(i){$('table-select').value=String(i);renderTable();document.querySelector('[data-view="tables"]').click()}
$('table-select').onchange=renderTable;$('table-search').oninput=renderTable;$('wrap-toggle').onclick=()=>{const off=$('matrix').classList.toggle('nowrap');$('wrap-toggle').textContent=`Wrap: ${off?'off':'on'}`};renderTable();
function evidenceItem(x){const url=safeUrl(x.source?.url),link=url?`<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(url)}</a>`:esc(x.source?.url||'');return `<div class="item"><b>${esc(x.source?.title||x.evidence_id)}</b><span class="meta"> · ${esc(x.table)} / ${esc(x.entity)} / ${esc(x.question)}</span><p>${esc(x.excerpt||'')}</p>${link}<details><summary>Full record</summary><pre class="json">${esc(json(x))}</pre></details></div>`}
function renderEvidence(){const term=$('evidence-search').value,items=evidence.filter(x=>matches(x,term));$('evidence-count').textContent=`${items.length}/${evidence.length}`;$('evidence-list').innerHTML=items.length?items.map(evidenceItem).join(''):'<div class="empty">No matching evidence.</div>'}
$('evidence-search').oninput=renderEvidence;renderEvidence();
const list=(id,items,empty)=>$(id).innerHTML=items.length?items.map(x=>`<details class="item"><summary>${esc(x.status||x.job_type||x.problem||x.proposal_id||x.challenge_id||x.job_id||'Record')}</summary><pre class="json">${esc(json(x))}</pre></details>`).join(''):`<div class="empty">${empty}</div>`;list('proposals',DATA.review.claim_proposals,'No claim proposals.');list('challenges',DATA.review.question_challenges,'No field challenges.');list('jobs',DATA.review.agent_jobs,'No agent jobs.');
function renderEvents(){const term=$('history-search').value,items=DATA.events.filter(x=>matches(x,term));$('history-count').textContent=`${items.length}/${DATA.events.length}`;$('event-list').innerHTML=items.length?items.map(x=>`<details class="item"><summary><span class="meta">#${x.seq}</span> ${esc(x.event_type)} · ${esc(x.actor)} · ${esc(x.recorded_at)}</summary><pre class="json">${esc(json(x))}</pre></details>`).join(''):'<div class="empty">No matching events.</div>'}$('history-search').oninput=renderEvents;renderEvents();
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.view).classList.add('active')});$('close').onclick=()=>$('detail').close();
</script></body></html>"""


def snapshot_data(store: Store, kind: str | None = None) -> dict[str, Any]:
    """Materialize the complete read-only state needed by the portable inspector."""
    overview = store.overview()
    available = [str(item["kind"]) for item in overview["entity_kinds"]]
    if kind and kind not in available:
        choices = ", ".join(available) or "none"
        raise EpiqError(
            "entity_kind_not_found", f"Unknown entity kind {kind!r}; choose from: {choices}"
        )
    selected = [kind] if kind else available
    return {
        "snapshot_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "overview": overview,
        "tables": [store.matrix(item) for item in selected],
        "events": store.history(),
        "review": {
            "claim_proposals": store.claim_proposals(None),
            "question_challenges": store.question_challenges(status=None),
            "agent_jobs": store.agent_jobs(),
        },
        "integrity": store.doctor(),
    }


def write_html(store: Store, output: str | Path, kind: str | None = None) -> Path:
    """Write a self-contained, read-only inspector for the current database state."""
    data = json.dumps(snapshot_data(store, kind), separators=(",", ":"), ensure_ascii=False)
    data = data.replace("</", "<\\/")
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(HTML.replace("__DATA__", data), encoding="utf-8")
    return output_path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kind", help="Optional single-table snapshot; defaults to the whole DB")
    args = parser.parse_args()
    print(write_html(Store(args.db), args.output, args.kind))


if __name__ == "__main__":
    main()
