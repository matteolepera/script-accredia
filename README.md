# Accredia certificates downloader

Specifica tecnica per lo scaricamento e l'aggiornamento della banca dati
pubblica delle organizzazioni/aziende con sistema di gestione certificato.

> Stato: progettazione. La repository contiene la specifica concordata; il
> downloader JSON verrà implementato e testato su Windows nel passaggio
> successivo.

## Obiettivo

Il programma deve:

1. utilizzare una sessione Playwright già validata;
2. aprire la pagina contenente tutti i risultati;
3. leggere il totale riportato dalla pagina;
4. calcolare il numero di pagine considerando 20 risultati per pagina;
5. ciclare il parametro `page` dell'URL;
6. individuare i certificati con il selettore CSS
   `div.ppsearch > table`;
7. trasformare ogni tabella in un documento JSON distinto;
8. permettere la ripresa di uno scaricamento interrotto;
9. riconoscere record nuovi, modificati, invariati e non più presenti;
10. produrre un controllo finale di completezza.

Il CAPTCHA non viene risolto o aggirato dal programma. Se la sessione salvata
non è più valida, il processo si arresta e richiede una nuova verifica manuale.

## Fonte

Pagina iniziale:

```text
https://services.accredia.it/ppsearch/accredia_companymask_remote.jsp?ID_LINK=1739&area=310
```

La pagina dei risultati utilizza la stessa JSP e contiene il parametro
zero-based `page`:

```text
...&page=0&submit=Cerca
...&page=1&submit=Cerca
...&page=2&submit=Cerca
```

Il programma deve conservare tutti i parametri della query ottenuta dopo la
ricerca e modificare esclusivamente `page`. Non deve ricostruire manualmente
l'intero URL, perché potrebbero essere presenti parametri o filtri aggiuntivi.

## Conteggio e paginazione

Il totale viene letto dal testo `Risultati:`. Il valore può contenere il punto
come separatore delle migliaia:

```text
Risultati: 369.135
```

Normalizzazione:

```text
"369.135" -> 369135
```

Numero di pagine:

```text
total_pages = ceil(total_results / 20)
```

Con 369.135 risultati:

```text
total_pages = 18.457
URL page     = 0 ... 18.456
ultima pagina = 15 risultati
```

Le cartelle sul filesystem sono numerate da 1, senza riempimento con zeri:

```text
URL page=0 -> pages/1/
URL page=1 -> pages/2/
```

Nel JSON vengono conservati sia l'indice URL zero-based sia il numero pagina
one-based.

## Individuazione dei certificati

Il contenitore dei risultati è:

```css
div.ppsearch
```

Ogni certificato/sede è rappresentato da una tabella figlia diretta:

```css
div.ppsearch > table
```

Per evitare di interpretare tabelle decorative o inattese come certificati, una
tabella viene accettata soltanto se il testo contiene anche il marcatore:

```text
N.Certificato
```

Validazione prevista:

- 20 tabelle in ogni pagina completa;
- `total_results % 20` tabelle nell'ultima pagina;
- almeno numero certificato, organismo, azienda e norma in ogni tabella;
- nessuna pagina viene marcata come completata se una tabella non è stata
  interpretata correttamente.

Se il numero di tabelle è inatteso, la risposta HTML viene conservata nella
cartella degli errori e la pagina viene ritentata. Non si continua ignorando il
problema.

## Significato di un record

Un risultato rappresenta una combinazione certificato/sede. Lo stesso numero
di certificato può comparire più volte quando copre più sedi, filiali o
stabilimenti.

Di conseguenza:

- la partita IVA non identifica un singolo risultato;
- il numero di certificato da solo potrebbe non essere globalmente univoco;
- pagina e posizione non sono identificatori stabili, perché possono cambiare
  dopo un aggiornamento della banca dati.

## Identificatori stabili

Sono previsti due identificatori.

### `certificate_id`

Identifica il certificato indipendentemente dalla sede:

```text
SHA-256(
  codice_organismo |
  numero_certificato_normalizzato |
  partita_iva_o_codice_fiscale |
  norma_normalizzata
)
```

Il codice organismo viene estratto dal parametro
`PPSEARCH_ORG_SEARCH_MASK_ORG` del link Accredia. Se partita IVA e codice
fiscale non sono presenti, viene usata come fallback la ragione sociale
normalizzata.

### `record_id`

Identifica la singola combinazione certificato/sede:

```text
SHA-256(
  certificate_id |
  tipo_sede_normalizzato |
  indirizzo_sede_normalizzato
)
```

Il digest completo viene scritto sia nel JSON sia nel nome file. Essendo
composto soltanto da caratteri esadecimali minuscoli, è utilizzabile senza
conversioni anche su Windows.

Se nella stessa esecuzione vengono trovate due tabelle con lo stesso
`record_id`, entrambe vengono segnalate come duplicati. Non vengono
sovrascritte silenziosamente.

## Nome file consigliato

```text
cert-<record_id>.json
```

Esempio:

```text
documenti/accredia/pages/1/cert-9f15c9f1c7d6321de8229c0a5f1a11e4978397b249ab2e44e6fa82b5411a467.json
```

La pagina e la posizione sono contenute nel documento JSON:

```json
{
  "source": {
    "url_page": 0,
    "page": 1,
    "position": 1
  }
}
```

Questa scelta evita collisioni senza perdere l'informazione `p1-1`.

## Struttura delle directory

Radice configurata:

```text
documenti/accredia/
```

Struttura prevista:

```text
documenti/accredia/
├── pages/
│   ├── 1/
│   │   ├── cert-<record-id>.json
│   │   └── ...
│   ├── 2/
│   │   └── ...
│   └── 18457/
│       └── ...
├── state/
│   ├── current-run.json
│   ├── pages.json
│   └── records-index.jsonl
├── runs/
│   └── <run-id>.json
├── archive/
│   └── ...
└── errors/
    ├── pages/
    └── records/
```

Poiché i nomi delle cartelle non sono riempiti con zeri, il programma non deve
mai affidarsi all'ordinamento alfabetico del filesystem (`1, 10, 100, 2`). Le
directory vengono ordinate convertendo sempre il nome in numero intero.

## Compatibilità Windows

Lo script deve essere sviluppato e verificato su Windows. Tutti i percorsi devono
essere costruiti con `pathlib.Path`; non devono contenere separatori `/` o `\\`
scritti manualmente nella logica applicativa.

Percorso relativo predefinito:

```text
documenti\accredia\pages\1\cert-<record_id>.json
```

Requisiti per nomi e percorsi:

- `record_id` usa soltanto cifre esadecimali minuscole;
- nessun nome contiene `:`, `*`, `?`, virgolette o altri caratteri vietati da
  Windows;
- non vengono usati nomi riservati come `CON`, `PRN`, `AUX`, `NUL`, `COM1` o
  `LPT1`;
- il percorso rimane volutamente poco profondo per evitare problemi con
  applicazioni Windows che non gestiscono correttamente i percorsi lunghi;
- tutti i JSON sono scritti in UTF-8 con `ensure_ascii=False`;
- i confronti dei percorsi non devono dipendere dalla distinzione tra maiuscole
  e minuscole del filesystem.

Prima di sostituire un file esistente, tutti gli handle devono essere chiusi.
La scrittura avviene in un file temporaneo nella stessa cartella e la
sostituzione usa `Path.replace()`. Su Windows l'operazione può fallire
temporaneamente se antivirus, indicizzazione o backup mantengono il file
aperto: sono quindi previsti retry brevi anche per le operazioni locali.

Con 18.457 cartelle e circa 20 JSON per cartella, la distribuzione evita di
concentrare 369.135 file nella stessa directory e rimane gestibile su NTFS.

## Schema JSON di ogni tabella

Ogni file contiene sia i campi strutturati sia una copia del contenuto sorgente
della tabella. Questo permette di correggere il parser in futuro senza dover
scaricare nuovamente la pagina.

```json
{
  "schema_version": 1,
  "record_id": "9f15c9f1c7d6321de8229c0a5f1a...",
  "certificate_id": "34f81d13e72101aa7f11b392ce90...",
  "certificate_number": "ABC-123",
  "issued_on": "2025-03-04",
  "status": "in corso di validità",
  "accreditation_body": {
    "code": "0895",
    "name": "APAVE CERTIFICATION ITALIA S.r.l.",
    "detail_url": "https://services.accredia.it/ppsearch/accredia_orgmask.jsp?ID_LINK=1739&area=310&PPSEARCH_ORG_SEARCH_MASK_ORG=0895",
    "website": "https://italy.apave.com/it-IT"
  },
  "company": {
    "name": "012 FACTORY S.P.A. SOCIETA' BENEFIT",
    "vat_or_tax_code": "04019110610",
    "site": {
      "type": "Sede Legale e Operativa",
      "address": "VIALE CARLO III DI BORBONE, 8",
      "postal_code": "81100",
      "city": "CASERTA",
      "province": "CE",
      "region": "Campania",
      "raw": "Sede Legale e Operativa - VIALE CARLO III DI BORBONE, 8 - 81100 - CASERTA ( CE ) - Campania"
    }
  },
  "scope": "Misure per garantire la parità di genere...",
  "standard": "UNI/PdR 125:2022",
  "accreditation_scheme": "SGQ",
  "sectors": [],
  "updated_on": "2026-08-03",
  "source": {
    "url": "https://services.accredia.it/ppsearch/accredia_companymask_remote.jsp?...&page=0",
    "url_page": 0,
    "page": 1,
    "position": 1,
    "scraped_at": "2026-08-05T12:00:00+02:00"
  },
  "raw_text": "Contenuto testuale completo e normalizzato della tabella",
  "raw_html": "<table>...</table>",
  "content_hash": "sha256-dei-campi-semantici"
}
```

### Regole dei campi

- Le date strutturate sono salvate come `YYYY-MM-DD`.
- Il testo originale rimane disponibile in `raw_text` e `raw_html`.
- `sectors` è sempre una lista, anche quando è vuota.
- Gli URL sono convertiti in URL assoluti.
- Le stringhe vengono ripulite dagli spazi ripetuti, ma non ne viene alterato il
  contenuto semantico.
- `vat_or_tax_code` conserva il valore pubblicato senza assumere che sia sempre
  una partita IVA italiana.

## `content_hash`

`content_hash` serve a capire se un record già noto è cambiato.

Viene calcolato sui campi semantici normalizzati:

- numero certificato;
- data di emissione;
- stato;
- organismo e relativi URL;
- ragione sociale;
- partita IVA/codice fiscale;
- sede;
- scopo;
- norma;
- schema;
- settori;
- data di aggiornamento.

Sono esclusi dal calcolo:

- pagina;
- posizione;
- URL della pagina risultati;
- data e ora dello scraping;
- formattazione HTML.

In questo modo lo spostamento di un risultato da una pagina all'altra non viene
considerato una modifica del certificato.

## Indice dei record

`state/records-index.jsonl` contiene una riga per ogni `record_id` conosciuto:

```json
{"record_id":"...","content_hash":"...","path":"pages/1/cert-....json","first_seen_run":"...","last_seen_run":"...","missing_runs":0}
```

L'indice permette di verificare rapidamente se un risultato è:

- `new`: `record_id` mai visto;
- `unchanged`: stesso `record_id` e stesso `content_hash`;
- `updated`: stesso `record_id`, ma `content_hash` differente;
- `moved`: stesso `record_id` e stesso contenuto, ma pagina differente;
- `missing`: presente nell'indice, ma non incontrato nell'esecuzione completa;
- `removed`: assente per più esecuzioni complete consecutive.

JSONL è preferito a un unico enorme oggetto JSON perché può essere letto e
riscritto in streaming.

## Gestione degli aggiornamenti

La banca dati non espone nella maschera un filtro affidabile per la data di
ultimo aggiornamento. Per rilevare tutte le modifiche è quindi necessario
rileggere le pagine e confrontare identificatori e hash.

Per ogni tabella scaricata:

1. calcolare `certificate_id` e `record_id`;
2. cercare `record_id` nell'indice;
3. calcolare `content_hash`;
4. se il record è invariato, non riscrivere il contenuto;
5. se è cambiato, aggiornare atomicamente il JSON;
6. se è nuovo, creare il JSON;
7. se si è spostato, aggiornare percorso e metadati;
8. impostare `last_seen_run` all'esecuzione corrente.

Al termine di un'esecuzione completa:

1. i record non incontrati vengono marcati `missing`;
2. non vengono eliminati immediatamente;
3. dopo una seconda esecuzione completa che conferma l'assenza, possono essere
   spostati in `archive/` e marcati `removed`;
4. viene scritto un report con nuovi, modificati, spostati, mancanti e rimossi.

La doppia verifica evita di eliminare dati a causa di una pagina temporaneamente
incompleta o di modifiche avvenute mentre lo scraping era in corso.

### Conseguenze degli spostamenti di pagina

Se un record passa da `pages/10/` a `pages/11/`, il file può essere
spostato senza riscriverne il contenuto. L'operazione viene eseguita soltanto
dopo aver completato e validato la pagina di destinazione.

Durante un'esecuzione incompleta la directory può contenere temporaneamente
record appartenenti allo snapshot precedente. `state/current-run.json` indica
che il processo non è ancora concluso. La pulizia dei file non più visti viene
effettuata soltanto dopo il completamento dell'intero ciclo.

## Ripresa di uno scaricamento interrotto

`state/current-run.json` registra almeno:

```json
{
  "run_id": "20260805T120000+0200",
  "status": "running",
  "total_results": 369135,
  "total_pages": 18457,
  "last_completed_url_page": 120,
  "written_records": 2420,
  "new_records": 0,
  "updated_records": 0,
  "failed_pages": []
}
```

Una pagina viene marcata come completata soltanto quando:

- la risposta è valida;
- il numero di tabelle è quello atteso;
- tutte le tabelle sono state trasformate in JSON;
- tutti i file sono stati scritti atomicamente;
- l'indice è stato aggiornato.

Alla ripartenza si riprende dalla prima pagina non completata. Se nel frattempo
il totale dei risultati è cambiato, l'esecuzione precedente viene chiusa come
incompleta e viene avviato un nuovo ciclo, senza cancellare i dati esistenti.

## Scrittura atomica

Ogni JSON viene scritto prima in un file temporaneo nella stessa directory:

```text
cert-<id>.json.tmp
```

Dopo la serializzazione e la validazione viene rinominato in:

```text
cert-<id>.json
```

La rinomina sullo stesso filesystem è atomica e impedisce di lasciare file JSON
troncati in caso di interruzione.

Lo stesso principio si applica agli indici e ai manifesti.

## Controlli della sessione

Una risposta `HTTP 200` non garantisce che la pagina contenga risultati. Quando
la sessione scade, Accredia può restituire la pagina di avviso con stato 200.

Ogni risposta deve quindi essere verificata cercando:

- assenza di `Verifica reCAPTCHA richiesta`;
- presenza di `div.ppsearch`;
- presenza del totale nella prima pagina;
- presenza delle tabelle attese.

Se la sessione non è valida, il processo si arresta senza marcare la pagina come
completata. Dopo una nuova verifica manuale può riprendere dalla stessa pagina.

## Retry e velocità

Configurazione iniziale prudente:

- una richiesta alla volta;
- almeno 2 secondi tra le pagine;
- timeout di 60 secondi;
- massimo 3 retry;
- backoff esponenziale per `429`, `500`, `502`, `503` e `504`;
- interruzione sugli errori di parsing ripetuti.

Dopo un benchmark su un numero limitato di pagine si potrà valutare una
concorrenza massima di 2 o 3 richieste. La concorrenza non deve compromettere la
sessione, la completezza o la stabilità del servizio remoto.

## Report di fine esecuzione

Ogni ciclo completo produce `runs/<run-id>.json`:

```json
{
  "run_id": "20260805T120000+0200",
  "status": "completed",
  "started_at": "2026-08-05T12:00:00+02:00",
  "completed_at": "2026-08-05T22:30:00+02:00",
  "total_results_start": 369135,
  "total_results_end": 369135,
  "expected_pages": 18457,
  "completed_pages": 18457,
  "json_files": 369135,
  "new_records": 120,
  "updated_records": 34,
  "unchanged_records": 368981,
  "moved_records": 41,
  "missing_records": 41,
  "removed_records": 0,
  "duplicate_record_ids": 0,
  "parse_errors": 0,
  "failed_pages": []
}
```

L'esecuzione è considerata completa soltanto se:

```text
completed_pages == expected_pages
json_files + duplicate_record_ids == total_results_end
parse_errors == 0
failed_pages == []
```

Una variazione tra `total_results_start` e `total_results_end` viene segnalata e
può richiedere un secondo passaggio per ottenere uno snapshot coerente.

## Ambiente di sviluppo previsto

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Se PowerShell impedisce l'attivazione dello script per la propria execution
policy, è possibile usare direttamente l'interprete dell'ambiente virtuale:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Per impostazione predefinita viene utilizzato Google Chrome installato sul
sistema. In alternativa:

```powershell
python -m playwright install chromium
```

## Aspetti ancora da implementare

- parsing basato su `div.ppsearch > table`;
- schema JSON descritto sopra;
- struttura `documenti/accredia/pages/`;
- `certificate_id`, `record_id` e `content_hash`;
- indice JSONL;
- manifest di avanzamento;
- scrittura atomica;
- riconoscimento di record nuovi, modificati e spostati;
- archiviazione dopo doppia conferma dell'assenza;
- report finale e controlli di completezza;
- test su una pagina reale dopo la validazione della sessione.