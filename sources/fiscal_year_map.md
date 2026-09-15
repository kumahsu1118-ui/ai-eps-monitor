# Fiscal Year Alignment (do not overwrite)

| Ticker | Reported FY end (typical) | Notes for calendar alignment |
|--------|---------------------------|------------------------------|
| NVDA | Late January | FY ending Jan → mapped to prior calendar slot; label Reported FY separately |
| AVGO | Late October / early November | FY2026 spans parts of CY2025–2026 |
| TSM | Calendar year (Dec) | Reported FY ≈ Calendar year (`fiscalEqualsCalendar: true`) |
| MSFT | June 30 | FY2027 ≈ Jul 2026–Jun 2027 |
| BE | Calendar year (Dec) | Confirm on each IR filing; Dec ending → fiscalEqualsCalendar |
| KEYS | October 31 | Confirm on each 10-K |

## Slot mapping (not true calendar-year EPS)

Rule used for **FY-mapped calendar slots** only:

- Fiscal Period Ending **Jan–Mar** → prior calendar year slot
- Otherwise → ending year slot

Reported Fiscal Period Ending labels are **never overwritten**.

This is a **slot mapping only**, not true calendar-year EPS. True CY EPS requires summing Q1+Q2+Q3+Q4 consensus with all four quarters present (never interpolated). When quarterly consensus is unavailable, `trueCalendarYearEps` is null.
