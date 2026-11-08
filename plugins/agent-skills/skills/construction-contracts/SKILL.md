---
name: construction-contracts
description: "Administering Australian standard-form construction contracts: the AS 4000 family (AS 4000:2025 and the superseded but still common AS 4000-1997, AS 4902 design and construct, AS 4901/4903 subcontracts, AS 4905/4906 minor works, AS 2124/4300 legacy, AS 4910 equipment supply), ABIC, state governments' amended AS suites, AS 4916/4917 construction management, ECI, managing contractor and GMP arrangements, NEC4 and FIDIC; choosing a delivery model and amending a standard form (special conditions, rise and fall, provisional sums); and the machinery a PM must run: notices and time bars, variations, extensions of time and delay costs, latent conditions, practical completion, liquidated damages, defects liability, security and retention, payment, show cause and termination, disputes, the superintendent's role, unfair contract terms. Use whenever a contract notice, claim, variation, EOT, delay, latent condition, practical completion, defect, LD, security, show cause or dispute comes up, or when choosing or reviewing a contract form."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Construction contracts

**First rule: the project's own contract wins.** Every standard form is amended by special conditions, and the
Annexure (Part A in the AS forms) sets the numbers: LD rate, defects period, security, reference dates, time periods,
who the superintendent is. Before you say what a clause means, find the executed contract in the organisation's
document store and read the actual clause and the special conditions. If you can't find them, say you're relying on
the unamended standard form and that it may differ.

**Second rule: know the edition.** AS 4000:2025 (published 27 June 2025) replaced AS 4000-1997, but most contracts
signed before mid-2025, and many since, use the 1997 edition; clause numbers changed in places. Always cite
"AS 4000-1997 cl 34.3" or "AS 4000:2025 cl …".

**Third rule: you don't hold the standards' text.** Standards Australia's terms forbid loading standards into an AI
system. What follows is a synthesis of public commentary; the exact words are in the purchased copy or the executed
contract. Contractual decisions go to a human; interpretation in dispute goes to the organisation's lawyer.

Detailed clause map for AS 4000-1997 and the 2025 changes: `references/as4000-clause-map.md`. NEC4, FIDIC,
equipment supply, EPC and LTSAs: `references/nec-fidic-oem.md`. Choosing a delivery model (three questions), ECI,
GMP, AS 4916 construction management, provisional sums, rise and fall clauses and the usual amendments to AS 4000
and AS 2124: `references/delivery-models-and-amendments.md`. (Load with `skills__load`, name
`construction-contracts`, `reference` the file name.)

## Which form, when

| Form | Use | Note |
|---|---|---|
| AS 4000:2025 / AS 4000-1997 | Construct only; principal designs; superintendent administers | Most common Australian head contract |
| AS 4902-2000 | Design and construct (AS 4000 family) | Fitness for **stated** purpose warranty; design consultants novated via the Annexure deed |
| AS 4901-1998 / AS 4903-2000 | Subcontracts under AS 4000 / AS 4902 | Use when the organisation is head contractor |
| AS 4905-2002 / AS 4906-2002 | Minor works: **4905 superintendent-administered, 4906 principal-administered** | State governments often use an amended version for small works |
| AS 4910-2002 | Supply of equipment with installation | Transformers, switchgear, BESS supply-and-install |
| AS 4916-2002 + AS 4917-2003 | Construction management: the CM is the principal's **agent**; trade contracts are with the principal | CM carries little risk (no LDs, no security, not liable for trades); fix the Annexure Part B "agent / not as agent" list |
| ECI agreement (bespoke) | Builder engaged before design is complete; fee, open-book trade tenders, then conversion to a lump sum | No standard form; define conversion and what happens if it fails |
| Managing contractor / GMP | Head contractor engages and is liable for the trades; fee + cost or converted lump sum | No standard form (amended AS 4000/4902 + ECI agreement); a GMP still moves for variations and latent conditions |
| AS 2124-1992, AS 4300-1995 | Legacy construct-only and D&C | Still met; numbering differs (EOT cl 35.5, variations cl 40, payment cl 42) |
| ABIC MW-2018 / SW-2018 | Architect-administered building work | Master Builders + Australian Institute of Architects |
| NEC4 ECC | Collaborative, programme-driven; options A-F | 8-week compensation event time bar (cl 61.3) |
| FIDIC 2017 Red / Yellow / Silver | International, OEM and EPC; Silver = turnkey EPC with no Engineer | 28-day claim notice time bar (cl 20.2.1) |

**Construction management is not managing contractor**: under AS 4916 the principal holds the trade contracts and
their default risk; a managing contractor holds them itself. Say which one before discussing risk.

AS 11000 never existed as a standard: it was a 2015 draft that was abandoned. GC21 is NSW's form; other state
governments use their own amended AS 4000-series suites, with their own procurement thresholds.

## The superintendent

Under AS 4000 the principal must ensure there is always a superintendent, who acts in two roles: as the principal's
agent (giving directions) and as an independent certifier and assessor (EOTs, payment, variations' value, practical
completion), where it must act **reasonably and in good faith** (AS 4000-1997 cl 20; AS 2124 says "honestly and
fairly"). The principal must not pressure the superintendent's certifying decisions. When the principal's PM is also
the superintendent, keep the two roles visibly separate: decisions as assessor are reasoned in writing on the
evidence, not on the principal's commercial preference. Directions may be oral but must be confirmed in writing.

## Notices and time bars (the thing that loses most claims)

AS 4000-1997, unamended, as commonly read (check the contract):

| Event | Notice | Clause |
|---|---|---|
| Anything that will probably delay work | Prompt written notice to **both** superintendent and principal, cause and estimated delay | 34.2 |
| EOT claim | Written claim **within 28 days** after the contractor should reasonably have become aware of the delay | 34.3 |
| Superintendent's EOT decision | **Within 28 days** of the claim, or the claim is **deemed granted** (often deleted by special conditions) | 34.5 |
| Latent condition | Promptly, before disturbing it where possible; costs incurred more than 28 days before notice are excluded | 25.2-25.3 |
| Other claims (breach, tort, statute) | "Prescribed notice" within **28 days** of becoming aware; particulars within 28 days of that | 41.1, 41.3 |
| Practical completion | Contractor gives at least 14 days' notice of anticipated PC; superintendent certifies or gives reasons within 14 days | 34.6 |
| Progress claim → certificate | Certificate within 14 days of the claim (or the claim is deemed the certificate); payment within 7 days of certificate and tax invoice | 37.1-37.2 |
| Final payment claim | Within 28 days after the defects liability period; final certificate within 14 days of it | 37.4 |
| Dispute | Notice of dispute; conference within 14 days; arbitration/litigation if unresolved 28 days after the notice | 42 (1997) |
| Show cause (contractor default) | Notice stating the breach, with at least 7 clear days to show cause | 39.3 |

Notes: in unamended AS 4000-1997 a missed cl 41.1 notice doesn't bar the claim (it gives the other side damages,
cl 41.2) — but principals very often amend it into a hard bar, and cl 34.3 is drafted as a condition precedent.
Treat every time bar as hard unless a lawyer says otherwise.

Courts generally enforce time bars (Peninsula Balmain v Abigroup [2002] NSWCA 211). But where the superintendent
has a power to extend time unilaterally (AS 4000-1997 cl 34.5), it must be exercised honestly, fairly and reasonably,
and a principal who caused the delay may not be able to keep LDs for it (the prevention principle; Probuild v DDI
Group [2017] NSWCA 151; Gaymark v Walter [1999] NTSC 143). Never rely on the unilateral power as a substitute for a
notice.

**Your routine**: screen every incoming letter, email and minute for notices; log each in the notices register with
the clause, trigger date and deadline; remind at 10 and 5 business days; when the organisation is the contractor,
draft a protective notice the same week any delay, variation or latent condition event happens. Serve notices
exactly as the contract's notices clause says (address, method; email only if the contract allows it).

## Variations

- No variation without a **written direction** from the superintendent (AS 4000-1997 cl 36.1); oral directions are
  confirmed in writing. AS 4000:2025: effective only if directed in writing before practical completion.
- The superintendent can first ask for a price and time proposal (cl 36.2). Variations for the contractor's
  convenience carry no extra time or money unless the direction says so (cl 36.3).
- Valuation hierarchy (cl 36.4): agreed price → contract rates → rates in a priced bill or schedule (as far as
  reasonable) → reasonable rates. Deductions include reasonable profit but not overheads.
- RFIs answered in a way that changes scope, and design development that changes the work, are variations: put them
  in the register.
- Before directing: estimate, budget source and delegation approval (the project budget and cost control skill).

## Extensions of time and delay costs

- EOT requires: a **qualifying cause** (as listed — typically principal's acts, variations, latent conditions,
  inclement weather beyond the Annexure allowance, industrial action as defined), actual delay to **practical
  completion** (critical path), and a valid, timely claim.
- **Concurrent delay**: AS 4000-1997 cl 34.4 lets the superintendent **apportion**; AS 2124 cl 35.5 gives no EOT for
  the concurrent period. Australian law is unsettled; the contract governs.
- **Delay damages** (cl 34.9) are paid per day of EOT only for **compensable causes** listed in the Annexure; claimed
  through cl 41. AS 4000:2025 defines the measure more tightly.
- Assess with records: programme current at the time, daily diaries, the delay event's dates, mitigation. See
  `construction-management` for delay analysis.

## Latent conditions

A physical condition on or near the site differing materially from what a competent contractor should reasonably
have anticipated from the information provided, reasonable enquiries and inspection (weather excluded) (cl 25.1).
Ask: what site information was given at tender; what a competent contractor would have expected; whether notice was
prompt. Valid claims are valued as variations (cl 25.3). Geotechnical reports and their reliance/disclaimer terms
decide most of these; involve the geotechnical engineer.

## Practical completion, LDs and defects

- **Practical completion**: the contract's definition (works complete except minor defects not preventing use;
  tests passed; documents handed over; under AS 4000:2025, legislative requirements satisfied). Under the 1997
  edition PC is generally the date the superintendent certifies (Abergeldie v Fairfield CC [2017] NSWCA 113); under
  2025 it can occur before the certificate.
- PC triggers: LDs stop, the defects liability period starts (4 pm on the date of PC), security reduces, risk and
  insurance pass, and the final claim clock starts after the DLP.
- **Liquidated damages** (cl 34.7): under AS 4000 the superintendent **certifies** LDs at the Annexure rate per day
  from the date for PC (adjusted by EOTs) to PC; repaid if a later EOT is granted. LDs are enforceable unless out of
  all proportion to the principal's legitimate interests (Paciocco v ANZ (2016) 258 CLR 525). "$Nil" in the Annexure
  may mean no delay damages at all — get legal advice before relying on it either way.
- **Defects liability** (cl 35): the contractor rectifies defects existing at PC and those notified in the DLP by the
  date directed; if not, the principal may have others do it at the contractor's cost.

## Security and retention

Cash retention or bank guarantees (cl 5), typically 5 % or 2.5 % + 2.5 %, reduced at PC and released after the final
certificate as the Annexure says. Check whether the state where the work is has a statutory retention trust scheme.
Keep a security register: type, issuer, amount, expiry, release trigger; warn 60 days before any expiry. A call on
security is a serious step: human decision, legal advice, and check the contract's notice requirement first.

## Payment

The contract's payment clause (AS 4000-1997 cl 37) runs alongside the security of payment legislation of the state
where the work is, which overrides inconsistent terms and has the hard statutory deadlines — load the security of
payment skill whenever a claim arrives. Unfixed materials are paid only if the contract allows and the conditions
(security, insurance, title, labelling) are met (cl 37.3). The final certificate is conclusive except for fraud,
latent defects, computational errors and disputes notified within the stated period (AS 4000-1997: 7 days) — diary
it.

## Show cause and termination

Default procedure (cl 39): a show cause notice naming the substantial breach and a date at least 7 clear days away;
if cause isn't shown, the principal may take work out of the contractor's hands or terminate. Termination is high
risk: wrongful termination is itself repudiation. **Never draft or send a show cause or termination notice without
the organisation's lawyer.** Insolvency (cl 39.11) and frustration (cl 40) have their own rules.

## Disputes

AS 4000-1997 cl 42: notice of dispute, conference within 14 days, then arbitration or litigation as the Annexure
says. AS 4000:2025 cl 42: negotiation first, then the Annexure's choice of mediation, expert determination or a
dispute avoidance board, and final resolution by arbitration or litigation (default litigation if nothing chosen).
Security of payment adjudication is separate and faster. Keep working: the contract usually requires performance to
continue during a dispute.

## Unfair contract terms and liability

- Since **9 November 2023**, unfair terms in **standard-form small business contracts** (a party with fewer than 100
  employees or under $10 m turnover) are illegal to propose or rely on, with large penalties. Heavily amended
  subcontracts and purchase orders given to small subcontractors on a take-it-or-leave-it basis are the exposure;
  very short time bars and one-sided set-off are typical risk terms. Get legal review of the organisation's
  templates.
- **Proportionate liability** (each state's civil liability legislation) applies to economic loss and property
  damage claims for failure to take reasonable care; states differ on whether the parties may contract out, so check
  the governing law.

## Red flags

A contract signed without its Annexure completed; no superintendent named; work under a letter of intent with no cap;
oral variations; a notice deadline nobody has diarised; a superintendent decision overdue (deemed EOT risk under
unamended cl 34.5); LDs being deducted without a superintendent's certificate; security expiring before the DLP
ends; the contractor's claims arriving in a batch near PC; a subcontract with pay-when-paid (void under security of
payment legislation).

## Escalate

The organisation's lawyer: any notice of dispute, show cause, termination, a disputed time bar, a claim over the
PM's delegation, an LD or security call, contract template changes. Project director: every contractual decision
you've prepared. Superintendent (if not the principal's PM): their assessments, with your records.
