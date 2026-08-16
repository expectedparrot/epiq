"""Build a portable, schema-driven HTML explorer for any Epiq database."""

# ruff: noqa: E501 -- embedded minified HTML, CSS, and JavaScript are intentionally intact.

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .errors import EpiqError
from .store import Store

HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Epiq database explorer</title>
<style>
:root{--ink:#18201d;--muted:#647069;--paper:#f5f5ef;--white:#fff;--line:#d3d8cf;--green:#0d6b55;--green2:#084a3c;--orange:#d96b3b;--blue:#316b8f;--purple:#76558e;--unknown:#e7e9e4}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}.hero{padding:52px max(24px,calc((100vw - 1220px)/2));background:linear-gradient(135deg,#072f29,#0d6b55);color:#fff}.kicker{text-transform:uppercase;letter-spacing:.16em;color:#9fd7c8;font-size:.74rem;font-weight:800}h1{margin:.4rem 0 .6rem;font-size:clamp(2.6rem,6vw,5.6rem);line-height:.94;letter-spacing:-.055em;max-width:920px}.dek{max-width:780px;color:#d2eee6;font:1.12rem/1.55 Georgia,serif}.shell{max-width:1220px;margin:auto;padding:26px 24px 70px}.tabs{position:sticky;top:0;z-index:10;display:flex;gap:8px;padding:14px 0;background:rgba(245,245,239,.94);backdrop-filter:blur(10px)}.tab{border:1px solid var(--line);border-radius:999px;padding:.62rem .9rem;background:#fff;color:var(--muted);font-weight:750;cursor:pointer}.tab.active{background:var(--green2);border-color:var(--green2);color:#fff}.view{display:none}.view.active{display:block}.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:8px 0 24px}.stat{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px}.stat b{display:block;font-size:2rem;letter-spacing:-.04em}.stat span{font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;font-weight:750}.panel{background:#fff;border:1px solid var(--line);border-radius:12px;margin:16px 0;overflow:hidden}.head{padding:15px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:14px;align-items:baseline}.head h2{margin:0;font-size:1.05rem}.head span{font-size:.78rem;color:var(--muted)}.pipeline{display:grid;grid-template-columns:repeat(5,1fr);gap:28px;padding:26px}.stage{position:relative;border:1px solid var(--line);border-radius:10px;padding:15px;background:#fafbf8;min-height:145px}.stage:not(:last-child):after{content:'→';position:absolute;right:-21px;top:42%;color:var(--orange);font-size:1.4rem;font-weight:900}.stage i{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:var(--green);color:#fff;font-style:normal;font-weight:800}.stage b{display:block;margin:.55rem 0 .3rem}.stage p{margin:0;color:var(--muted);font-size:.78rem;line-height:1.45}.split{display:grid;grid-template-columns:1.3fr .7fr;gap:16px}.bars{padding:18px}.bar-row{display:grid;grid-template-columns:130px 1fr 48px;gap:10px;align-items:center;margin:11px 0;font-size:.82rem}.track{height:12px;background:var(--unknown);border-radius:999px;overflow:hidden}.fill{height:100%;background:linear-gradient(90deg,var(--green),#45a88c);border-radius:999px}.callout{margin:18px;padding:16px;border-left:4px solid var(--orange);background:#fff8f3;font:1rem/1.55 Georgia,serif}.matrix-wrap{overflow:auto;max-height:72vh}.matrix{border-collapse:separate;border-spacing:0;width:100%;font-size:.76rem}.matrix th,.matrix td{padding:.58rem .65rem;border-right:1px solid var(--line);border-bottom:1px solid var(--line);min-width:125px}.matrix th{position:sticky;top:0;background:var(--green2);color:#fff;text-align:left;z-index:2}.matrix th:first-child{left:0;z-index:3}.matrix td:first-child{position:sticky;left:0;background:#fff;font-weight:800;z-index:1}.cell{border:0;border-radius:7px;padding:.4rem .5rem;width:100%;text-align:left;font-size:.72rem;cursor:pointer;font-weight:750}.answered{background:#e7f5ef;color:var(--green2)}.beta{background:#fff0e7;color:#98451f}.unasked{background:var(--unknown);color:#6f7772}.notfound{background:#e8f0f6;color:#315f7d}.medium:after{content:' · medium';font-weight:500;opacity:.75}.legend{display:flex;flex-wrap:wrap;gap:8px;padding:12px 18px;border-top:1px solid var(--line);font-size:.74rem;color:var(--muted)}.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px}.gaps{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;padding:18px}.gap{display:flex;justify-content:space-between;gap:10px;border:1px solid var(--line);border-radius:9px;padding:12px}.gap b{font-size:.82rem}.gap span{font-size:.73rem;color:var(--muted)}.badge{align-self:center;white-space:nowrap;background:var(--unknown);border-radius:999px;padding:.25rem .45rem;font-size:.68rem!important}.sources{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;padding:18px}.source{border:1px solid var(--line);border-radius:9px;padding:13px}.source a{color:var(--blue);font-weight:800;font-size:.82rem}.source p{color:var(--muted);font:italic .84rem/1.45 Georgia,serif;margin:.5rem 0 0}.source small{color:var(--muted)}dialog{border:0;border-radius:14px;box-shadow:0 25px 90px rgba(0,0,0,.25);max-width:680px;width:calc(100% - 32px);padding:0}dialog::backdrop{background:rgba(7,25,21,.65)}.modal-head{padding:18px 20px;background:var(--green2);color:#fff}.modal-body{padding:20px}.modal-body blockquote{margin:1rem 0;padding-left:1rem;border-left:3px solid var(--orange);font:italic 1rem/1.5 Georgia,serif;color:var(--muted)}.close{float:right;background:transparent;border:1px solid #83ac9f;color:#fff;border-radius:999px;cursor:pointer}.token{font:700 .72rem ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--purple)}@media(max-width:850px){.stats{grid-template-columns:repeat(2,1fr)}.pipeline{grid-template-columns:1fr}.stage:not(:last-child):after{content:'↓';right:auto;left:50%;top:auto;bottom:-26px}.split,.sources,.gaps{grid-template-columns:1fr}}
</style></head><body>
<header class="hero"><div class="kicker">Live SQLite projection · Epiq</div><h1 id="project-title"></h1><p class="dek" id="project-dek"></p></header>
<main class="shell"><nav class="tabs"><button class="tab active" data-view="flow">Overview</button><button class="tab" data-view="matrix">Data matrix</button><button class="tab" data-view="charts">Charts</button><button class="tab" data-view="gaps">Unknowns</button><button class="tab" data-view="sources">Evidence</button></nav>
<section id="flow" class="view active"><div class="stats" id="stats"></div><div class="panel"><div class="head"><h2>From a question to an inspectable cell</h2><span>effects are explicit</span></div><div class="pipeline"><div class="stage"><i>1</i><b>Define entities</b><p>Typed entities form the population shown in this projection.</p></div><div class="stage"><i>2</i><b>Define questions</b><p>Each field is a versioned, typed question rather than an unexamined column.</p></div><div class="stage"><i>3</i><b>Collect evidence</b><p>An agent stores a bounded excerpt, URL, retrieval date, and content hash.</p></div><div class="stage"><i>4</i><b>Assert or report a gap</b><p>Supported values become claims; an unsuccessful bounded search becomes NotFound.</p></div><div class="stage"><i>5</i><b>Project views</b><p>The matrix derives current cells while retaining evidence lineage and epistemic state.</p></div></div></div><div class="split"><div class="panel"><div class="head"><h2>Coverage by entity</h2><span>answered or investigated questions</span></div><div class="bars" id="coverage"></div></div><div class="panel"><div class="head"><h2>Important boundary</h2></div><div class="callout">The database establishes what its evidence supports. Unasked, NotFound, Contested, and a supported negative value are different states.</div></div></div></section>
<section id="matrix" class="view"><div class="panel"><div class="head"><h2>Current projection</h2><span>click investigated cells to inspect their lineage</span></div><div class="matrix-wrap"><table class="matrix" id="matrix-table"></table></div><div class="legend"><span><i class="dot" style="background:#e7f5ef"></i>answered</span><span><i class="dot" style="background:#fff0e7"></i>contested</span><span><i class="dot" style="background:#e8f0f6"></i>searched; not found</span><span><i class="dot" style="background:var(--unknown)"></i>unasked</span></div></div><div class="callout"><b>Interpretation:</b> “Not found” records a bounded research outcome. It does not assert that the entity lacks the property.</div></section>
<section id="charts" class="view"><div id="chart-list"></div></section>
<section id="gaps" class="view"><div class="panel"><div class="head"><h2>Generated research backlog</h2><span>every item corresponds to an Unasked cell</span></div><div class="gaps" id="gap-list"></div></div></section>
<section id="sources" class="view"><div class="panel"><div class="head"><h2>Evidence used by active claims</h2><span>deduplicated across cells</span></div><div class="sources" id="source-list"></div></div></section>
</main><dialog id="detail"><div class="modal-head"><button class="close" onclick="detail.close()">Close</button><b id="detail-title"></b></div><div class="modal-body" id="detail-body"></div></dialog>
<script>const DATA=__DATA__;
const questions=DATA.questions,entities=DATA.rows;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label=q=>q.definition?.label||q.name.replaceAll('_',' ');
const allLineage=[];entities.forEach(r=>Object.values(r.cells).forEach(c=>(c.lineage||[]).forEach(x=>allLineage.push(x))));const uniqueSources=[...new Map(allLineage.map(x=>[x.evidence_id,x])).values()];
document.title=`${DATA.project.name} · Epiq explorer`;document.getElementById('project-title').textContent=DATA.project.name;document.getElementById('project-dek').textContent=`A schema-driven projection of ${entities.length} ${DATA.entity_kind} entities across ${questions.length} typed questions. Every investigated cell exposes its evidence and epistemic state.`;
document.getElementById('stats').innerHTML=[[entities.length,DATA.entity_kind],[questions.length,'typed questions'],[uniqueSources.length,'evidence records'],[allLineage.length,'claim lineages'],[entities.reduce((n,r)=>n+questions.filter(q=>r.cells[q.name].state!=='Answered').length,0),'open or contested']].map(x=>`<div class="stat"><b>${x[0]}</b><span>${esc(x[1])}</span></div>`).join('');
document.getElementById('coverage').innerHTML=entities.map(r=>{const n=questions.filter(q=>r.cells[q.name].state!=='Unasked').length,pct=questions.length?100*n/questions.length:0;return `<div class="bar-row"><b>${esc(r.name)}</b><div class="track"><div class="fill" style="width:${pct}%"></div></div><span>${n}/${questions.length}</span></div>`}).join('');
const short=v=>{if(v===null||v===undefined)return '—';if(typeof v==='string'||typeof v==='number'||typeof v==='boolean')return String(v);if(v?.amount!==undefined){const currency=v.currency||'';return `${currency} ${Number(v.amount).toLocaleString()}`.trim()}return JSON.stringify(v)};
const formatted=(q,v)=>typeof v==='number'?(q.definition?.unit==='USD'?`$${v.toLocaleString()}`:v.toLocaleString()):short(v);
document.getElementById('matrix-table').innerHTML=`<thead><tr><th>${esc(DATA.entity_kind)}</th>${questions.map(q=>`<th title="${esc(q.value_type)} · ${esc(q.name)}">${esc(label(q))}</th>`).join('')}</tr></thead><tbody>${entities.map((r,ri)=>`<tr><td>${esc(r.name)}</td>${questions.map(q=>{const c=r.cells[q.name];if(c.state==='Unasked')return `<td><button class="cell unasked">Unasked</button></td>`;if(c.state==='NotFound')return `<td><button class="cell notfound" onclick="showCell(${ri},'${esc(q.name)}')">Not found</button></td>`;const cls=c.state==='Contested'?'beta':'answered',display=c.state==='Contested'?`${c.values.length} conflicting values`:formatted(q,c.value??c.values);return `<td><button class="cell ${cls} ${c.confidence==='medium'?'medium':''}" onclick="showCell(${ri},'${esc(q.name)}')">${esc(display)}</button></td>`}).join('')}</tr>`).join('')}</tbody>`;
const numeric=questions.filter(q=>entities.some(r=>typeof r.cells[q.name]?.value==='number'));document.getElementById('chart-list').innerHTML=numeric.length?numeric.map(q=>{const rows=entities.map(r=>({name:r.name,value:r.cells[q.name]?.value})).filter(x=>typeof x.value==='number').sort((a,b)=>b.value-a.value),max=Math.max(...rows.map(x=>x.value));return `<div class="panel"><div class="head"><h2>${esc(label(q))}</h2><span>${esc(q.definition?.release||q.value_type)}</span></div><div class="bars">${rows.map(x=>`<div class="bar-row"><b>${esc(x.name)}</b><div class="track"><div class="fill" style="width:${100*x.value/max}%"></div></div><span>${esc(formatted(q,x.value))}</span></div>`).join('')}</div></div>`}).join(''):'<div class="callout">This projection has no answered numeric questions to chart.</div>';
const gaps=[];entities.forEach(r=>questions.forEach(q=>{const c=r.cells[q.name];if(c.state!=='Answered')gaps.push({entity:r.name,label:label(q),state:c.state,notes:c.research?.notes})}));document.getElementById('gap-list').innerHTML=gaps.length?gaps.map(g=>`<div class="gap"><div><b>${esc(g.entity)}</b><br><span>${esc(g.notes||`Research: ${g.label}`)}</span></div><span class="badge">${esc(g.state)}</span></div>`).join(''):'<p>No open or contested cells in this projection.</p>';
document.getElementById('source-list').innerHTML=uniqueSources.length?uniqueSources.map(x=>`<article class="source"><a target="_blank" rel="noreferrer" href="${esc(x.source.url)}">${esc(x.source.title)}</a><br><small>${esc(x.evidence_id)} · confidence ${esc(x.confidence)}</small><p>${esc(x.excerpt)}</p></article>`).join(''):'<p>No active claim evidence in this projection.</p>';
function showCell(row,name){const r=entities[row],q=questions.find(x=>x.name===name),c=r.cells[name];document.getElementById('detail-title').textContent=`${r.name} · ${label(q)}`;if(c.state==='NotFound'){document.getElementById('detail-body').innerHTML=`<p><b>Research outcome:</b> NotFound</p><p>${esc(c.research.notes)}</p><p><b>Search:</b> <code>${esc(c.research.query)}</code></p><p>This is not a negative claim. It records that a bounded search did not find sufficient evidence.</p>`}else{document.getElementById('detail-body').innerHTML=`<p><b>Projected state:</b> ${esc(c.state)}</p><p><b>Projected value:</b> ${esc(formatted(q,c.value??c.values))}${c.confidence?` · ${esc(c.confidence)} confidence`:''}</p>`+(c.lineage||[]).map(p=>`<p><b>Claim token:</b> <span class="token">${esc(p.token)}</span></p><blockquote>${esc(p.excerpt)}</blockquote><p><b>Evidence:</b> <a target="_blank" rel="noreferrer" href="${esc(p.source.url)}">${esc(p.source.title)}</a></p>`).join('')+`<p>This cell is a view. Its durable atoms are claims, evidence fragments, sources, and the events that produced them.</p>`}document.getElementById('detail').showModal()}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.view).classList.add('active')});
</script></body></html>"""


def write_html(store: Store, output: str | Path, kind: str | None = None) -> Path:
    """Write a self-contained explorer for one database projection."""
    overview = store.overview()
    kinds = overview["entity_kinds"]
    if not kinds:
        raise EpiqError("empty_project", "The database has no entities to display")
    available = {item["kind"] for item in kinds}
    default_kind = max(
        kinds,
        key=lambda item: (
            item["questions"] > 0,
            item["questions"],
            item["entities"],
        ),
    )["kind"]
    kind = kind or default_kind
    if kind not in available:
        choices = ", ".join(sorted(available))
        raise EpiqError(
            "entity_kind_not_found", f"Unknown entity kind {kind!r}; choose from: {choices}"
        )
    data = store.matrix(kind)
    data["project"] = overview["project"]
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(HTML.replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    return output_path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kind", help="Entity kind to project; defaults to the richest schema")
    args = parser.parse_args()
    print(write_html(Store(args.db), args.output, args.kind))


if __name__ == "__main__":
    main()
