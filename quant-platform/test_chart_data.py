import urllib.request
import json
import traceback

def test_screener_and_bars():
    print("[1] Testing Backend API: /api/screener/scan")
    scan_req = {
        "strategy_name": "均线唯美平滑发散策略",
        "start": "2026-03-20",
        "end": "2026-03-27",
        "exchanges": [],
        "sectors": None,
        "hot_only": False,
        "strategy_params": {}
    }
    
    try:
        req = urllib.request.Request("http://localhost:8888/api/screener/scan", 
                                    data=json.dumps(scan_req).encode('utf-8'),
                                    headers={'Content-Type': 'application/json'},
                                    method='POST')
        
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            results = res_data.get("results", [])
            print(f" -> Screener returned {len(results)} signals.")
            
            if not results:
                print(" -> No signals found for testing, we will use a hardcoded stock (000670) for the chart data test.")
                target_code = "000670"
                target_date = "2026-03-26"
            else:
                first = results[0]
                target_code = first["code"]
                target_date = first["date"]
                print(f" -> Found stock {target_code} on date {target_date}")
                print(f" -> Strategy close price returned: {first.get('close')}")
                if first.get('close') is None:
                    print("❌ Error: close price is NOT attached to the signal response.")
                else:
                    print(f"✅ Success: close price is correctly passed to the frontend: {first['close']}")
    
    except Exception as e:
        print(f"❌ Failed to reach screener: {e}")
        traceback.print_exc()
        target_code = "000670"
        target_date = "2026-03-26"

    print(f"\n[2] Testing Backend API: /api/bars/{target_code} (as Frontend LightweightCharts would)")
    try:
        with urllib.request.urlopen(f"http://localhost:8888/api/bars/{target_code}") as response:
            bars = json.loads(response.read().decode('utf-8'))
            print(f" -> Fetched {len(bars)} bars.")
            
            if len(bars) == 0:
                print(f"❌ Error: No bars found for {target_code}")
                return
                
            dates = [b['date'].split(' ')[0] for b in bars]
            if len(dates) != len(set(dates)):
                print("❌ Error: Duplicate dates found! This will crash LightweightCharts.")
            else:
                print("✅ Success: All dates are strictly unique.")
                
            date_found = target_date in dates
            if date_found:
                print(f"✅ Success: Signal date '{target_date}' perfectly matches a date inside the bars array!")
            else:
                print(f"❌ Error: Signal date '{target_date}' is MISSING in bars array! This would crash LightweightCharts markers.")
                
            # Simulate frontend math check for exceptions
            bad_bars = 0
            for b_idx, b in enumerate(bars):
                o = float(b.get("open", 0) or 0)
                c = float(b.get("close", 0) or 0)
                h = float(b.get("high", 0) or 0)
                l = float(b.get("low", 0) or 0)
                v = float(b.get("volume", 0) or 0)
                
                # Check TS format violations
                if h < max(o, c) or l > min(o, c):
                    bad_bars += 1
            
            if bad_bars > 0:
                print(f"❌ Error: Found {bad_bars} bars with structurally invalid prices (e.g. high < open). Frontend has countermeasures, but backend data is dirty.")
            else:
                print("✅ Success: All bars passed LightweightCharts High/Low structural boundary tests.")
                
    except Exception as e:
        print(f"❌ Failed to fetch bars: {e}")
        traceback.print_exc()
        
if __name__ == '__main__':
    test_screener_and_bars()
