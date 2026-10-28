import json
with open('cdt_full_table.json', 'r') as f:
    data = json.load(f)

# Sort by code
data.sort(key=lambda x: x['code'])

with open('cdt_table.txt', 'w', encoding='utf-8') as f:
    for item in data:
        code = item.get('code', '')
        desc = item.get('description', '')
        cat = item.get('category', '')
        f.write(f"{code}: {desc} ({cat})\n")
