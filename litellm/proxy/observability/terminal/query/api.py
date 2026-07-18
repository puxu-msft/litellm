from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from typing import Protocol

from litellm.proxy.observability.terminal.query.coordinator import QueryFailed, QueryRejected, QueryResult


class QueryService(Protocol):
    def query(self, sql: str) -> QueryResult: ...


class SQLRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str


def install_query_routes(app: FastAPI, coordinator: QueryService) -> None:
    async def query(request: SQLRequest):
        result = coordinator.query(request.sql)
        if isinstance(result, QueryRejected):
            return {"ok": False, "error": result.detail, "kind": "rejected"}
        if isinstance(result, QueryFailed):
            return {"ok": False, "error": result.detail, "kind": "failed"}
        return {"ok": True, "columns": result.columns, "rows": result.rows}

    app.add_api_route("/terminal-archive/query", query, methods=["POST"])

    async def inspector() -> HTMLResponse:
        return HTMLResponse(_INSPECTOR_HTML)

    app.add_api_route("/terminal-archive", inspector, methods=["GET"], response_class=HTMLResponse)


_INSPECTOR_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terminal Archive</title><style>
:root{color-scheme:light dark;font-family:"IBM Plex Mono","Source Code Pro",monospace}body{margin:0;background:#101310;color:#e7ebe4}
header{padding:18px 24px;border-bottom:1px solid #394139;display:flex;justify-content:space-between}main{padding:20px 24px}
textarea{width:100%;min-height:92px;box-sizing:border-box;background:#171c17;color:#e7ebe4;border:1px solid #4b584b;padding:12px}
button{margin-top:10px;background:#d6ff4b;color:#121500;border:0;padding:9px 16px;font-weight:700;cursor:pointer}
table{margin-top:20px;border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;border-bottom:1px solid #343c34;padding:8px;vertical-align:top}
.status{color:#aab5aa;font-size:12px}.error{color:#ff7d70;white-space:pre-wrap}</style></head>
<body><header><strong>Terminal Archive</strong><span class="status" id="status">ready</span></header><main>
<textarea id="sql">SELECT event_id,event_type FROM terminal_events ORDER BY event_id LIMIT 100</textarea><br><button id="run">Run query</button>
<div id="error" class="error"></div><table><thead id="head"></thead><tbody id="body"></tbody></table></main>
<script>const q=id=>document.getElementById(id);q('run').onclick=async()=>{q('status').textContent='running';q('error').textContent='';
const r=await fetch('/terminal-archive/query',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({sql:q('sql').value})});
const d=await r.json();if(!d.ok){q('error').textContent=d.error;q('status').textContent=d.kind;return}
const head=q('head'),body=q('body');head.replaceChildren();body.replaceChildren();const hr=document.createElement('tr');
d.columns.forEach(x=>{const th=document.createElement('th');th.textContent=String(x);hr.appendChild(th)});head.appendChild(hr);
d.rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(x=>{const td=document.createElement('td');td.textContent=String(x);tr.appendChild(td)});body.appendChild(tr)});
q('status').textContent=d.rows.length+' rows'};</script>
</body></html>"""
