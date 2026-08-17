"""Generates a self-contained interactive HTML visualisation of a subgraph."""

import json

import networkx as nx

# DECISION: one saturated hue per node type, used as a solid fill. The previous
# palette drew hollow rings on near-black fills, which read as noise at small
# sizes; a filled dot with a same-hue glow keeps the type legible at 14px.
_TYPE_COLORS: dict[str, str] = {
    "function":  "#f97316",   # slurp orange
    "class":     "#3b82f6",
    "module":    "#8b5cf6",
    "interface": "#06b6d4",
    "struct":    "#10b981",
    "macro":     "#f59e0b",
    "property":  "#6366f1",
    "import":    "#64748b",
    "default":   "#94a3b8",
    # Diff-mode types, used by `slurp diff --viz`.
    "added":     "#22c55e",
    "removed":   "#ef4444",
    "modified":  "#eab308",
    "unchanged": "#475569",
}

_SIZE_MIN = 14.0
_SIZE_MAX = 32.0

# Imports are structural noise next to real symbols — kept visible but recessive.
_IMPORT_SIZE = 10.0

# Labels up to this length are drawn inside the node; longer ones sit below it.
_INLINE_LABEL_MAX = 12

# Below this score a node's label is revealed on hover instead of drawn always.
_QUIET_LABEL_SCORE = 0.3


def _node_size(score: float) -> float:
    return _SIZE_MIN + score * (_SIZE_MAX - _SIZE_MIN)


def _darken(hex_color: str, factor: float = 0.15) -> str:
    """Return *hex_color* darkened by *factor* (0–1), for node borders.

    Args:
        hex_color: Colour as "#rrggbb".
        factor: Fraction of each channel to remove.

    Returns:
        The darkened colour as "#rrggbb".
    """
    raw = hex_color.lstrip("#")
    channels = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return "#" + "".join(f"{max(0, int(c * (1 - factor))):02x}" for c in channels)


def _type_color(node_type: str) -> str:
    return _TYPE_COLORS.get(node_type, _TYPE_COLORS["default"])


# Extensions that mark a label as a source file rather than a symbol.
_MODULE_EXTENSIONS = (
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".pyi",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".swift", ".rb",
    ".php", ".cs", ".c", ".h", ".cpp", ".cc", ".hpp", ".lua", ".ex",
    ".exs", ".ps1", ".psm1",
)


def _infer_type_from_label(label: str) -> str:
    """Guess a node type from its label alone.

    Graphify-generated graphs carry no per-symbol ``type``; without this every
    node would render in the single ``default`` grey. The heuristics are ordered
    most- to least-specific.

    Args:
        label: The node's display label.

    Returns:
        A key of _TYPE_COLORS.
    """
    text = str(label).strip()
    if not text:
        return "default"
    if text.endswith("()"):
        return "function"
    lowered = text.lower()
    if lowered.endswith(_MODULE_EXTENSIONS):
        return "module"
    # A bare CapitalisedName with no extension reads as a type, not a file.
    head = text.split(".")[-1].split("/")[-1]
    if head[:1].isupper():
        return "class"
    return "default"


def _resolve_node_type(attrs: dict) -> str:
    """Determine which palette entry a node should use.

    An explicit ``type`` wins when it names a real palette entry. Otherwise the
    label is inspected — including for ``file_type: 'code'`` nodes, where the
    label is the only signal available.

    Args:
        attrs: The node's attribute dict.

    Returns:
        A key of _TYPE_COLORS.
    """
    explicit = attrs.get("type")
    if explicit and explicit in _TYPE_COLORS:
        return explicit

    label = attrs.get("label", "")
    file_type = attrs.get("file_type")
    if file_type == "code":
        return _infer_type_from_label(label)
    if file_type == "document":
        return "module"

    # No explicit type and no file_type: fall back to the label, then to any
    # unrecognised explicit type string (kept so it still shows in the legend).
    inferred = _infer_type_from_label(label)
    if inferred != "default":
        return inferred
    return "default"


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
    present_types: list[str] = []
    for nid in display_ids:
        attrs = G.nodes[nid]
        score = scores.get(nid, 0.0)
        node_type = _resolve_node_type(attrs)
        if node_type not in present_types:
            present_types.append(node_type)
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

    # DECISION: the legend lists only the types actually present. A fixed legend of
    # all fourteen types would be mostly dead entries on a typical subgraph.
    legend = [
        {"type": t, "color": _type_color(t)}
        for t in sorted(present_types)
    ]

    # Token savings vs naively injecting the full graph (≈ 50 tokens/node baseline).
    baseline = stats["nodes_total"] * 50
    saved = max(0, baseline - stats["tokens_used"])
    saved_pct = round(saved / baseline * 100) if baseline > 0 else 0

    palette = {
        t: {"fill": c, "border": _darken(c)} for t, c in _TYPE_COLORS.items()
    }

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
        .replace("<<<PALETTE_JSON>>>",   json.dumps(palette))
        .replace("<<<LEGEND_JSON>>>",    json.dumps(legend, ensure_ascii=False))
        .replace("<<<INLINE_MAX>>>",     str(_INLINE_LABEL_MAX))
        .replace("<<<IMPORT_SIZE>>>",    str(_IMPORT_SIZE))
        .replace("<<<QUIET_SCORE>>>",    str(_QUIET_LABEL_SCORE))
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
    :root {
      --bg:        #0d1117;
      --surface:   #0f172a;
      --border:    #1e293b;
      --ink:       #e2e8f0;
      --ink-dim:   #94a3b8;
      --ink-mute:  #475569;
      --accent:    #f97316;
      --accent-2:  #fbbf24;
      --cyan:      #22d3ee;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--ink);
      font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      -webkit-font-smoothing: antialiased;
    }

    /* ---- header ---- */
    #hdr {
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 11px 18px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      flex-wrap: wrap;
    }
    .brand {
      color: var(--accent);
      font-weight: 700;
      font-size: 14px;
      letter-spacing: .4px;
    }
    .qry { font-size: 12.5px; color: var(--ink-mute); }
    .qry em {
      color: var(--accent);
      font-style: normal;
      font-weight: 700;
    }
    .stats {
      font-size: 11.5px;
      color: var(--ink-dim);
      display: flex;
      align-items: center;
      gap: 9px;
    }
    .stats b { color: var(--ink); font-weight: 600; }
    .sep { color: var(--ink-mute); }
    .save {
      margin-left: auto;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      color: #1c1207;
      font-weight: 700;
      font-size: 11px;
      padding: 4px 12px;
      border-radius: 999px;
      white-space: nowrap;
      box-shadow: 0 0 16px rgba(249, 115, 22, .25);
    }

    /* ---- layout ---- */
    #wrap { display: flex; flex: 1; overflow: hidden; position: relative; }
    #graph {
      flex: 1;
      min-width: 0;
      background-color: var(--bg);
      /* Subtle dot grid: gives the canvas depth without competing with edges. */
      background-image: radial-gradient(rgba(30, 41, 59, .8) 1px, transparent 1px);
      background-size: 30px 30px;
    }

    /* ---- legend ---- */
    #legend {
      position: absolute;
      left: 14px;
      bottom: 14px;
      background: rgba(15, 23, 42, .92);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 9px 11px;
      font-size: 10.5px;
      backdrop-filter: blur(6px);
      user-select: none;
      max-width: 190px;
    }
    #legend-hd {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      color: var(--ink-mute);
      text-transform: uppercase;
      letter-spacing: .7px;
      font-size: 9px;
      font-weight: 700;
      cursor: pointer;
    }
    #legend-hd:hover { color: var(--ink-dim); }
    #legend-items { margin-top: 7px; display: grid; gap: 4px; }
    #legend-items.hidden { display: none; }
    .lg-row { display: flex; align-items: center; gap: 7px; color: var(--ink-dim); }
    .lg-dot {
      width: 8px; height: 8px; border-radius: 50%;
      flex-shrink: 0;
    }

    /* ---- panel ---- */
    #panel {
      width: 286px;
      flex-shrink: 0;
      background: var(--surface);
      border-left: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #phdr {
      padding: 13px 15px 11px;
      font-size: 9.5px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .9px;
      color: var(--ink-mute);
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    #pbody { overflow-y: auto; padding: 15px; flex: 1; }
    .hint { color: var(--ink-mute); font-size: 12px; text-align: center; padding-top: 36px; line-height: 1.7; }

    .node-hd { display: flex; align-items: center; gap: 9px; margin-bottom: 4px; }
    .node-dot {
      width: 11px; height: 11px; border-radius: 50%;
      flex-shrink: 0;
    }
    .node-label {
      color: #fff;
      font-size: 16px;
      font-weight: 700;
      word-break: break-word;
      line-height: 1.25;
    }
    .type-badge {
      display: inline-block;
      padding: 2px 9px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: .3px;
      color: #0b1220;
      margin-bottom: 15px;
    }

    .pf { margin-bottom: 14px; }
    .pf-lbl {
      font-size: 9.5px;
      text-transform: uppercase;
      letter-spacing: .6px;
      color: var(--ink-mute);
      margin-bottom: 5px;
    }
    .pf-val { font-size: 12px; color: var(--ink); word-break: break-word; line-height: 1.5; }
    .score-row { display: flex; align-items: baseline; justify-content: space-between; }
    .score-num { font-size: 13px; font-weight: 700; color: var(--accent); }
    .bar-bg {
      height: 5px;
      background: #16202e;
      border-radius: 3px;
      margin-top: 7px;
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      border-radius: 3px;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      box-shadow: 0 0 10px rgba(249, 115, 22, .5);
    }
    .relevance { font-size: 11.5px; color: var(--ink-dim); line-height: 1.55; }
    .src {
      color: var(--cyan);
      font-size: 11px;
      word-break: break-all;
      text-decoration: none;
      border-bottom: 1px dotted rgba(34, 211, 238, .4);
    }
    .src:hover { border-bottom-style: solid; }
    .conn {
      font-size: 11.5px;
      color: var(--ink-dim);
      margin-top: 6px;
      display: flex;
      gap: 7px;
      align-items: baseline;
    }
    .conn .arrow { color: var(--accent); font-weight: 700; flex-shrink: 0; }
    .conn .peer { color: var(--ink); word-break: break-word; }
    .conn .rel { color: var(--ink-mute); }

    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--ink-mute); }
  </style>
</head>
<body>
<div id="hdr">
  <span class="brand">&#9889; slurp</span>
  <span class="qry">query: <em><<<QUERY>>></em></span>
  <span class="stats">
    <span><b><<<NODES_SELECTED>>></b>&thinsp;/&thinsp;<<<NODES_TOTAL>>> nodes<<<TOP_BADGE>>></span>
    <span class="sep">&middot;</span>
    <span><b><<<TOKENS_USED>>></b>&thinsp;/&thinsp;<<<TOKENS_BUDGET>>> tokens</span>
    <span class="sep">&middot;</span>
    <span><b><<<COVERAGE_PCT>>>%</b> coverage</span>
  </span>
  <span class="save">Saved ~<<<SAVED_TOKENS>>> tokens &middot; <<<SAVED_PCT>>>%</span>
</div>
<div id="wrap">
  <div id="graph"></div>
  <div id="legend">
    <div id="legend-hd"><span>Node types</span><span id="legend-tog">&minus;</span></div>
    <div id="legend-items"></div>
  </div>
  <div id="panel">
    <div id="phdr">Node Details</div>
    <div id="pbody"><div class="hint">Click a node<br>to inspect it</div></div>
  </div>
</div>
<script>
var PALETTE = <<<PALETTE_JSON>>>;
var LEGEND  = <<<LEGEND_JSON>>>;
var INLINE_MAX = <<<INLINE_MAX>>>;
var IMPORT_SIZE = <<<IMPORT_SIZE>>>;
var QUIET_SCORE = <<<QUIET_SCORE>>>;

function c(t){ return PALETTE[t] || PALETTE["default"]; }

var RN = <<<NODES_JSON>>>;
var RE = <<<EDGES_JSON>>>;

var DIFF_TYPES=["added","removed","modified","unchanged"];
var vn = new vis.DataSet(RN.map(function(n){
  var col = c(n.node_type);
  var isDiff = DIFF_TYPES.indexOf(n.node_type) !== -1;
  var sz = isDiff ? (n.node_type === "unchanged" ? 16 : 22)
                  : (n.node_type === "import" ? IMPORT_SIZE : n.size);
  // Short labels sit inside the node; longer ones would blow the circle out of
  // proportion and lose the score-encoded size, so they hang underneath.
  // Low-scoring nodes would carpet the canvas in text, so their label is held
  // back until hover. They stay "dot" so revealing it never resizes the node.
  var quiet = !isDiff && n.score < QUIET_SCORE;
  var inside = !quiet && String(n.label).length <= INLINE_MAX;
  return {
    id: n.id,
    label: quiet ? "" : n.label,
    title: n.label,
    size: sz,
    shape: inside ? "circle" : "dot",
    widthConstraint: inside ? {minimum: sz * 1.9, maximum: sz * 3.4} : undefined,
    color: {
      background: col.fill,
      border: col.border,
      highlight: {background: col.fill, border: "#ffffff"},
      hover: {background: col.fill, border: "#ffffff"}
    },
    font: {
      color: "#ffffff",
      size: 12,
      face: '"JetBrains Mono", ui-monospace, Menlo, Consolas, monospace',
      strokeWidth: 4,
      strokeColor: "rgba(2,6,23,0.95)",
      vadjust: inside ? 0 : 1
    },
    borderWidth: 2,
    borderWidthSelected: 3,
    opacity: n.isolated ? 0.55 : 1.0,
    // Glow in the node's own hue — this is what makes types readable at 14px.
    shadow: {enabled: true, color: col.fill, size: 12, x: 0, y: 0},
    _r: n
  };
}));

var _anyDiff = RN.some(function(n){ return DIFF_TYPES.slice(0,3).indexOf(n.node_type) !== -1; });
var ve = new vis.DataSet(RE.map(function(e){
  return {
    from:e.from, to:e.to,
    label:"",
    arrows:{to:{enabled:true,scaleFactor:0.42}},
    color:{color:"rgba(148,163,184,0.25)",highlight:"#f97316",hover:"#f97316",opacity:1},
    smooth:{type:"continuous"},
    _rel: e.label
  };
}));

var net = new vis.Network(
  document.getElementById("graph"),
  {nodes:vn, edges:ve},
  {
    nodes:{shape:"dot"},
    edges:{width:1,selectionWidth:2,hoverWidth:1},
    physics:{
      barnesHut:{gravitationalConstant:-2000,centralGravity:0.3,springLength:60,springConstant:0.04,damping:0.09},
      stabilization:{enabled:true,iterations:500,onlyDynamicEdges:false}
    },
    interaction:{hover:true,tooltipDelay:150,hideEdgesOnDrag:true},
    layout:{improvedLayout:true}
  }
);
net.once('stabilized', function(){ net.setOptions({physics:{enabled:false}}); });

/* ---- reveal held-back labels on hover ---- */
net.on("hoverNode", function(p){
  var nd = vn.get(p.node);
  if(nd && !nd.label){ vn.update({id: p.node, label: nd._r.label}); }
});
net.on("blurNode", function(p){
  var nd = vn.get(p.node);
  if(nd && nd._r.score < QUIET_SCORE && DIFF_TYPES.indexOf(nd._r.node_type) === -1){
    vn.update({id: p.node, label: ""});
  }
});

/* ---- legend ---- */
(function(){
  var box = document.getElementById("legend");
  if(!LEGEND.length){ box.style.display = "none"; return; }
  var items = document.getElementById("legend-items");
  items.innerHTML = LEGEND.map(function(l){
    return '<div class="lg-row"><span class="lg-dot" style="background:'+l.color+
           ';box-shadow:0 0 6px '+l.color+'"></span>'+x(l.type)+'</div>';
  }).join("");
  document.getElementById("legend-hd").addEventListener("click", function(){
    var hidden = items.classList.toggle("hidden");
    document.getElementById("legend-tog").innerHTML = hidden ? "+" : "&minus;";
  });
})();

/* ---- relevance copy ---- */
function relevanceText(score){
  if(score >= 0.75) return "Top match \\u2014 scored very highly against your query, so it was selected first.";
  if(score >= 0.45) return "Strong match \\u2014 directly relevant to your query.";
  if(score >= 0.20) return "Related \\u2014 moderate relevance, or pulled in as a neighbour of a strong match.";
  return "Context \\u2014 low direct relevance; included because it connects to selected nodes.";
}

net.on("click",function(p){
  var body=document.getElementById("pbody");
  if(!p.nodes.length){body.innerHTML='<div class="hint">Click a node<br>to inspect it</div>';return;}
  var nd=vn.get(p.nodes[0]);
  var d=nd._r;
  var col=c(d.node_type);
  var pct=Math.round(d.score*100);
  var h="";

  h+='<div class="node-hd"><span class="node-dot" style="background:'+col.fill+
     ';box-shadow:0 0 9px '+col.fill+'"></span><span class="node-label">'+x(d.label)+'</span></div>';
  if(d.node_type&&d.node_type!=="default"){
    h+='<span class="type-badge" style="background:'+col.fill+'">'+x(d.node_type)+'</span>';
  }

  h+='<div class="pf"><div class="pf-lbl">Score</div>'+
     '<div class="score-row"><span class="pf-val">relevance</span>'+
     '<span class="score-num">'+d.score.toFixed(4)+'</span></div>'+
     '<div class="bar-bg"><div class="bar-fill" style="width:'+pct+'%"></div></div></div>';

  h+=fld("Relevance",'<div class="relevance">'+relevanceText(d.score)+'</div>');

  if(d.description){h+=fld("Description",'<span style="color:var(--ink-dim)">'+x(d.description)+"</span>");}
  if(d.file_path){
    h+=fld("Source",'<a class="src" href="file://'+x(d.file_path)+'">'+x(d.file_path)+"</a>");
  }
  if(d.importance!=null){h+=fld("Importance",String(d.importance));}

  var ce=net.getConnectedEdges(p.nodes[0]);
  if(ce.length){
    var ch="";
    ce.slice(0,8).forEach(function(eid){
      var e=ve.get(eid);
      var oid=e.from===p.nodes[0]?e.to:e.from;
      var dir=e.from===p.nodes[0]?"&rarr;":"&larr;";
      var on=vn.get(oid);
      var ol=on?on._r.label:String(oid);
      var rel=e._rel?'<span class="rel">'+x(e._rel)+"</span> ":"";
      ch+='<div class="conn"><span class="arrow">'+dir+'</span>'+rel+
          '<span class="peer">'+x(ol)+"</span></div>";
    });
    if(ce.length>8){ch+='<div class="conn" style="color:var(--ink-mute)">+ '+(ce.length-8)+" more</div>";}
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
