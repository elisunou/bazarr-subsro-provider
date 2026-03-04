# 🎬 Subs.ro Provider pentru Bazarr

Provider complet pentru [subs.ro](https://subs.ro) integrat în [Bazarr](https://bazarr.media),
portat din addon-ul Kodi `service.subtitles.subsro`.

---

## 📋 Cerințe

- Bazarr instalat și funcțional
- Cont pe [subs.ro](https://subs.ro) cu cheie API
  - Obții cheia de la: **https://subs.ro/api**

---

## 📦 Instalare

### Pasul 1 — Descarcă fișierul

Descarcă `subsro.py` din acest repo sau clonează repo-ul:

```bash
git clone https://github.com/<username>/bazarr-subsro-provider
```

### Pasul 2 — Copiază fișierul în Bazarr

Găsește directorul de instalare Bazarr și copiază fișierul:

**Linux / Docker:**
```bash
cp subsro.py /path/to/bazarr/custom_libs/subliminal_patch/providers/subsro.py
```

**Windows:**
```
Copiază subsro.py în:
C:\Program Files\Bazarr\custom_libs\subliminal_patch\providers\
```

**Synology / NAS:**
```
/volume1/docker/bazarr/custom_libs/subliminal_patch/providers/
```

> 💡 **Cum găsești directorul Bazarr?**
> În interfața Bazarr mergi la **Settings → General** și verifică calea de instalare.

### Pasul 3 — Înregistrează provider-ul

Deschide fișierul:
```
<bazarr>/custom_libs/subliminal_patch/providers/__init__.py
```

Adaugă la sfârșitul listei de importuri:
```python
from .subsro import SubsRoProvider
```

### Pasul 4 — Restartează Bazarr

**Linux / systemd:**
```bash
sudo systemctl restart bazarr
```

**Docker:**
```bash
docker restart bazarr
```

**Windows:**
Repornește serviciul Bazarr din Services sau repornește aplicația.

### Pasul 5 — Configurare în interfața Bazarr

1. Deschide Bazarr în browser
2. Mergi la **Settings → Providers**
3. Apasă **+** pentru a adăuga un provider nou
4. Selectează **SubsRo** din listă
5. Completează:
   - **API Key** → cheia de la https://subs.ro/api
   - **Auth Method** → `header` (recomandat) sau `query`
6. Apasă **Save**
7. Gata! ✅

---

## 🌍 Limbile suportate

| Limbă | Cod |
|-------|-----|
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

## ⚡ Caracteristici

### Matchmaking avansat
Algoritmul calculează un scor de potrivire pentru fiecare subtitrare:

| Criteriu | Scor |
|----------|------|
| Episod identic `S##E##` | +100 |
| Episod diferit | -50 |
| Rezoluție identică (1080p, 4K etc.) | +40 |
| Rezoluție diferită | -30 |
| Sursă identică (BluRay, WEB-DL, HDTV) | +50 |
| Sursă diferită | -20 |
| Release group identic | +30 |
| Similaritate generală (difflib) | 0 → +20 |
| Traducător prioritar (Netflix, HBO etc.) | +15 |

### Alte caracteristici
- 🔑 **Autentificare dublă**: header `X-Subs-Api-Key` sau query `?apiKey=`
- 🔍 **Căutare inteligentă**: IMDb ID → TMDb ID → titlu (fallback automat)
- 📥 **Download robust**: endpoint standard + fallback `downloadLink`
- 🗜️ **ZIP cu mai multe episoade**: alege automat cel mai potrivit fișier
- 🔤 **Detectare encoding**: UTF-8 → ISO-8859-2 → Windows-1250 → Latin1
- 🚫 **Filtrare SDH/HI**: exclude subtitrările hearing impaired

---

## 🐛 Depanare

### Provider-ul nu apare în listă
- Verifică că ai copiat corect fișierul în directorul `providers/`
- Verifică că ai adăugat importul în `__init__.py`
- Restartează Bazarr și verifică log-urile

### Eroare 401 - Cheie API invalidă
- Verifică cheia pe https://subs.ro/api
- Asigură-te că nu are spații la început/sfârșit
- Generează o cheie nouă dacă e necesar

### Nu găsește subtitrări
- Activează debug log în Bazarr: **Settings → General → Debug**
- Verifică log-urile pentru mesaje de la `SubsRo`
- Verifică quota rămasă pe https://subs.ro/api

### Activare debug log Bazarr
`Settings → General → Debug` → activează → restartează Bazarr

---

## 📡 API Reference

- **Base URL**: `https://api.subs.ro/v1.0`
- **Autentificare**: header `X-Subs-Api-Key` sau query `?apiKey=`
- **Căutare**: `GET /search/{imdbid|tmdbid|title}/{value}?language=ro`
- **Download**: `GET /subtitle/{id}/download` → ZIP (octet-stream)
- **Quota**: `GET /quota`

---

## 📄 Licență

MIT License — folosește liber, modifică, distribuie.
