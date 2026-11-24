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
  - Si la referencia trae DOI y el DOI NO existe, se busca la obra por título y
    autor (la cita sin el DOI): si aparece, METADATOS DUDOSOS con la explicación
    (errata de escritura si difiere en <=3 caracteres, o DOI erróneo/inventado);
    si no aparece, NO VERIFICABLE.
  - Si la referencia trae DOI y el DOI existe, también se comprueba que el
    título registrado aparezca en la cita (>=0.50 de sus palabras
    significativas; si tiene menos de 3, deben estar todas); si no,
    METADATOS DUDOSOS: el DOI pertenece a otra obra.

Veredictos:
  VERIFICADA        existe y los metadatos coinciden
  METADATOS DUDOSOS existe pero el año citado no corresponde
  RETRACTADA        existe pero fue retractada
  NO VERIFICABLE    no aparece en Crossref ni OpenAlex
                    (posible alucinación… o literatura gris sin indexar)
  NO COMPROBADA     una fuente no respondió (límite de tasa, conexión): NO es un
                    veredicto sobre la referencia; --continuar la reintenta

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
UMBRAL_TITULO_DOI = 0.50  # con DOI: fracción mínima de palabras del título REGISTRADO presentes en la cita
ESTADO_ERROR = "NO COMPROBADA"   # una fuente no respondió: NO es un veredicto sobre la referencia
DETALLE_ERROR = ("Una fuente (Crossref u OpenAlex) no respondió (límite de tasa o problema de "
                 "conexión). No es un veredicto sobre la referencia: reintente más tarde")


# ----------------------------------------------------------------------
# Utilidades HTTP y de texto
# ----------------------------------------------------------------------

def fetch_json_estado(url, timeout=30, reintentos=2):
    """
    GET con User-Agent identificado. Respuesta de tres vías, para no confundir
    "la fuente dice que no existe" con "la fuente no respondió":
      ("ok", datos)          la fuente respondió
      ("noencontrado", None) HTTP 404: registro inexistente (hallazgo definitivo)
      ("error", None)        límite de tasa, error del servidor o falla de conexión
    """
    for intento in range(reintentos + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return ("ok", json.loads(r.read().decode("utf-8")))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return ("noencontrado", None)
            if intento < reintentos:
                time.sleep(3 * (intento + 1))
        except Exception:
            if intento < reintentos:
                time.sleep(3 * (intento + 1))
    return ("error", None)


def fetch_json(url, timeout=30, reintentos=2):
    """Forma antigua: devuelve los datos, o None ante cualquier cosa que no sea una respuesta limpia."""
    estado, datos = fetch_json_estado(url, timeout, reintentos)
    return datos if estado == "ok" else None


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
    """('ok', registro) | ('noencontrado', None) | ('error', None)."""
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    estado, datos = fetch_json_estado(url)
    return (estado, datos.get("message")) if estado == "ok" else (estado, None)


def crossref_busqueda(referencia, filas=3):
    """Lista de candidatos (vacía = confirmado que no hay nada), o None si la consulta falló."""
    url = ("https://api.crossref.org/works?query.bibliographic=%s&rows=%d"
           "&select=title,issued,DOI,author&mailto=%s"
           % (urllib.parse.quote(referencia), filas, MAILTO))
    estado, datos = fetch_json_estado(url)
    if estado == "error":
        return None
    if estado != "ok":
        return []
    return datos.get("message", {}).get("items", [])


def openalex_busqueda(texto, filas=5):
    """Búsqueda general en OpenAlex: lista de dicts crudos (vacía = nada), o None si la consulta falló."""
    url = ("https://api.openalex.org/works?search=%s&per-page=%d"
           "&select=display_name,publication_year,doi,is_retracted,authorships&mailto=%s"
           % (urllib.parse.quote(texto), filas, MAILTO))
    estado, datos = fetch_json_estado(url)
    if estado == "error":
        return None
    if estado != "ok":
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
    candidatos = openalex_busqueda(consulta, filas=5)
    if candidatos is None:
        return None          # la consulta falló: estado de retracción desconocido
    for cand in candidatos:
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


def buscar_mejor(referencia):
    """
    Busca la obra citada por título y autor: Crossref primero y, si no hay un
    emparejamiento aceptable, OpenAlex como respaldo.
    Devuelve (mejor, mejor_score, mejor_score_bruto, fallo), donde `mejor` es
    (titulo, anio, doi, apellido, fuente, retractada_directa) o None, y `fallo`
    indica que alguna fuente consultada no respondió (entonces "no hay
    coincidencia" no es un hallazgo).
    """
    mejor, mejor_score, mejor_score_bruto = None, 0.0, 0.0
    fallo = False
    candidatos = crossref_busqueda(referencia)
    if candidatos is None:
        fallo, candidatos = True, []
    for item in candidatos:
        titulo, anio, doi, apellido = campos_item(item)
        acepta, s = emparejamiento_aceptable(referencia, titulo, apellido)
        mejor_score_bruto = max(mejor_score_bruto, s)
        if acepta and s > mejor_score:
            mejor, mejor_score = (titulo, anio, doi, apellido, "Crossref", None), s

    if mejor is None:
        candidatos_oa = openalex_busqueda(referencia)
        if candidatos_oa is None:
            fallo, candidatos_oa = True, []
        for cand in candidatos_oa:
            titulo = cand.get("display_name") or ""
            anio = cand.get("publication_year")
            doi = (cand.get("doi") or "").replace("https://doi.org/", "")
            apellido = openalex_apellido(cand)
            acepta, s = emparejamiento_aceptable(referencia, titulo, apellido)
            mejor_score_bruto = max(mejor_score_bruto, s)
            if acepta and s > mejor_score:
                mejor, mejor_score = (titulo, anio, doi, apellido, "OpenAlex",
                                      bool(cand.get("is_retracted"))), s
    return mejor, mejor_score, mejor_score_bruto, fallo


def distancia_edicion(a, b):
    """Distancia de Levenshtein entre dos cadenas (sin distinguir mayúsculas)."""
    a, b = a.lower(), b.lower()
    previa = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        actual = [i]
        for j, cb in enumerate(b, 1):
            actual.append(min(previa[j] + 1, actual[j - 1] + 1, previa[j - 1] + (ca != cb)))
        previa = actual
    return previa[-1]


UMBRAL_DOI_TIPOGRAFICO = 3   # hasta tantos caracteres de diferencia: probable error de escritura


def quitar_doi(referencia, doi):
    """La cita sin su DOI (ni el prefijo doi.org / 'doi:'), para buscar la obra solo por título y autor."""
    texto = referencia.replace(doi, " ")
    texto = re.sub(r"(?i)\b(?:https?://)?(?:dx\.)?doi\.org/?", " ", texto)
    texto = re.sub(r"(?i)\bdoi\s*:?\s*", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def nota_doi_inexistente(doi_citado, doi_real, titulo, apellido, anio):
    """Explica que el DOI citado no existe pero la obra sí, y si parece una errata o un DOI inventado."""
    base = ("El DOI citado (%s) no existe, pero la obra sí: «%s» (%s, %s). "
            % (doi_citado, titulo, apellido or "s. a.", anio or "s. f."))
    if not doi_real:
        return base + "Esa obra no tiene DOI en los registros consultados: verifíquese el DOI a mano"
    d = distancia_edicion(doi_citado, doi_real)
    if d <= UMBRAL_DOI_TIPOGRAFICO:
        return base + ("Su DOI es %s: el citado difiere en %d carácter(es), probable error de escritura"
                       % (doi_real, d))
    return base + ("Su DOI es %s: el citado no se parece a ese, así que es erróneo o inventado" % doi_real)


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
    nota_doi = ""      # se llena si el DOI citado no existe pero la obra sí

    # --- Caso 1: la referencia trae DOI -> verificación directa en Crossref
    if doi_en_ref:
        estado_doi, item = crossref_por_doi(doi_en_ref)
        if estado_doi == "error":
            resultado["estado"] = ESTADO_ERROR
            resultado["detalle"] = DETALLE_ERROR
            return resultado
        if item is None:
            # El DOI citado no existe. Antes de concluir nada, se busca la obra por título
            # y autor (la cita sin el DOI): si aparece, el problema es el DOI (errata o
            # inventado), no la obra.
            mejor, mejor_score, mejor_score_bruto, fallo = buscar_mejor(quitar_doi(referencia, doi_en_ref))
            if mejor is None and fallo:
                resultado["estado"] = ESTADO_ERROR
                resultado["detalle"] = DETALLE_ERROR
                return resultado
            if mejor is None:
                resultado["estado"] = "NO VERIFICABLE"
                resultado["score_titulo"] = round(mejor_score_bruto, 2)
                resultado["detalle"] = ("Ni el DOI citado ni el título aparecen en Crossref ni OpenAlex: "
                                        "posible referencia fabricada, o literatura gris sin indexar")
                return resultado
            titulo, anio, doi, apellido, fuente, retractada_directa = mejor
            resultado.update(fuente=fuente, doi=doi or "", titulo_encontrado=titulo,
                             autor_encontrado=apellido or "", anio_encontrado=anio)
            resultado["score_titulo"] = round(mejor_score, 2)
            nota_doi = nota_doi_inexistente(doi_en_ref, doi, titulo, apellido, anio)
            if retractada_directa:
                resultado["retractada"] = "SI"
                resultado["estado"] = "RETRACTADA"
                resultado["detalle"] = nota_doi + ". OpenAlex marca este trabajo como retractado"
                return resultado
        else:
            titulo, anio, doi, apellido = campos_item(item)
            resultado.update(fuente="Crossref", doi=doi, titulo_encontrado=titulo,
                             autor_encontrado=apellido or "", anio_encontrado=anio)
            resultado["score_titulo"] = round(score_titulo(referencia, titulo), 2)
            # El DOI existe, pero ¿es la obra que dice la cita? Un DOI real pegado a un
            # título inventado es una alucinación frecuente: se compara el título
            # registrado con el citado. Si el título registrado es muy corto (<3 palabras
            # significativas) se exige que estén todas, como en la búsqueda sin DOI.
            n_tok = len(tokens_significativos(titulo))
            minimo = UMBRAL_TITULO_DOI if n_tok >= MIN_TOKENS else 0.99
            if n_tok >= 1 and resultado["score_titulo"] < minimo:
                resultado["estado"] = "METADATOS DUDOSOS"
                resultado["detalle"] = (
                    "El DOI existe pero está registrado a otra obra: «%s» (%s, %s). "
                    "Solo %d %% de las palabras clave de ese título aparecen en la cita: "
                    "posible título inventado con un DOI real, o título traducido/abreviado; "
                    "verifíquese a mano" % (titulo, apellido or "s. a.", anio or "s. f.",
                                            round(resultado["score_titulo"] * 100)))
                return resultado
    else:
        # --- Caso 2: sin DOI -> búsqueda por título y autor (Crossref, luego OpenAlex)
        mejor, mejor_score, mejor_score_bruto, fallo = buscar_mejor(referencia)

        if mejor is None and fallo:
            # "No encontrada" solo es un hallazgo si todas las fuentes consultadas respondieron.
            resultado["estado"] = ESTADO_ERROR
            resultado["detalle"] = DETALLE_ERROR
            return resultado

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
    retractada, incierto = False, False
    if resultado["doi"]:
        r = openalex_retractado_por_doi(resultado["doi"])
        if r is None:
            incierto = True
        else:
            retractada = r
    if not retractada and resultado["titulo_encontrado"]:
        r = openalex_retractado_por_titulo(resultado["titulo_encontrado"],
                                           resultado["anio_encontrado"])
        if r is None:
            incierto = True
        elif r:
            retractada = True
    if retractada:
        resultado["retractada"] = "SI"
        resultado["estado"] = "RETRACTADA"
        resultado["detalle"] = ("OpenAlex marca este trabajo como retractado "
                                "(detectado vía cotejo de duplicados)" if resultado["doi"] else
                                "OpenAlex marca este trabajo como retractado")
    elif incierto:
        # Nunca dar por "limpia" una comprobación que falló.
        resultado["retractada"] = "NO COMPROBADA"
        resultado["detalle"] = ((resultado["detalle"] + ". " if resultado["detalle"] else "") +
                                "No se pudo confirmar el estado de retracción (la fuente no respondió): "
                                "verifíquese a mano antes de confiar en esta referencia")
    else:
        resultado["retractada"] = "NO"

    # --- El DOI citado no existía, pero la obra sí: queda como METADATOS DUDOSOS
    #     (salvo que esté retractada, que es más grave) y con la explicación delante.
    if nota_doi:
        if resultado["estado"] == "VERIFICADA":
            resultado["estado"] = "METADATOS DUDOSOS"
        resultado["detalle"] = nota_doi + (". " + resultado["detalle"] if resultado["detalle"] else "")

    return resultado


# ----------------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------------

ICONOS = {
    "VERIFICADA": "✅",
    "NO VERIFICABLE": "❌",
    "METADATOS DUDOSOS": "⚠️ ",
    "RETRACTADA": "🚫",
    "NO COMPROBADA": "🔁",
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
    # Las filas "NO COMPROBADA" (una fuente no respondió) no cuentan como hechas: se reintentan.
    ya_hechas, filas_previas, a_reintentar = set(), [], 0
    if continuar and os.path.exists(salida):
        with open(salida, encoding="utf-8-sig") as f:
            for fila in csv.DictReader(f):
                if fila["estado"] == ESTADO_ERROR:
                    a_reintentar += 1
                else:
                    filas_previas.append(fila)
                    ya_hechas.add(fila["referencia"])
        print("Retomando: %d referencias ya procesadas en %s%s" % (
            len(ya_hechas), salida,
            (" (+ %d no comprobadas, se reintentan)" % a_reintentar) if a_reintentar else ""))

    pendientes = [r for r in referencias if r not in ya_hechas]
    print("Verificando %d referencias (%d en total)...\n" % (len(pendientes), len(referencias)))

    modo = "w"   # se reescribe el reporte conservando las filas ya buenas, sin duplicar las reintentadas
    conteo = {}
    candado = threading.Lock()

    def procesar(ref):
        r = verificar_referencia(ref)
        if pausa:
            time.sleep(pausa)
        return r

    with open(salida, modo, newline="", encoding="utf-8-sig") as fsal:
        w = csv.DictWriter(fsal, fieldnames=CAMPOS_CSV, extrasaction="ignore")
        w.writeheader()
        for fila in filas_previas:
            w.writerow(fila)
        fsal.flush()
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
    for estado in ["VERIFICADA", "METADATOS DUDOSOS", "NO VERIFICABLE", "RETRACTADA", ESTADO_ERROR]:
        if conteo.get(estado):
            print("  %s %-20s %d" % (ICONOS[estado], estado, conteo[estado]))
    print("=" * 60)
    if conteo.get(ESTADO_ERROR):
        print("\n%d referencias NO se pudieron comprobar (una fuente no respondió); no son un veredicto."
              % conteo[ESTADO_ERROR])
        print("Ejecute de nuevo con --continuar para reintentar solo esas.")
    print("\nReporte guardado en: %s" % salida)


if __name__ == "__main__":
    main()
