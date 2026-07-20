# Reference Integrity Checker

A free, open tool to verify the integrity of bibliographic references at scale — and the research pipeline behind an ongoing scientometric study on fabricated ("hallucinated") and unverifiable references in the scholarly literature.

**Try it live: [reftruth.com](https://reftruth.com)** — no installation, no account, runs entirely in your browser.

## What it does

Given a plain-text list of references (one per line), the checker verifies each one in cascade against open scholarly databases:

1. **Does it exist?** Crossref first (DOI lookup or bibliographic search); OpenAlex as fallback; Semantic Scholar as the final layer for content not indexed by the first two (e.g., delisted journals).
2. **Do the metadata match?** Title-token matching, first-author surname check, and year tolerance of ±2 (to absorb online-first vs. print date offsets).
3. **Is it retracted?** Double check via OpenAlex: by DOI *and* by title — because duplicate DOI registrations can carry the retraction flag on only one record (real case: Wakefield 1998, *The Lancet*, where the commonly matched DOI variant is not flagged).

### Verdicts

| Verdict | Meaning |
|---|---|
| `VERIFICADA` | Exists and metadata match |
| `METADATOS DUDOSOS` | Exists, but the cited year does not match the registered one |
| `RETRACTADA` | Exists but has been retracted |
| `NO VERIFICABLE` | Not found in any source — possible fabrication, *or* legitimate gray literature |

A key design principle: **"not found" is not the same as "fabricated."** Unverifiable references are a mixture of hallucinations, gray literature (books, reports, theses, standards, patents), and real papers published in venues outside the mainstream indexes (including delisted/predatory journals). Separating those populations is the core methodological problem this project addresses.

## Usage

```bash
# Basic run
python3 verificador.py references.txt --salida report.csv

# Large batches: parallel, quiet, resumable
python3 verificador.py references.txt --salida report.csv --hilos 8 --silencioso --continuar
```

Second-stage verification of "article-shaped" unverifiable references against Semantic Scholar:

```bash
# Optional: export a free S2 API key for a dedicated rate limit
export S2_API_KEY=your_key
python3 verificar_candidatas_s2.py candidatas.csv --salida candidatas_s2.csv --continuar
```

No dependencies beyond the Python 3 standard library. All APIs used (Crossref, OpenAlex, Semantic Scholar) are free and require no key (a free Semantic Scholar key is recommended for batch work).

## Pilot results (July 2026)

5,000 unresolved references (reference strings that Crossref's own citation matching could not link to a DOI), sampled from ~1,200 randomly selected 2024 journal articles with open reference lists:

| Outcome | Share |
|---|---|
| Verified (rescued by this pipeline) | 25.2% |
| Metadata mismatch (cited year off by >2) | 12.3% |
| Not verifiable | 62.5% |

Within the unverifiable set, 16.5% are "article-shaped" candidates requiring second-stage verification; manual spot-checks show that some of these are real papers published in journals delisted from major indexes — a population that naive hallucination detectors misclassify as fabricated.

## Data files

- `referencias_prueba.txt` — synthetic test set (real, fabricated, wrong-year, and retracted references)
- `muestra_real_2024.txt`, `muestra_piloto_5000.txt`, `muestra_piloto_procedencia.csv` — real unresolved references sampled from Crossref (2024), with citing-article provenance
- `reporte_piloto_5000.csv` — full pilot output
- `candidatas_fabricacion.csv` — article-shaped unverifiable candidates from the pilot

## Roadmap

- [ ] Semantic Scholar layer over all pilot candidates (in progress)
- [ ] Public benchmark set with human-annotated labels
- [ ] Static web app (client-side only — Crossref and OpenAlex support CORS)
- [ ] Large-scale prevalence study across disciplines and years

## Author

Daniel Aristizábal Torres — independent researcher, scientometrics and research integrity.

## License

MIT — see [LICENSE](LICENSE).
