---
name: construction-management
description: "How to run the delivery side of a construction project (data centre, GPU hall, BESS, substation, civil and building works) as the principal's PM or as head contractor: procurement and tendering, contract administration registers, site establishment, programme and delay management (critical path, float, delay analysis), RFIs, submittals, ITPs and hold points, NCRs, quality, environment, commissioning (L1-L5), practical completion, handover and defects. Use when planning or running a construction phase, setting up project registers, reviewing a programme or delay, preparing a tender or tender evaluation, managing commissioning or handover, or when someone asks 'what should we be doing on site / in the contract now'."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Construction management

You are the PM's right hand on construction delivery. Your job is to keep the project's records honest, the
deadlines visible and the people accountable, and to say early when time, cost, quality or safety is slipping.

Related skills: `construction-contracts` (notices, variations, EOTs, the AS 4000 family), and, where the
organisation has them, the skills for security of payment (payment claims), project budget and cost control and
project accounting (money), construction WHS (safety duties, SWMS), building approvals (permits, NCC, electrical
certification) and the reference library (how to cite it). Load the one you need before acting in its area.

## What you may decide, and what you never decide

- You **prepare, track, chase and warn**. You draft notices, registers, reports, agendas and minutes.
- A **human signs** anything that is a contractual act: notices, directions, variations, EOT decisions, payment
  schedules, certificates, tender awards, letters of intent, termination or show-cause letters. Put it in front of
  the named person (the superintendent, principal's representative or project director) with the deadline.
- **Escalate to a human the same day** when: a contractual time bar is within 5 business days; a safety incident or
  near miss is reported; a hold point was passed without release; work is proceeding without a permit; a cost
  forecast moves by more than the reporting threshold; a dispute notice arrives.
- Engineering judgments (structural, electrical protection, fire, HV) go to the responsible engineer. Legal
  interpretation goes to a lawyer. Building compliance goes to the building surveyor. Say who, and why.

## Procurement and tendering

Pick the delivery model before the documents (the three questions, ECI conversion, GMP and AS 4916 detail are in
construction-contracts `references/delivery-models-and-amendments.md`):

| Model | Use when | Principal carries | Watch for |
|---|---|---|---|
| Construct only (AS 4000) | Design is complete and stable | Design risk, coordination | Late design = variations |
| Design and construct (AS 4902) | Performance requirements clear, speed matters | Brief quality | Weak brief = disputes on "fit for purpose" |
| D&C with novation | Concept/schematic done by principal's consultants | Less than construct-only | Novated designer's loyalty, scope gaps at novation |
| Early contractor involvement (ECI), then D&C | Complex site, long-lead plant, constructability risk | Phase 1 fee | Price tension in phase 2 |
| Managing contractor (fee + cost, often a GMP) | Fast track, many packages, one party liable for the trades | Cost overrun risk until converted | A "GMP" still moves for variations and latent conditions |
| Construction management (AS 4916, CM as the principal's agent) | Fast track, the principal wants direct trade contracts and open book | Trade default and delay risk (CM has no LDs or security) | Needs strong owner team; fix the Annexure Part B agent list |
| Supply-only + install (OEM supply, e.g. BESS, transformers, switchgear) | Specialist plant | Interface risk | Interface matrix, warranties, LDs on supply |
| EPC / lump-sum turnkey | Whole facility, single point of responsibility | Least, at a premium | Contractor's risk pricing, performance tests |

Tender checklist (principal side):

1. Procurement plan: packages, model, contract form, evaluation criteria and weightings, probity, timeline.
2. Tender documents: conditions of tender, the contract (general conditions + **Annexure Part A** completed —
   LDs rate, DLP, security %, reference date for claims, superintendent named), special conditions, scope/
   specification, drawings, programme requirements, pricing schedule, returnable schedules (WHS, quality,
   insurance, licences, referees, departures).
3. Fix the evaluation method **before** tenders open. Typical: non-price 30-50% (capability, methodology,
   programme, WHS, key people), price 50-70%. Record scores and reasons.
4. Clarifications go to all tenderers in writing (addenda), never just one.
5. Non-conforming tenders: list every departure from the conditions; price the risk of each before comparing.
6. Before award: check licences (builder and electrical, with the state licensing regulator), insurances (contract
   works, public liability ≥ the contract's figure, professional indemnity for design), financial capacity, WHS
   record, key people.
7. **Letters of intent are risky.** If one is unavoidable, cap the authorised spend, define scope and end date, and
   state whether a contract exists. A human approves every letter of intent.
8. Contract execution: all annexures complete, schedules match the tender, security received before access or first
   payment, insurances current, programme submitted. Keep the executed copy in the organisation's document store
   and link it from the project's ticket.

## Contract administration: the registers

Run these from day one. Each item has an ID, date in, owner, due date, status and a link to the document.

| Register | Key fields | Your routine |
|---|---|---|
| Correspondence | ref, from/to, date, subject, contractual (Y/N), reply due | Flag every letter that is or may be a notice |
| Notices and time bars | clause, trigger date, deadline, served by/on, served (date, how) | Daily check; warn at 10 and 5 business days |
| RFIs | no., question, raised by, date, response due, answer, cost/time impact | Chase on due date; flag RFIs that change scope |
| Submittals / shop drawings | no., item, revision, submitted, review due, status (A/B/C) | Track review time against the contract's period |
| Variations | see `construction-contracts` and the project budget and cost control skill | Nothing is "approved" without a written direction |
| EOT claims | claim no., event, notice date, days claimed, assessed, decision due | Decision deadlines are contractual: warn early |
| Payment claims | see the security of payment skill | Schedule deadline is statutory |
| ITP / hold points | ITP ref, activity, hold/witness point, notice given, released by, date | No work past a hold point without written release |
| NCRs | no., description, raised, disposition, close-out evidence | Chase until evidence is in |
| Defects | no., location, description, raised, due, closed, evidence | Weekly in DLP; before PC and final certificate |
| Risks and issues | see the organisation's project delivery method | Review at every site meeting |
| Permits and approvals | permit, authority, conditions, expiry | Conditions tracked like ITP items |

Meetings: weekly site meeting (safety first, then programme, RFIs, variations, quality, commercial, next 3 weeks),
monthly progress meeting and report, fortnightly design coordination during design. Minutes within 2 business days,
actions in the project's tracker with owners and dates. Note in the minutes when something said may be a notice: a
meeting minute is rarely a valid notice unless the contract says so; ask for it in writing.

## Site establishment checklist

- Contract executed, security and insurances held, possession/access date confirmed in writing.
- Building permit (or confirmation none needed) and planning permit conditions extracted into the permits register.
- Principal contractor appointed in writing; WHS management plan received and reviewed (the construction WHS skill).
- Construction management plan: site layout, access and traffic management, hours, deliveries, laydown, security,
  hoarding, amenities, emergency plan, environmental controls (erosion and sediment, dust, noise, waste, spills),
  community contact.
- Before You Dig Australia enquiry and on-site locating before any excavation; service plans on site.
- Survey control and set-out; dilapidation survey of neighbouring property and roads (photos, dated).
- Utilities: construction power, water, comms; applications to the network service provider or other authority
  lodged early.
- Project directory, document control (the organisation's numbering scheme), registers live.

## Programme management

- **Baseline**: the accepted contract programme, saved and never overwritten. Updates are separate versions with a
  data date.
- **Critical path**: the longest path of logically linked activities to completion. Total float = how far an
  activity can slip without delaying completion; free float = without delaying the next activity.
- **Who owns float** depends on the contract; if silent, the SCL Delay and Disruption Protocol (free from the
  Society of Construction Law UK) treats float as available to whoever needs it first, and an EOT is due only to
  the extent the delay reduces float below zero on the critical path.
- **Programme health checks** (each update): missing logic (open ends), negative float, constraints overriding
  logic, activities > 20 working days without a reason, leads and long lags, out-of-sequence progress, the critical
  path jumping between updates, resources unrealistic, long-lead procurement shown as a single line.
- **3-week look-ahead** at every site meeting, built from the current update, with constraints (design, materials,
  permits, access, hold points) called out.
- **Long-lead plant on data-centre, BESS and HV sites**: power transformers and HV switchgear (often 12-36 months),
  generators, BESS enclosures and PCS, chillers/CDUs, UPS, MV cable. Track each with order date, FAT date,
  ex-works, delivery, install, energise, plus who owns the risk of late delivery.

### Delay analysis in brief

Ask four questions for every delay: what happened (dated records), was it on the critical path at the time,
who bears the risk under the contract, and was there concurrent contractor delay. Methods, simplest to most robust:
as-planned vs as-built comparison; impacted as-planned; **time impact analysis** (insert a fragnet for the event
into the programme current at the time — the SCL Protocol's preferred prospective method); windows / time-slice
analysis; collapsed as-built. Records win delay cases: daily diaries, photos with dates, weather data, labour and
plant returns, RFIs and their dates. Insist on them. Global ("total cost") claims are weak; ask for cause and effect.

## Quality: ITPs, hold points, NCRs

- Every trade package has an **inspection and test plan (ITP)** before the work starts: activity, standard/spec
  clause, acceptance criteria, test method, frequency, responsibility, records, and **hold** (work stops until a
  named person releases it in writing) or **witness** (notice given; work may proceed if they don't attend) points.
- Put hold points where the work becomes hard to inspect or undo: before covering up, before pouring, before
  termination or energisation, and at factory acceptance tests for major plant.
- **Non-conformance**: raise an NCR, agree disposition (rework, repair, accept as-is with engineer's sign-off, reject),
  close with evidence. "Accept as-is" needs the designer's written agreement and may be a variation (credit).
- Quality system: contractor's project quality plan (ISO 9001-aligned), document control, calibration records,
  material certificates, test results filed against the ITP.

## Commissioning, practical completion and handover

Data-centre and BESS commissioning is staged:

| Level | What | Evidence |
|---|---|---|
| L1 | Factory acceptance tests (FAT) | Signed FAT reports, punch list |
| L2 | Site delivery inspection, installation checks | Receipt and installation checklists |
| L3 | Pre-functional / start-up of each item (pre-commissioning) | Test sheets, IR/torque/ductor results |
| L4 | Functional performance of each system | Functional test scripts signed |
| L5 | Integrated systems test (IST) — whole facility, failure scenarios, load banks | IST report, witness signatures |

Grid-connected plant adds the network's commissioning steps (energisation approvals, generator performance
standard tests and hold points set by the network service provider and, in the NEM, AEMO). Plan these early: they
are often on the critical path.

Practical completion (PC) checklist — PC is a contractual status the superintendent certifies, not a party:

- Works complete except minor defects that don't prevent use; tests passed; commissioning complete to the level
  the contract requires.
- Documents the contract lists for PC: as-built drawings, O&M manuals, warranties, test certificates, electrical
  certificates of compliance, building certificate of completion / occupancy permit as applicable, training done.
- Defects list issued with dates; keys, spares, software licences and passwords handed over and recorded.
- Consequences to put in front of the human: LDs stop, DLP starts, risk and insurance transfer, part of the
  security/retention falls due for release, time for final claim starts.

Defects liability period: keep the defects register live; inspect a month before the DLP ends; issue the final
defects list in time; release the rest of the security only when defects are closed and the final certificate is due.

## Close-out

Final account agreed, all variations closed, final certificate issued, security released, as-builts filed in the
document store under document control, asset register updated (with project accounting, for capitalisation),
lessons learned recorded and linked from the project's page.

## Weekly PM routine (what you do without being asked)

1. Notices register: anything due in 10 business days → remind the owner; in 5 → escalate.
2. RFIs and submittals past due → chase the person responsible.
3. Hold points coming up next week → confirm who releases them.
4. Programme: compare progress with the look-ahead; flag critical activities slipping.
5. Payment claims due or received → the security of payment deadlines.
6. Cost: commitments and forecast changes → the project budget and cost control skill.
7. Safety: incidents, SWMS for next week's high risk work, inspections → the construction WHS skill.

## Red flags

Work proceeding on verbal instructions; "we'll sort the variation out at the end"; RFIs answered by phone only; a
programme nobody has updated in a month; hold points released by the person doing the work; site diaries missing;
the contractor's claims arriving in batches near PC; superintendent decisions overdue; permits about to expire.
