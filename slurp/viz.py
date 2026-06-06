"""Generates a self-contained interactive HTML visualisation of a subgraph."""

import json
from pathlib import Path

import networkx as nx

_TYPE_COLORS: dict[str, dict] = {
    "function": {"bg": "#0d2137", "border": "#4fc3f7", "font": "#79d3f7"},
    "class":    {"bg": "#0d200f", "border": "#6dbf72", "font": "#8dd490"},
    "module":   {"bg": "#201800", "border": "#e0a030", "font": "#f0b840"},
    "concept":  {"bg": "#180d24", "border": "#b878d8", "font": "#cc99e8"},
    "file":     {"bg": "#200d14", "border": "#e07898", "font": "#f094b0"},
    "default":  {"bg": "#141c26", "border": "#5e7a96", "font": "#7a9ab4"},
}

_SIZE_MIN = 18.0
_SIZE_MAX = 50.0


def _node_size(score: float) -> float:
    return _SIZE_MIN + score * (_SIZE_MAX - _SIZE_MIN)


def _esc_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_html(
    G: nx.DiGraph,
    stats: dict,
    scores: dict[str, float],
    query: str,
    max_nodes: int = 50,
) -> str:
    """Build a self-contained HTML string for an interactive subgraph visualisation.

    Args:
        G:         Subgraph produced by budget.select_subgraph.
        stats:     Stats dict from budget.select_subgraph.
        scores:    Node scores from scorer.score_nodes (full graph, not just subgraph).
        query:     Original query string shown in the header.
        max_nodes: Maximum nodes to render. If the subgraph is larger, only the
                   top-scoring max_nodes are shown (edges to hidden nodes are dropped).
                   Keeps the visualisation readable regardless of budget size.

    Returns:
        Complete HTML string ready to write to a file.
    """
    # Truncate to top-scoring nodes for readability.
    display_ids = sorted(G.nodes, key=lambda n: scores.get(n, 0.0), reverse=True)
    original_count = len(display_ids)
    if original_count > max_nodes:
        display_ids = display_ids[:max_nodes]
    display_set = set(display_ids)

    top_badge = (
        f" &middot; showing top {len(display_ids)}" if original_count > max_nodes else ""
    )

    # Edges where both endpoints are in the displayed set.
    display_edges = [(u, v) for u, v in G.edges if u in display_set and v in display_set]

    # Nodes connected by at least one displayed edge (non-isolated in this view).
    connected: set[str] = set()
    for u, v in display_edges:
        connected.add(u)
        connected.add(v)

    nodes = []
    for nid in display_ids:
        attrs = G.nodes[nid]
        score = scores.get(nid, 0.0)
        node_type = attrs.get("type") or attrs.get("file_type") or "default"
        nodes.append({
            "id": nid,
            "label": attrs.get("label", str(nid)),
            "size": _node_size(score),
            "score": round(score, 4),
            "node_type": node_type,
            "isolated": nid not in connected,
            "description": attrs.get("description") or "",
            "file_path": attrs.get("file_path") or attrs.get("source_file") or "",
            "importance": attrs.get("importance"),
        })

    edges = []
    for u, v in display_edges:
        edge_attrs = G.edges[u, v]
        edges.append({
            "from": u,
            "to": v,
            "label": edge_attrs.get("relation", ""),
        })

    # Token savings vs naively injecting the full graph (≈ 50 tokens/node baseline).
    baseline = stats["nodes_total"] * 50
    saved = max(0, baseline - stats["tokens_used"])
    saved_pct = round(saved / baseline * 100) if baseline > 0 else 0

    return (
        _HTML_TEMPLATE
        .replace("<<<QUERY>>>",          _esc_html(query))
        .replace("<<<NODES_SELECTED>>>", str(stats["nodes_selected"]))
        .replace("<<<NODES_TOTAL>>>",    str(stats["nodes_total"]))
        .replace("<<<TOKENS_USED>>>",    f"{stats['tokens_used']:,}")
        .replace("<<<TOKENS_BUDGET>>>",  f"{stats['tokens_budget']:,}")
        .replace("<<<COVERAGE_PCT>>>",   str(stats["coverage_pct"]))
        .replace("<<<SAVED_TOKENS>>>",   f"{saved:,}")
        .replace("<<<SAVED_PCT>>>",      str(saved_pct))
        .replace("<<<TOP_BADGE>>>",      top_badge)
        .replace("<<<NODES_JSON>>>",     json.dumps(nodes, ensure_ascii=False))
        .replace("<<<EDGES_JSON>>>",     json.dumps(edges, ensure_ascii=False))
    )


# ---------------------------------------------------------------------------
# HTML template
# Placeholders use <<<NAME>>> to avoid conflicts with Python .format() and JS {}.
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>slurp — <<<QUERY>>></title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/dist/vis-network.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0d1117;
      color: #c9d1d9;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #hdr {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 10px 18px;
      background: #161b22;
      border-bottom: 1px solid #30363d;
      flex-shrink: 0;
      flex-wrap: wrap;
    }
    .brand { color: #58a6ff; font-weight: 700; font-size: 14px; letter-spacing: .3px; }
    .qry   { font-size: 13px; color: #8b949e; }
    .qry em { color: #e6edf3; font-style: normal; font-weight: 600; }
    .chip {
      background: #21262d;
      border: 1px solid #30363d;
      border-radius: 999px;
      padding: 3px 11px;
      font-size: 11px;
      color: #8b949e;
      white-space: nowrap;
    }
    .chip strong { color: #c9d1d9; }
    .chip-save { background: #0d1f0f; border-color: #1f6527; color: #3fb950; }
    #wrap { display: flex; flex: 1; overflow: hidden; }
    #graph { flex: 1; background: #0d1117; min-width: 0; }
    #panel {
      width: 268px;
      flex-shrink: 0;
      background: #161b22;
      border-left: 1px solid #30363d;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #phdr {
      padding: 12px 14px 10px;
      font-size: 10px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .8px;
      color: #484f58;
      border-bottom: 1px solid #30363d;
      flex-shrink: 0;
    }
    #pbody { overflow-y: auto; padding: 14px; flex: 1; }
    .hint { color: #484f58; font-size: 12px; text-align: center; padding-top: 32px; }
    .pf { margin-bottom: 12px; }
    .pf-lbl {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: .5px;
      color: #484f58;
      margin-bottom: 4px;
    }
    .pf-val { font-size: 12px; color: #c9d1d9; word-break: break-all; }
    .badge {
      display: inline-block;
      padding: 1px 9px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
    }
    .bar-bg { height: 3px; background: #21262d; border-radius: 2px; margin-top: 5px; }
    .bar-fill { height: 100%; border-radius: 2px; }
    .conn { font-size: 11px; color: #8b949e; margin-top: 5px; }
    .conn span { color: #c9d1d9; }
    ::-webkit-scrollbar { width: 5px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  </style>
</head>
<body>
<div id="hdr">
  <span class="brand">&#9889; slurp</span>
  <span class="qry">query: <em><<<QUERY>>></em></span>
  <span class="chip"><strong><<<NODES_SELECTED>>></strong>&thinsp;/&thinsp;<<<NODES_TOTAL>>> nodes<<<TOP_BADGE>>></span>
  <span class="chip"><strong><<<TOKENS_USED>>></strong>&thinsp;/&thinsp;<<<TOKENS_BUDGET>>> tokens &middot; <<<COVERAGE_PCT>>>% coverage</span>
  <span class="chip chip-save">&#x1F4B0; Saved ~<<<SAVED_TOKENS>>> tokens (<<<SAVED_PCT>>>%)</span>
</div>
<div id="wrap">
  <div id="graph"></div>
  <div id="panel">
    <div id="phdr">Node Details</div>
    <div id="pbody"><div class="hint">Click a node to inspect</div></div>
  </div>
</div>
<script>
var TC = {
  "function": {bg:"#0d2137",border:"#4fc3f7",font:"#79d3f7"},
  "class":    {bg:"#0d200f",border:"#6dbf72",font:"#8dd490"},
  "module":   {bg:"#201800",border:"#e0a030",font:"#f0b840"},
  "concept":  {bg:"#180d24",border:"#b878d8",font:"#cc99e8"},
  "file":     {bg:"#200d14",border:"#e07898",font:"#f094b0"},
  "default":  {bg:"#141c26",border:"#5e7a96",font:"#7a9ab4"}
};
function c(t){ return TC[t] || TC["default"]; }

var RN = <<<NODES_JSON>>>;
var RE = <<<EDGES_JSON>>>;

var vn = new vis.DataSet(RN.map(function(n){
  var col = c(n.node_type);
  var op = n.isolated ? 0.4 : 1.0;
  return {
    id: n.id, label: n.label, size: n.size,
    color: {background:col.bg, border:col.border, highlight:{background:col.bg, border:"#e6edf3"}},
    font: {color:col.font, size:13, strokeWidth:3, strokeColor:'#0d1117'},
    chosen: false,
    borderWidth: n.isolated ? 1 : 2,
    opacity: op,
    shadow:{enabled:!n.isolated,color:"rgba(0,0,0,.5)",size:6,x:2,y:2},
    _r:n
  };
}));

var ve = new vis.DataSet(RE.map(function(e){
  return {
    from:e.from, to:e.to, label:e.label, arrows:"to",
    color:{color:"#21262d",highlight:"#58a6ff",hover:"#58a6ff"},
    font:{color:"#484f58",size:10,align:"middle",background:"#0d1117"},
    smooth:{type:"dynamic"}
  };
}));

var net = new vis.Network(
  document.getElementById("graph"),
  {nodes:vn, edges:ve},
  {
    nodes:{shape:"dot"},
    edges:{width:1.5,selectionWidth:3},
    physics:{
      barnesHut:{gravitationalConstant:-2000,centralGravity:0.3,springLength:80,springConstant:0.05,damping:0.09},
      stabilization:{enabled:true,iterations:500,onlyDynamicEdges:false}
    },
    interaction:{hover:true,tooltipDelay:150,hideEdgesOnDrag:true},
    layout:{improvedLayout:true}
  }
);
net.once('stabilized', function(){ net.setOptions({physics:{enabled:false}}); });

net.on("click",function(p){
  var body=document.getElementById("pbody");
  if(!p.nodes.length){body.innerHTML='<div class="hint">Click a node to inspect</div>';return;}
  var nd=vn.get(p.nodes[0]);
  var d=nd._r;
  var col=c(d.node_type);
  var pct=Math.round(d.score*100);
  var h="";
  h+=fld("Label",'<span style="color:'+col.font+';font-weight:700">'+x(d.label)+"</span>");
  if(d.node_type&&d.node_type!=="default"){
    h+=fld("Type",'<span class="badge" style="background:'+col.bg+';color:'+col.font+';border:1px solid '+col.border+'">'+x(d.node_type)+"</span>");
  }
  h+=fld("Score",'<span class="pf-val">'+d.score.toFixed(4)+'</span><div class="bar-bg"><div class="bar-fill" style="width:'+pct+'%;background:'+col.border+'"></div></div>');
  if(d.description){h+=fld("Description",'<span style="color:#8b949e">'+x(d.description)+"</span>");}
  if(d.file_path){h+=fld("Source",'<span style="color:#58a6ff;font-size:11px">'+x(d.file_path)+"</span>");}
  if(d.importance!=null){h+=fld("Importance",String(d.importance));}
  var ce=net.getConnectedEdges(p.nodes[0]);
  if(ce.length){
    var ch="";
    ce.slice(0,6).forEach(function(eid){
      var e=ve.get(eid);
      var oid=e.from===p.nodes[0]?e.to:e.from;
      var dir=e.from===p.nodes[0]?"&rarr;":"&larr;";
      var on=vn.get(oid);
      var ol=on?on._r.label:String(oid);
      ch+='<div class="conn">'+dir+(e.label?" "+x(e.label)+" ":" ")+'<span>'+x(ol)+"</span></div>";
    });
    if(ce.length>6){ch+='<div class="conn" style="color:#484f58">+ '+(ce.length-6)+" more</div>";}
    h+=fld("Connections ("+ce.length+")",ch);
  }
  body.innerHTML=h;
});

function fld(lbl,val){
  return '<div class="pf"><div class="pf-lbl">'+x(lbl)+'</div><div class="pf-val">'+val+"</div></div>";
}
function x(s){
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
</script>
</body>
</html>
"""
