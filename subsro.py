# -*- coding: utf-8 -*-
"""
Subs.ro provider pentru Bazarr
Portat din addon-ul Kodi service.subtitles.subsro
API: https://api.subs.ro/v1.0  (OpenAPI 3.0)

Structură conformă cu template-ul oficial Bazarr:
  https://github.com/morpheus65535/bazarr/blob/master/custom_libs/subliminal_patch/providers/napisy24.py

Instalare:
  cp subsro.py <bazarr>/custom_libs/subliminal_patch/providers/subsro.py
  Adaugă în providers/__init__.py:
    from .subsro import SubsRoProvider
"""

from __future__ import absolute_import

import difflib
import logging
import os
import re
import zipfile
from io import BytesIO

import requests
from requests import Session

from subliminal_patch.providers import Provider
from subliminal_patch.subtitle import Subtitle
from subliminal.exceptions import AuthenticationError, ProviderError
from subliminal.subtitle import fix_line_ending
from subliminal.video import Episode, Movie
from subzero.language import Language

logger = logging.getLogger(__name__)

API_BASE = "https://api.subs.ro/v1.0"

# Mapare ISO 639-1 → enum API subs.ro
LANG_MAP = {
    "ro": "ro",
    "en": "en",
    "it": "ita",
    "fr": "fra",
    "de": "ger",
    "hu": "ung",
    "el": "gre",
    "pt": "por",
    "es": "spa",
}


# ============================================================================
#                    MATCHMAKING (portat 1:1 din addon Kodi)
# ============================================================================

def detect_resolution(name: str):
    """Detectează rezoluția dintr-un nume de fișier."""
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


def calculate_match_score(subtitle_name: str, video_name: str) -> tuple:
    """
    Calculează scorul de potrivire între subtitrare și video.
    Portat 1:1 din addon-ul Kodi (calculate_match_score).
    Returnează: (score: int, details: dict)
    """
    score = 0
    details = {}

    sub_lower   = subtitle_name.lower()
    video_lower = os.path.basename(video_name).lower()

    # 1. Potrivire episod S##E## (normalizat cu int – ignoră zero-padding)
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

    # 2. Rezoluție (+40 identică, -30 diferită)
    video_res = detect_resolution(video_lower)
    sub_res   = detect_resolution(sub_lower)
    if video_res and sub_res:
        if video_res == sub_res:
            score += 40
            details['resolution_match'] = True
        else:
            score -= 30
            details['resolution_match'] = False
    details['video_resolution'] = video_res or 'unknown'
    details['sub_resolution']   = sub_res   or 'unknown'

    # 3. Sursă (BluRay / WEB / HDTV)
    sources = {
        'bluray': ['bluray', 'bdrip', 'brrip', 'remux'],
        'web':    ['web-dl', 'webrip', 'webdl', 'amzn', 'nf', 'netflix'],
        'hdtv':   ['hdtv', 'pdtv'],
    }
    video_source = next(
        (st for st, kws in sources.items() if any(k in video_lower for k in kws)), None
    )
    sub_source = next(
        (st for st, kws in sources.items() if any(k in sub_lower for k in kws)), None
    )
    if video_source and sub_source:
        if video_source == sub_source:
            score += 50
            details['source_match'] = True
        else:
            score -= 20
            details['source_match'] = False

    # 4. Release group
    video_group = re.search(r'-([a-z0-9]+)(?:\.[a-z0-9]+)?$', video_lower)
    sub_group   = re.search(r'-([a-z0-9]+)(?:\.[a-z0-9]+)?$', sub_lower)
    if video_group and sub_group:
        if video_group.group(1) == sub_group.group(1):
            score += 30
            details['group_match'] = True

    # 5. Similaritate generală (difflib)
    video_stem = os.path.splitext(os.path.basename(video_name))[0].lower()
    sub_stem   = os.path.splitext(subtitle_name)[0].lower()
    similarity = difflib.SequenceMatcher(None, video_stem, sub_stem).ratio()
    score += int(similarity * 20)
    details['similarity'] = round(similarity, 3)

    # 6. Traducător prioritar
    priority_translators = ['subrip', 'retail', 'netflix', 'hbo', 'amazon']
    if any(t in sub_lower for t in priority_translators):
        score += 15
        details['priority_translator'] = True

    return score, details


# ============================================================================
#                              SUBTITLE CLASS
# ============================================================================

class SubsRoSubtitle(Subtitle):
    """Subs.ro Subtitle."""

    provider_name = 'subsro'

    def __init__(self, language, item: dict, video_name: str = ""):
        super(SubsRoSubtitle, self).__init__(language)

        # Câmpuri din SubtitleItem (schema OpenAPI subs.ro)
        self.subtitle_id   = int(item.get('id', 0))
        self.title         = item.get('title', '')
        self.year          = item.get('year')
        self.imdb_id       = item.get('imdbid', '')
        self.tmdb_id       = item.get('tmdbid')
        self.translator    = item.get('translator', '')
        self.content_type  = item.get('type', '')       # movie | series
        self.download_link = item.get('downloadLink', '')
        self.release_info  = item.get('title', '')

        # Scor matchmaking calculat local
        self.match_score, self.match_details = calculate_match_score(
            self.title, video_name
        )

    @property
    def id(self):
        return str(self.subtitle_id)

    def get_matches(self, video):
        """Returnează matches conform sistemului Subliminal/Bazarr."""
        matches = set()

        if isinstance(video, Episode):
            if video.series and video.series.lower() in self.title.lower():
                matches.add('series')
            if video.season and video.episode:
                ep_str = 's%02de%02d' % (video.season, video.episode)
                if ep_str in self.title.lower():
                    matches.add('season')
                    matches.add('episode')
        elif isinstance(video, Movie):
            if video.title and video.title.lower() in self.title.lower():
                matches.add('title')
            if video.year and self.year and int(self.year) == video.year:
                matches.add('year')

        # IMDb
        if self.imdb_id and hasattr(video, 'imdb_id') and video.imdb_id:
            norm_sub   = self.imdb_id.lstrip('tt')
            norm_video = str(video.imdb_id).lstrip('tt')
            if norm_sub == norm_video:
                matches.add('imdb_id')

        # Rezoluție și sursă din matchmaking local
        if self.match_details.get('resolution_match'):
            matches.add('resolution')
        if self.match_details.get('source_match'):
            matches.add('source')
        if self.match_details.get('group_match'):
            matches.add('release_group')

        logger.debug(
            'SubsRo subtitle "%s" matches=%s score=%+d',
            self.title[:60], matches, self.match_score
        )
        return matches


# ============================================================================
#                              PROVIDER CLASS
# ============================================================================

class SubsRoProvider(Provider):
    """Subs.ro Provider pentru Bazarr."""

    provider_name = 'subsro'
    subtitle_class = SubsRoSubtitle

    # Limbile suportate de subs.ro
    languages = {
        Language('ron'),  # ro
        Language('eng'),  # en
        Language('ita'),
        Language('fra'),
        Language('deu'),  # ger
        Language('hun'),  # ung
        Language('ell'),  # gre
        Language('por'),
        Language('spa'),
    }

    video_types = (Episode, Movie)

    def __init__(self, api_key='', auth_method='header'):
        """
        :param api_key:     Cheia API de la https://subs.ro/api
        :param auth_method: 'header' (X-Subs-Api-Key) sau 'query' (?apiKey=)
        """
        if not api_key:
            raise AuthenticationError('Cheia API pentru subs.ro este obligatorie.')
        self.api_key     = api_key.strip()
        self.auth_method = auth_method
        self.session     = None

    # ------------------------------------------------------------------
    # Auth helpers (portat din get_auth() din addon Kodi)
    # ------------------------------------------------------------------

    def _get_auth(self):
        """
        Returnează (headers, params_extra) conform schemei OpenAPI:
          - ApiKeyHeader: X-Subs-Api-Key
          - ApiKeyQuery:  ?apiKey=
        """
        if self.auth_method == 'query':
            return {'Accept': 'application/json'}, {'apiKey': self.api_key}
        return {'X-Subs-Api-Key': self.api_key, 'Accept': 'application/json'}, {}

    def _get(self, path, params=None, binary=False):
        """GET autentificat. Ridică excepții la erori API."""
        headers, extra = self._get_auth()
        all_params = dict(params or {})
        all_params.update(extra)

        response = self.session.get(
            '%s%s' % (API_BASE, path),
            headers=headers,
            params=all_params,
            timeout=15,
        )

        if response.status_code == 401:
            raise AuthenticationError('Cheie API subs.ro invalidă (401).')
        if response.status_code == 404:
            raise ProviderError('Resursa nu a fost găsită: %s' % path)
        if response.status_code != 200:
            raise ProviderError('Eroare API subs.ro: %d' % response.status_code)

        return response.content if binary else response.json()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        self.session = Session()
        # Validare cheie API via /quota
        try:
            data  = self._get('/quota')
            quota = data.get('quota', {})
            logger.info(
                'SubsRo autentificat. Quota: %s/%s rămase.',
                quota.get('remaining_quota', '?'),
                quota.get('total_quota', '?'),
            )
        except AuthenticationError:
            raise
        except Exception as e:
            logger.warning('SubsRo: nu s-a putut verifica quota: %s', e)

    def terminate(self):
        if self.session:
            self.session.close()
            self.session = None

    # ------------------------------------------------------------------
    # Căutare
    # ------------------------------------------------------------------

    def _lang_code(self, language):
        """Convertește Language → enum API subs.ro."""
        return LANG_MAP.get(language.alpha2, 'ro')

    def _build_query(self, video):
        """
        Determină câmpul și valoarea de căutare:
          Prioritate: imdbid → tmdbid → title
        """
        imdb_id = getattr(video, 'imdb_id', None)
        if imdb_id:
            raw = str(imdb_id)
            if not raw.startswith('tt'):
                raw = 'tt' + raw
            return 'imdbid', raw

        tmdb_id = getattr(video, 'tmdb_id', None)
        if tmdb_id and str(tmdb_id).isdigit() and int(tmdb_id) > 0:
            return 'tmdbid', str(tmdb_id)

        # Fallback title
        if isinstance(video, Episode):
            value = '%s S%02dE%02d' % (video.series, video.season, video.episode)
        else:
            value = video.title

        return 'title', value

    def query(self, video, language):
        """Caută subtitrări și returnează lista sortată după scor."""
        field, value   = self._build_query(video)
        lang_code      = self._lang_code(language)

        logger.debug('SubsRo query: field=%s value=%r lang=%s', field, value, lang_code)

        try:
            data = self._get(
                '/search/%s/%s' % (field, requests.utils.quote(str(value), safe='')),
                params={'language': lang_code},
            )
        except ProviderError as e:
            logger.warning('SubsRo: eroare căutare: %s', e)
            return []

        if data.get('status') != 200:
            return []

        items = data.get('items', [])
        logger.debug('SubsRo: %d subtitrări găsite.', len(items))

        video_name = getattr(video, 'name', '') or ''
        subtitles  = [SubsRoSubtitle(language, item, video_name) for item in items]

        # Filtrare hearing impaired
        subtitles = [
            s for s in subtitles
            if not re.search(r'\b(sdh|hearing.impaired)\b', s.title, re.I)
        ]

        # Sortare după scor matchmaking (portat din addon Kodi)
        subtitles.sort(key=lambda s: s.match_score, reverse=True)

        if subtitles:
            logger.debug(
                'SubsRo top 3: %s',
                [(s.title[:40], s.match_score) for s in subtitles[:3]],
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
    # Download (portat din download_subtitle() din addon Kodi)
    # ------------------------------------------------------------------

    def download_subtitle(self, subtitle):
        """
        Descarcă arhiva ZIP și extrage .srt/.ass cu cel mai bun scor.
        Endpoint: GET /subtitle/{id}/download → application/octet-stream
        Fallback: downloadLink din SubtitleItem dacă e prezent.
        """
        if subtitle.download_link:
            # Fallback: downloadLink direct din SubtitleItem
            headers, extra = self._get_auth()
            headers.pop('Accept', None)
            response = self.session.get(
                subtitle.download_link, headers=headers, params=extra, timeout=15
            )
            if response.status_code != 200:
                raise ProviderError('Download eșuat: %d' % response.status_code)
            content = response.content
            logger.debug('SubsRo: download via downloadLink')
        else:
            # Endpoint standard: GET /subtitle/{id}/download
            content = self._get('/subtitle/%d/download' % subtitle.subtitle_id, binary=True)
            logger.debug('SubsRo: download via endpoint standard')

        # Extrage .srt/.ass din ZIP
        with zipfile.ZipFile(BytesIO(content)) as z:
            srts = sorted([
                f for f in z.namelist()
                if f.lower().endswith(('.srt', '.ass'))
            ])
            if not srts:
                raise ProviderError('Arhiva nu conține fișiere .srt/.ass')

            # Dacă există mai multe fișiere → alege pe cel cu cel mai bun scor
            # (logica din addon Kodi – multi_episode_handling)
            if len(srts) > 1:
                best_srt, best_score = srts[0], -999
                for srt in srts:
                    score, _ = calculate_match_score(
                        os.path.basename(srt), subtitle.title
                    )
                    if score > best_score:
                        best_score, best_srt = score, srt
                logger.debug(
                    "SubsRo: ales '%s' (scor %+d) din %d fișiere.",
                    os.path.basename(best_srt), best_score, len(srts),
                )
            else:
                best_srt = srts[0]

            raw = z.read(best_srt)

        # Detectare encoding (portat din addon Kodi)
        text = None
        for enc in ['utf-8', 'iso-8859-2', 'windows-1250', 'latin1']:
            try:
                text = raw.decode(enc)
                logger.debug('SubsRo: encoding detectat: %s', enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = raw.decode('latin1', errors='ignore')

        # fix_line_ending așteaptă bytes (conform template Bazarr)
        subtitle.content = fix_line_ending(text.encode('utf-8'))
