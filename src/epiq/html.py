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
:root{--ink:#17201d;--muted:#69726d;--paper:#f5f4ed;--panel:#fff;--line:#d9d9cf;--green:#145d49;--pale:#e7f3ed;--yellow:#fff3cf;--blue:#eaf1f7;--red:#a54232;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input,select{font:inherit}.top{background:#123e33;color:#fff;padding:28px 34px}.eyebrow,.meta,.type{font:700 .72rem/1.4 var(--mono);letter-spacing:.08em;text-transform:uppercase}.eyebrow{color:#a9d8c8}.top h1{font-size:clamp(2rem,5vw,4rem);letter-spacing:-.05em;margin:.25rem 0}.top p{margin:0;color:#cbe2da}.readonly{display:inline-block;margin-top:14px;border:1px solid #8bb5a7;border-radius:999px;padding:5px 9px;font:700 .7rem var(--mono);letter-spacing:.07em;text-transform:uppercase}.layout{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:calc(100vh - 180px)}nav{padding:22px 14px;border-right:1px solid var(--line);background:#efeee6}nav button{display:block;width:100%;border:0;background:transparent;text-align:left;padding:11px 13px;border-radius:8px;color:var(--muted);font-weight:700;cursor:pointer}nav button.active{background:var(--green);color:#fff}main{padding:24px;min-width:0}.view{display:none}.view.active{display:block}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}.toolbar input,.toolbar select{border:1px solid var(--line);border-radius:8px;background:#fff;padding:10px 12px;min-width:240px}.stats,.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px}.card{padding:16px}.card b{display:block;font-size:1.7rem}.card span{color:var(--muted);font-size:.78rem}.panel{margin:16px 0;overflow:hidden}.panel-head{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px}.panel-head h2{font-size:1rem;margin:0}.scroll{overflow:auto;max-height:68vh}table{border-collapse:separate;border-spacing:0;width:100%;font-size:.82rem}th,td{padding:10px 12px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);text-align:left;vertical-align:top;max-width:360px;overflow-wrap:anywhere}th{position:sticky;top:0;background:#eeeee6;z-index:2}th:first-child,td:first-child{position:sticky;left:0;background:#fafaf6;z-index:1}th:first-child{z-index:3}.cell{border:0;border-radius:7px;padding:7px 8px;width:100%;text-align:left;cursor:pointer}.answered{background:var(--pale);color:#174e3e}.contested{background:var(--yellow);color:#745600}.notfound{background:var(--blue);color:#315a78}.unasked{background:#eeeeea;color:#737873}.schema{padding:14px 16px;display:grid;gap:8px}.field{border:1px solid var(--line);border-radius:8px;padding:10px}.field b{margin-right:8px}.type{color:var(--muted)}.items{display:grid;gap:10px;padding:14px}.item{border:1px solid var(--line);border-radius:8px;padding:12px}.item p{margin:.45rem 0;color:var(--muted);font-family:Georgia,serif}.item a{color:var(--green);overflow-wrap:anywhere}.item summary{cursor:pointer;font-weight:750}.json{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 var(--mono);background:#f1f1eb;border-radius:7px;padding:10px}.empty{padding:30px;color:var(--muted);text-align:center}dialog{border:0;border-radius:12px;box-shadow:0 24px 80px #0005;width:min(760px,calc(100% - 28px));max-height:88vh;padding:0}dialog::backdrop{background:#10251fc9}.modal-head{position:sticky;top:0;background:var(--green);color:#fff;padding:16px 18px}.modal-head button{float:right;border:1px solid #ffffff77;background:transparent;color:#fff;border-radius:999px;padding:4px 9px;cursor:pointer}.modal-body{padding:18px;overflow-wrap:anywhere}.modal-body blockquote{border-left:3px solid #8bb5a7;margin:10px 0;padding-left:12px;font-family:Georgia,serif;color:#4f5853}.lineage{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}@media(max-width:720px){.layout{grid-template-columns:1fr}nav{position:sticky;top:0;z-index:5;display:flex;overflow:auto;border-right:0;border-bottom:1px solid var(--line);padding:8px}nav button{white-space:nowrap;width:auto}main{padding:14px}}
</style></head><body>
<header class="top"><div class="eyebrow">Epiq database snapshot</div><h1 id="title"></h1><p id="subtitle"></p><span class="readonly">Read only · no database connection</span></header>
<div class="layout"><nav><button class="active" data-view="overview">Overview</button><button data-view="tables">Tables</button><button data-view="evidence">Evidence</button><button data-view="reviews">Review queues</button><button data-view="history">Event history</button></nav><main>
<section id="overview" class="view active"><div class="stats" id="stats"></div><div class="panel"><div class="panel-head"><h2>Tables</h2><span class="meta">Current projection</span></div><div class="cards" id="table-cards" style="padding:14px"></div></div><div class="panel"><div class="panel-head"><h2>Integrity</h2></div><div id="integrity" class="items"></div></div></section>
<section id="tables" class="view"><div class="toolbar"><select id="table-select"></select><input id="table-search" type="search" placeholder="Filter rows and values"></div><div class="panel"><div class="panel-head"><h2 id="table-title"></h2><span id="table-summary" class="meta"></span></div><div class="scroll"><table id="matrix"></table></div><div class="schema" id="schema"></div></div></section>
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
$('integrity').innerHTML=`<div class="item"><b>${DATA.integrity.ok?'Checks passed':'Problems found'}</b><pre class="json">${esc(json(DATA.integrity))}</pre></div>`;
$('table-select').innerHTML=DATA.tables.map((t,i)=>`<option value="${i}">${esc(t.entity_kind)}</option>`).join('');
function cellValue(c){return c.state==='Answered'?(c.value??c.values):c.state==='Contested'?c.values:c.state==='NotFound'?'Not found':'Unasked'}
function renderTable(){const t=DATA.tables[+$('table-select').value],term=$('table-search').value;if(!t){$('matrix').innerHTML='';$('schema').innerHTML='<div class="empty">No table selected.</div>';return}$('table-title').textContent=t.entity_kind;const rows=t.rows.filter(r=>matches(r,term));$('table-summary').textContent=`${rows.length}/${t.rows.length} rows · ${t.questions.length} fields`;$('matrix').innerHTML=`<thead><tr><th>${esc(t.entity_kind)}</th>${t.questions.map(q=>`<th>${esc(label(q))}<br><span class="type">${esc(q.value_type)}</span></th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.name)}</td>${t.questions.map(q=>{const c=r.cells[q.name],cls=c.state.toLowerCase();return `<td><button class="cell ${cls}" data-table="${esc(t.entity_kind)}" data-row="${esc(r.entity_id)}" data-question="${esc(q.name)}">${esc(short(cellValue(c)))}</button></td>`}).join('')}</tr>`).join('')}</tbody>`;$('schema').innerHTML=t.questions.length?t.questions.map(q=>`<div class="field"><b>${esc(label(q))}</b><span class="type">${esc(q.name)} · ${esc(q.value_type)}</span><pre class="json">${esc(json(q.definition))}</pre></div>`).join(''):'<div class="empty">No fields.</div>';document.querySelectorAll('.cell').forEach(b=>b.onclick=()=>showCell(b.dataset.table,b.dataset.row,b.dataset.question))}
function showCell(kind,id,name){const t=DATA.tables.find(x=>x.entity_kind===kind),r=t.rows.find(x=>x.entity_id===id),q=t.questions.find(x=>x.name===name),c=r.cells[name];$('detail-title').textContent=`${r.name} · ${label(q)}`;let body=`<p><b>State:</b> ${esc(c.state)} · <b>Type:</b> ${esc(q.value_type)}</p><p><b>Value:</b></p><pre class="json">${esc(json(cellValue(c)))}</pre>`;if(c.temporal)body+=`<p><b>Temporal status:</b></p><pre class="json">${esc(json(c.temporal))}</pre>`;if(c.research)body+=`<p><b>Research outcome:</b></p><pre class="json">${esc(json(c.research))}</pre>`;if(!(c.lineage||[]).length)body+='<p>No claim lineage.</p>';for(const l of c.lineage||[]){const url=safeUrl(l.source?.url),source=url?`<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(l.source.title||url)}</a>`:esc(l.source?.title||l.source?.url||'');body+=`<div class="lineage"><b>${esc(l.claim_id||'Claim')}</b> · ${esc(l.confidence||'')}<blockquote>${esc(l.excerpt||'')}</blockquote><p>${source}</p><pre class="json">${esc(json(l))}</pre></div>`}$('detail-body').innerHTML=body;$('detail').showModal()}
function openTable(i){$('table-select').value=String(i);renderTable();document.querySelector('[data-view="tables"]').click()}
$('table-select').onchange=renderTable;$('table-search').oninput=renderTable;renderTable();
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
