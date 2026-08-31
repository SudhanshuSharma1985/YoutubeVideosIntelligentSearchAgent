#!/usr/bin/env python3
"""
l2gen - condition-agnostic L1->L2 decision-forest compiler.

Learned from two clinician-authored ground truths in cds-med-recommenders-clinical-l2:
  hld-ada/  (dyslipidemia)   and   htn-acc/2025/  (hypertension)

Input : a condition profile (YAML) + a recommendation catalogue
Output: forest JSON (IR), Mermaid decision tree, and the L2 TSV tables,
        all generated from one source so they cannot drift.

The profile carries everything condition-local. Nothing in this file
mentions a specific disease.
"""
from __future__ import annotations
import json, re, sys, csv, io
from collections import defaultdict, deque
from pathlib import Path

import yaml

# =========================================================================
# 1. GRAMMAR  - invariant across both ground truths, not profile-tunable
# =========================================================================
NODE_TYPES = {
    "entry":          {"shape": ("([", "])"), "terminal": False, "class": "entry"},
    "context":        {"shape": ("([", "])"), "terminal": False, "class": "context"},
    "driver":         {"shape": ("[",  "]"),  "terminal": False, "class": "driver"},
    "compute":        {"shape": ("{{", "}}"), "terminal": False, "class": "compute"},
    "recommendation": {"shape": ("[",  "]"),  "terminal": True,  "class": "recommendation"},
    "noguidance":     {"shape": ("[",  "]"),  "terminal": True,  "class": "noguidance"},
    "module_entry":   {"shape": ("([", "])"), "terminal": False, "class": "module_entry"},
    "module_call":    {"shape": ("[",  "]"),  "terminal": True,  "class": "module_call"},
}
GUARD_KINDS = {"boolean", "null_check", "comparison", "range", "enum_member",
               "unknown", "always", "otherwise"}

# Colour convention taken from the ground-truth renderings.
CLASSDEF = """  classDef entry fill:#d9d9d9,stroke:#666,color:#000;
  classDef context fill:#d9d9d9,stroke:#666,color:#000;
  classDef driver fill:#2ec4d6,stroke:#0b7c8a,color:#000;
  classDef compute fill:#3ecf3e,stroke:#1a7a1a,color:#000;
  classDef recommendation fill:#f5f04a,stroke:#8a8300,color:#000;
  classDef noguidance fill:#f26a1b,stroke:#8a3a00,color:#000;
  classDef module_entry fill:#2f7fe0,stroke:#154a86,color:#fff;
  classDef module_call fill:#2f7fe0,stroke:#154a86,color:#fff;"""


# =========================================================================
# 2. IR constructors
# =========================================================================
def node(nid, ntype, label, **kw):
    d = {"id": nid, "type": ntype, "label": label}
    d.update({k: v for k, v in kw.items() if v is not None})
    d.setdefault("provenance", [])
    return d


def edge(frm, to, kind, display, **kw):
    g = {"kind": kind, "display": display}
    g.update({k: v for k, v in kw.items() if k in ("op", "value", "unit") and v is not None})
    return {"from": frm, "to": to, "guard": g,
            "provenance": kw.get("provenance", [])}


def labelled(name, section):
    """Ground truth puts the guideline section inside the node label."""
    return f"{name} ({section})" if section else name


# =========================================================================
# 3. ARCHETYPES  - reconciled from both ground truths
# =========================================================================
class Builder:
    def __init__(self, prof):
        self.p = prof
        self.cond = prof["condition"]
        self.naming = prof["naming_profile"]
        self.drivers = {d["name"]: d for d in prof["drivers"]}
        self.recs = {r["id"]: r for r in prof["recommendations"]}
        self.modules = []

    # ---- helpers --------------------------------------------------------
    def ng(self, key):
        return self.naming["noguidance"].format(condition=self.cond, reason=key)

    def drv(self, name):
        d = self.drivers[name]
        return node(name, "compute" if d.get("derived") else "driver",
                    labelled(d.get("label", name), d.get("section")),
                    variable=name, provenance=d.get("provenance", []))

    def rec_node(self, rid):
        r = self.recs[rid]
        return node(rid, "recommendation", rid,
                    grade=r.get("grade"), polarity=r.get("polarity", "recommended"),
                    provenance=r.get("provenance", []))

    def branches(self, name):
        """Total branch set for a driver: declared branches + null branch."""
        d = self.drivers[name]
        out = list(d["branches"])
        if d.get("nullable", True):
            if not any(b.get("kind") == "unknown" for b in out):
                out.append({"kind": "unknown", "display": f"{name} unavailable/unknown",
                            "to": d.get("on_unknown", "NOGUIDANCE:MissingData")})
        return out

    # ---- A1: scope gate (root) -----------------------------------------
    def scope_gate(self):
        nodes, edges = [], []
        nodes.append(node("Start", "entry", "Start"))
        nodes.append(node(f"Ctx-{self.cond}", "context",
                          self.p.get("context_label", f"{self.cond} Clinical Drivers Strategy")))
        edges.append(edge("Start", f"Ctx-{self.cond}", "always", "always"))
        cursor = f"Ctx-{self.cond}"

        for i, gate in enumerate(self.p["scope_gates"]):
            gid = gate["driver"]
            nodes.append(self.drv(gid))
            if i == 0:
                edges.append(edge(cursor, gid, "always", "always"))
            for br in self.branches(gid):
                tgt = br.get("to")
                if tgt and tgt.startswith("NOGUIDANCE:"):
                    t = self.ng(tgt.split(":", 1)[1])
                    if not any(n["id"] == t for n in nodes):
                        nodes.append(node(t, "noguidance", t,
                                          provenance=gate.get("provenance", [])))
                    edges.append(edge(gid, t, br["kind"], br["display"],
                                      op=br.get("op"), value=br.get("value"),
                                      provenance=gate.get("provenance", [])))
                elif tgt and tgt.startswith("REC:"):
                    rid = tgt.split(":", 1)[1]
                    if not any(n["id"] == rid for n in nodes):
                        nodes.append(self.rec_node(rid))
                    edges.append(edge(gid, rid, br["kind"], br["display"],
                                      op=br.get("op"), value=br.get("value")))
                elif tgt and tgt in self.drivers:
                    edges.append(edge(gid, tgt, br["kind"], br["display"],
                                      op=br.get("op"), value=br.get("value")))
                else:
                    edges.append(edge(gid, "__NEXT__", br["kind"], br["display"],
                                      op=br.get("op"), value=br.get("value")))
            cursor = gid
        return nodes, edges, cursor

    # ---- A4: treatment-state split -------------------------------------
    def treatment_split(self, cursor, nodes, edges):
        ts = self.p["treatment_split"]
        nodes.append(self.drv(ts["driver"]))
        for br in self.branches(ts["driver"]):
            tgt = br.get("to")
            if tgt and tgt.startswith("MODULE:"):
                mid = tgt.split(":", 1)[1]
                cid = f"Call-{mid}"
                nodes.append(node(cid, "module_call", self.modtitle(mid), target=mid))
                edges.append(edge(ts["driver"], cid, br["kind"], br["display"],
                                  op=br.get("op"), value=br.get("value")))
            elif tgt and tgt.startswith("NOGUIDANCE:"):
                t = self.ng(tgt.split(":", 1)[1])
                if not any(n["id"] == t for n in nodes):
                    nodes.append(node(t, "noguidance", t))
                edges.append(edge(ts["driver"], t, br["kind"], br["display"]))
            elif tgt and tgt.startswith("REC:"):
                rid = tgt.split(":", 1)[1]
                if not any(n["id"] == rid for n in nodes):
                    nodes.append(self.rec_node(rid))
                edges.append(edge(ts["driver"], rid, br["kind"], br["display"],
                                  op=br.get("op"), value=br.get("value")))
            elif tgt and tgt in self.drivers:
                if not any(n["id"] == tgt for n in nodes):
                    nodes.append(self.drv(tgt))
                edges.append(edge(ts["driver"], tgt, br["kind"], br["display"],
                                  op=br.get("op"), value=br.get("value")))
        return ts["driver"]

    def modtitle(self, mid):
        for m in self.p["modules"]:
            if m["id"] == mid:
                return m["title"]
        return mid

    def _wire_step(self, step, nodes, edges, tag):
        """Wire one decision step. Targets may be REC:, MODULE:, NOGUIDANCE: or a driver name."""
        name = step["driver"]
        if not any(n["id"] == name for n in nodes):
            nodes.append(self.drv(name))
        for br in self.branches(name):
            tgt = br.get("to")
            if tgt is None:
                continue
            if tgt.startswith("REC:"):
                dst = tgt.split(":", 1)[1]
                if not any(n["id"] == dst for n in nodes):
                    nodes.append(self.rec_node(dst))
            elif tgt.startswith("MODULE:"):
                mid = tgt.split(":", 1)[1]
                dst = f"Call-{mid}-from-{tag}"
                if not any(n["id"] == dst for n in nodes):
                    nodes.append(node(dst, "module_call", self.modtitle(mid), target=mid))
            elif tgt.startswith("NOGUIDANCE:"):
                dst = self.ng(tgt.split(":", 1)[1])
                if not any(n["id"] == dst for n in nodes):
                    nodes.append(node(dst, "noguidance", dst))
            else:
                dst = tgt
                if not any(n["id"] == dst for n in nodes):
                    nodes.append(self.drv(dst))
            edges.append(edge(name, dst, br["kind"], br["display"],
                              op=br.get("op"), value=br.get("value"),
                              provenance=br.get("provenance", [])))

    # ---- A5/A6: generic module builder ---------------------------------
    def build_module(self, m):
        nodes = [node(f"Entry-{m['id']}", "module_entry", m["title"])]
        edges = []
        cursor = f"Entry-{m['id']}"
        # a module is a declarative list of decision steps
        first = m["steps"][0]["driver"]
        edges.append(edge(cursor, first, "always", "always"))
        for step in m["steps"]:
            self._wire_step(step, nodes, edges, m["id"])
        return {"id": m["id"], "kind": "module", "title": m["title"],
                "entry": f"Entry-{m['id']}", "status": "built",
                "nodes": nodes, "edges": edges}

    # ---- assemble -------------------------------------------------------
    def build(self):
        nodes, edges, cursor = self.scope_gate()
        # stitch the "continue" edges of the scope gates into a chain
        chain = [g["driver"] for g in self.p["scope_gates"]] + self.p["driver_spine"]
        for i, name in enumerate(self.p["driver_spine"]):
            nodes.append(self.drv(name))
        nxt = {chain[i]: chain[i + 1] for i in range(len(chain) - 1)}
        ts_driver = self.p["treatment_split"]["driver"]
        nxt[chain[-1]] = ts_driver
        for e in edges:
            if e["to"] == "__NEXT__":
                e["to"] = nxt[e["from"]]
        # driver spine: each driver continues on "known", diverts on unknown
        for name in self.p["driver_spine"]:
            d = self.drivers[name]
            edges.append(edge(name, nxt[name], "null_check",
                              d.get("known_label", f"{name} Known"),
                              op="is_not_null",
                              provenance=d.get("provenance", [])))
            if d.get("nullable", True):
                t = self.ng(d.get("on_unknown_reason", "MissingData"))
                if not any(n["id"] == t for n in nodes):
                    nodes.append(node(t, "noguidance", t))
                edges.append(edge(name, t, "unknown", f"{name} unavailable/unknown"))
        cursor = self.treatment_split(chain[-1], nodes, edges)
        for step in self.p.get("root_steps", []):
            self._wire_step(step, nodes, edges, "root")

        root = {"id": f"{self.cond}-DecisionTree", "kind": "root",
                "title": f"{self.cond} Decision Tree", "entry": "Start",
                "status": "built", "nodes": nodes, "edges": edges}
        subtrees = [root] + [self.build_module(m) for m in self.p["modules"]]
        return {"forest_id": self.p["forest_id"], "condition": self.cond,
                "source": self.p["source"], "evidence_grading": self.p["evidence_grading"],
                "naming_profile": self.naming,
                "drivers": self.p["drivers"], "recommendations": self.p["recommendations"],
                "constraints": self.p.get("constraints", []),
                "scope": {"no_guidance": self.p.get("scope_declaration", {}),
                          "handoffs": self.p.get("handoffs", [])},
                "subtrees": subtrees}


# =========================================================================
# 4. VALIDATOR  - every lint traceable to a ground-truth observation
# =========================================================================
class Validator:
    def __init__(self, forest):
        self.f = forest
        self.errs, self.warns, self.info = [], [], []
        self.drivers = {d["name"]: d for d in forest["drivers"]}
        self.recs = {r["id"]: r for r in forest["recommendations"]}
        self.id_re = re.compile(forest["naming_profile"]["id_regex"])

    def E(self, code, msg): self.errs.append(f"[{code}] {msg}")
    def W(self, code, msg): self.warns.append(f"[{code}] {msg}")

    def run(self):
        entries = {s["id"]: s["entry"] for s in self.f["subtrees"]}
        for st in self.f["subtrees"]:
            seen_ids = defaultdict(int)
            for n in st["nodes"]:
                seen_ids[n["id"]] += 1
            for nid, c in seen_ids.items():
                if c > 1:
                    self.E("A13", f"{st['id']}: node id '{nid}' declared {c} times")
            nodes = {n["id"]: n for n in st["nodes"]}
            out, inc = defaultdict(list), defaultdict(list)
            for e in st["edges"]:
                if e["from"] not in nodes:
                    self.E("A0", f"{st['id']}: edge from unknown node {e['from']}"); continue
                if e["to"] not in nodes:
                    self.E("A0", f"{st['id']}: edge to unknown node {e['to']}"); continue
                out[e["from"]].append(e); inc[e["to"]].append(e)

            # A1 id grammar
            for nid, n in nodes.items():
                if n["type"] in ("recommendation", "noguidance") and not self.id_re.match(nid):
                    self.E("A1", f"{st['id']}: leaf id '{nid}' violates naming_profile regex")
            # A2 single entry, no incoming
            if st["entry"] not in nodes:
                self.E("A2", f"{st['id']}: entry {st['entry']} missing")
            elif inc[st["entry"]]:
                self.E("A2", f"{st['id']}: entry {st['entry']} has incoming edges")
            # A3 branching, terminality
            for nid, n in nodes.items():
                spec = NODE_TYPES[n["type"]]
                if spec["terminal"] and out[nid]:
                    self.E("A3", f"{st['id']}: terminal {n['type']} {nid} has outgoing edges")
                if n["type"] in ("driver", "compute") and len(out[nid]) < 2:
                    self.E("A3", f"{st['id']}: decision node {nid} has {len(out[nid])} branch(es)")
            # A4 guard totality incl. NULL  (GT: 'Risk unavailable/unknown', 'lipidPanel is null')
            for nid, n in nodes.items():
                if n["type"] not in ("driver", "compute"):
                    continue
                var = n.get("variable")
                d = self.drivers.get(var)
                if d is None:
                    self.E("A4", f"{st['id']}: node {nid} references undeclared driver '{var}'"); continue
                kinds = {e["guard"]["kind"] for e in out[nid]}
                if d.get("nullable", True) and "unknown" not in kinds and "null_check" not in kinds:
                    self.E("A4", f"{st['id']}: driver {nid} is nullable but has no unknown/null branch "
                                 f"(declare nullable:false with a reason to suppress)")
                if d["type"] == "enum":
                    dom = set(d.get("domain", []))
                    cov = set()
                    for e in out[nid]:
                        for v in (e["guard"].get("value") or []):
                            if v in cov:
                                self.E("A4", f"{st['id']}: {nid} value '{v}' matched by >1 guard")
                            cov.add(v)
                    if dom - cov and "otherwise" not in kinds:
                        self.E("A4", f"{st['id']}: {nid} not exhaustive, uncovered={sorted(dom-cov)}")
                    if cov - dom:
                        self.E("A4", f"{st['id']}: {nid} guards outside domain: {sorted(cov-dom)}")
                if d["type"] == "boolean":
                    bools = {str(e["guard"].get("value")) for e in out[nid]
                             if e["guard"]["kind"] == "boolean" and "value" in e["guard"]}
                    if bools and bools != {"True", "False"}:
                        self.E("A4", f"{st['id']}: boolean {nid} guards {sorted(bools)} != TRUE/FALSE")
            # A5 reachability
            seen, q = set(), deque([st["entry"]])
            while q:
                x = q.popleft()
                if x in seen: continue
                seen.add(x)
                for e in out[x]: q.append(e["to"])
            for nid in nodes:
                if nid not in seen:
                    self.E("A5", f"{st['id']}: {nid} unreachable")
            # A6 statelessness  (GT: no loops; cadence lives in worklist/timeline tables)
            colour = {}
            def dfs(u):
                colour[u] = 1
                for e in out[u]:
                    v = e["to"]
                    if colour.get(v) == 1:
                        self.E("A6", f"{st['id']}: cycle {u} -> {v}; trees must be stateless "
                                     f"per encounter, move cadence to the timeline table")
                    elif colour.get(v) is None:
                        dfs(v)
                colour[u] = 2
            for nid in nodes:
                if colour.get(nid) is None: dfs(nid)
            # A7 module call resolution
            for nid, n in nodes.items():
                if n["type"] == "module_call" and n.get("target") not in entries:
                    self.E("A7", f"{st['id']}: module_call {nid} target '{n.get('target')}' unresolved")
            # A8 provenance in label  (GT: 'Has CKD (5.3.8)')
            for nid, n in nodes.items():
                if n["type"] in ("driver", "compute"):
                    d = self.drivers.get(n.get("variable"), {})
                    if d.get("section") and f"({d['section']})" not in n["label"]:
                        self.W("A8", f"{st['id']}: {nid} label lacks its guideline section")

        # ---- cross-node lints ------------------------------------------
        # A9 guard-aware dedup: same label + different guard sets => must stay distinct
        by_label = defaultdict(list)
        for st in self.f["subtrees"]:
            out = defaultdict(list)
            for e in st["edges"]: out[e["from"]].append(e)
            for n in st["nodes"]:
                if n["type"] in ("driver", "compute"):
                    gs = frozenset(e["guard"]["display"] for e in out[n["id"]])
                    by_label[n["label"]].append((st["id"], n["id"], gs))
        for lab, insts in by_label.items():
            gsets = {g for _, _, g in insts}
            if len(insts) > 1 and len(gsets) > 1:
                self.info.append(f"[A9] '{lab}' appears {len(insts)}x with {len(gsets)} distinct "
                                 f"guard sets - MUST NOT be merged by dedup")
        # A10 no hidden compound predicates inside a boolean
        for name, d in self.drivers.items():
            if d["type"] == "boolean":
                note = (d.get("definition") or "")
                if re.search(r"[<>]=?\s*\d", note):
                    self.E("A10", f"driver '{name}' hides a compound predicate in its definition "
                                  f"(\"{note}\"); decompose into exposed drivers")
        # A11 risk-model validated range gate
        for name, d in self.drivers.items():
            if d.get("risk_model"):
                rng = d["risk_model"].get("validated_range")
                if rng and not d.get("gate_driver"):
                    self.E("A11", f"driver '{name}' consumes {d['risk_model']['name']} "
                                  f"(validated {rng}) with no declared gate_driver")
        # A12 every recommendation carries a verified guideline section
        for rid, r in self.recs.items():
            if not r.get("section"):
                self.E("A12", f"recommendation {rid} has no guideline section")
            elif r.get("section_verified") is False:
                self.E("A12", f"recommendation {rid} section '{r['section']}' is not "
                              f"source-verified; verify against L1 before publication")
        return self


# =========================================================================
# 5. REFERENTIAL INTEGRITY across tree <-> tables
# =========================================================================
def referential_integrity(forest):
    errs = []
    leaf_recs, leaf_ng, used_drivers = set(), set(), set()
    for st in forest["subtrees"]:
        for n in st["nodes"]:
            if n["type"] == "recommendation": leaf_recs.add(n["id"])
            if n["type"] == "noguidance":     leaf_ng.add(n["id"])
            if n["type"] in ("driver", "compute"): used_drivers.add(n.get("variable"))
    cat = {r["id"] for r in forest["recommendations"]}
    dec = {d["name"] for d in forest["drivers"]}
    for r in sorted(leaf_recs - cat):
        errs.append(f"[R1] tree leaf {r} has no row in Recommendation Definitions")
    for r in sorted(cat - leaf_recs):
        errs.append(f"[R2] Recommendation Definitions row {r} is unreachable in the tree")
    for d in sorted(used_drivers - dec):
        errs.append(f"[R3] tree node uses driver {d} absent from Exposed Clinical Drivers")
    for d in sorted(dec - used_drivers):
        errs.append(f"[R4] declared driver {d} is never used in the tree")
    for r in forest["recommendations"]:
        for m in r.get("med_orders", []):
            if not m.get("rxnorm") and not m.get("class"):
                errs.append(f"[R5] {r['id']} med order '{m.get('name')}' has neither RxNorm nor class")
    return errs


# =========================================================================
# 6. BOUNDARY PROBES from declared numeric cut points
# =========================================================================
def boundary_probes(forest, classifiers):
    """classifiers: {driver_name: python callable} supplied by the profile module."""
    rows = []
    for d in forest["drivers"]:
        fn = classifiers.get(d["name"])
        if not fn:
            continue
        for case in d.get("probes", []):
            got = fn(**case["input"])
            rows.append((d["name"], case["input"], case["expect"], got,
                         "PASS" if got == case["expect"] else "FAIL"))
    return rows


# =========================================================================
# 7. EMITTERS
# =========================================================================
def to_mermaid(st):
    L = ["flowchart TD"]
    sid = lambda x: re.sub(r"[^A-Za-z0-9]", "_", x)
    for n in st["nodes"]:
        o, c = NODE_TYPES[n["type"]]["shape"]
        L.append(f'  {sid(n["id"])}{o}"{n["label"]}"{c}')
    for e in st["edges"]:
        d = e["guard"]["display"]
        a, b = sid(e["from"]), sid(e["to"])
        L.append(f"  {a} --> {b}" if d == "always" else f'  {a} -->|"{d}"| {b}')
    L.append(CLASSDEF)
    by = defaultdict(list)
    for n in st["nodes"]:
        by[NODE_TYPES[n["type"]]["class"]].append(sid(n["id"]))
    for k, v in by.items():
        L.append(f"  class {','.join(v)} {k};")
    return "\n".join(L)


def tsv(rows, header):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def emit_tables(forest):
    t = {}
    t["Exposed Clinical Drivers.tsv"] = tsv(
        [[d["name"], d["type"], d.get("label", d["name"]), d.get("section", ""),
          "|".join(str(x) for x in d.get("domain", [])), d.get("unit", ""),
          "yes" if d.get("nullable", True) else "no",
          ";".join(f"{b['system']}:{b['code']}" for b in d.get("bindings", []))]
         for d in forest["drivers"]],
        ["driver", "type", "label", "guideline_section", "domain", "unit",
         "nullable", "terminology_bindings"])
    t["Clinical Concepts.tsv"] = tsv(
        [[b["system"], b["code"], b.get("display", ""), d["name"]]
         for d in forest["drivers"] for b in d.get("bindings", [])],
        ["code_system", "code", "display", "driver"])
    t["Recommendation Definitions.tsv"] = tsv(
        [[r["id"], r.get("grade", ""), r.get("polarity", "recommended"),
          r.get("text", ""), r.get("section", ""),
          r.get("cadence", ""), "|".join(m.get("name", "") for m in r.get("med_orders", []))]
         for r in forest["recommendations"]],
        ["recommendation_id", "grade", "polarity", "text", "guideline_section",
         "cadence", "med_orders"])
    t["Med Orders.tsv"] = tsv(
        [[r["id"], m.get("name", ""), m.get("class", ""), m.get("rxnorm", ""),
          m.get("dose", ""), m.get("caution", "")]
         for r in forest["recommendations"] for m in r.get("med_orders", [])],
        ["recommendation_id", "medication", "class", "rxnorm", "dose", "caution"])
    t["Constraints.tsv"] = tsv(
        [[c["id"], c.get("grade", ""), c["scope"], c["rule"], c.get("section", "")]
         for c in forest.get("constraints", [])],
        ["constraint_id", "grade", "scope", "rule", "guideline_section"])
    return t


# =========================================================================
# 8. CLI
# =========================================================================
def compile_profile(path, classifiers=None, outdir="."):
    prof = yaml.safe_load(Path(path).read_text())
    forest = Builder(prof).build()
    v = Validator(forest).run()
    ri = referential_integrity(forest)
    probes = boundary_probes(forest, classifiers or {})
    forest["validation"] = {
        "errors": v.errs + ri, "warnings": v.warns, "info": v.info,
        "probes_passed": sum(1 for p in probes if p[4] == "PASS"),
        "probes_total": len(probes),
    }
    out = Path(outdir); out.mkdir(parents=True, exist_ok=True)
    (out / f"{prof['condition'].lower()}-forest.json").write_text(json.dumps(forest, indent=2))
    md = [f"# {prof['condition']} Decision Tree\n",
          f"Source: {prof['source']['citation']}\n",
          f"Grading system: {prof['evidence_grading']['name']}\n"]
    for st in forest["subtrees"]:
        md.append(f"\n## {st['title']}\n")
        md.append("```mermaid\n" + to_mermaid(st) + "\n```\n")
    md.append("\n## L2 Tables\n")
    for name in emit_tables(forest):
        md.append(f"- [{name.replace('.tsv','')}]({name})")
    (out / f"{prof['condition'].lower()}-decision-tree.md").write_text("\n".join(md))
    for name, body in emit_tables(forest).items():
        (out / f"{prof['condition']} {name}").write_text(body)
    return forest, v, ri, probes


if __name__ == "__main__":
    import importlib.util
    prof_path = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else "."
    clf = {}
    helper = Path(prof_path).with_suffix(".classifiers.py")
    if helper.exists():
        spec = importlib.util.spec_from_file_location("clf", helper)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        clf = mod.CLASSIFIERS
    forest, v, ri, probes = compile_profile(prof_path, clf, outdir)
    n = sum(len(s["nodes"]) for s in forest["subtrees"])
    e = sum(len(s["edges"]) for s in forest["subtrees"])
    print("=" * 74)
    print(f"{forest['condition']}: {len(forest['subtrees'])} subtrees | {n} nodes | {e} edges | "
          f"{len(forest['recommendations'])} recommendations | {len(forest['drivers'])} drivers")
    print("=" * 74)
    print(f"STRUCTURAL + REFERENTIAL ERRORS: {len(v.errs)+len(ri)}")
    for x in v.errs + ri: print("  ERR ", x)
    print(f"WARNINGS: {len(v.warns)}")
    for x in v.warns: print("  WARN", x)
    print(f"NOTES: {len(v.info)}")
    for x in v.info: print("  NOTE", x)
    print(f"BOUNDARY PROBES: {sum(1 for p in probes if p[4]=='PASS')}/{len(probes)}")
    for name, inp, exp, got, r in probes:
        if r == "FAIL" or True:
            print(f"  {r}  {name}{inp} expect={exp} got={got}")
