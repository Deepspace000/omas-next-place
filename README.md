# Oma's Next Place

A rental search for Oma: small 1-bedroom places around Redcliffe and Brisbane's north, up to about $400 a week, where her cat is allowed, with no stairs, near shops and a bus.

Page: https://deepspace000.github.io/omas-next-place/

- `rent_search.py` searches rent.com.au. A GitHub Action runs it every hour from 6am to 10pm (Brisbane time), and when the page's search button is pressed.
- `update.py` merges each search into `data/listings.json` and writes `data/site.json`, which the page shows.
- Claude reads new ads (`data/assessments.json`) and finds agents' emails (`data/contacts/`), from a scheduled task on the home PC.
