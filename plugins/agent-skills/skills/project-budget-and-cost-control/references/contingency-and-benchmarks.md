# Contingency sizing and cost benchmarks

## Commonwealth cost estimation guidance (a good public method to copy)

The Australian Government's Department of Infrastructure publishes cost estimation guidance for projects it
funds (https://investment.infrastructure.gov.au/resources-funding-recipients/cost-estimation-guidance). Useful
points for any organisation, even one not bound by it:

- A probabilistic estimate (P50 and P90) is required for projects whose outturn cost, including contingency,
  exceeds **$25 m**. Below that a deterministic method is acceptable.
- **P50**: a 50 % chance the final cost is not exceeded. **P90**: 90 %. Budget the project at P50 plus the
  PM-held contingency; tell the decision maker the P90 and hold the difference as management reserve.
- Guidance Note 3B (deterministic contingency, v2.0, Nov 2023) gives two methods:
  - **Factor-based**: score six factors (scope definition, risk identification, constructability, key dates,
    site information, interfaces) to get a % contingency. The Department warns against generic percentages
    without a project risk assessment.
  - **Range-based** (below).

## Range-based method (works in a spreadsheet)

For each of up to about 20 independent cost elements and each priced risk, estimate the best case (BC, about P5),
most likely (ML) and worst case (WC, about P95).

- Expected value: **E = (3·BC + 10·ML + 3·WC) / 16**
- v = (WC − BC) / 3.25; k = 0.2 · ((WC + BC − 2·ML) / v)²; variance = ((WC − BC) / (3.35 − k))²
- For a risk with probability p, include p × its expected impact (and scale its variance).
- **P50 ≈ Σ E**; σ = √(Σ variance); **P80 ≈ P50 + 0.84σ**; **P90 ≈ P50 + 1.28σ**

Worked example, a BESS project ($ m):

| Element | BC | ML | WC | E | Variance |
|---|---|---|---|---|---|
| BESS equipment | 9.5 | 10.0 | 12.0 | 10.28 | 0.93 |
| Civil and structural | 2.8 | 3.0 | 4.0 | 3.15 | 0.25 |
| HV connection | 1.8 | 2.0 | 3.2 | 2.19 | 0.38 |
| Risk: transformer delay (30 %) | 0 | 0.5 | 1.2 | 0.16 | 0.04 |

Base (sum of ML) $15.0 m. P50 ≈ $15.78 m (5.2 % contingency); σ ≈ 1.27; P90 ≈ $17.40 m (16 %).
The Department's own spreadsheet treats risk variance more precisely; this simplification is good enough for an
early budget. For a major project use proper Monte Carlo software and a facilitated risk workshop.

## Contingency rules

- Design contingency sits in the base estimate and burns down as design matures; release unused amounts.
- Construction contingency (PM-held) funds risks in the register only. Each drawdown names the risk it retires.
- Management reserve (sponsor-held) is outside the performance baseline; releasing it is a budget change.
- Report contingency remaining against the residual risk exposure (e.g. P80 of remaining risks). A ratio below 1.0
  is a warning.

## Escalation indices

- ABS Producer Price Indexes, Australia (cat. 6427.0): output of construction industries, building construction by
  state, quarterly (https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/producer-price-indexes-australia).
- Rawlinsons Australian Construction Handbook (regional indices for capital and regional cities) and Cordell (CoreLogic): paid.
- State every estimate in base-date dollars, then escalate each period's cash flow to outturn. Don't escalate the
  whole sum to the completion date.

## Public benchmarks (check the source before quoting; excludes land, connection and owner's costs)

- **BESS** (CSIRO GenCost 2025-26 Final, July 2026, 2025 real dollars, per usable kWh): 2-hour ≈ $525/kWh; 4-hour
  ≈ $385/kWh (battery 265 + balance of plant 120); 8-hour ≈ $308/kWh. Costs fell 11-16 % in the year.
  https://www.csiro.au/-/media/Energy/GenCost-2025-26-Final/GenCost_2025-26_Final_Report_20260715.pdf
- **Data centres** (Turner & Townsend Data Centre Construction Cost Index 2025-26): reported in US$ per watt of IT
  load for 30-50 MW air-cooled builds; costs rose about 5.5 % in 2025; liquid cooling adds roughly 7-10 %. Get the
  nearest city's figure from the report itself and add regional and freight loadings for the site.
  https://www.turnerandtownsend.com/insights-old/data-centre-construction-cost-index-2025-2026/

Benchmarks are a sense check, never an estimate. Say where a figure came from and its date.
