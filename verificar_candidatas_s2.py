#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cuarta capa de la cascada: Semantic Scholar (~220M trabajos, incluye
mucho contenido no-Crossref: revistas deslistadas, repositorios, actas).

Toma candidatas_fabricacion.csv (referencias con forma de artículo que no
aparecen en Crossref ni OpenAlex) y las busca en Semantic Scholar.

Veredictos:
  EXISTE_NO_INDEXADA  la encontró S2 -> no es alucinación; es un trabajo
                      real fuera de Crossref/OpenAlex (revista deslistada,
                      depredadora, repositorio, etc.)
  SOBREVIVIENTE       tampoco aparece en S2 -> candidata fuerte a
                      fabricación; pasa a revisión humana

Uso:
    python3 verificar_candidatas_s2.py candidatas_fabricacion.csv --salida candidatas_s2.csv
    (soporta --continuar y --limite N para pruebas)

API: https://api.semanticscholar.org/graph/v1 (gratuita; sin clave va
limitada, el script respeta 429 con espera y reintento).
"""

import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from verificador import emparejamiento_aceptable, extraer_anio, normalizar

USER_AGENT = "VerificadorReferencias/0.3 (mailto:danielaristo@yahoo.com)"
API_KEY = os.environ.get("S2_API_KEY", "")
if not API_KEY:
    _ruta_clave = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".s2_key")
    if os.path.exists(_ruta_clave):
        API_KEY = open(_ruta_clave).read().strip()
# Con clave: 1 req/s dedicada (acumulada entre endpoints). Sin clave: cuota
# global compartida, ir muy despacio.
PAUSA_SEG = 1.1 if API_KEY else 8.0
PAUSA_INTERNA = 1.1 if API_KEY else 1.5
CAMPOS = ["referencia", "doi_articulo_citante", "veredicto", "titulo_s2",
          "anio_s2", "venue_s2", "ids_s2", "score_titulo"]


def _s2_get(url, max_reintentos=3):
    cabeceras = {"User-Agent": USER_AGENT}
    if API_KEY:
        cabeceras["x-api-key"] = API_KEY
    espera = 20
    for _ in range(max_reintentos):
        req = urllib.request.Request(url, headers=cabeceras)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8")).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code in (429, 403, 503):   # límite de tasa: esperar y reintentar
                time.sleep(espera)
                espera = min(espera * 1.7, 60)
            elif e.code in (400, 404):
                return []
            else:
                time.sleep(10)
        except Exception:
            time.sleep(10)
    return None  # agotó reintentos


CAMPOS_API = "title,year,venue,authors,externalIds"


def s2_match(texto):
    """
    Endpoint /paper/search/match: devuelve el mejor emparejamiento de título
    (cuota separada de /paper/search, suele estar más despejada). 404 = sin match.
    """
    url = ("https://api.semanticscholar.org/graph/v1/paper/search/match?query=%s&fields=%s"
           % (urllib.parse.quote(texto[:300]), CAMPOS_API))
    return _s2_get(url)


def s2_busqueda(texto, filas=5, max_reintentos=3):
    """
    Fallar rápido: pocos reintentos y esperas cortas. Bajo throttling
    sostenido conviene marcar ERROR_API y avanzar; las fallidas se
    repescan en pasadas posteriores con --continuar.
    """
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?query=%s&limit=%d&fields=%s"
           % (urllib.parse.quote(texto[:300]), filas, CAMPOS_API))
    return _s2_get(url, max_reintentos)


def s2_consulta(texto):
    """Intenta primero el endpoint match; si su cuota falla, cae a search."""
    r = s2_match(texto)
    if r is not None:
        return r
    return s2_busqueda(texto)


def segmentos_consulta(ref):
    """
    El buscador de S2 no entiende citas completas (0 resultados): hay que
    consultar con el segmento del título. Se parte la referencia en trozos
    (evitando romper iniciales tipo 'M.N.') y se devuelven los 2 segmentos
    con más tokens significativos — el título casi siempre es uno de ellos.
    """
    partes = re.split(r"(?<![A-Z])\.\s+|:\s+|\?\s+|;\s+|\"\s*", ref)
    cands = []
    for p in partes:
        toks = [t for t in normalizar(p).split() if len(t) > 3]
        if len(toks) >= 4:
            cands.append((len(toks), p.strip()))
    cands.sort(key=lambda x: -x[0])
    return [c[1] for c in cands[:2]] or [ref]


def primer_apellido(cand):
    auts = cand.get("authors") or []
    if not auts:
        return None
    partes = (auts[0].get("name") or "").split()
    return partes[-1] if partes else None


def verificar(ref):
    """Devuelve (veredicto, dict_datos) para una candidata."""
    mejor, mejor_score = None, 0.0
    hubo_respuesta = False
    for consulta in segmentos_consulta(ref):
        resultados = s2_consulta(consulta)
        if resultados is None:
            continue
        hubo_respuesta = True
        for cand in resultados:
            titulo = cand.get("title") or ""
            acepta, s = emparejamiento_aceptable(ref, titulo, primer_apellido(cand))
            if acepta and s > mejor_score:
                mejor, mejor_score = cand, s
        if mejor is not None:
            break  # ya hay match aceptable; no gastar otra consulta
        time.sleep(PAUSA_INTERNA)
    if not hubo_respuesta:
        return "ERROR_API", {}
    if mejor is None:
        return "SOBREVIVIENTE", {"score_titulo": 0.0}
    # cotejo de año igual que la cascada principal (±2)
    anio_ref = extraer_anio(ref)
    anio_s2 = mejor.get("year")
    if anio_ref and anio_s2 and abs(anio_ref - anio_s2) > 2:
        return "SOBREVIVIENTE", {"score_titulo": round(mejor_score, 2)}
    ids = mejor.get("externalIds") or {}
    return "EXISTE_NO_INDEXADA", {
        "titulo_s2": mejor.get("title") or "",
        "anio_s2": anio_s2 or "",
        "venue_s2": mejor.get("venue") or "",
        "ids_s2": ";".join("%s:%s" % kv for kv in list(ids.items())[:3]),
        "score_titulo": round(mejor_score, 2),
    }


def main():
    entrada = sys.argv[1] if len(sys.argv) > 1 else "candidatas_fabricacion.csv"
    salida = "candidatas_s2.csv"
    if "--salida" in sys.argv:
        salida = sys.argv[sys.argv.index("--salida") + 1]
    continuar = "--continuar" in sys.argv
    limite = None
    if "--limite" in sys.argv:
        limite = int(sys.argv[sys.argv.index("--limite") + 1])

    with open(entrada, encoding="utf-8-sig") as f:
        candidatas = list(csv.DictReader(f))
    if limite:
        candidatas = candidatas[:limite]

    ya_hechas = set()
    if continuar and os.path.exists(salida):
        with open(salida, encoding="utf-8-sig") as f:
            for fila in csv.DictReader(f):
                if fila["veredicto"] != "ERROR_API":
                    ya_hechas.add(fila["referencia"])
        print("Retomando: %d ya procesadas" % len(ya_hechas), flush=True)

    pendientes = [c for c in candidatas if c["referencia"] not in ya_hechas]
    print("Verificando %d candidatas contra Semantic Scholar...\n" % len(pendientes), flush=True)

    modo = "a" if (continuar and ya_hechas) else "w"
    conteo = {}
    inicio = time.time()
    with open(salida, modo, newline="", encoding="utf-8-sig") as fsal:
        w = csv.DictWriter(fsal, fieldnames=CAMPOS)
        if modo == "w":
            w.writeheader()
            fsal.flush()
        for i, c in enumerate(pendientes, 1):
            veredicto, datos = verificar(c["referencia"])
            fila = {"referencia": c["referencia"],
                    "doi_articulo_citante": c.get("doi_articulo_citante", ""),
                    "veredicto": veredicto}
            fila.update(datos)
            w.writerow(fila)
            fsal.flush()
            conteo[veredicto] = conteo.get(veredicto, 0) + 1
            if i % 25 == 0 or i == len(pendientes):
                ritmo = (time.time() - inicio) / i
                print("[%d/%d] %s | ~%.0f min restantes"
                      % (i, len(pendientes),
                         " ".join("%s:%d" % kv for kv in sorted(conteo.items())),
                         ritmo * (len(pendientes) - i) / 60), flush=True)
            time.sleep(PAUSA_SEG)

    print("\nRESUMEN:")
    for k, v in sorted(conteo.items(), key=lambda x: -x[1]):
        print("  %-20s %d" % (k, v))
    print("\nGuardado en: %s" % salida)


if __name__ == "__main__":
    main()
