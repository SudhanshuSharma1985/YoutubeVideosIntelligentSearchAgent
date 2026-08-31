#!/usr/bin/env python3
"""
l2extract.py - clinical guideline PDF -> draft L2 condition profile.

Stages 0-2 of the L1->L2 pipeline. Emits a YAML profile skeleton that a human
completes, then feeds to l2gen.py, which compiles it into the decision forest
and the L2 tables.

    pip install pymupdf anthropic pyyaml
    export ANTHROPIC_API_KEY=...

    python3 l2extract.py guideline.pdf \
        --condition Hypertension \
        --repo ~/cds-med-recommenders-clinical-l2 \
        --out profiles/htn-draft.yaml

WHAT THIS DELIBERATELY REFUSES TO GUESS
---------------------------------------
1. Scope        Which patients the recommender refuses. Not in the guideline.
                Emitted as _TODO_scope_declaration. Skipping this is what took
                a hypertension run from 100% to 53% leaf recall against the
                clinician-authored ground truth.
2. Classifiers  It records that a driver is derived; it will not write the
                comparison. Every cut point you hand-write is one you chose a
                direction for, and it gets +/-1 probes.
3. Naming       Reads sibling condition folders and reports the observed ID
                patterns. It does not pick - conventions differ per condition.
4. Provenance   Every recommendation lands section_verified: false, which fails
                l2gen's A12 lint until a human checks it against source text.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("pip install pymupdf")

try:
    import anthropic
except ImportError:
    sys.exit("pip install anthropic")


MODEL = "claude-sonnet-4-6"
_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


# ============================================================ model helpers
def ask(system: str, user: str, max_tokens: int = 8000, images=None) -> str:
    content = []
    for img in images or []:
        content.append({"type": "image",
                        "source": {"type": "base64",
                                   "media_type": "image/png", "data": img}})
    content.append({"type": "text", "text": user})
    r = client().messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": content}])
    txt = "".join(b.text for b in r.content if b.type == "text")
    for fence in ("```json", "```yaml", "```"):
        txt = txt.replace(fence, "")
    return txt.strip()


def ask_json(system: str, user: str, **kw):
    raw = ask(system + "\n\nRespond with JSON only. No preamble, no fences.",
              user, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", raw, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


# ================================================ Stage 0: inventory + triage
def page_inventory(doc):
    """Structural pass over the document. No model calls, no cost."""
    figures, sections = [], []
    for i, page in enumerate(doc):
        t = page.get_text()
        for m in re.finditer(r"Figure (\d+)\.\s*([^\n]{0,90})", t):
            figures.append({"n": int(m.group(1)), "page": i + 1,
                            "caption": " ".join(m.group(2).split())})
        for m in re.finditer(
                r"^\s*(\d+(?:\.\d+){1,3})\.?\s+([A-Z][^\n]{6,80})$", t, re.M):
            sections.append({"id": m.group(1), "title": m.group(2).strip(),
                             "page": i + 1})
    seen = set()
    figures = [f for f in figures if not (f["n"] in seen or seen.add(f["n"]))]
    return sorted(figures, key=lambda f: f["n"]), sections


AUX_RE = re.compile(
    r"(REFERENCES|Disclosures|Appendix \d|Writing Committee|"
    r"Reviewer Disclosures|Supplemental Material)", re.I)
CITE_RE = re.compile(r"^\s*\d{1,3}\.\s+[A-Z][a-z]+ [A-Z]{1,3},", re.M)


def classify_pages(doc):
    """Core vs auxiliary. Typically removes 40-50% of a long guideline
    before any model call touches it."""
    core, aux = [], []
    for i, page in enumerate(doc):
        t = page.get_text()
        header_hit = bool(AUX_RE.search(t[:1500]))
        citations = len(CITE_RE.findall(t))
        is_aux = (header_hit and len(t) < 6000) or citations > 12
        (aux if is_aux else core).append(i + 1)
    return core, aux


# ================================ Stage 1a: recommendation tables (normative)
REC_SYS = """You extract recommendations from a clinical practice guideline.

A recommendation is a numbered, graded statement inside a recommendation table.
Body prose, evidence summaries, synopsis paragraphs and figure captions are NOT
recommendations - do not return them.

For each recommendation return an object with:
  number       its number within the table
  section      the guideline section number it sits under, if visible
  grade        exactly as printed ("1", "2a", "2b", "3: No Benefit",
               "3: Harm", "1B", "2C", ...)
  loe          level of evidence if printed separately, else null
  polarity     recommended | reasonable | may_be_reasonable | no_benefit | harm
  text         the full sentence, verbatim
  population   short phrases describing who it applies to
  conditions   [{driver, op, value, unit}] for every testable clause
  action       {type, intervention}
  cadence      any stated follow-up or reassessment interval, else null

HARD RULES
- polarity must follow the grade. A "3: Harm" or "3: No Benefit" recommendation
  is a prohibition, never a positive action. Inverting this inverts clinical
  meaning and is the most damaging error you can make here.
- Copy every number and unit exactly as printed. Never round. Never convert
  between units. Preserve >= vs > and <= vs < precisely.
- If a clause combines thresholds ("SBP >= 130 or DBP >= 80"), record each
  operand separately in conditions."""


def extract_recommendations(doc, core_pages, hint=""):
    """Walk core pages, batch each recommendation table, extract."""
    out, buf, start = [], [], None
    table_hdr = re.compile(
        r"^\s*COR\s+LOE\s+Recommendation|Recommendations? for ", re.M)
    for p in core_pages:
        t = doc[p - 1].get_text()
        if table_hdr.search(t):
            if buf:
                out += _rec_batch(buf, start, hint)
            buf, start = [t], p
        elif buf and len(buf) < 4:
            buf.append(t)
    if buf:
        out += _rec_batch(buf, start, hint)
    for i, r in enumerate(out):
        r["id"] = f"REC-{r.get('section') or 'X'}-{str(r.get('number', i)).zfill(2)}"
    return out


def _rec_batch(pages, page_no, hint):
    try:
        recs = ask_json(
            REC_SYS,
            f"Guideline pages beginning at page {page_no}. {hint}\n\n"
            + "\n\n".join(pages)[:60000])
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! recommendation batch p{page_no}: {e}", file=sys.stderr)
        return []
    if isinstance(recs, dict):
        recs = recs.get("recommendations", [])
    for r in recs:
        r.setdefault("page", page_no)
    return recs


# ================================== Stage 1b: algorithm figures (vision pass)
FIG_SYS = """You transcribe a clinical algorithm figure into a decision graph.

Return {"nodes": [...], "edges": [...]}.
  node  {id, type, label, section}
        type is one of: driver | compute | recommendation | noguidance | link
        section is the guideline section printed inside the box, if any
  edge  {from, to, guard}
        guard is the arrow label, transcribed verbatim

HARD RULES
- Transcribe only what is drawn. Never infer a branch that is not on the page.
- Copy every number and comparison operator exactly. >= and > are different
  clinical rules and must not be normalised to each other.
- A box that points at another figure is type "link".
- If the figure is a linear checklist with no branch points, return
  {"shape": "checklist", "items": [...]} instead of nodes and edges. Such a
  figure cannot be transcribed as a tree and needs archetype instantiation
  by a human."""


def extract_figures(doc, figures, dpi=200):
    out = []
    for f in figures:
        pix = doc[f["page"] - 1].get_pixmap(dpi=dpi)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        try:
            g = ask_json(FIG_SYS,
                         f"Figure {f['n']}: {f['caption']}\n\nTranscribe it.",
                         images=[b64], max_tokens=8000)
        except Exception as e:                               # noqa: BLE001
            print(f"  ! figure {f['n']}: {e}", file=sys.stderr)
            continue
        g.update(figure=f["n"], caption=f["caption"], page=f["page"])
        out.append(g)
    return out


def figure_agreement(figs, recs):
    """Cross-modal check: numbers drawn in figure guards that appear in no
    recommendation text. The cheap version of the check that caught a figure
    contradicting its own guideline's recommendation table."""
    fig_nums = Counter()
    for g in figs:
        for e in g.get("edges", []):
            for n in re.findall(r"\d+\.?\d*", str(e.get("guard", ""))):
                fig_nums[n] += 1
    rec_text = " ".join(r.get("text", "") or "" for r in recs)
    return sorted(n for n in fig_nums if n not in rec_text and len(n) >= 2)


# ======================================= Stage 2: exposed-driver dictionary
DRV_SYS = """You build a data dictionary from extracted recommendation conditions.

Cluster every operand into canonical drivers. For each driver return:
  name         camelCase identifier
  type         boolean | enum | quantity | code
  unit         for quantities
  domain       for enums
  label        the human label a clinician would use
  section      the guideline section where the concept is defined
  derived      true if computed from other drivers, else false
  inputs       for derived drivers, the driver names it consumes
  definition   one line; state thresholds ONLY for derived drivers
  risk_model   {name, validated_range} if this is a risk score

HARD RULES
- A boolean driver's definition must never contain a numeric comparison.
  "office BP >= 130 while on >= 3 agents" is a derived enum, not a boolean.
  Hidden compound predicates cannot be boundary-tested and are rejected
  downstream.
- Two clauses testing the same concept at different thresholds are ONE driver
  with a derived classifier, not two separate booleans.
- Any driver consuming a risk score must carry risk_model with the score's
  validated range, so an age or eligibility gate can be enforced above it."""


def build_dictionary(recs):
    payload = [{"rec": r.get("id"),
                "conditions": r.get("conditions", []),
                "population": r.get("population", [])} for r in recs]
    d = ask_json(DRV_SYS, json.dumps(payload)[:60000], max_tokens=10000)
    if isinstance(d, dict):
        d = d.get("drivers", d)
    return d if isinstance(d, list) else []


# =========================================== house conventions from siblings
def learn_conventions(repo, condition):
    """Read sibling condition folders for naming conventions.

    The guideline cannot tell you this. Conventions differ per condition -
    one folder may use Exclusion-*, another {Condition}-NoGuidance-* - so this
    reports what it finds and leaves the choice to a human."""
    fallback = {
        "id_regex": r"^(Rec-[A-Za-z0-9\-_]+|"
                    + condition + r"-NoGuidance-[A-Za-z0-9]+)$",
        "noguidance": condition + "-NoGuidance-{reason}",
        "_learned_from": [],
        "_note": "no repo supplied - this is a guess, replace it",
    }
    if not repo:
        return fallback
    p = Path(repo).expanduser()
    if not p.exists():
        print(f"  ! repo not found: {p}", file=sys.stderr)
        return fallback

    ids, folders = [], []
    for md in p.rglob("*Decision Tree*.md"):
        folders.append(md.parent.name)
        try:
            txt = md.read_text(errors="ignore")
        except OSError:
            continue
        ids += re.findall(
            r"\b((?:Rec|Exclusion)[-A-Za-z0-9_]{3,}"
            r"|[A-Z][A-Za-z]+-NoGuidance-[A-Za-z0-9]+)\b", txt)
    return {
        "observed_ids": sorted(set(ids))[:60],
        "_learned_from": sorted(set(folders)),
        "_note": "REVIEW: choose the convention this condition follows, then "
                 "write id_regex and noguidance by hand. Conventions differ "
                 "across folders - do not assume one is canonical.",
    }


# =========================================================== profile assembly
def assemble(condition, source, recs, drivers, figs, conv, conflicts):
    grades = sorted({r.get("grade") for r in recs if r.get("grade")})
    return {
        "forest_id": f"{condition.upper()}-DRAFT-v1",
        "condition": condition,
        "source": source,

        "evidence_grading": {
            "name": "REVIEW: ACC/AHA COR+LOE, KDIGO strength+certainty, ADA?",
            "strength": grades,
        },

        "naming_profile": conv,

        "_TODO_scope_declaration": {
            "_instructions":
                "PRODUCT DECISION - not extractable from the guideline. List "
                "every patient group this recommender refuses, each with a "
                "reason, plus every handoff to another recommender. This is "
                "the single largest lever on ground-truth fidelity: an "
                "undeclared scope gets filled in by whatever the model finds "
                "in the guideline, and coverage metrics have no denominator "
                "without it.",
            "_example": {
                "AdultsOnly": "paediatric cases are out of scope",
                "Pregnancy": "separate pathway",
                "MissingData": "a required exposed driver was not supplied",
            },
        },
        "_TODO_handoffs": [],

        "drivers": drivers,

        "_TODO_scope_gates": [],
        "_TODO_driver_spine": [d["name"] for d in drivers
                               if not d.get("derived")][:8],
        "_TODO_treatment_split": {},
        "_TODO_modules": [{"candidate_id": f"Fig{g['figure']}",
                           "caption": g.get("caption"),
                           "shape": g.get("shape", "graph")}
                          for g in figs],
        "_TODO_classifiers": [
            {"driver": d["name"], "inputs": d.get("inputs", []),
             "note": "write one function + probes at every boundary +/-1"}
            for d in drivers if d.get("derived")],

        "recommendations": [
            {"id": r["id"],
             "grade": r.get("grade"),
             "polarity": r.get("polarity", "recommended"),
             "section": r.get("section"),
             "section_verified": False,
             "text": r.get("text"),
             "cadence": r.get("cadence"),
             "_page": r.get("page")}
            for r in recs],

        "_conflicts": conflicts,

        "_constraints_candidates": [
            {"id": f"CONSTRAINT-{r['id']}",
             "grade": r.get("grade"),
             "section": r.get("section"),
             "rule": r.get("text"),
             "why": "prohibition or global rule - applies on every path, so it "
                    "cannot be a branch. Move to the constraints table."}
            for r in recs if r.get("polarity") in ("harm", "no_benefit")],
    }


# ==================================================================== driver
def main():
    ap = argparse.ArgumentParser(
        description="Guideline PDF -> draft L2 condition profile")
    ap.add_argument("pdf")
    ap.add_argument("--condition", required=True,
                    help="condition name, e.g. Hypertension")
    ap.add_argument("--out", default="draft-profile.yaml")
    ap.add_argument("--repo",
                    help="path to cds-med-recommenders-clinical-l2, for "
                         "learning house naming conventions")
    ap.add_argument("--skip-figures", action="store_true",
                    help="skip the vision pass (faster, cheaper)")
    ap.add_argument("--dpi", type=int, default=200)
    a = ap.parse_args()

    doc = fitz.open(a.pdf)
    print(f"[0] {len(doc)} pages")

    figures, sections = page_inventory(doc)
    core, aux = classify_pages(doc)
    print(f"[0] {len(figures)} figures | {len(sections)} sections | "
          f"{len(core)} core / {len(aux)} auxiliary pages")

    print("[1a] recommendation tables ...")
    recs = extract_recommendations(doc, core)
    print(f"     {len(recs)} recommendations | grades: "
          f"{dict(Counter(r.get('grade') for r in recs))}")

    figs = []
    if not a.skip_figures:
        print(f"[1b] transcribing {len(figures)} figures ...")
        figs = extract_figures(doc, figures, dpi=a.dpi)
        checklists = [g["figure"] for g in figs if g.get("shape") == "checklist"]
        if checklists:
            print(f"     figures {checklists} are checklists, not trees - "
                  f"they need archetype instantiation by a human")

    conflicts = []
    orphans = figure_agreement(figs, recs) if figs else []
    if orphans:
        conflicts.append({
            "type": "figure_number_absent_from_recommendation_text",
            "values": orphans,
            "action": "precedence rule: recommendation table > figure > prose. "
                      "Verify each value against the source before using it."})
        print(f"     {len(orphans)} figure number(s) not found in any "
              f"recommendation text - logged to _conflicts")

    print("[2] driver dictionary ...")
    drivers = build_dictionary(recs)
    derived = sum(1 for d in drivers if d.get("derived"))
    print(f"    {len(drivers)} drivers ({derived} derived)")

    conv = learn_conventions(a.repo, a.condition)
    if conv.get("_learned_from"):
        print(f"    naming learned from: {', '.join(conv['_learned_from'])}")

    source = {"title": doc.metadata.get("title") or Path(a.pdf).stem,
              "citation": "REVIEW", "file": Path(a.pdf).name}
    prof = assemble(a.condition, source, recs, drivers, figs, conv, conflicts)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(prof, sort_keys=False, width=100,
                                  allow_unicode=True))
    out.with_suffix(".figures.json").write_text(json.dumps(figs, indent=2))

    print(f"\nwrote {out}")
    print(f"wrote {out.with_suffix('.figures.json')}")
    print("\nBEFORE running l2gen, a human must:")
    print("  1. fill _TODO_scope_declaration - every refusal, with a reason")
    print("  2. write naming_profile.id_regex from the observed_ids list")
    print(f"  3. write {derived} classifier function(s) with +/-1 boundary probes")
    print("  4. set section_verified: true only after checking source text")
    print(f"  5. resolve {len(conflicts)} conflict(s) and "
          f"{len(prof['_constraints_candidates'])} constraint candidate(s)")
    print("  6. shape the forest: scope_gates, driver_spine, "
          "treatment_split, modules")


if __name__ == "__main__":
    main()
