# Dashboard update notes

Quick notes on this round of changes, mostly documenting the duplicate incident thing since that one wasn't obvious from just looking at the app.

## Duplicate incidents

Found 168 incidents (336 rows) getting logged twice in the data. Turns out LVPD's daily PDFs sometimes carry the tail end of one day over into the next day's post — so the same incident shows up under two different `log_date`s with everything else (incident #, timestamp, address, nature) identical. Our scraper treats each PDF as its own source, so it picked up both copies every time this happened.

Fixed by deduping on `incident` (LVPD's own case number, the actual unique ID — not `log_date`) and keeping the first one we see. Row count went from 18,330 → 18,162 after the fix.

## Everything else

- Renamed "ADW" → "Assault w/Deadly Weapon" (nobody outside PD knows what ADW stands for)
- Renamed "Nature" to "Incident Type" everywhere it shows up — filter label, chart legend, Assist Agency caption
- Added a "Select All" button next to the Incident Type filter — multiselect already had clear-all built in, just needed a way back
- Heatmap: renamed to "Total Incidents by Day of Week & Hour", hour axis now shows a.m./p.m. instead of AM/PM
- Daily line chart: renamed to "Total Incidents by Day", x-axis spells out full month names now (April 2025, not Apr 2025)
