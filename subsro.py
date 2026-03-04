# -*- coding: utf-8 -*-
"""
Subs.ro Provider pentru Bazarr
================================
Portat EXACT din addon-ul Kodi service.subtitles.subsro
API: https://api.subs.ro/v1.0 (OpenAPI 3.0)

Conform template-ului oficial Bazarr (napisy24.py):
  - from __future__ import absolute_import
  - from subzero.language import Language
  - from subliminal.subtitle import fix_line_ending
  - subtitle.content = fix_line_ending(bytes)
  - Provider.initialize() / terminate()
  - list_subtitles() + download_subtitle()

Instalare:
  1. cp subsro.py <bazarr>/custom_libs/subliminal_patch/providers/subsro.py
  2. In providers/__init__.py adauga:
       from .subsro import SubsRoProvider
  3. Restart Bazarr
  4. Settings -> Providers -> + -> SubsRo -> API Key
"""

from __future__ import absolute_import

import difflib
import hashlib
import json
import logging
import os
import re
import time
import zipfile
from io import BytesIO

try:
    import rarfile
    HAS_RARFILE = True
except ImportError:
    HAS_RARFILE = False
    logger_import = logging.getLogger(__name__)
    logger_import.warning('SubsRo: libraria rarfile nu e instalata. Arhivele .rar nu vor fi suportate. Ruleaza: pip install rarfile')

import requests
from requests import Session

from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle
from subliminal.exceptions import AuthenticationError, ProviderError
from subliminal.subtitle import fix_line_ending
from subliminal.video import Episode, Movie
from subzero.language import Language

logger = logging.getLogger(__name__)

API_BASE = 'https://api.subs.ro/v1.0'

# Mapare ISO 639-1 -> enum API subs.ro (conform schemei OpenAPI)
LANG_MAP = {
    'ro': 'ro',
    'en': 'en',
    'it': 'ita',
    'fr': 'fra',
    'de': 'ger',
    'hu': 'ung',
    'el': 'gre',
    'pt': 'por',
    'es': 'spa',
}

# Fallback errors (portat din handle_api_error din addon)
FALLBACK_ERRORS = {
    400: 'Cerere invalida.',
    401: 'Cheie API invalida! Verifica setarile provider-ului.',
    403: 'Acces interzis sau limita de download atinsa.',
    404: 'Subtitrarea nu a fost gasita.',
    429: 'Prea multe cereri! Incearca mai tarziu.',
    500: 'Eroare de server Subs.ro. Revenim imediat.',
}

# Cache in memorie - inlocuieste cache-ul pe disk din addon
# { cache_key: (timestamp, data) }
_MEMORY_CACHE = {}
CACHE_DURATION = 600  # 10 minute in secunde


# ============================================================================
#   CACHE — portat din get_cache_key / load_from_cache / save_to_cache
# ============================================================================

def _get_cache_key(field, value, language='ro'):
    """Genereaza cheie unica MD5 pentru cache. Portat din get_cache_key()."""
    cache_string = '%s:%s:%s' % (field, value, language)
    return hashlib.md5(cache_string.encode('utf-8')).hexdigest()


def _load_from_cache(cache_key):
    """
    Incarca rezultate din cache daca nu au expirat.
    Portat din load_from_cache() — verifica varsta (file_age in addon, time.time() aici).
    """
    entry = _MEMORY_CACHE.get(cache_key)
    if not entry:
        return None
    timestamp, data = entry
    age = time.time() - timestamp
    if age > CACHE_DURATION:
        logger.debug('SubsRo: cache expirat pentru %s', cache_key)
        del _MEMORY_CACHE[cache_key]
        return None
    logger.debug('SubsRo: cache hit pentru %s', cache_key)
    return data


def _save_to_cache(cache_key, data):
    """Salveaza in cache. Portat din save_to_cache()."""
    _MEMORY_CACHE[cache_key] = (time.time(), data)
    logger.debug('SubsRo: salvat in cache: %s', cache_key)


# ============================================================================
#   HANDLE API ERROR — portat exact din handle_api_error() din addon
#   Schema ErrorResponse: { status, message, meta: { requestId } }
# ============================================================================

def _handle_api_error(status_code, response=None):
    """
    Gestioneaza erorile API conform schemei ErrorResponse.
    Incearca sa citeasca 'message' si 'requestId' din body.
    Portat exact din handle_api_error() din addon Kodi.
    """
    api_message = None
    request_id  = None

    if response is not None:
        try:
            err_body    = response.json()
            api_message = err_body.get('message')
            request_id  = err_body.get('meta', {}).get('requestId')
        except Exception:
            pass

    msg = api_message or FALLBACK_ERRORS.get(
        status_code, 'Eroare API necunoscuta (Cod: %d)' % status_code
    )

    if request_id:
        logger.error('SubsRo API error %d | requestId=%s | %s', status_code, request_id, msg)
    else:
        logger.error('SubsRo API error %d | %s', status_code, msg)

    return msg


# ============================================================================
#   MATCHMAKING — portat 1:1 din calculate_match_score() + detect_resolution()
# ============================================================================

def _detect_resolution(name):
    """
    Detecteaza rezolutia dintr-un nume de fisier, evitand coliziuni substring.
    Ordine: de la mai specific la mai general.
    Portat exact (ca functie interna) din calculate_match_score() din addon.
    """
    n = name.lower()
    if re.search(r'(?<![a-z])(2160p|4320p)(?![a-z0-9])', n):
        return '2160p'
    if re.search(r'(?<![a-z])4k(?![a-z0-9])', n):
        return '2160p'
    if re.search(r'(?<![a-z])uhd(?![a-z0-9])', n):
        return '2160p'
    if re.search(r'(?<![a-z])(1080p|1080i|fhd)(?![a-z0-9])', n):
        return '1080p'
    if re.search(r'(?<![a-z])720p(?![a-z0-9])', n):
        return '720p'
    if re.search(r'(?<![a-z])480p(?![a-z0-9])', n):
        return '480p'
    return None


def calculate_match_score(subtitle_name, video_file, match_resolution=True):
    """
    Calculeaza scorul de potrivire intre subtitrare si video.
    Portat 1:1 din calculate_match_score() din addon Kodi.
    match_resolution corespunde setarii ADDON.getSetting('match_resolution').
    Returneaza: (score: int, details: dict)
    """
    score   = 0
    details = {}

    sub_lower   = subtitle_name.lower()
    video_lower = os.path.basename(video_file).lower()

    # 1. Detectare episod — normalizat cu int() pentru a ignora zero-padding
    #    Acopera: s05e05, s5e5, s05e5, s5e05 etc.
    episode_pattern = r's(\d+)e(\d+)'
    sub_match   = re.search(episode_pattern, sub_lower)
    video_match = re.search(episode_pattern, video_lower)

    if sub_match and video_match:
        sub_ep   = (int(sub_match.group(1)),   int(sub_match.group(2)))
        video_ep = (int(video_match.group(1)), int(video_match.group(2)))
        if sub_ep == video_ep:
            score += 100
            details['episode_match'] = True
        else:
            score -= 50
            details['episode_match'] = False

    # 2. Rezolutie — conditionata de match_resolution (ca in addon cu getSetting)
    if match_resolution:
        video_res = _detect_resolution(video_lower)
        sub_res   = _detect_resolution(sub_lower)

        if video_res and sub_res:
            if video_res == sub_res:
                score += 40
                details['resolution_match'] = True
            else:
                score -= 30
                details['resolution_match'] = False
        details['video_resolution'] = video_res or 'unknown'
        details['sub_resolution']   = sub_res   or 'unknown'

    # 3. Sursa (BluRay, WEB-DL, HDTV)
    sources = {
        'bluray': ['bluray', 'bdrip', 'brrip', 'remux'],
        'web':    ['web-dl', 'webrip', 'webdl', 'amzn', 'nf', 'netflix'],
        'hdtv':   ['hdtv', 'pdtv'],
    }

    video_source = None
    sub_source   = None

    for src_type, keywords in sources.items():
        if any(k in video_lower for k in keywords):
            video_source = src_type
        if any(k in sub_lower for k in keywords):
            sub_source = src_type

    if video_source and sub_source:
        if video_source == sub_source:
            score += 50
            details['source_match'] = True
        else:
            score -= 20
            # Nota: addon-ul NU seteaza details['source_match'] = False cand diferita
            # Portat exact asa

    # 4. Release group
    video_group = re.search(r'-([a-z0-9]+)(?:\.[a-z0-9]+)?$', video_lower)
    sub_group   = re.search(r'-([a-z0-9]+)(?:\.[a-z0-9]+)?$', sub_lower)

    if video_group and sub_group:
        if video_group.group(1) == sub_group.group(1):
            score += 30
            details['group_match'] = True

    # 5. Similaritate generala (difflib) — exact ca in addon
    video_name = os.path.splitext(os.path.basename(video_file))[0].lower()
    sub_name   = os.path.splitext(subtitle_name)[0].lower()
    similarity = difflib.SequenceMatcher(None, video_name, sub_name).ratio()
    score += int(similarity * 20)
    details['similarity'] = similarity  # fara round() — exact ca in addon

    # 6. Traducator prioritar
    priority_translators = ['subrip', 'retail', 'netflix', 'hbo', 'amazon']
    if any(t in sub_lower for t in priority_translators):
        score += 15
        details['priority_translator'] = True

    return score, details


def _filter_subtitles_hi(subtitles):
    """
    Filtreaza hearing impaired.
    Portat exact din filter_subtitles() din addon:
      'hearing' not in title.lower() AND 'sdh' not in title.lower()
    (Nu regex — exact ca in addon)
    """
    filtered = [
        s for s in subtitles
        if 'hearing' not in s.title.lower()
        and 'sdh' not in s.title.lower()
    ]
    logger.debug('SubsRo: dupa filtrare HI: %d subtitrari', len(filtered))
    return filtered


def _pick_best_file(namelist, reference_name):
    """
    Alege cel mai bun .srt/.ass dintr-o lista de fisiere prin matchmaking.
    Portat din logica multi_episode_handling == '2' din addon Kodi.
    """
    srts = sorted([f for f in namelist if f.lower().endswith(('.srt', '.ass'))])
    if not srts:
        return None

    if len(srts) == 1:
        return srts[0]

    best_srt   = srts[0]
    best_score = -999

    for srt in srts:
        score, _ = calculate_match_score(os.path.basename(srt), reference_name)
        if score > best_score:
            best_score = score
            best_srt   = srt

    logger.debug(
        'SubsRo: ales "%s" (Scor: %+d) din %d fisiere.',
        os.path.basename(best_srt), best_score, len(srts),
    )
    return best_srt


def _extract_from_archive(content, reference_name=''):
    """
    Extrage .srt/.ass din arhiva ZIP sau RAR.
    Detecteaza tipul dupa magic bytes:
      ZIP: primii 2 bytes = b'PK'
      RAR: primii 4 bytes = b'Rar!'
    Daca nu e arhiva, incearca sa returneze continutul direct (.srt nearhivat).
    """
    is_zip = content[:2] == b'PK'
    is_rar = content[:4] == b'Rar!'

    if is_zip:
        try:
            with zipfile.ZipFile(BytesIO(content)) as z:
                f_name = _pick_best_file(z.namelist(), reference_name)
                if not f_name:
                    raise ProviderError('SubsRo: arhiva ZIP nu contine fisiere .srt/.ass')
                logger.debug('SubsRo: extrage din ZIP: %s', f_name)
                return z.read(f_name)
        except zipfile.BadZipFile as e:
            raise ProviderError('SubsRo: arhiva ZIP invalida: %s' % e)

    elif is_rar:
        if not HAS_RARFILE:
            raise ProviderError(
                'SubsRo: arhiva este RAR dar libraria rarfile nu e instalata. '
                'Ruleaza: pip install rarfile'
            )
        try:
            with rarfile.RarFile(BytesIO(content)) as rf:
                f_name = _pick_best_file(rf.namelist(), reference_name)
                if not f_name:
                    raise ProviderError('SubsRo: arhiva RAR nu contine fisiere .srt/.ass')
                logger.debug('SubsRo: extrage din RAR: %s', f_name)
                return rf.read(f_name)
        except rarfile.Error as e:
            raise ProviderError('SubsRo: arhiva RAR invalida: %s' % e)

    else:
        # Poate fi direct .srt nearhivat
        logger.debug('SubsRo: continut non-arhiva, incerc direct ca .srt')
        return content


def _decode_content(raw, encoding_priority=0):
    """
    Detecteaza encoding si decodifica subtitrarea.
    Portat exact din blocul encoding din download_subtitle() din addon:
      encodings = ['utf-8', 'iso-8859-2', 'windows-1250', 'latin1']
      daca encoding_priority > 0: roteste lista
    Returneaza bytes UTF-8.
    """
    encodings = ['utf-8', 'iso-8859-2', 'windows-1250', 'latin1']

    if encoding_priority > 0:
        # Roteste lista conform prioritatii — exact ca in addon
        encodings = encodings[encoding_priority:] + encodings[:encoding_priority]

    text = None
    for enc in encodings:
        try:
            text = raw.decode(enc)
            logger.debug('SubsRo: encoding detectat: %s', enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue

    if not text:
        text = raw.decode('latin1', errors='ignore')

    return text.encode('utf-8')


# ============================================================================
#   SUBTITLE CLASS
# ============================================================================

class SubsRoSubtitle(Subtitle):
    """Subs.ro Subtitle."""

    provider_name = 'subsro'

    def __init__(self, language, item, video_name='', match_resolution=True):
        super(SubsRoSubtitle, self).__init__(language)

        # Toate campurile din SubtitleItem (schema OpenAPI subs.ro)
        self.subtitle_id   = int(item.get('id', 0))        # integer
        self.title         = item.get('title', '')
        self.year          = item.get('year')               # integer
        self.imdb_id       = item.get('imdbid', '')         # string cu/fara prefix 'tt'
        self.tmdb_id       = item.get('tmdbid')             # integer
        self.translator    = item.get('translator', '')
        self.content_type  = item.get('type', '')           # movie | series
        self.download_link = item.get('downloadLink', '')   # URL direct din schema
        self.page_link     = item.get('link', '')           # URL pagina subtitrare
        self.description   = item.get('description', '')
        self.release_info  = item.get('title', '')
        self.created_at    = item.get('createdAt', '')
        self.updated_at    = item.get('updatedAt', '')
        self.poster        = item.get('poster', '')
        self.item_language = item.get('language', '')

        # Scor matchmaking calculat local (portat din sort_subtitles_by_match)
        self.match_score, self.match_details = calculate_match_score(
            self.title, video_name, match_resolution=match_resolution
        )

    @property
    def id(self):
        return str(self.subtitle_id)

    def get_matches(self, video):
        """Returneaza matches pentru sistemul de scoring Subliminal/Bazarr."""
        matches = set()

        if isinstance(video, Episode):
            if video.series and video.series.lower() in self.title.lower():
                matches.add('series')
            if video.season and video.episode:
                # Folosim acelasi pattern S##E## ca in addon
                ep_str = 's%02de%02d' % (video.season, video.episode)
                if ep_str in self.title.lower():
                    matches.add('season')
                    matches.add('episode')

        elif isinstance(video, Movie):
            if video.title and video.title.lower() in self.title.lower():
                matches.add('title')
            if video.year and self.year:
                try:
                    if int(self.year) == video.year:
                        matches.add('year')
                except (ValueError, TypeError):
                    pass

        # IMDb — normalizam 'tt' prefix (ca in addon: imdbid.startswith('tt'))
        if self.imdb_id and hasattr(video, 'imdb_id') and video.imdb_id:
            norm_sub   = str(self.imdb_id).lstrip('tt')
            norm_video = str(video.imdb_id).lstrip('tt')
            if norm_sub and norm_sub == norm_video:
                matches.add('imdb_id')

        # Din matchmaking local
        if self.match_details.get('resolution_match'):
            matches.add('resolution')
        if self.match_details.get('source_match'):
            matches.add('source')
        if self.match_details.get('group_match'):
            matches.add('release_group')

        logger.debug(
            'SubsRo subtitle "%s" matches=%s score=%+d',
            self.title[:60], matches, self.match_score,
        )
        return matches


# ============================================================================
#   PROVIDER CLASS
# ============================================================================

class SubsRoProvider(Provider):
    """Subs.ro Provider pentru Bazarr."""

    provider_name  = 'subsro'
    subtitle_class = SubsRoSubtitle

    # Limbile suportate — conform enum-ului din schema OpenAPI subs.ro
    languages = {
        Language('ron'),  # ro
        Language('eng'),  # en
        Language('ita'),
        Language('fra'),
        Language('deu'),  # ger -> deu in ISO 639-3
        Language('hun'),  # ung -> hun
        Language('ell'),  # gre -> ell
        Language('por'),
        Language('spa'),
    }

    video_types = (Episode, Movie)

    def __init__(self, api_key='', auth_method='header', match_resolution=True,
                 encoding_priority=0, filter_hearing_impaired=True):
        """
        :param api_key:                  Cheia API de la https://subs.ro/api
        :param auth_method:              'header' (X-Subs-Api-Key) sau 'query' (?apiKey=)
        :param match_resolution:         Activeaza matching rezolutie (ca match_resolution din addon)
        :param encoding_priority:        0-3, roteste lista de encodings (ca encoding_priority din addon)
        :param filter_hearing_impaired:  Filtreaza SDH/HI (ca filter_by_hearing_impaired din addon)
        """
        if not api_key or not api_key.strip():
            raise AuthenticationError('Cheia API pentru subs.ro este obligatorie.')

        self.api_key                 = api_key.strip()
        self.auth_method             = auth_method
        self.match_resolution        = match_resolution
        self.encoding_priority       = int(encoding_priority)
        self.filter_hearing_impaired = filter_hearing_impaired
        self.session                 = None
        self._api_validated          = False

    # ------------------------------------------------------------------
    # Auth — portat din get_auth() din addon
    # Schema OpenAPI: ApiKeyHeader (X-Subs-Api-Key) sau ApiKeyQuery (?apiKey=)
    # ------------------------------------------------------------------

    def _get_auth(self):
        """
        Returneaza (headers, params_extra).
        Portat exact din get_auth() din addon:
          auth_method '0'/'header' -> X-Subs-Api-Key
          auth_method '1'/'query'  -> ?apiKey=
        """
        if self.auth_method in ('query', '1'):
            return {'Accept': 'application/json'}, {'apiKey': self.api_key}
        return {'X-Subs-Api-Key': self.api_key, 'Accept': 'application/json'}, {}

    def _get(self, path, params=None, binary=False):
        """GET autentificat cu error handling conform schemei ErrorResponse."""
        headers, extra = self._get_auth()
        all_params = dict(params or {})
        all_params.update(extra)

        try:
            response = self.session.get(
                '%s%s' % (API_BASE, path),
                headers=headers,
                params=all_params,
                timeout=15,
            )
        except requests.exceptions.Timeout:
            raise ProviderError('SubsRo: timeout la cerere %s' % path)
        except requests.exceptions.ConnectionError as e:
            raise ProviderError('SubsRo: eroare conexiune: %s' % e)

        if response.status_code != 200:
            msg = _handle_api_error(response.status_code, response)
            if response.status_code == 401:
                raise AuthenticationError(msg)
            raise ProviderError(msg)

        return response.content if binary else response.json()

    # ------------------------------------------------------------------
    # Validate API Key — portat din validate_api_key() din addon
    # Endpoint: GET /quota (conform schemei OpenAPI)
    # ------------------------------------------------------------------

    def _validate_api_key(self):
        """
        Valideaza cheia API via GET /quota.
        Portat din validate_api_key() din addon:
          - 200 -> valid
          - 401 -> invalid, ridica AuthenticationError
          - altceva / exceptie -> acceptam cheia (ca in addon)
        Schema QuotaResponse: { status, meta, quota: { total_quota, used_quota,
                                remaining_quota, quota_type, ip_address, api_key } }
        """
        try:
            headers, extra = self._get_auth()
            response = self.session.get(
                '%s/quota' % API_BASE,
                headers=headers,
                params=extra,
                timeout=5,
            )
            if response.status_code == 200:
                data  = response.json()
                quota = data.get('quota', {})
                logger.info(
                    'SubsRo: cheie API valida | quota_type=%s | remaining=%s/%s',
                    quota.get('quota_type', '?'),
                    quota.get('remaining_quota', '?'),
                    quota.get('total_quota', '?'),
                )
                self._api_validated = True
            elif response.status_code == 401:
                logger.error('SubsRo: cheie API invalida (401)')
                raise AuthenticationError('Cheie API subs.ro invalida (401).')
            else:
                logger.error('SubsRo: eroare validare API: %d', response.status_code)
                # Acceptam cheia la erori necunoscute (ca in addon)
                self._api_validated = True
        except AuthenticationError:
            raise
        except Exception as e:
            # Acceptam cheia daca avem probleme de conexiune (ca in addon)
            logger.warning('SubsRo: nu s-a putut valida cheia API: %s', e)
            self._api_validated = True

    # ------------------------------------------------------------------
    # Check Quota — portat din check_quota() din addon
    # ------------------------------------------------------------------

    def _check_quota(self):
        """
        Verifica quota si logheaza avertisment daca e sub 10%.
        Portat din check_quota() din addon.
        Schema QuotaInfo: total_quota, used_quota, remaining_quota, quota_type
        """
        try:
            headers, extra = self._get_auth()
            response = self.session.get(
                '%s/quota' % API_BASE,
                headers=headers,
                params=extra,
                timeout=5,
            )
            if response.status_code == 200:
                data       = response.json()
                quota_info = data.get('quota', {})

                total      = quota_info.get('total_quota', 0)
                used       = quota_info.get('used_quota', 0)
                remaining  = quota_info.get('remaining_quota', 0)
                quota_type = quota_info.get('quota_type', 'unknown')

                logger.debug(
                    'SubsRo quota (%s): %d/%d (folosit: %d)',
                    quota_type, remaining, total, used,
                )

                # Avertisment sub 10% — exact ca in addon
                if total > 0 and remaining < (total * 0.1):
                    logger.warning(
                        'SubsRo: AVERTISMENT quota scazuta! Ramasa: %d/%d cereri',
                        remaining, total,
                    )
            elif response.status_code == 401:
                logger.error('SubsRo: quota check - cheie API invalida (401)')
                _handle_api_error(401, response)
        except Exception as e:
            logger.debug('SubsRo: eroare verificare quota: %s', e)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        self.session = Session()
        # Validare API key la prima utilizare (ca in search_subtitles din addon)
        if not self._api_validated:
            self._validate_api_key()

    def terminate(self):
        if self.session:
            self.session.close()
            self.session = None

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _lang_code(self, language):
        """Converteste Language Subliminal -> enum API subs.ro."""
        return LANG_MAP.get(language.alpha2, 'ro')

    def _build_query(self, video):
        """
        Determina field + value pentru cautare.
        Schema searchField enum: imdbid | tmdbid | title | release
        Prioritate portata din search_subtitles() din addon:
          imdbid (startswith 'tt') -> tmdbid (isdigit > 0) -> title
        """
        imdb_id = getattr(video, 'imdb_id', None)
        if imdb_id:
            raw = str(imdb_id)
            # Addon verifica startswith('tt') — normalizam
            if not raw.startswith('tt'):
                raw = 'tt' + raw
            return 'imdbid', raw

        tmdb_id = getattr(video, 'tmdb_id', None)
        if tmdb_id and str(tmdb_id).isdigit() and int(tmdb_id) > 0:
            return 'tmdbid', str(tmdb_id)

        # Fallback title — exact ca in addon:
        # tvshow + S##E## daca serial, altfel title
        if isinstance(video, Episode):
            value = '%s S%sE%s' % (
                video.series,
                str(video.season).zfill(2),
                str(video.episode).zfill(2),
            )
        else:
            value = video.title

        return 'title', value

    # ------------------------------------------------------------------
    # Search — portat din search_subtitles() din addon
    # GET /search/{searchField}/{value}?language=...
    # Schema SearchResponse: { status, meta:{requestId}, count, items:[SubtitleItem] }
    # ------------------------------------------------------------------

    def query(self, video, language):
        """Cauta subtitrari cu cache, quota check si matchmaking."""

        # Verifica quota periodic (ca in addon cu check_quota)
        self._check_quota()

        field, value = self._build_query(video)
        lang_code    = self._lang_code(language)

        logger.debug('SubsRo query: field=%s value=%r lang=%s', field, value, lang_code)

        # Cache — portat din load_from_cache / save_to_cache
        cache_key   = _get_cache_key(field, value, lang_code)
        cached_data = _load_from_cache(cache_key)

        if cached_data:
            logger.debug('SubsRo: folosesc date din cache')
            data = cached_data
        else:
            try:
                data = self._get(
                    '/search/%s/%s' % (
                        field,
                        requests.utils.quote(str(value), safe=''),
                    ),
                    params={'language': lang_code},
                )

                # Logam requestId din meta — exact ca in addon
                request_id = data.get('meta', {}).get('requestId', '')
                logger.debug(
                    'SubsRo raspuns API: status=%s count=%d requestId=%s',
                    data.get('status'), data.get('count', 0), request_id,
                )

                # Salvam in cache doar daca status == 200 (ca in addon)
                if data.get('status') == 200:
                    _save_to_cache(cache_key, data)

            except ProviderError as e:
                logger.warning('SubsRo: eroare cautare: %s', e)
                return []

        if data.get('status') != 200:
            return []

        items = data.get('items', [])
        count = data.get('count', len(items))
        logger.debug('SubsRo: total subtitrari gasite: %d', count)

        if not items:
            return []

        video_name = getattr(video, 'name', '') or ''

        # Creare obiecte subtitle cu scor matchmaking
        subtitles = [
            SubsRoSubtitle(
                language, item, video_name,
                match_resolution=self.match_resolution,
            )
            for item in items
        ]

        # Filtrare hearing impaired — portat exact din filter_subtitles() din addon
        if self.filter_hearing_impaired:
            subtitles = _filter_subtitles_hi(subtitles)

        # Sortare dupa scor — portat din sort_subtitles_by_match() din addon
        subtitles.sort(key=lambda s: s.match_score, reverse=True)

        # Log top 3 — ca in addon
        if subtitles:
            logger.debug('SubsRo top 3 potriviri:')
            for i, s in enumerate(subtitles[:3]):
                logger.debug(
                    '  #%d (Scor: %+d): %s', i + 1, s.match_score, s.title[:60]
                )

        return subtitles

    def list_subtitles(self, video, languages):
        """Intrare standard Bazarr/Subliminal."""
        subtitles = []
        for language in languages:
            if language not in self.languages:
                continue
            subtitles.extend(self.query(video, language))
        return subtitles

    # ------------------------------------------------------------------
    # Download — portat din download_subtitle() din addon
    # GET /subtitle/{id}/download -> application/octet-stream (ZIP)
    # Fallback: downloadLink din SubtitleItem
    # ------------------------------------------------------------------

    def download_subtitle(self, subtitle):
        """
        Descarca arhiva ZIP si extrage .srt/.ass cu cel mai bun scor.
        Portat din download_subtitle() din addon Kodi.

        Endpoint primar:  GET /subtitle/{id}/download -> application/octet-stream
        Fallback:         downloadLink din SubtitleItem daca e furnizat de API
        Schema: {id} este integer.
        """
        headers, extra_params = self._get_auth()
        # Endpoint-ul returneaza binar (octet-stream), nu JSON
        # Portat exact: headers.pop('Accept', None) din addon
        headers.pop('Accept', None)

        # id trebuie sa fie integer conform schemei — portat din addon
        try:
            sub_id_int = int(subtitle.subtitle_id)
        except (TypeError, ValueError):
            raise ProviderError('SubsRo: ID subtitrare invalid: %s' % subtitle.subtitle_id)

        # Alege URL: downloadLink din SubtitleItem sau endpoint standard
        # Portat exact din addon: daca download_link -> foloseste el, altfel construieste URL
        if subtitle.download_link:
            url = subtitle.download_link
            logger.debug('SubsRo: download via downloadLink din SubtitleItem: %s', url)
        else:
            url = '%s/subtitle/%d/download' % (API_BASE, sub_id_int)
            logger.debug('SubsRo: download via endpoint standard: %s', url)

        try:
            r = self.session.get(url, headers=headers, params=extra_params, timeout=15)
        except requests.exceptions.RequestException as e:
            raise ProviderError('SubsRo: eroare download: %s' % e)

        if r.status_code != 200:
            msg = _handle_api_error(r.status_code, r)
            raise ProviderError(msg)

        content = r.content

        # Extrage din arhiva ZIP sau RAR
        raw = _extract_from_archive(content, reference_name=subtitle.title)

        # Detectare encoding cu prioritate configurabila — portat exact din addon
        decoded = _decode_content(raw, encoding_priority=self.encoding_priority)

        # fix_line_ending — conform template-ului oficial Bazarr (napisy24.py)
        subtitle.content = fix_line_ending(decoded)
