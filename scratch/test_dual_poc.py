#!/usr/bin/env python3
"""
PoC Zero-Risk: EUR/USD Spot (web) vs Futuros 6E
===============================================
Este script es completamente aislado en scratch/. NO modifica el dashboard principal.
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Try to import dependencies
try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    print("[WARN] yfinance not available")

try:
    import requests
    from bs4 import BeautifulSoup
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    print("[WARN] requests/beautifulsoup4 not available")


def test_6e_futures():
    """Test 1: Futuros 6E con yfinance"""
    print("\n" + "="*60)
    print("PRUEBA A: FUTUROS 6E=F (yfinance)")
    print("="*60)
    
    if not YFINANCE_AVAILABLE:
        print("[FAIL] yfinance no disponible")
        return {"status": "NO-GO", "reason": "yfinance missing"}
    
    result = {
        "has_volume": False,
        "has_oi": False,
        "oi_pct_nan": 100,
        "volume_pct_nan": 100,
        "num_candles": 0,
        "coherent_with_mt5": None,
        "adds_value": None,
        "details": {}
    }
    
    try:
        ticker = yf.Ticker("6E=F")
        print(f"[INFO] Ticker: {ticker.ticker}")
        
        # Descargar M15 últimos 5 días
        print("[INFO] Descargando M15 últimos 5 días...")
        hist = ticker.history(period="5d", interval="15m")
        
        if hist.empty:
            print("[FAIL] No se obtuvieron datos")
            result["status"] = "NO-GO"
            return result
        
        result["num_candles"] = len(hist)
        print(f"[OK] Obtenidas {len(hist)} velas")
        print(f"[INFO] Columnas: {list(hist.columns)}")
        print(f"[INFO] Rango: {hist.index[0]} - {hist.index[-1]}")
        
        # Check Volume
        if 'Volume' in hist.columns:
            vol_nan = hist['Volume'].isna().sum()
            vol_zero = (hist['Volume'] == 0).sum()
            result["volume_pct_nan"] = (vol_nan / len(hist)) * 100
            result["has_volume"] = result["volume_pct_nan"] < 50 and vol_zero < len(hist) * 0.8
            print(f"[INFO] Volume - NaN: {vol_nan} ({result['volume_pct_nan']:.1f}%), Zeros: {vol_zero}")
            result["details"]["volume_sample"] = hist['Volume'].tail(3).tolist()
        else:
            print("[WARN] No hay columna Volume")
        
        # Check Open Interest
        if 'Open Interest' in hist.columns:
            oi_col = 'Open Interest'
        elif 'Oi' in hist.columns:
            oi_col = 'Oi'
        else:
            oi_col = None
        
        if oi_col:
            oi_nan = hist[oi_col].isna().sum()
            oi_zero = (hist[oi_col] == 0).sum()
            result["oi_pct_nan"] = (oi_nan / len(hist)) * 100
            result["has_oi"] = result["oi_pct_nan"] < 50 and oi_zero < len(hist) * 0.8
            print(f"[INFO] {oi_col} - NaN: {oi_nan} ({result['oi_pct_nan']:.1f}%), Zeros: {oi_zero}")
            result["details"]["oi_sample"] = hist[oi_col].tail(3).tolist()
        else:
            print("[WARN] No hay columna Open Interest/Oi")
        
        # Mostrar últimas velas
        print("\n[INFO] Últimas 3 velas:")
        print(hist.tail(3)[['Open', 'High', 'Low', 'Close', 'Volume'] if 'Volume' in hist.columns else ['Open', 'High', 'Low', 'Close']])
        
        # Verificar si hay suficiente para cálculos
        if len(hist) >= 200:
            print(f"[OK] Suficiente histórico para SMA/RSI/MACD ({len(hist)} >= 200)")
        else:
            print(f"[WARN] Histórico limitado ({len(hist)} < 200)")
        
        # Veredicto preliminar
        if result["has_volume"] and result["has_oi"]:
            result["status"] = "GO"
            result["adds_value"] = True
            print("\n[VEREDICTO A] GO - Volume y OI útiles")
        elif result["has_volume"]:
            result["status"] = "GO-PARCIAL"
            result["adds_value"] = "limited"
            print("\n[VEREDICTO A] GO-PARCIAL - Solo Volume útil (sin OI)")
        else:
            result["status"] = "NO-GO"
            result["adds_value"] = False
            print("\n[VEREDICTO A] NO-GO - Datos insuficientes")
            
    except Exception as e:
        print(f"[ERROR] {e}")
        result["status"] = "ERROR"
        result["error"] = str(e)
    
    return result


def test_web_scraping():
    """Test 2: Extracción web EUR/USD spot"""
    print("\n" + "="*60)
    print("PRUEBA B: EXTRACCIÓN WEB EUR/USD SPOT")
    print("="*60)
    
    result = {
        "fxstreet": {"status": "UNKNOWN", "time": 0, "has_content": False},
        "dailyfx": {"status": "UNKNOWN", "time": 0, "has_content": False},
        "stable": None
    }
    
    if not REQUESTS_AVAILABLE:
        print("[FAIL] requests/beautifulsoup4 no disponibles")
        result["status"] = "NO-GO"
        return result
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,es;q=0.8',
    }
    
    # URLs de ejemplo (pueden cambiar, pero probamos acceso)
    urls = [
        ("fxstreet", "https://www.fxstreet.com/analysis/eurusd"),
        ("dailyfx", "https://www.dailyfx.com/eur-usd"),
    ]
    
    for name, url in urls:
        print(f"\n[INFO] Probando {name}: {url}")
        try:
            start = time.time()
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
            elapsed = time.time() - start
            result[name]["time"] = elapsed
            result[name]["status_code"] = resp.status_code
            
            print(f"[INFO] Status: {resp.status_code}, Tiempo: {elapsed:.2f}s, Bytes: {len(resp.text)}")
            
            # Detectar Cloudflare
            content_lower = resp.text.lower()
            if any(x in content_lower for x in ['cf-challenge', 'checking your browser', 'ray id', 'cloudflare']):
                print(f"[WARN] Posible Cloudflare challenge detectado")
                result[name]["status"] = "BLOQUEADO"
                result[name]["has_content"] = False
            elif resp.status_code == 200 and len(resp.text) > 10000:
                # Intentar extraer algo
                soup = BeautifulSoup(resp.text, 'html.parser')
                # Buscar texto relevante
                text = soup.get_text()
                if 'eur' in text.lower() and ('usd' in text.lower() or 'dollar' in text.lower()):
                    print(f"[OK] Contenido relevante encontrado")
                    result[name]["has_content"] = True
                    result[name]["status"] = "OK"
                else:
                    result[name]["status"] = "PARCIAL"
                    result[name]["has_content"] = True
            elif resp.status_code in [403, 429]:
                print(f"[WARN] Bloqueado ({resp.status_code})")
                result[name]["status"] = "BLOQUEADO"
            else:
                print(f"[WARN] Respuesta inesperada")
                result[name]["status"] = "LENTO" if elapsed > 8 else "PARCIAL"
            
        except requests.exceptions.Timeout:
            print(f"[ERROR] Timeout")
            result[name]["status"] = "LENTO"
            result[name]["time"] = 15
        except Exception as e:
            print(f"[ERROR] {e}")
            result[name]["status"] = "ERROR"
            result[name]["error"] = str(e)
    
    # Veredicto
    ok_count = sum(1 for k in ['fxstreet', 'dailyfx'] if result[k]["status"] == "OK")
    blocked_count = sum(1 for k in ['fxstreet', 'dailyfx'] if result[k]["status"] == "BLOQUEADO")
    
    if blocked_count >= 2:
        result["status"] = "NO-GO"
        result["stable"] = False
        print("\n[VEREDICTO B] NO-GO - Ambos sitios bloqueados")
    elif ok_count >= 1 and blocked_count == 0:
        result["status"] = "GO"
        result["stable"] = True
        print("\n[VEREDICTO B] GO - Extracción estable")
    elif ok_count >= 1:
        result["status"] = "GO-PARCIAL"
        result["stable"] = "partial"
        print("\n[VEREDICTO B] GO-PARCIAL - Algunos bloqueos")
    else:
        result["status"] = "NO-GO"
        result["stable"] = False
        print("\n[VEREDICTO B] NO-GO - Sin contenido útil")
    
    return result


def main():
    print("="*60)
    print("POC ZERO-RISK: EUR/USD SPOT + FUTUROS 6E")
    print(f"Fecha: {datetime.now().isoformat()}")
    print("="*60)
    
    # Ejecutar pruebas
    res_a = test_6e_futures()
    res_b = test_web_scraping()
    
    # Comparación cruzada
    print("\n" + "="*60)
    print("PRUEBA C: COMPARACIÓN CRUZADA")
    print("="*60)
    print("[INFO] Comparación manual requerida:")
    print("  - Precio 6E=F vs EUR/USD en MT5 (mismo instante)")
    print("  - Comparar último swing/key level en ambos")
    print("  - Verificar si estructura coincide")
    print("[INFO] Completa esto manualmente viendo MT5 vs los datos de arriba")
    
    # Veredicto final
    print("\n" + "="*60)
    print("VEREDICTO FINAL GO/NO-GO")
    print("="*60)
    
    status_a = res_a.get('status', 'UNKNOWN')
    status_b = res_b.get('status', 'UNKNOWN')
    
    print(f"A) 6E=F - Volume: {'SÍ' if res_a.get('has_volume') else 'NO'} | OI: {'SÍ' if res_a.get('has_oi') else 'NO'} | Status: {status_a} | Aporta valor: {res_a.get('adds_value')}")
    print(f"B) Web - FXStreet: {res_b['fxstreet']['status']} ({res_b['fxstreet']['time']:.1f}s) | DailyFX: {res_b['dailyfx']['status']} ({res_b['dailyfx']['time']:.1f}s) | Status: {status_b}")
    print(f"C) Comparación cruzada: PENDIENTE (manual con MT5)")
    
    # Decisión
    if status_a in ['GO', 'GO-PARCIAL'] and status_b == 'GO':
        print("\n>>> VEREDICTO: GO <<<")
        print("RAZÓN: Ambos elementos funcionan. Vale la pena integrar con ambos mercados.")
        print("SIGUIENTE PASO: Avanzar con Fases 0-3 del plan completo")
    elif status_a in ['GO', 'GO-PARCIAL'] and status_b == 'GO-PARCIAL':
        print("\n>>> VEREDICTO: GO-PARCIAL <<<")
        print("RAZÓN: 6E aporta valor, web funciona parcialmente. Integrar pero con manejo de errores.")
        print("SIGUIENTE PASO: Avanzar con precaución, añadir fallbacks para web scraping")
    elif status_a == 'GO' and status_b == 'NO-GO':
        print("\n>>> VEREDICTO: GO-PARCIAL <<<")
        print("RAZÓN: 6E muy bueno pero web bloqueado. Considerar usar solo datos propios de 6E + MT5.")
        print("SIGUIENTE PASO: Mejor usar 6E para cálculos propios, buscar alternativas para web")
    elif status_a == 'GO-PARCIAL' and status_b == 'NO-GO':
        print("\n>>> VEREDICTO: NO-GO <<<")
        print("RAZÓN: Ni 6E aporta mucho ni web funciona. No compensa la integración.")
        print("SIGUIENTE PASO: No integrar, mantener dashboard tal como está")
    else:
        print("\n>>> VEREDICTO: REVISAR <<<")
        print("RAZÓN: Resultados mixtos o con errores. Revisar detalles arriba.")
        print("SIGUIENTE PASO: Analizar errores y decidir caso por caso")
    
    print("\n" + "="*60)
    print("PoC completado. Resultados guardados en memoria.")
    print("="*60)


if __name__ == "__main__":
    main()
