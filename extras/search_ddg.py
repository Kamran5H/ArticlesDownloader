import urllib.request
import urllib.parse
import re

query = 'Physical Chemistry P Bahadur pdf download'
url = 'https://lite.duckduckgo.com/lite/'
data = urllib.parse.urlencode({'q': query}).encode('utf-8')
req = urllib.request.Request(url, data=data, headers={'User-Agent': 'Mozilla/5.0'})
try:
    with urllib.request.urlopen(req, timeout=15) as response:
        html = response.read().decode('utf-8', errors='ignore')
        matches = re.findall(r'class="result-url" href="(.*?)"', html, re.IGNORECASE)
        print('Links found:', len(matches))
        for m in matches:
            print(m)
except Exception as e:
    print("Error:", e)
