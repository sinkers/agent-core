---
name: project-budget-and-cost-control
description: "How to set up and run a project budget: estimates and cost plans (AIQS stages, AACE classes), contingency and escalation, WBS and cost codes, commitments vs actuals vs accruals, forecast final cost, the monthly cost report, variation and change control, contingency drawdown, cash flow and S-curves, and earned value (CPI, SPI, EAC, ETC, VAC, TCPI, earned schedule) with worked formulas. Use when building or reviewing a budget or estimate, writing a cost report, assessing a variation's budget effect, forecasting cost at completion, checking cash flow, or when someone asks 'are we over budget', 'what will this cost at the end' or 'how much contingency is left'."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Project budget and cost control

You keep the project's numbers honest and early. A forecast that moves a little each month is good control; a
forecast that never moves until it jumps is a failure. Accounting treatment (revenue, capitalisation, GST, month-end)
is in `project-accounting`; payment claim law is in the security-of-payment skill for the project's jurisdiction,
if the deployment has one.

**You never approve spend.** Budgets, transfers between cost codes, contingency drawdowns and variations are approved
by the person with the delegation (the organisation's delegation of authority; if you don't know who, ask the project sponsor).
You prepare the numbers and the recommendation, and you record the approval with its date and approver.

## 1. Estimate and budget

### Estimate maturity

| AIQS stage (Australian Cost Management Manual) | Typical design | AACE class | Typical accuracy |
|---|---|---|---|
| Order of cost / feasibility | 0-2 % (concept idea) | Class 5 | -20 to -50 % / +30 to +100 % |
| Concept cost plan | 1-15 % | Class 4 | -15 to -30 % / +20 to +50 % |
| Preliminary (schematic) cost plan | 10-40 % | Class 3 | -10 to -20 % / +10 to +30 % |
| Detailed cost plan (design development) | 30-75 % | Class 2 | -5 to -15 % / +5 to +20 % |
| Pre-tender estimate (documentation) | 65-100 % | Class 1 | -3 to -10 % / +3 to +15 % |

(AACE ranges from AACE International RP 18R-97; the AIQS stages are the Australian names. Quote the class and
range with every estimate so nobody reads a concept figure as a tender price.)

### What goes in a budget

- **Base estimate**: direct costs by WBS element (quantities × rates, quotes, benchmarks) plus preliminaries
  (site establishment, supervision, cranes, amenities, insurance — often 8-15 % on building work), design and
  consultant fees, authority and connection fees, owner's costs (PM, legal, permits, land, financing).
- **Escalation**: from the estimate's price date to the cash-flow midpoint of each package. Use a stated index
  (ABS Producer Price Index for building construction for the project's state; Rawlinsons; Cordell; supplier
  escalation clauses for plant priced in USD/EUR plus an FX allowance). Show the rate and the period.
- **Contingency**: for known-unknowns inside the scope (design development, construction risk, latent conditions).
  Size it by **risk analysis** for anything material: list risks with probability and cost range, run a simple
  Monte Carlo, and set contingency so the budget is at P50 (expected) with the P90 figure reported to the decision
  maker. A flat percentage (e.g. 5-10 % at tender, 15-30 % at concept) is acceptable only for small or early work —
  say so when you use one.
- **Management reserve**: for unknown-unknowns and scope changes; held by the sponsor, outside the PM's budget.
- The range-based contingency method, the Commonwealth's P50/P90 guidance (probabilistic estimates above $25 m)
  and public $/kWh and $/W benchmarks are in `references/contingency-and-benchmarks.md` (load with `skills__load`
  name `project-budget-and-cost-control`, reference `contingency-and-benchmarks.md`).
- **No board decision on less than a Class 3 estimate** with a quantified risk analysis. Label every estimate with
  its class, base date and exclusions.
- **Exclusions and assumptions**: always written down with the estimate.

### WBS and cost codes

Build the cost breakdown from the WBS so cost, programme and scope share one structure. Use the organisation's cost
code structure where it has one. A workable general form is `<project>-<phase>-<package>-<element>-<cost type>`:
numbered packages (e.g. 10 Site and civil, 20 Building), elements within each package (e.g. 30.10, 30.20), and a
cost-type letter such as L labour · M materials · P plant · S subcontract · D design · F fees · C contingency.

A **control account** is the level where one person is accountable for scope, schedule and budget (usually the
package). Report at control-account level; drill down when something moves.

## 2. Tracking: the four numbers people confuse

| Term | Means | Comes from |
|---|---|---|
| **Budget** (current) | Original budget + approved changes and transfers | Budget log |
| **Committed** | Value of orders and contracts placed, including approved variations | POs, subcontracts, variation register |
| **Actual** | Cost incurred and invoiced/recorded in the ledger | Accounts (Xero or ERP) |
| **Accrued** | Work done or goods received but not yet invoiced | Site progress, delivery records |

**Cost to date = actual + accrued.** **Forecast to complete (ETC)** = what is still to be spent, by line, from
remaining commitments plus uncommitted scope, pending variations (at likely value) and risks. **Forecast final
cost (FFC, EAC) = cost to date + ETC.** **Variance = current budget − FFC** (positive = under budget).

Rules:

- Every line's FFC is at least its committed value, unless a credit is agreed in writing.
- Pending variations go in the forecast at their **likely** value, and are listed, not hidden in contingency.
- Accrue monthly for work done but not invoiced; reverse when the invoice arrives.
- No PO, no commitment, no pay: an invoice without a PO is a red flag, not a surprise.

### The monthly cost report

Columns, by control account then total:

| Original budget | Approved changes | Current budget | Committed | Actual to date | Accrued | Cost to date | Forecast to complete | Forecast final cost | Variance to budget | Movement since last month |

Below the table: contingency (start, drawn, remaining, forecast need), pending variations (count and value),
top five cost risks with range, cash flow vs plan, earned value indicators (section 4), and a short narrative: what
moved, why, and what is being done. One page for executives; the detail behind it.

## 3. Change control

- **Variation register** columns: no., description, instructed by (name, document, date), contract clause, type
  (principal-directed, contractor-proposed, latent condition, provisional sum adjustment), status (notified,
  priced, under assessment, approved, rejected, disputed), claimed value, assessed value, agreed value, time
  impact (days claimed/granted), budget source (contingency, management reserve, scope transfer), approver, date.
- Before a principal directs a change: estimate cost and time, identify the budget source, get the delegate's
  approval, then the superintendent issues the written direction under the contract.
- **Contingency drawdown log**: date, amount, risk or reason, cost code it moves to, approver. Contingency is for
  risks in the approved scope; a scope increase is a change to the budget, approved by the sponsor, not a drawdown.
- **Provisional sums and prime cost items**: allowances in the contract for work or items not yet defined. When the
  actual cost is known the contract sum is adjusted by the difference, and the contractor's margin (the % in the
  Annexure) is added to an excess — some contracts also deduct it from a saving, and some apply it to the whole
  actual cost rather than the difference, so check the project's contract and
  its edition (AS 4000-1997 and AS 4000:2025 number these clauses differently). Example: allowance $110,000, actual
  $132,000, margin 20 % → adjustment = $22,000 + $4,400 = **$26,400** added to the contract sum. Track each one:
  allowance, actual, adjustment, margin, and the superintendent's direction that authorised the work. Red flags:
  provisional sums above about 10 % of the contract sum, or spent without a direction.

## 4. Earned value

Use earned value on any project large enough for a monthly report. The Australian standard is AS 4817:2019
(Project, programme and portfolio management — Guidance on earned value management, an adoption of ISO 21508:2018,
which superseded AS 4817-2006); the PMI Practice Standard for EVM says the same in more depth.

Definitions (all at the status date):

- **BAC** budget at completion: the total time-phased budget (performance measurement baseline, excluding
  management reserve).
- **PV** planned value: budget of work scheduled to date. **EV** earned value: budget of work actually done
  (% complete × budget, per activity). **AC** actual cost of the work done (cost to date, including accruals).

| Measure | Formula | Read it as |
|---|---|---|
| Cost variance | CV = EV − AC | negative = overspending for the work done |
| Schedule variance | SV = EV − PV | negative = behind (in dollars) |
| Cost performance index | CPI = EV / AC | 0.90 = getting 90 c of work per dollar |
| Schedule performance index | SPI = EV / PV | 0.85 = 85 % of planned work done |
| Estimate at completion (typical: current efficiency continues) | EAC = BAC / CPI | the usual first forecast |
| EAC (atypical: past variance won't recur) | EAC = AC + (BAC − EV) | optimistic, needs a reason |
| EAC (cost and schedule pressure both matter) | EAC = AC + (BAC − EV) / (CPI × SPI) | pessimistic bound |
| EAC (bottom-up) | EAC = AC + bottom-up ETC | best when re-estimated |
| Estimate to complete | ETC = EAC − AC | |
| Variance at completion | VAC = BAC − EAC | negative = forecast overrun |
| To-complete performance index | TCPI = (BAC − EV) / (BAC − AC) to hit BAC; (BAC − EV) / (EAC − AC) to hit EAC | > 1.1 to hit BAC is rarely achieved |

**Earned schedule**: SPI in dollars drifts back to 1.0 at the end of every late project. Earned schedule (ES) is the
time at which the current EV was planned to be earned; **SPI(t) = ES / AT** (AT = actual time elapsed) and stays
meaningful to the end. Forecast duration ≈ planned duration / SPI(t).

**Measuring % complete** (agree the method per activity at baseline): 0/100 (short activities, < 1 month), 50/50,
weighted milestones (e.g. procurement: order 10 %, FAT 30 %, delivery 70 %, installed 90 %, commissioned 100 %),
units complete (metres of cable, panels installed), physical % assessed (with evidence), level of effort (PM,
supervision: earns with time — don't let it mask the rest). Never use "% of budget spent" as % complete.

**Thresholds** (the organisation's default unless the project sets its own) work in three steps: CPI or SPI below
the first level → explain in the report; below the second level, or VAC worse than the set % of BAC → escalate to
the sponsor with a recovery plan; TCPI to BAC above 1.10 → say plainly the budget will not be met without change.

Worked example: see `references/evm-worked-example.md` (load with `skills__load` name
`project-budget-and-cost-control`, reference `evm-worked-example.md`).

## 5. Cash flow and S-curves

- Build the cash flow from the programme: each package's cost spread over its activities (or a standard S-curve
  profile), then shift for payment terms (claim, payment schedule and due date under the contract and the
  applicable security-of-payment law), retention held and released, deposits and milestone payments for plant
  (often 10-30 % at order, balance at FAT/delivery), and GST.
- Plot planned (PV), earned (EV) and actual (AC) cumulative curves monthly. AC above EV = cost overrun; EV below PV =
  behind programme.
- **Front-loading** (contractor's early claims exceed work done) shows as claimed value well above your EV: assess
  claims against measured progress, not the contractor's schedule of values.

## 6. Weekly and monthly routine

Weekly: new commitments (POs, subcontracts, variations) entered and coded; invoices without POs chased; pending
variations priced; large risks reviewed. Monthly (before the cost report): accruals from site progress; EV from
measured % complete; FFC reviewed line by line with each package owner; contingency reconciled; cash flow updated;
the report issued with movements explained.

## Red flags

Forecast unchanged for three months on an active package; commitments exceeding budget on a line with no
approved transfer; costs accrued against no commitment; variations "agreed on site" without a written direction;
contingency spent on scope growth; CPI drifting down three months running; EAC = BAC every month (nobody is
forecasting); claims paid ahead of measured progress; retention not being held; FX exposure on imported plant not
hedged or allowed for.

## Escalate

To the project sponsor: forecast overrun beyond the threshold, contingency below the forecast need, a single
variation above the PM's delegation. To the CFO/accountant: anything about tax, revenue recognition, capitalisation,
or ledger corrections. Never present a forecast as approved budget, and never change a baseline without a recorded
approval.
