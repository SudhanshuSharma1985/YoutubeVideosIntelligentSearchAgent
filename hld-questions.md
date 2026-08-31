# Ten Questions on the Hyperlipidemia Ground Truth

Anchored to what is actually drawn in the `hld-ada` decision trees, read alongside the `htn-acc/2025` trees. Where the two golden sets disagree, that disagreement is itself the question — because a convention frozen from one and applied to the other is what took a hypertension run from 100% to 53% leaf recall.

**Caveat:** I read the HLD trees from phone photos of a screen. Correct me on anything I have misread.

---

## Where the two golden sets diverge

| | Hyperlipidemia | Hypertension |
|---|---|---|
| Refusal naming | `Exclusion-NoLipidPanel` | `Hypertension-NoGuidance-Pregnancy` |
| Rec ID separator | `Rec-Primary-AgeGT80` — all hyphens | `Rec-Init-Stage1_CVD` — underscore before qualifier |
| Dispatch axis | clinical pathway (severe / secondary / diabetes / subclinical / primary) | treatment state (on therapy vs not) |
| Therapy state | `statinIntensity` tested early, interleaved with clinical dispatch | clean split into Initial and Adjusting modules |
| Driver spine | none visible | `Has CKD (5.3.8) → Has CVD (5.2.1) → …` |
| Recommendation leaves | flow onward into `Titration Algorithm` | terminal |
| Shared module | yes — Titration, entered from every pathway | none |

If HLD predates HTN, several of these may be deliberate improvements rather than drift — in which case HLD should be brought forward, not copied.

---

## 1. What do the green diamond and cyan rectangle actually mean?

Across both golden sets, *derived vs raw* now fits nearly everything. The whole Titration Algorithm is green (computed LDL comparisons). Fig 6 and Fig 13 open with a large green diamond — a computed risk or plaque-burden category — with cyan below. Fig 7 is entirely cyan: `hasCHDorLDLASCVD`, `age`, `hasHFH` are raw drivers. The base tree is cyan throughout. In HTN, `Already on antihypertensive`, `Has Hypertensive`, `Stage 1 or 2` and `Number of Pharm Classes` are all green and all derived.

**One exception breaks it:** `Is Blood Pressure At Goal?` is cyan in HTN and clearly derived.

**Ask:** is derived-vs-raw the rule, and is that node an authoring slip?

**Why it matters:** this goes straight into the frozen node grammar. The generator currently uses derived-vs-raw because it is the only rule I could state precisely.

---

## 2. Is there a null policy, or is it per-driver judgement?

HLD handles missing data three different ways in one forest:

- `lipidPanel IS null` → `Exclusion-NoLipidPanel` — a refusal
- `statinIntensity: null` → routes to `ableToUseStatin` — a different driver
- `apoB: null` vs non-null in Figs 11–12 → **two different recommendations**

HTN has no equivalent missing-BP exclusion at all.

**Ask:** is there a written policy for missing drivers, or is each one decided as it is drawn? And is the absence of a missing-BP exclusion in HTN deliberate or just not drawn?

**Why it matters:** this is the gap where I had to invent a node (`NoGuidance-MissingData`) that exists in neither golden set. Nullability is a hard lint in the compiler; I need to know what it should enforce.

---

## 3. Why is age 80+ in scope but age under 30 out?

HLD treats `Rec-Primary-AgeGT80` as a **recommendation** (yellow) but age under 30 as an **exclusion** (orange). Same axis, opposite answers.

**Ask:** what made 80-plus worth advising on and under-30 a refusal?

**Why it matters:** that reasoning *is* the scope policy. It is the thing I most need written down, and it is the single largest lever on ground-truth fidelity.

---

## 4. Are recommendation nodes terminal, or do they chain?

This is the biggest structural question of the ten.

In HLD the yellow `Rec-*` boxes in Figs 7, 11–12 and 13 all flow onward into the blue `Titration Algorithm`. In HTN, recommendations terminate.

**Ask:** does a patient come out with *one* recommendation ID or a *sequence* of them?

**Why it matters:** it changes what a Recommendation Definitions row means, and whether "one recommendation per path" is even the right invariant. Both the hypertension and CKD forests I built assume one per path. If HLD is right, that assumption is wrong and both need rework.

---

## 5. Where do cross-cutting constraints live?

HLD has obvious candidates that cannot be branches: statin contraindication in pregnancy, maximum-tolerated-dose rules, and whatever Class 3 recommendations the source guideline carries.

**Ask:** which table holds these today?

**Why it matters:** in HTN I found three such rules — no triple RAS blockade (Class 3: Harm), the BP goal, and white-coat exclusion — with no visible home. A Class 3: Harm rule that is not in a table has silently vanished from the artifact. If the answer here is "nowhere," that is a portfolio-wide gap, not an HLD one.

---

## 6. What coding level do Med Orders rows carry?

HLD's therapy ladder is richer than HTN's: statin intensity tiers, ezetimibe, bempedoic acid, PCSK9 inhibitors, inclisiran, each with eligibility rules attached.

**Ask:** Medispan codes (there is a `medispan-integration` folder), RxNorm, or class-level identifiers? And is "high-intensity statin" a coded concept or free text?

**Why it matters:** it decides whether generated Med Orders rows are directly usable or need a mapping pass, and it is one of the referential-integrity checks.

---

## 7. Does `age` appear twice in the base tree as one node or two?

The HLD base tree tests age as a gate (`age >= 30`) and again as a band (`30-79` / `>=80` / `<30`).

**Ask:** is that the same driver reused, or two deliberately distinct nodes?

**Why it matters:** this is exactly the must-not-merge case. In HTN, `Number of Pharm Classes` appears twice with different thresholds — 3+ when not at goal, 4+ when at goal — and merging them would delete the definition of resistant hypertension. Confirming HLD does the same thing validates a compiler rule that currently rests on one example.

---

## 8. Does the Titration Algorithm loop?

The chain I can see runs linearly down to `Rec-LDL-*` leaves with no visible loop-back. HTN's ground truth also has no loop, even though the guideline figure draws one.

**Ask:** confirm that reassessment cadence is hook-driven and the tree is stateless per encounter — and which table owns the interval (Worklists? MedTimeline?).

**Why it matters:** if it holds across both conditions I can freeze statelessness as an invariant and drop the acyclicity exception entirely.

---

## 9. What decides absorb versus delegate?

HLD handles `Fig 9: Diabetes without ASCVD` **inside** its own forest. HTN hands HFrEF **off** to `chf-hfref-aha-acc-hfsa` via `Rec-Hypertension-GoTo-HF-MedRecommender`.

**Ask:** what is the rule? Diabetes is arguably as much "another condition's territory" as heart failure is. Is there a canonical list of handoff targets across the portfolio?

**Why it matters:** it determines whether a comorbidity becomes a subtree or a one-line routing recommendation, which changes the size of the forest substantially.

---

## 10. Which guideline is the HLD tree actually built on?

The folder is named `hld-ada`, but the source I have been working from is the 2026 ACC/AHA dyslipidemia guideline.

**Ask:** ADA Standards of Care, ACC/AHA, or both?

**Why it matters:** this is a one-word answer that decides whether anything I have said about HLD is anchored to the right document. Worth asking first.

---

## If the meeting is short

**10 first** — one word, and it tells you whether the other nine are even about the right guideline.

Then **1, 2 and 4.** Colour semantics and null handling go into the frozen grammar. Whether recommendations chain or terminate changes the core data model and would send two already-built forests back for rework.

**3, 5, 6, 7, 8, 9** are all cheap to answer once someone who drew these trees is in the room, and each removes an assumption I am currently making on their behalf.
