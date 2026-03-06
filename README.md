# 🎬 Subs.ro Provider pentru Bazarr

Provider complet pentru [subs.ro](https://subs.ro) integrat în [Bazarr](https://bazarr.media),
portat **exact** din addon-ul Kodi `service.subtitles.subsro`.

---

## 📋 Cerințe

- Bazarr instalat și funcțional
- Cont pe [subs.ro](https://subs.ro) → cheie API de la **https://subs.ro/api**

---

## 📦 Instalare

### Pasul 1 — Copiază fișierul

**Linux / Docker:**
```bash
cp subsro.py <bazarr>/custom_libs/subliminal_patch/providers/subsro.py
```

**Windows:**
```
C:\Program Files\Bazarr\custom_libs\subliminal_patch\providers\
```

**Synology / NAS:**
```
/volume1/docker/bazarr/custom_libs/subliminal_patch/providers/
```

### Pasul 2 — Înregistrează provider-ul

Editează:
```
<bazarr>/custom_libs/subliminal_patch/providers/__init__.py
```

Adaugă:
```python
from .subsro import SubsRoProvider
```

### Pasul 3 — Restartează Bazarr

```bash
# Linux systemd
sudo systemctl restart bazarr

# Docker
docker restart bazarr
```

### Pasul 4 — Configurare în Bazarr

1. **Settings → Providers → +**
2. Selectează **SubsRo**
3. Completează:
   - **API Key** → de la https://subs.ro/api
   - **Auth Method** → `header` (recomandat) sau `query`
4. **Save** ✅

---

## ⚡ Ce este portat exact din addon-ul Kodi

### 🔍 Căutare (`search_subtitles`)
- Prioritate: **IMDb ID → TMDb ID → Titlu** cu fallback automat
- Pentru seriale construiește `Titlu S01E01` când nu are ID
- Logare `requestId` din meta API pentru depanare
- Salvare în cache **doar dacă `status == 200`**

### 💾 Cache (`load_from_cache` / `save_to_cache`)
- Cheie MD5 din `field:value:language` — exact ca în addon
- Durată: 10 minute (înlocuiește cache-ul pe disk din Kodi)

### 🎯 Matchmaking (`calculate_match_score`)

Portat **linie cu linie**, inclusiv toate regex-urile:

| # | Criteriu | Regex / Logică | Scor |
|---|----------|---------------|------|
| 1 | Episod `S##E##` | `r's(\d+)e(\d+)'` normalizat cu `int()` | +100 / -50 |
| 2 | Rezoluție | `r'(?<![a-z])(2160p\|4320p)(?![a-z0-9])'` etc. | +40 / -30 |
| 3 | Sursă | `bluray/bdrip/brrip/remux`, `web-dl/webrip` etc. | +50 / -20 |
| 4 | Release group | `r'-([a-z0-9]+)(?:\.[a-z0-9]+)?$'` | +30 |
| 5 | Similaritate | `difflib.SequenceMatcher` × 20 | 0–20 |
| 6 | Traducător | `subrip/retail/netflix/hbo/amazon` | +15 |

> **Nota:** Rezoluția e condiționată de `match_resolution` (ca `getSetting('match_resolution')` în addon)

### 🔑 Autentificare (`get_auth`)
- `header` → `X-Subs-Api-Key` (recomandat)
- `query` → `?apiKey=`

### ✅ Validare cheie API (`validate_api_key`)
- Via `GET /quota` la pornire
- 200 → valid | 401 → AuthenticationError | altceva → acceptă (ca în addon)

### 📊 Quota (`check_quota`)
- Verifică la fiecare căutare
- Avertisment în log când quota < **10%**
- Logare `quota_type`, `remaining`, `total`, `used`

### ⚠️ Error handling (`handle_api_error`)
- Citește `message` + `requestId` din schema `ErrorResponse`
- Fallback la mesaje locale pentru 400/401/403/404/429/500

### 🚫 Filtrare HI (`filter_subtitles`)
- Exclude dacă `'hearing' in title.lower()` OR `'sdh' in title.lower()`
- Portat exact — **nu regex**, exact ca în addon

### 📥 Download (`download_subtitle`)
- Endpoint: `GET /subtitle/{id}/download` → ZIP (octet-stream)
- Fallback: `downloadLink` din `SubtitleItem` dacă e prezent
- `id` validat ca `integer` conform schemei

### 🗜️ Extracție arhive ZIP și RAR
- Detectare automată tip arhivă după **magic bytes** (`PK` = ZIP, `Rar!` = RAR)
- Dacă arhiva conține mai multe episoade → alege automat cel mai potrivit prin matchmaking
- Fallback: dacă nu e arhivă, încearcă conținut direct `.srt`
- RAR necesită `pip install rarfile` (opțional — ZIP funcționează fără)

### 🔤 Encoding (`encoding_priority`)
- Lista: `['utf-8', 'iso-8859-2', 'windows-1250', 'latin1']`
- Rotire dinamică prin `encoding_priority` — exact ca în addon
- Fallback absolut: `latin1` cu `errors='ignore'`

---

## 🌍 Limbile suportate

| Limbă | Cod API |
|-------|---------|
| 🇷🇴 Română | `ro` |
| 🇬🇧 Engleză | `en` |
| 🇮🇹 Italiană | `ita` |
| 🇫🇷 Franceză | `fra` |
| 🇩🇪 Germană | `ger` |
| 🇭🇺 Maghiară | `ung` |
| 🇬🇷 Greacă | `gre` |
| 🇵🇹 Portugheză | `por` |
| 🇪🇸 Spaniolă | `spa` |

---

## 🐛 Depanare

**Provider-ul nu apare** → verifică copia fișierului și importul din `__init__.py`

**Eroare 401** → verifică cheia API pe https://subs.ro/api

**Nu găsește subtitrări** → activează debug: `Settings → General → Debug`; caută în log `SubsRo`

---

## 📡 API Reference (OpenAPI 3.0)

| Endpoint | Metodă | Descriere |
|----------|--------|-----------|
| `/search/{field}/{value}` | GET | Caută — field: `imdbid\|tmdbid\|title\|release` |
| `/subtitle/{id}` | GET | Detalii subtitrare |
| `/subtitle/{id}/download` | GET | Descarcă arhiva ZIP |
| `/quota` | GET | Info quota API |

---
### suport 
.[Donează cât dorești]_(https://revolut.me/ionutrevoo).
---

## 📄 Licență

MIT License
