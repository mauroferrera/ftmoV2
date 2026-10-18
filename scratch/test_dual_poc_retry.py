#!/usr/bin/env python3
"""
PoC Zero-Risk: EUR/USD Spot (web) vs Futuros 6E - Retry with fallbacks
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


def test_6e_futures_retry():
    print("\n" + "="*60)
    print("PRUEBA A RETRY: FUTUROS 6E=F")
    print("="*60)
    
    if not YFINANCE_AVAILABLE:
        print("[FAIL] yfinance no disponible")
        return {"status": "NO-GO"}
    
    result = {"has_volume": False, "has_oi": False, "status": "UNKNOWN"}
    
    try:
        # Try with different approach
        print("[INFO] Intentando con yf.download...")
        import pandas as pd
        data = yf.download("6E=F", period="2d", interval="15m", progress=False)
        if not data.empty:
            print(f"[OK] yf.download obtuvo {len(data)} velas")
            print(f"[INFO] Columnas: {list(data.columns)}")
            if 'Volume' in data.columns or ('Volume', '6E=F') in data.columns:
                result["has_volume"] = True
                print("[OK] Volume presente")
            # Check for OI in info
            try:
                ticker = yf.Ticker("6E=F")
                info = ticker.info
                if info.get('openInterest'):
                    result["has_oi"] = True
                    print(f"[OK] OI en info: {info.get('openInterest')}")
            except:
                pass
            result["status"] = "GO-PARTIAL" if result["has_volume"] else "NO-GO"
            print(f"\n[VEREDICTO A] {result['status']}")
            return result
        else:
            print("[FAIL] Sin datos con yf.download")
    except Exception as e:
        print(f"[ERROR] {e}")
    
    # Try alternative - just check if we can get current price
    try:
        print("[INFO] Intentando obtener precio actual...")
        ticker = yf.Ticker("6E=F")
        price = ticker.history(period="1d", interval="1m")
        if not price.empty:
            print(f"[OK] Precio actual accesible: {price['Close'].iloc[-1]}")
            result["status"] = "GO-PARTIAL"
            result["has_price"] = True
            print("[VEREDICTO A] GO-PARTIAL - Solo precio accesible, histórico con rate limit")
            return result
    except Exception as e:
        print(f"[ERROR] {e}")
    
    result["status"] = "NO-GO"
    return result


def test_web_alternatives():
    print("\n" + "="*60)
    print("PRUEBA B ALTERNATIVA: WEB CON DIFERENTES ENFOQUES")
    print("="*60)
    
    result = {"status": "UNKNOWN"}
    
    if not REQUESTS_AVAILABLE:
        result["status"] = "NO-GO"
        return result
    
    # Try with different headers and approaches
    headers_list = [
        {'User-Agent': 'curl/7.68.0'},
        {'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)'},
        {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
    ]
    
    for i, headers in enumerate(headers_list):
        print(f"\n[INFO] Intento {i+1} con headers diferentes...")
        try:
            resp = requests.get("https://www.investing.com/currencies/eur-usd-analysis", 
                              headers=headers, timeout=10)
            print(f"[INFO] Status: {resp.status_code}, Bytes: {len(resp.text)}")
            if resp.status_code == 200 and len(resp.text) > 5000:
                print("[OK] Investing.com accesible")
                result["status"] = "GO-PARTIAL"
                result["source"] = "investing.com"
                return result
        except Exception as e:
            print(f"[ERROR] {e}")
    
    result["status"] = "NO-GO"
    return result


def main():
    print("="*60)
    print("POC RETRY CON FALLBACKS")
    print(f"Fecha: {datetime.now().isoformat()}")
    print("="*60)
    
    res_a = test_6e_futures_retry()
    res_b = test_web_alternatives()
    
    print("\n" + "="*60)
    print("VEREDICTO RETRY")
    print("="*60)
    print(f"A) 6E: {res_a['status']}")
    print(f"B) Web alternativo: {res_b['status']}")
    
    if res_a['status'] in ['GO', 'GO-PARTIAL'] and res_b['status'] in ['GO', 'GO-PARTIAL']:
        print("\n>>> GO-PARTIAL <<<")
        print("Con fallbacks ambos parcialmente accesibles")
    elif res_a['status'] in ['GO', 'GO-PARTIAL']:
        print("\n>>> GO-PARTIAL <<<")
        print("6E accesible con limitaciones, web necesita enfoque diferente")
    else:
        print("\n>>> REVISAR ESTRATEGIA <<<")
        print("Ambos con limitaciones - considerar usar solo MT5 + análisis propio")


if __name__ == "__main__":
    main()
