# Earned value: worked example

A 2 MW GPU hall fit-out. Budget at completion (BAC) $12.0 m over 10 months, planned evenly by package. Status
date: end of month 5.

| Package | Budget | Planned % by month 5 | PV | Measured % complete | EV | Actual + accrued (AC) |
|---|---|---|---|---|---|---|
| Civil and slab | 1.5 | 100 % | 1.50 | 100 % | 1.50 | 1.65 |
| Building shell | 2.5 | 80 % | 2.00 | 70 % | 1.75 | 1.90 |
| HV/MV electrical | 4.0 | 40 % | 1.60 | 30 % | 1.20 | 1.30 |
| Cooling | 3.0 | 30 % | 0.90 | 30 % | 0.90 | 0.95 |
| PM and supervision (level of effort) | 1.0 | 50 % | 0.50 | 50 % | 0.50 | 0.55 |
| **Total** | **12.0** | | **6.50** | | **5.85** | **6.35** |

## The indicators

- CV = EV − AC = 5.85 − 6.35 = **−$0.50 m** (spent $0.5 m more than the work done is worth)
- SV = EV − PV = 5.85 − 6.50 = **−$0.65 m** (behind programme)
- CPI = 5.85 / 6.35 = **0.921**
- SPI = 5.85 / 6.50 = **0.900**

## Forecasts

- EAC (typical) = BAC / CPI = 12.0 / 0.921 = **$13.03 m**
- EAC (atypical) = AC + (BAC − EV) = 6.35 + 6.15 = **$12.50 m** — only if the causes are fixed and won't recur
- EAC (CPI × SPI) = 6.35 + 6.15 / (0.921 × 0.900) = 6.35 + 7.42 = **$13.77 m** — pessimistic bound
- ETC (typical) = 13.03 − 6.35 = **$6.68 m**
- VAC (typical) = 12.0 − 13.03 = **−$1.03 m** (−8.6 % of BAC)
- TCPI to hit BAC = (12.0 − 5.85) / (12.0 − 6.35) = 6.15 / 5.65 = **1.088** — the rest of the job must run 9 %
  more efficiently than planned. Possible only with a real change.

## Earned schedule

PV was planned at $1.30 m per month. EV of $5.85 m was planned to be earned at 5.85 / 1.30 = **4.5 months**.
SPI(t) = ES / AT = 4.5 / 5 = **0.90**. Forecast duration ≈ 10 / 0.90 = **11.1 months**: about five weeks late
unless recovered. Check this against the critical path; earned value shows the trend, the programme shows whether
the late work is critical.

## What the report says

"At month 5 the project has done $5.85 m of work for $6.35 m (CPI 0.92) and is 10 % behind plan (SPI 0.90).
The overrun is in civil (rock in the slab excavation, a latent-conditions claim is under assessment) and the
building shell (steel delivered three weeks late). If performance continues, the forecast final cost is $13.0 m,
$1.0 m (8.6 %) over budget; the range is $12.5 m to $13.8 m. Contingency remaining is $0.6 m. This is beyond the
5 % escalation threshold: recommended actions are ... and a decision is needed on ..."

Notes:

- Level-of-effort lines (PM and supervision) earn with time and always show SPI = 1.0; report them but don't let
  them dilute the indicators of the real work.
- The CPI × SPI EAC assumes cost efficiency will be dragged down by schedule pressure; it's a bound, not a forecast.
- The bottom-up EAC (each package owner re-estimates the remaining work) should be done at least quarterly and
  compared with these formula results; big differences need an explanation.
