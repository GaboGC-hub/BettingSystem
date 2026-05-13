import requests
import json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
}

r = requests.get("https://www.betano.co/danae-webapi/api/live/events/84333758", headers=headers)
if r.status_code != 200:
    r = requests.get("https://www.betano.co/danae-webapi/api/live/overview/latest?includeVirtuals=true&queryLanguageId=8&queryOperatorId=17", headers=headers)

if r.status_code == 200:
    data = r.json()
    selections = {}
    if "selections" in data:
        if isinstance(data["selections"], dict):
            selections = data["selections"]
        else:
            for s in data["selections"]:
                selections[s.get("id")] = s
    elif "data" in data and "selections" in data["data"]:
        selections = data["data"]["selections"]
        
    res_list = []
    
    if isinstance(selections, dict):
        for k, v in selections.items():
            name = v.get("name", "").lower()
            if "tarjeta" in name or "card" in name or ("m" in name and "s" in name): 
                res_list.append(v)
    elif isinstance(selections, list):
        for v in selections:
            name = v.get("name", "").lower()
            res_list.append(v)

    # Solo imprimir nombres reales
    for x in list(res_list)[:20]:
         print(f"ID={x.get('id')} NAME='{x.get('name')}' PRICE={x.get('price')}")
else:
    print("FALLO HTTP:", r.status_code)
