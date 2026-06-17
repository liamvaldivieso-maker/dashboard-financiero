import json
import os
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from parser import parse_factura

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}


def sync_to_supabase(factura: dict):
    try:
        r = httpx.post(
            f"{SUPABASE_URL}/rest/v1/facturas",
            headers=SUPABASE_HEADERS,
            json=factura,
            timeout=10,
        )
        if r.status_code in (200, 201):
            print(f"  Supabase OK: {factura['archivo']}")
        else:
            print(f"  Supabase error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"  Supabase excepcion: {e}")


# --- Paths ---
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data" / "facturas.json"
WATCH_DIR = Path.home() / "Desktop" / "FACTURAS"
STATIC_DIR = BASE_DIR / "static"

DATA_FILE.parent.mkdir(exist_ok=True)
WATCH_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)


# --- Storage ---
def load_data() -> list[dict]:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def save_data(facturas: list[dict]):
    DATA_FILE.write_text(json.dumps(facturas, ensure_ascii=False, indent=2), encoding="utf-8")


def add_or_update(factura: dict):
    facturas = load_data()
    existing = [f for f in facturas if f["archivo"] != factura["archivo"]]
    existing.append(factura)
    existing.sort(key=lambda f: f.get("fecha", ""), reverse=True)
    save_data(existing)
    print(f"  Guardada: {factura['archivo']} — {factura['subtotal']} €")
    sync_to_supabase(factura)


# --- File watcher ---
class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".pdf"):
            time.sleep(0.5)  # espera a que el archivo termine de copiarse
            self._process(event.src_path)

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.endswith(".pdf"):
            time.sleep(0.5)
            self._process(event.dest_path)

    def _process(self, path: str):
        print(f"Nueva factura detectada: {Path(path).name}")
        result = parse_factura(path)
        if result:
            add_or_update(result)
        else:
            print(f"  No se pudo parsear: {path}")


def start_watcher():
    observer = Observer()
    observer.schedule(PDFHandler(), str(WATCH_DIR), recursive=True)
    observer.start()
    print(f"Vigilando carpeta: {WATCH_DIR}")
    return observer


# --- Escaneo inicial ---
def scan_existing():
    pdfs = list(WATCH_DIR.rglob("*.pdf"))
    if not pdfs:
        return
    existing_files = {f["archivo"] for f in load_data()}
    new_pdfs = [p for p in pdfs if p.name not in existing_files]
    if new_pdfs:
        print(f"Escaneando {len(new_pdfs)} PDF(s) existentes...")
        for pdf in new_pdfs:
            result = parse_factura(str(pdf))
            if result:
                add_or_update(result)


# --- API ---
app = FastAPI(title="Dashboard Eflexx")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/facturas")
def get_facturas():
    return JSONResponse(content=load_data())


@app.get("/api/resumen")
def get_resumen():
    facturas = load_data()
    ingresos = [f for f in facturas if f.get("tipo") == "ingreso"]
    gastos = [f for f in facturas if f.get("tipo") == "gasto"]
    return {
        "total_facturas": len(facturas),
        "ingresos_brutos": round(sum(f["subtotal"] for f in ingresos), 2),
        "iva_repercutido": round(sum(f["iva"] for f in ingresos), 2),
        "irpf_retenido": round(sum(f["irpf"] for f in ingresos), 2),
        "neto_cobrado": round(sum(f["neto"] for f in ingresos), 2),
        "gastos_brutos": round(sum(f["subtotal"] for f in gastos), 2),
        "iva_soportado": round(sum(f["iva"] for f in gastos), 2),
    }


@app.post("/api/rescan")
def rescan():
    pdfs = list(WATCH_DIR.rglob("*.pdf"))
    processed = []
    for pdf in pdfs:
        result = parse_factura(str(pdf))
        if result:
            add_or_update(result)
            processed.append(pdf.name)
    return {"procesadas": processed}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    html_file = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    scan_existing()
    observer = start_watcher()
    try:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
    finally:
        observer.stop()
        observer.join()
