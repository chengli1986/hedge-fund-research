#!/bin/bash
# Download test fixtures from live sources (run manually when needed)
set -eo pipefail
cd "$(dirname "$0")"

echo "Downloading Man Group fixture..."
curl -s 'https://www.man.com/insights' -o man-group-insights.html

echo "Downloading Bridgewater fixture..."
curl -s 'https://www.bridgewater.com/research-and-insights' -o bridgewater-research.html

echo "Downloading GMO API fixture..."
python3 -c "
import requests, json
from bs4 import BeautifulSoup
resp = requests.get('https://www.gmo.com/americas/research-library/', cookies={'GMO_region':'NorthAmerica'}, timeout=30)
soup = BeautifulSoup(resp.text, 'html.parser')
grid = soup.select_one('section.article-grid[data-endpoint]')
api_url = 'https://www.gmo.com' + grid['data-endpoint'] + '&currentPage=1'
api_resp = requests.get(api_url, cookies={'GMO_region':'NorthAmerica'}, timeout=30)
with open('gmo-api-response.json','w') as f:
    json.dump(api_resp.json(), f, indent=2)
print(f'  Saved {len(api_resp.json().get(\"listing\",[]))} articles')
"

echo "Done. Fixtures saved to tests/fixtures/"
