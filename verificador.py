#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Verificador de Integridad de Referencias — prototipo v0.3
=========================================================
Toma una lista de referencias bibliográficas (texto plano, una por línea)
y las verifica en cascada contra Crossref y OpenAlex:

  1. ¿Existe? Crossref primero; si no, OpenAlex como respaldo
     (detecta referencias fabricadas/alucinadas por LLMs).
  2. ¿Los metadatos coinciden? (año con tolerancia ±2 por el desfase
     online-first vs. impreso; título; apellido del primer autor)
  3. ¿Está retractada? — doble chequeo: por DOI y por título en OpenAlex,
     porque hay registros DOI duplicados cuyo flag de retracción difiere
     (caso real: Wakefield 1998 en The Lancet).

Reglas de emparejamiento (v0.3):
  - Títulos con <3 tokens significativos ("Introduction", "Got Blog?")
    solo se aceptan con coincidencia total Y apellido del autor presente.
  - Si el apellido del primer autor NO aparece en la referencia, se exige
    coincidencia de título casi perfecta (>=0.95).

Veredictos:
  VERIFICADA        existe y los metadatos coinciden
  METADATOS DUDOSOS existe pero el año citado no corresponde
  RETRACTADA        existe pero fue retractada
  NO VERIFICABLE    no aparece en Crossref ni OpenAlex
                    (posible alucinación… o literatura gris sin indexar)

Uso:
    python3 verificador.py referencias.txt
    python3 verificador.py referencias.txt --salida reporte.csv
    python3 verificador.py refs.txt --salida rep.csv --silencioso --continuar

Opciones:
    --salida X      archivo CSV de salida (default: reporte.csv)
    --silencioso    imprime solo el avance cada 100 referencias
    --continuar     retoma un reporte existente (salta las ya procesadas)
    --pausa X       segundos entre referencias (default: 0.5)

APIs usadas (todas gratuitas, sin clave):
    - Crossref REST API  (https://api.crossref.org)
    - OpenAlex API       (https://api.openalex.org)
"""

import csv
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

MAILTO = "danielaristo@yahoo.com"  # polite pool de Crossref/OpenAlex
USER_AGENT = "VerificadorReferencias/0.3 (mailto:%s)" % MAILTO
UMBRAL_TITULO = 0.60      # fracción mínima de tokens del título presentes en la referencia
UMBRAL_SIN_AUTOR = 0.95   # exigencia si el apellido del autor no aparece en la referencia
UMBRAL_DUPLICADO = 0.90   # similitud para considerar dos registros el mismo trabajo
MIN_TOKENS = 3            # tokens significativos mínimos para confiar solo en el título
TOLERANCIA_ANIO = 2       # ±2 años (desfase online-first vs. impreso)


# ----------------------------------------------------------------------
# Utilidades HTTP y de texto
# ----------------------------------------------------------------------

def fetch_json(url, timeout=30, reintentos=2):
    """GET con User-Agent identificado; devuelve dict o None si falla."""
    for intento in range(reintentos + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if intento < reintentos:
                time.sleep(3 * (intento + 1))
        except Exception:
            if intento < reintentos:
                time.sleep(3 * (intento + 1))
    return None


def normalizar(texto):
    """minúsculas, sin acentos, solo alfanumérico y espacios."""
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9 ]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def tokens_significativos(titulo):
    return [t for t in normalizar(titulo).split() if len(t) > 3]


def score_titulo(referencia, titulo):
    """Fracción de tokens significativos del título candidato presentes en la referencia."""
    tokens = tokens_significativos(titulo)
    if not tokens:
        return 0.0
    ref_norm = " " + normalizar(referencia) + " "
    presentes = sum(1 for t in tokens if (" " + t + " ") in ref_norm)
    return presentes / len(tokens)


def apellido_presente(referencia, apellido):
    if not apellido:
        return False
    ap = normalizar(apellido)
    return bool(ap) and (" " + ap + " ") in (" " + normalizar(referencia) + " ")


def emparejamiento_aceptable(referencia, titulo, apellido):
    """
    Decide si un candidato (titulo, apellido de primer autor) es un
    emparejamiento confiable para la referencia. Devuelve (acepta, score).
    """
    s = score_titulo(referencia, titulo)
    n = len(tokens_significativos(titulo))
    autor_ok = apellido_presente(referencia, apellido)
    if n < MIN_TOKENS:
        # título demasiado corto para fiarse solo de él
        return (s >= 0.99 and autor_ok), s
    if apellido and not autor_ok:
        # el autor registrado no aparece citado: exigir título casi perfecto
        return (s >= UMBRAL_SIN_AUTOR), s
    return (s >= UMBRAL_TITULO), s


def extraer_doi(referencia):
    m = re.search(r"10\.\d{4,9}/[^\s,;\"']+", referencia)
    if not m:
        return None
    return m.group(0).rstrip(".)]")


def extraer_anio(referencia):
    m = re.search(r"\b(19|20)\d{2}\b", referencia)
    return int(m.group(0)) if m else None


# ----------------------------------------------------------------------
# Consultas a las APIs
# ----------------------------------------------------------------------

def crossref_por_doi(doi):
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    datos = fetch_json(url)
    return datos.get("message") if datos else None


def crossref_busqueda(referencia, filas=3):
    url = ("https://api.crossref.org/works?query.bibliographic=%s&rows=%d"
           "&select=title,issued,DOI,author&mailto=%s"
           % (urllib.parse.quote(referencia), filas, MAILTO))
    datos = fetch_json(url)
    if not datos:
        return []
    return datos.get("message", {}).get("items", [])


def openalex_busqueda(texto, filas=5):
    """Búsqueda general en OpenAlex; devuelve lista de dicts crudos."""
    url = ("https://api.openalex.org/works?search=%s&per-page=%d"
           "&select=display_name,publication_year,doi,is_retracted,authorships&mailto=%s"
           % (urllib.parse.quote(texto), filas, MAILTO))
    datos = fetch_json(url)
    if not datos:
        return []
    return datos.get("results", [])


def openalex_apellido(cand):
    """Apellido del primer autor de un registro de OpenAlex (última palabra del nombre)."""
    auts = cand.get("authorships") or []
    if not auts:
        return None
    nombre = (auts[0].get("author") or {}).get("display_name") or ""
    partes = nombre.split()
    return partes[-1] if partes else None


def openalex_retractado_por_doi(doi):
    """True/False según OpenAlex para un DOI exacto, o None si no se pudo consultar."""
    url = "https://api.openalex.org/works/https://doi.org/%s?select=is_retracted&mailto=%s" % (doi, MAILTO)
    datos = fetch_json(url)
    if datos is None:
        return None
    return bool(datos.get("is_retracted", False))


def openalex_retractado_por_titulo(titulo, anio):
    """
    Busca en OpenAlex registros con el mismo título y año (±1) y devuelve True
    si ALGUNO está retractado. Atrapa el caso de DOIs duplicados donde el
    registro emparejado no tiene el flag pero su duplicado canónico sí.
    """
    consulta = re.sub(r"[,:|&]", " ", titulo)
    for cand in openalex_busqueda(consulta, filas=5):
        titulo_cand = cand.get("display_name") or ""
        anio_cand = cand.get("publication_year")
        if score_titulo(titulo, titulo_cand) >= UMBRAL_DUPLICADO:
            if anio and anio_cand and abs(anio - anio_cand) > 1:
                continue
            if cand.get("is_retracted"):
                return True
    return False


# ----------------------------------------------------------------------
# Lógica de verificación
# ----------------------------------------------------------------------

def campos_item(item):
    """Extrae (titulo, anio, doi, apellido_primer_autor) de un item de Crossref."""
    titulo = (item.get("title") or [""])[0]
    fecha = (item.get("issued", {}).get("date-parts") or [[None]])[0]
    anio = fecha[0] if fecha else None
    apellido = (item.get("author") or [{}])[0].get("family")
    return titulo, anio, item.get("DOI"), apellido


def verificar_referencia(referencia):
    """Devuelve un dict con el veredicto de una referencia."""
    resultado = {
        "referencia": referencia,
        "estado": "",
        "fuente": "",
        "doi": "",
        "titulo_encontrado": "",
        "autor_encontrado": "",
        "anio_referencia": extraer_anio(referencia),
        "anio_encontrado": "",
        "retractada": "",
        "score_titulo": "",
        "detalle": "",
    }

    doi_en_ref = extraer_doi(referencia)

    # --- Caso 1: la referencia trae DOI -> verificación directa en Crossref
    if doi_en_ref:
        item = crossref_por_doi(doi_en_ref)
        if item is None:
            resultado["estado"] = "NO VERIFICABLE"
            resultado["detalle"] = "El DOI citado no existe en Crossref (posible alucinación)"
            return resultado
        titulo, anio, doi, apellido = campos_item(item)
        resultado.update(fuente="Crossref", doi=doi, titulo_encontrado=titulo,
                         autor_encontrado=apellido or "", anio_encontrado=anio)
        resultado["score_titulo"] = round(score_titulo(referencia, titulo), 2)
    else:
        # --- Caso 2: sin DOI -> búsqueda en Crossref
        mejor, mejor_score, mejor_score_bruto = None, 0.0, 0.0
        for item in crossref_busqueda(referencia):
            titulo, anio, doi, apellido = campos_item(item)
            acepta, s = emparejamiento_aceptable(referencia, titulo, apellido)
            mejor_score_bruto = max(mejor_score_bruto, s)
            if acepta and s > mejor_score:
                mejor, mejor_score = (titulo, anio, doi, apellido, "Crossref", None), s

        # --- Caso 3: Crossref no la encontró -> respaldo en OpenAlex
        if mejor is None:
            for cand in openalex_busqueda(referencia):
                titulo = cand.get("display_name") or ""
                anio = cand.get("publication_year")
                doi = (cand.get("doi") or "").replace("https://doi.org/", "")
                apellido = openalex_apellido(cand)
                acepta, s = emparejamiento_aceptable(referencia, titulo, apellido)
                mejor_score_bruto = max(mejor_score_bruto, s)
                if acepta and s > mejor_score:
                    mejor, mejor_score = (titulo, anio, doi, apellido, "OpenAlex",
                                          bool(cand.get("is_retracted"))), s

        if mejor is None:
            resultado["estado"] = "NO VERIFICABLE"
            resultado["score_titulo"] = round(mejor_score_bruto, 2)
            resultado["detalle"] = ("No aparece en Crossref ni OpenAlex: posible referencia "
                                    "fabricada, o literatura gris sin indexar")
            return resultado

        titulo, anio, doi, apellido, fuente, retractada_directa = mejor
        resultado.update(fuente=fuente, doi=doi or "", titulo_encontrado=titulo,
                         autor_encontrado=apellido or "", anio_encontrado=anio)
        resultado["score_titulo"] = round(mejor_score, 2)
        if retractada_directa:
            resultado["retractada"] = "SI"
            resultado["estado"] = "RETRACTADA"
            resultado["detalle"] = "OpenAlex marca este trabajo como retractado"
            return resultado

    # --- Cotejo de año (±2 por desfase online-first vs. impreso)
    anio_ref = resultado["anio_referencia"]
    anio_enc = resultado["anio_encontrado"]
    if anio_ref and anio_enc and abs(anio_ref - anio_enc) > TOLERANCIA_ANIO:
        resultado["estado"] = "METADATOS DUDOSOS"
        resultado["detalle"] = ("Año citado (%s) no coincide con el registrado (%s)"
                                % (anio_ref, anio_enc))
    else:
        resultado["estado"] = "VERIFICADA"

    # --- Estado de retracción: por DOI y, si sale limpio, por título
    #     (los registros DOI duplicados pueden tener el flag solo en el canónico)
    retractada = False
    if resultado["doi"]:
        retractada = openalex_retractado_por_doi(resultado["doi"]) is True
    if not retractada and resultado["titulo_encontrado"]:
        retractada = openalex_retractado_por_titulo(resultado["titulo_encontrado"],
                                                    resultado["anio_encontrado"])
    if retractada:
        resultado["retractada"] = "SI"
        resultado["estado"] = "RETRACTADA"
        resultado["detalle"] = ("OpenAlex marca este trabajo como retractado "
                                "(detectado vía cotejo de duplicados)" if resultado["doi"] else
                                "OpenAlex marca este trabajo como retractado")
    else:
        resultado["retractada"] = "NO"

    return resultado


# ----------------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------------

ICONOS = {
    "VERIFICADA": "✅",
    "NO VERIFICABLE": "❌",
    "METADATOS DUDOSOS": "⚠️ ",
    "RETRACTADA": "🚫",
}

CAMPOS_CSV = ["referencia", "estado", "fuente", "doi", "titulo_encontrado",
              "autor_encontrado", "anio_referencia", "anio_encontrado",
              "retractada", "score_titulo", "detalle"]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    archivo = sys.argv[1]
    salida = "reporte.csv"
    if "--salida" in sys.argv:
        salida = sys.argv[sys.argv.index("--salida") + 1]
    silencioso = "--silencioso" in sys.argv
    continuar = "--continuar" in sys.argv
    pausa = 0.5
    if "--pausa" in sys.argv:
        pausa = float(sys.argv[sys.argv.index("--pausa") + 1])
    hilos = 1
    if "--hilos" in sys.argv:
        hilos = max(1, int(sys.argv[sys.argv.index("--hilos") + 1]))

    with open(archivo, encoding="utf-8") as f:
        referencias = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    # Modo continuar: saltar referencias ya presentes en el reporte
    ya_hechas = set()
    if continuar and os.path.exists(salida):
        with open(salida, encoding="utf-8-sig") as f:
            for fila in csv.DictReader(f):
                ya_hechas.add(fila["referencia"])
        print("Retomando: %d referencias ya procesadas en %s" % (len(ya_hechas), salida))

    pendientes = [r for r in referencias if r not in ya_hechas]
    print("Verificando %d referencias (%d en total)...\n" % (len(pendientes), len(referencias)))

    modo = "a" if (continuar and ya_hechas) else "w"
    conteo = {}
    candado = threading.Lock()

    def procesar(ref):
        r = verificar_referencia(ref)
        if pausa:
            time.sleep(pausa)
        return r

    with open(salida, modo, newline="", encoding="utf-8-sig") as fsal:
        w = csv.DictWriter(fsal, fieldnames=CAMPOS_CSV)
        if modo == "w":
            w.writeheader()
        inicio = time.time()
        i = 0

        def registrar(r):
            nonlocal i
            with candado:
                i += 1
                w.writerow(r)
                fsal.flush()
                conteo[r["estado"]] = conteo.get(r["estado"], 0) + 1
                if silencioso:
                    if i % 100 == 0 or i == len(pendientes):
                        transcurrido = time.time() - inicio
                        ritmo = transcurrido / i
                        restante = ritmo * (len(pendientes) - i) / 60
                        print("[%d/%d] %s | ~%.0f min restantes"
                              % (i, len(pendientes),
                                 " ".join("%s:%d" % kv for kv in sorted(conteo.items())),
                                 restante), flush=True)
                else:
                    icono = ICONOS.get(r["estado"], "?")
                    ref = r["referencia"]
                    print("%s [%d/%d] %s%s" % (icono, i, len(pendientes), r["estado"],
                                               (" (vía %s)" % r["fuente"]) if r["fuente"] else ""))
                    print("   %s" % (ref[:100] + ("..." if len(ref) > 100 else "")))
                    if r["detalle"]:
                        print("   → %s" % r["detalle"])
                    if r["doi"]:
                        print("   → DOI: %s | score título: %s" % (r["doi"], r["score_titulo"]))
                    print()

        if hilos == 1:
            for ref in pendientes:
                registrar(procesar(ref))
        else:
            with ThreadPoolExecutor(max_workers=hilos) as pool:
                futuros = [pool.submit(procesar, ref) for ref in pendientes]
                for fut in as_completed(futuros):
                    registrar(fut.result())

    print("=" * 60)
    print("RESUMEN (de esta corrida)")
    for estado in ["VERIFICADA", "METADATOS DUDOSOS", "NO VERIFICABLE", "RETRACTADA"]:
        if conteo.get(estado):
            print("  %s %-20s %d" % (ICONOS[estado], estado, conteo[estado]))
    print("=" * 60)
    print("\nReporte guardado en: %s" % salida)


if __name__ == "__main__":
    main()
