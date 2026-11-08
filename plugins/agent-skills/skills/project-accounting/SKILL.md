---
name: project-accounting
description: "Project accounting for construction and energy projects under Australian standards: month-end close for a project (cut-off, accruals, reconciling commitments to the ledger), PO and invoice controls (no PO no pay, three-way match, delegations), retention and bank guarantee registers, GST on progress claims and retentions, RCTIs, TPAR and ABN withholding, revenue recognition when the organisation is the contractor (AASB 15 over time, cost-to-cost, WIP over/under billing, onerous contracts), and capitalisation when the organisation builds its own assets (AASB 116, borrowing costs AASB 123, componentisation, impairment, grants). Use when preparing or checking month-end project figures, a WIP schedule, an accrual, a capitalisation question, invoice approval, or anything about how a project cost or revenue is accounted for."
version: 1.0.0
owner: agent-core
applies_to: ["*"]
---

# Project accounting

You prepare and check project numbers so the accountant can rely on them. **Accounting policy, tax and audit
judgments belong to the organisation's accountant or CFO**: you give them clean data, flag the judgment, and never
post to the ledger or change a policy. Budget and forecasting practice is in `project-budget-and-cost-control`;
payment claim law is in the security-of-payment skill for the project's jurisdiction, if the deployment has one.

## Month-end close for a project

1. **Cut-off**: everything delivered or done up to the last day of the month is in this month. Get delivery
   dockets and site progress up to that date.
2. **Accruals**: for each subcontract and supplier, value of work done or goods received (from QS assessment or the
   PM's measured progress) minus invoices received. Accrue the difference; reverse it next month when the invoice
   arrives. Don't accrue for work not done, and don't accrue a contractor's claim at face value — use the assessed
   amount.
3. **Reconcile** the project system (commitments, actuals) to the general ledger (Xero, MYOB or the ERP) by cost
   code. Clear unallocated postings and miscoded invoices before the cost report goes out.
4. **Registers updated**: retention (held from subcontractors, held by clients), bank guarantees and other security
   (issuer, amount, expiry, release condition, beneficiary), insurances. Flag any expiring within 60 days.
5. **Report**: cost report (see `project-budget-and-cost-control`), plus the WIP schedule if the organisation is the
   contractor, or the capital WIP summary if it is the owner.

## Controls on spending

- **No PO, no pay.** Every commitment has a purchase order or signed contract before work starts. An invoice with no
  PO goes back to the requester, and is a red flag in the weekly cost check.
- **Three-way match** before approval: the PO (or contract), the receipt (delivery or measured progress), and a
  valid tax invoice. Price, quantity and GST must agree.
- **Delegation of authority**: approvals follow the organisation's delegation; requester, approver and payer are
  different people. Variations and contingency drawdowns follow the same delegation.
- **Supplier bank detail changes are verified by phone on a number already on file**, never one in the email asking
  for the change. Payment-redirection fraud targets construction payments.
- Check ABNs on ABN Lookup for new suppliers: a mismatched ABN, or GST charged by a supplier not registered for GST,
  goes to the accountant.

## GST, TPAR and withholding

- **Tax invoices** must show: that it is a tax invoice, the supplier's identity and ABN, date, description,
  quantity/price, GST (or that the total includes GST), and for sales of **$1,000 or more** the buyer's identity or
  ABN (ATO; GSTR 2013/1). No valid tax invoice: no input tax credit. Hold the claim for credit until one arrives.
- **Progress claims** are progressive supplies (GST Act s 156-5): GST attaches to each progress component when it is
  invoiced or paid, whichever is first (accruals basis, s 29-5).
- **Retentions**: under the Goods and Services Tax: (Particular Attribution Rules for Retention Payments)
  Determination 2017, GST on the retained amount is attributable when it is actually paid, or earlier if invoiced.
  So invoice the net claim and invoice the retention when its release is due. (The 2017 determination will sunset
  around April 2027 unless remade: the accountant should check.)
- **Recipient-created tax invoices (RCTIs)**: a head contractor may issue the tax invoice for a subcontractor's claim
  only under a written RCTI agreement that meets the ATO determination (both GST registered; the supplier agrees not
  to issue its own tax invoices; each notifies changes in registration). Check the agreement is on file first.
  AS 4000:2025 has a GST clause dealing with RCTIs; the 1997 edition doesn't.
- **Payment schedule ≠ tax invoice.** If the scheduled amount differs from an invoice, an adjustment note or new
  invoice is needed.
- **TPAR**: businesses primarily in building and construction that pay contractors for building and construction
  services lodge a Taxable Payments Annual Report by **28 August** (online). Whether the organisation is caught in a
  given year is the accountant's call: tell them about contractor payments.
- **No ABN quoted**: withhold **47 %** unless an exception applies. Foreign contractors doing construction work in
  Australia (e.g. an overseas BESS or EPC vendor installing on site) may trigger foreign-resident withholding:
  ask the accountant before the first payment.

## When the organisation is the contractor: revenue (AASB 15)

- Revenue on a construction contract is usually recognised **over time** (AASB 15 para 35): the contractor's work
  creates or enhances an asset the customer controls (building on the customer's site), or the asset has no
  alternative use to the contractor and the contractor has an enforceable right to payment for work done to date.
- **Measuring progress (input method, cost-to-cost)**: % complete = cost incurred to date ÷ total forecast cost;
  revenue to date = % complete × transaction price. Exclude costs that don't show progress, such as materials
  delivered but not installed and abnormal waste; for a big purchased item (a transformer, BESS enclosures)
  revenue may be recognised at cost with no margin until it is installed (para B19).
- **Variations and claims** go into the transaction price only when approved or enforceable and when it is highly
  probable there won't be a significant reversal (paras 18-21, 56-57). An unapproved claim is not revenue.
- **Contract asset / liability**: revenue recognised more than billed = contract asset (under-billed); billed more
  than revenue = contract liability (over-billed).
- **WIP schedule** columns: contract value, approved variations, forecast cost, forecast margin, cost to date,
  % complete, revenue earned to date, billed to date, over/(under) billing, retention held by the client.
- **Onerous contract** (AASB 137): if unavoidable costs will exceed the revenue, the **whole expected loss is
  recognised now**, not spread. Example: contract $20 m, forecast cost rises from $18 m to $21 m → a $1 m loss
  provision this month. Tell the CFO the day the forecast shows a loss.

## When the organisation is the owner: capital projects (AASB 116, 123, 136, 120)

- **Capitalise** directly attributable costs (AASB 116 paras 16-17): purchase price net of discounts, site
  preparation, delivery and handling, installation and assembly, testing, professional fees (design, PM, surveyors),
  employee costs that arise directly from the build, and the initial estimate of dismantling and restoration costs
  (relevant for BESS end-of-life and leased sites).
- **Expense, don't capitalise** (paras 19-20): general administration and overheads, training, opening and launch
  costs, abnormal waste, initial operating losses.
- **Income during commissioning**: proceeds from selling output before the asset is ready (e.g. energy traded by a
  BESS during commissioning, compute sold during testing) go to profit or loss, not against the asset's cost
  (AASB 116 as amended by AASB 2020-3).
- **Capitalisation stops** when the asset is capable of operating as management intends — usually practical
  completion and commissioning. Then it moves from assets under construction (capital WIP) to the right asset class.
- **Borrowing costs** (AASB 123): interest on funds used for a qualifying asset (one that takes a substantial time to
  build) is capitalised from when spending, borrowing and activity have all started, suspended during long
  inactivity, and stops when the asset is substantially complete.
- **Componentise** for depreciation, so code costs to components from the start: building shell, HV transformers and
  switchgear, generators, UPS (and its batteries), cooling, BESS modules, PCS/inverters, IT and GPUs each have
  different lives. The accountant sets the lives; your job is that the cost codes let them split the cost.
- **Impairment** (AASB 136): flag to the CFO any indicator — a big overrun relative to the asset's value, loss of a
  tenant or offtake, a connection or tariff change, technology obsolescence. Assets under construction can be
  impaired.
- **Grants** (AASB 120, e.g. ARENA): recognised when there is reasonable assurance the conditions will be met. Track
  grant-funded and co-funded costs on separate codes and track milestone conditions; a repayment clause is a
  liability risk.
- **R&D tax incentive**: only for genuinely experimental work (not routine construction). Mention it to the tax
  adviser if a project has novel technical work; registration deadlines apply.

## Weekly checks (5 minutes)

Invoices waiting more than 5 days for approval; invoices without POs; new suppliers without ABN checks; bank detail
change requests; guarantees or insurances expiring in 60 days; retention not deducted where the contract says it
should be.

## Escalate to the accountant or CFO

Any onerous contract or forecast loss; WIP judgments on unapproved variations or claims; capitalisation of anything
unusual (owner's staff time, financing, commissioning income); componentisation and useful lives; impairment
indicators; grant conditions; GST on retentions or RCTI set-up; TPAR; withholding for foreign contractors; ledger
corrections. You give them the figures and the documents; they decide.
