import asyncio
from collections.abc import Callable
from difflib import SequenceMatcher
from functools import lru_cache, partial
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
import re
from typing import Any
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlparse
import urllib.error
import urllib.request

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from ourgroceries import OurGroceries

try:
    import gkeepapi
except ImportError:  # Permet d'exécuter les tests unitaires sans gkeepapi installé
    gkeepapi = None

GOOGLE_TASKS_SCOPES = ["https://www.googleapis.com/auth/tasks"]

# Préfixes conversationnels fréquents dictés à Gemini for Home
PREFIX_COMMANDS_RE = re.compile(
    r"^(?:s['’]il\s+te\s+pla[iî]t\s+)?"
    r"(?:peux-tu\s+|tu\s+peux\s+)?"
    r"(?:il\s+(?:me\s+|nous\s+)?faut|j['’]ai\s+besoin\s+(?:de\s+|d['’])|"
    r"penser\s+[àa]\s+(?:acheter|prendre)|acheter|prendre|r?ajouter?|mets?|mettre)\s+",
    re.IGNORECASE,
)

# Articles définis, indéfinis et partitifs en début d'expression
LEADING_ARTICLES_RE = re.compile(
    r"^(?:du\s+|de\s+la\s+|de\s+l['’]\s*|des\s+|un\s+|une\s+|"
    r"le\s+|la\s+|l['’]\s*|les\s+|d['’]\s*|de\s+|quelques\s+)",
    re.IGNORECASE,
)


def clean_task_title(raw_task_name: str) -> str:
    """Nettoie l'intitulé brut issu de Google Tasks (commandes vocales, articles)."""
    name = raw_task_name.strip()
    if not name:
        return ""

    # Suppression des verbes d'action éventuels ("ajoute", "il me faut", etc.)
    name = PREFIX_COMMANDS_RE.sub("", name).strip()

    # Suppression des articles/partitifs en début de chaîne (ex: "du lait" -> "lait")
    for _ in range(2):
        cleaned = LEADING_ARTICLES_RE.sub("", name).strip()
        if cleaned == name or not cleaned:
            break
        name = cleaned

    # Normalisation des espaces multiples
    name = re.sub(r"\s+", " ", name).strip()
    return name


@lru_cache(maxsize=512)
def normalize_for_comparison(text: str) -> str:
    """Normalise une chaîne pour la comparaison (minuscules, sans accents ni ligatures)."""
    text = text.lower().strip()
    text = text.replace("œ", "oe").replace("æ", "ae")
    # Décomposition Unicode et suppression des accents
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    # Remplacement de la ponctuation et des tirets/apostrophes par des espaces
    cleaned = re.sub(r"['’\-_,.;:!?/\\()\[\]]+", " ", without_accents)
    return re.sub(r"\s+", " ", cleaned).strip()


@lru_cache(maxsize=512)
def singularize_french(normalized_text: str) -> str:
    """Rapprochement singulier/pluriel basique mot par mot pour le français."""
    words = []
    for word in normalized_text.split():
        if len(word) > 4 and word.endswith("eaux"):
            words.append(word[:-1])  # ex: poireaux -> poireau, gateaux -> gateau
        elif len(word) > 3 and word.endswith(("s", "x")):
            words.append(word[:-1])  # ex: tomates -> tomate, oeufs -> oeuf, choux -> chou
        else:
            words.append(word)
    return " ".join(words)


def format_new_item_name(cleaned_name: str) -> str:
    """Formate proprement un nouvel article avec une majuscule initiale."""
    if not cleaned_name:
        return ""
    return cleaned_name[0].upper() + cleaned_name[1:]


def is_item_crossed_off(item: dict) -> bool:
    """
    Vérifie si un article OurGroceries est barré.
    L'API OurGroceries utilise principalement 'crossedOffAt' (timestamp)
    et parfois 'crossedOff' (booléen).
    """
    return bool(item.get("crossedOffAt") or item.get("crossedOff"))


def crossed_off_timestamp(item: dict) -> float:
    """Retourne l'horodatage numérique de rayure (crossedOffAt), ou 0.0 si absent."""
    raw = item.get("crossedOffAt")
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def split_crossed_off_for_cleanup(
    og_items: list[dict],
    keep_count: int = 80,
) -> tuple[list[dict], list[dict]]:
    """
    Sépare les articles rayés entre ceux à conserver (les `keep_count` plus récents
    selon `crossedOffAt`) et les plus anciens à supprimer.
    """
    keep_n = max(0, keep_count)
    crossed_items = [item for item in og_items if is_item_crossed_off(item)]
    crossed_sorted = sorted(
        crossed_items,
        key=crossed_off_timestamp,
        reverse=True,
    )
    return crossed_sorted[:keep_n], crossed_sorted[keep_n:]


FRENCH_STOPWORDS = {
    "les",
    "des",
    "aux",
    "avec",
    "sans",
    "pour",
    "dans",
    "sur",
    "une",
    "mon",
    "ton",
    "son",
    "mes",
    "tes",
    "ses",
}


def significant_words(singularized_text: str) -> list[str]:
    """Extrait les mots significatifs (longueur >= 3, hors mots outils français)."""
    return [
        w
        for w in singularized_text.split()
        if len(w) >= 3 and w not in FRENCH_STOPWORDS
    ]


def parse_and_match_item(
    raw_task_name: str,
    og_items: list[dict],
    cutoff: float = 0.75,
) -> tuple[dict | None, float, str]:
    """
    Cherche la meilleure correspondance parmi les éléments présents (actifs ou barrés)
    de la liste cible OurGroceries.

    Retourne un tuple :
      (matched_item_dict_or_None, best_similarity_score, formatted_clean_name)
    """
    cleaned_name = clean_task_title(raw_task_name)
    formatted_name = format_new_item_name(cleaned_name)
    if not cleaned_name:
        return None, 0.0, ""

    norm_input = normalize_for_comparison(cleaned_name)
    sing_input = singularize_french(norm_input)
    sig_input = significant_words(sing_input)

    best_item = None
    best_score = 0.0

    for item in og_items:
        item_value = item.get("value", "").strip()
        if not item_value:
            continue

        norm_cat = normalize_for_comparison(item_value)
        sing_cat = singularize_french(norm_cat)
        cat_words = sing_cat.split()

        if norm_input == norm_cat or (sing_input and sing_input == sing_cat):
            score = 1.0
        else:
            score_raw = SequenceMatcher(None, norm_input, norm_cat).ratio()
            score_sing = SequenceMatcher(None, sing_input, sing_cat).ratio()
            score = max(score_raw, score_sing)

            # Correspondance par mot-clé / mot directeur (ex: "Lait" -> "lait cawol")
            if sig_input and cat_words and all(w in cat_words for w in sig_input):
                token_score = 0.88 if cat_words[0] == sig_input[0] else 0.80
                score = max(score, token_score)

        # En cas d'égalité parfaite de score, on privilégie l'article barré (à réactiver),
        # puis le plus récemment rayé.
        if score > best_score:
            best_score = score
            best_item = item
        elif score == best_score and best_item is not None:
            item_crossed = is_item_crossed_off(item)
            best_crossed = is_item_crossed_off(best_item)
            if (item_crossed and not best_crossed) or (
                item_crossed
                and best_crossed
                and crossed_off_timestamp(item) > crossed_off_timestamp(best_item)
            ):
                best_item = item

    if best_item is not None and best_score >= cutoff:
        return best_item, round(best_score, 3), best_item["value"]

    return None, round(best_score, 3), formatted_name


def _pick_best_matching_item(matches: list[dict]) -> dict | None:
    """Privilégie un article barré (le plus récemment rayé), sinon le premier article trouvé."""
    if not matches:
        return None
    crossed = [item for item in matches if is_item_crossed_off(item)]
    if crossed:
        return max(crossed, key=crossed_off_timestamp)
    return matches[0]


def find_exact_local_match(raw_task_name: str, og_items: list[dict]) -> dict | None:
    """
    Cherche une correspondance directe (score == 1.0 : insensible à la casse,
    aux accents/ligatures et au singulier/pluriel) dans la liste OurGroceries,
    afin d'éviter un appel Gemini inutile.
    """
    cleaned_name = clean_task_title(raw_task_name)
    if not cleaned_name:
        return None

    norm_input = normalize_for_comparison(cleaned_name)
    if not norm_input:
        return None
    sing_input = singularize_french(norm_input)

    exact_matches: list[dict] = []
    for item in og_items:
        item_value = item.get("value", "").strip()
        if not item_value:
            continue
        norm_cat = normalize_for_comparison(item_value)
        sing_cat = singularize_french(norm_cat)
        if norm_input == norm_cat or (sing_input and sing_input == sing_cat):
            exact_matches.append(item)

    return _pick_best_matching_item(exact_matches)


def find_catalog_item(matched_name: str | None, og_items: list[dict]) -> dict | None:
    """Retrouve un dictionnaire d'article dans og_items depuis le nom renvoyé par Gemini."""
    if not matched_name:
        return None
    candidate = matched_name.strip()
    if not candidate or candidate.lower() in ("null", "none"):
        return None

    # 1. Correspondance exacte
    exact = [item for item in og_items if item.get("value", "") == candidate]
    if exact:
        return _pick_best_matching_item(exact)

    # 2. Correspondance insensible à la casse
    lower_cand = candidate.lower()
    case_insensitive = [
        item for item in og_items if item.get("value", "").strip().lower() == lower_cand
    ]
    if case_insensitive:
        return _pick_best_matching_item(case_insensitive)

    # 3. Correspondance normalisée (sans accents / ligatures)
    norm_cand = normalize_for_comparison(candidate)
    if norm_cand:
        normalized = [
            item
            for item in og_items
            if normalize_for_comparison(item.get("value", "")) == norm_cand
        ]
        if normalized:
            return _pick_best_matching_item(normalized)

    return None


def match_tasks_with_gemini(
    raw_titles: list[str],
    og_items: list[dict],
    api_key: str,
    model: str = "gemini-3.5-flash-lite",
) -> list[dict]:
    """
    Appelle l'API Gemini Flash pour réconcilier intelligemment en un seul batch
    les tâches vocales Google Tasks avec le catalogue d'articles OurGroceries.
    """
    if not raw_titles:
        return []

    catalog_values: list[str] = []
    seen: set[str] = set()
    for item in og_items:
        val = item.get("value", "").strip()
        if val and val not in seen:
            seen.add(val)
            catalog_values.append(val)

    indexed_tasks = [
        {"task_index": idx, "spoken_task": title}
        for idx, title in enumerate(raw_titles)
    ]

    prompt = (
        "Tu es un assistant chargé de réconcilier des articles de liste de courses en français "
        "dictés à la voix sur Google Home (Google Keep / Google Tasks) avec les articles déjà existants dans "
        "la liste OurGroceries de l'utilisateur.\n\n"
        "Règles :\n"
        "1. Pour chaque tâche dans `tasks`, nettoie mentalement les commandes vocales et articles "
        "(ex: 'ajoute du', 'de la', 'des', 'un', 'il me faut').\n"
        "2. Cherche si un article du `catalog` correspond sémantiquement :\n"
        "   - Associe un nom générique à la marque ou variante habituelle du catalogue "
        "(ex: 'Lait' -> 'lait cawol', 'Beurre' -> 'Beurre demi-sel').\n"
        "   - Reconnais les synonymes et métonymies courantes "
        "(ex: 'Sopalin' -> 'Essuie-tout', 'PQ' -> 'Papier toilette', 'Coca' -> 'Cola').\n"
        "   - Reconnais les équivalences culinaires, régionales et d'ingrédients désignant le même produit en rayon "
        "(ex: 'galettes blé noir' ou 'galettes de sarrasin' -> 'galettes bretonnes', "
        "'crème liquide' -> 'crème fleurette', 'fromage râpé' -> 'emmental râpé').\n"
        "   - Tolère le singulier/pluriel, les accents, les ligatures (oeufs/œufs) et les fautes de dictée vocale.\n"
        "   - Privilégie la réutilisation d'un article existant du `catalog` dès qu'il s'agit du même produit acheté habituellement par le foyer. "
        "Ne crée un nouvel article (`null`) que si le produit est réellement distinct (ex: 'galettes de riz' vs 'galettes bretonnes').\n"
        "3. Si un article du catalogue correspond, renseigne `matched_catalog_item` avec sa chaîne EXACTE "
        "telle qu'elle apparaît dans `catalog` (sinon mets `null`).\n"
        "4. Renseigne `clean_new_name` avec le nom propre nettoyé et une majuscule initiale (ex: 'Chocolat noir').\n"
        "5. Renseigne `confidence` entre 0.0 et 1.0 et `reason` avec une courte explication en français.\n\n"
        f"catalog = {json.dumps(catalog_values, ensure_ascii=False)}\n"
        f"tasks = {json.dumps(indexed_tasks, ensure_ascii=False)}"
    )

    base_generation_config = {
        "temperature": 0.1,
        "responseMimeType": "application/json",
        "responseSchema": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "task_index": {"type": "INTEGER"},
                    "matched_catalog_item": {"type": "STRING", "nullable": True},
                    "clean_new_name": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                    "reason": {"type": "STRING"},
                },
                "required": [
                    "task_index",
                    "matched_catalog_item",
                    "clean_new_name",
                    "confidence",
                    "reason",
                ],
            },
        },
    }

    fallback_models = [
        model,
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.8-flash",
        "gemini-flash-latest",
    ]
    candidate_models = list(dict.fromkeys(m for m in fallback_models if m))

    response_data = None
    last_error = ""

    for candidate_model in candidate_models:
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(candidate_model, safe='')}:generateContent?key={quote(api_key, safe='')}"
        )
        generation_config = dict(base_generation_config)
        if candidate_model.startswith("gemini-3") and "lite" not in candidate_model:
            generation_config["thinkingConfig"] = {"thinkingLevel": "low"}

        request_body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(request_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as http_err:
            err_body = http_err.read().decode("utf-8", errors="replace")
            if http_err.code in (503, 429, 404):
                last_error = f"HTTP {http_err.code} ({candidate_model}) : {err_body[:200]}"
                continue
            raise RuntimeError(
                f"HTTP {http_err.code} Gemini API ({candidate_model}) : {err_body[:250]}"
            ) from http_err
        except (urllib.error.URLError, TimeoutError) as net_err:
            last_error = f"Timeout/Réseau ({candidate_model}) : {net_err}"
            continue

    if response_data is None:
        raise RuntimeError(f"Gemini indisponible : {last_error}")

    candidates = response_data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Réponse Gemini vide (aucun candidat retourné).")

    parts = candidates[0].get("content", {}).get("parts", [])
    text_payload = "".join(part.get("text", "") for part in parts).strip()
    if not text_payload:
        raise RuntimeError("Réponse Gemini vide (texte absent).")

    parsed = json.loads(text_payload)
    if not isinstance(parsed, list):
        raise ValueError("Le JSON renvoyé par Gemini n'est pas une liste.")

    return parsed


def get_env_clean(key: str, default: str = "") -> str:
    """Lit une variable d'environnement en supprimant espaces et guillemets éventuels."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    val = raw.strip()
    if len(val) >= 2 and (
        (val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")
    ):
        val = val[1:-1].strip()
    return val


def get_google_tasks_service():
    """Construit le client Google Tasks API via OAuth2 Refresh Token."""
    client_id = get_env_clean("GOOGLE_CLIENT_ID")
    client_secret = get_env_clean("GOOGLE_CLIENT_SECRET")
    refresh_token = get_env_clean("GOOGLE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError(
            "Variables Google manquantes : GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET "
            "et GOOGLE_REFRESH_TOKEN sont requis."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=GOOGLE_TASKS_SCOPES,
    )
    # Rafraîchit le jeton d'accès immédiatement
    creds.refresh(Request())
    return build("tasks", "v1", credentials=creds, cache_discovery=False)


# Cache en mémoire du client Google Keep pour les exécutions successives ("Warm Container")
_KEEP_CACHE: dict[str, Any] = {
    "email": None,
    "master_token": None,
    "client": None,
}

KEEP_NOTE_URL_RE = re.compile(
    r"^https?://keep\.google\.com/(?:u/\d+/)?#[a-z]+/([^/?#]+)",
    re.IGNORECASE,
)


def parse_keep_note_ids(raw_value: str) -> list[str]:
    """
    Découpe GOOGLE_KEEP_NOTE_ID en liste d'identifiants/titres normalisés
    (séparés par virgules ou retours à la ligne, avec prise en charge des URL Keep).
    """
    if not raw_value:
        return []

    identifiers: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\r\n]+", raw_value):
        token = part.strip()
        if len(token) >= 2 and (
            (token[0] == '"' and token[-1] == '"')
            or (token[0] == "'" and token[-1] == "'")
        ):
            token = token[1:-1].strip()
        if not token:
            continue

        url_match = KEEP_NOTE_URL_RE.match(token)
        if url_match:
            token = unquote(url_match.group(1)).strip()

        if token and token not in seen:
            seen.add(token)
            identifiers.append(token)

    return identifiers


def describe_keep_note_sharing(note: Any, current_email: str = "") -> dict[str, Any]:
    """Analyse l'état de partage (propriétaire, collaborateurs) d'une note Google Keep."""
    collaborators = getattr(note, "collaborators", None)
    current_norm = current_email.strip().lower()

    all_emails: list[str] = []
    seen_emails: set[str] = set()
    owner_email: str | None = None
    current_role: str | None = None

    if collaborators is not None:
        all_fn = getattr(collaborators, "all", None)
        if callable(all_fn):
            try:
                entries = all_fn()
            except Exception:
                entries = []
            for entry in entries or []:
                email_str = (
                    entry.strip()
                    if isinstance(entry, str)
                    else (getattr(entry, "email", "") or "").strip()
                )
                if email_str and email_str.lower() not in seen_emails:
                    seen_emails.add(email_str.lower())
                    all_emails.append(email_str)

        raw_collab_map = getattr(collaborators, "_collaborators", None)
        if isinstance(raw_collab_map, dict):
            for raw_email, role in raw_collab_map.items():
                email_str = (
                    raw_email.strip()
                    if isinstance(raw_email, str)
                    else (getattr(raw_email, "email", "") or "").strip()
                )
                if not email_str:
                    continue
                role_val = getattr(role, "value", role)
                if role_val == "O" and owner_email is None:
                    owner_email = email_str
                if current_norm and email_str.lower() == current_norm:
                    current_role = str(role_val) if role_val is not None else None
                if not callable(all_fn) and role_val in ("O", "W", "A"):
                    if email_str.lower() not in seen_emails:
                        seen_emails.add(email_str.lower())
                        all_emails.append(email_str)

    other_collaborators = [
        email for email in all_emails if not current_norm or email.lower() != current_norm
    ]
    shared_with_me = bool(
        (owner_email and current_norm and owner_email.lower() != current_norm)
        or current_role == "W"
    )
    shared = bool(shared_with_me or other_collaborators)

    return {
        "shared": shared,
        "shared_with_me": shared_with_me,
        "owner": owner_email,
        "collaborators": all_emails,
        "other_collaborators": other_collaborators,
    }


def _format_keep_note_summary(note: Any, current_email: str = "") -> str:
    """Formate une note Google Keep pour les messages d'erreur et de diagnostic."""
    title = (getattr(note, "title", "") or "").strip() or "(sans titre)"
    sharing = describe_keep_note_sharing(note, current_email=current_email)
    tags: list[str] = []
    if sharing["shared_with_me"]:
        if sharing["owner"]:
            tags.append(f"[partagée par {sharing['owner']}]")
        else:
            tags.append("[partagée avec vous]")
    elif sharing["other_collaborators"]:
        tags.append(f"[partagée avec {', '.join(sharing['other_collaborators'])}]")
    if getattr(note, "archived", False):
        tags.append("[archivée]")
    tag_str = f" {' '.join(tags)}" if tags else ""
    return f"'{title}'{tag_str} (id={getattr(note, 'id', '?')})"


def resolve_keep_notes(
    keep: Any,
    identifiers: list[str],
    current_email: str = "",
) -> list[Any]:
    """
    Résout une liste d'identifiants Google Keep (id, server_id ou titre exact)
    en objets List dédupliqués, incluant les listes partagées avec le compte.
    """
    all_notes = [
        n
        for n in keep.all()
        if not getattr(n, "trashed", False) and not getattr(n, "deleted", False)
    ]
    by_id: dict[str, Any] = {}
    by_server_id: dict[str, Any] = {}
    for note in all_notes:
        nid = getattr(note, "id", None)
        if nid:
            by_id[nid] = note
        sid = getattr(note, "server_id", None)
        if sid:
            by_server_id[sid] = note

    resolved: list[Any] = []
    seen_resolved_ids: set[str] = set()
    non_checklists: list[Any] = []
    seen_non_checklist_ids: set[str] = set()
    missing: list[str] = []

    def _record_candidate(candidate: Any) -> None:
        cid = getattr(candidate, "id", None)
        if not hasattr(candidate, "unchecked"):
            if cid is None or cid not in seen_non_checklist_ids:
                if cid is not None:
                    seen_non_checklist_ids.add(cid)
                non_checklists.append(candidate)
            return
        if cid is None or cid not in seen_resolved_ids:
            if cid is not None:
                seen_resolved_ids.add(cid)
            resolved.append(candidate)

    for ident in identifiers:
        candidate = by_id.get(ident) or by_server_id.get(ident)
        if candidate is None and hasattr(keep, "get"):
            fetched = keep.get(ident)
            if (
                fetched is not None
                and not getattr(fetched, "trashed", False)
                and not getattr(fetched, "deleted", False)
            ):
                candidate = fetched

        if candidate is not None:
            _record_candidate(candidate)
            continue

        ident_lower = ident.strip().lower()
        title_matches = [
            n
            for n in all_notes
            if (getattr(n, "title", "") or "").strip().lower() == ident_lower
        ]
        if not title_matches:
            missing.append(ident)
            continue

        checklist_matches = [n for n in title_matches if hasattr(n, "unchecked")]
        if not checklist_matches:
            for n in title_matches:
                _record_candidate(n)
            continue

        active_matches = [
            n for n in checklist_matches if not getattr(n, "archived", False)
        ]
        chosen = active_matches if active_matches else checklist_matches
        for n in chosen:
            _record_candidate(n)

    if non_checklists:
        bad = ", ".join(
            f"'{getattr(n, 'title', '') or '(sans titre)'}' (id={getattr(n, 'id', '?')})"
            for n in non_checklists
        )
        raise ValueError(
            f"La ou les notes Google Keep suivantes ne sont pas des listes à cases à cocher : {bad}."
        )

    if missing:
        available = ", ".join(
            _format_keep_note_summary(n, current_email=current_email)
            for n in all_notes
        ) or "aucune note trouvée"
        missing_str = ", ".join(f"'{m}'" for m in missing)
        raise ValueError(
            f"GOOGLE_KEEP_NOTE_ID : liste(s) introuvable(s) dans Google Keep ({missing_str}). "
            f"Notes disponibles : {available}"
        )

    return resolved


def collect_keep_pending_items(
    keep_notes: list[Any],
    current_email: str = "",
) -> tuple[list[dict], list[dict]]:
    """Extrait tous les articles non cochés et les métadonnées des listes Keep résolues."""
    pending_items: list[dict] = []
    keep_lists_meta: list[dict] = []

    for note in keep_notes:
        if not hasattr(note, "unchecked"):
            raise ValueError(
                f"La note Google Keep '{getattr(note, 'title', getattr(note, 'id', '?'))}' "
                "n'est pas une liste à cases à cocher."
            )

        note_title = (getattr(note, "title", "") or "").strip() or "(sans titre)"
        note_server_id = getattr(note, "server_id", None)
        sharing = describe_keep_note_sharing(note, current_email=current_email)

        note_pending: list[dict] = []
        for item in note.unchecked:
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            note_pending.append(
                {
                    "id": item.id,
                    "title": text,
                    "keep_note_id": note.id,
                    "keep_note_server_id": note_server_id,
                    "keep_note_title": note_title,
                    "keep_item_server_id": getattr(item, "server_id", None),
                    "_keep_item": item,
                    "_keep_note": note,
                }
            )

        pending_items.extend(note_pending)
        meta: dict[str, Any] = {
            "id": note.id,
            "title": note_title,
            "shared": sharing["shared"],
            "shared_with_me": sharing["shared_with_me"],
            "unchecked_count": len(note_pending),
        }
        if sharing["owner"]:
            meta["owner"] = sharing["owner"]
        keep_lists_meta.append(meta)

    return pending_items, keep_lists_meta


def _apply_keep_item_checked(keep_item: Any, keep_note: Any = None) -> None:
    """Coche un article Google Keep en veillant à renseigner parent_server_id (listes partagées)."""
    if keep_note is not None:
        note_server_id = getattr(keep_note, "server_id", None)
        if note_server_id and getattr(keep_item, "parent_server_id", None) is None:
            keep_item.parent_server_id = note_server_id
    keep_item.checked = True


def mark_google_keep_items_checked(
    keep_client: Any,
    tasks_to_mark_done: list[dict],
) -> None:
    """
    Coche les articles traités dans Google Keep et synchronise avec reprise en cas de conflit
    (notamment lors d'éditions concurrentes sur une liste partagée).
    """
    if not tasks_to_mark_done:
        return

    for task in tasks_to_mark_done:
        _apply_keep_item_checked(task["_keep_item"], task.get("_keep_note"))

    if keep_client is None:
        return

    try:
        keep_client.sync()
        return
    except Exception:
        pass

    try:
        keep_client.sync(resync=True)

        notes_by_id: dict[str, Any] = {}
        notes_by_server_id: dict[str, Any] = {}
        for note in keep_client.all():
            if getattr(note, "trashed", False) or getattr(note, "deleted", False):
                continue
            nid = getattr(note, "id", None)
            if nid:
                notes_by_id[nid] = note
            sid = getattr(note, "server_id", None)
            if sid:
                notes_by_server_id[sid] = note

        reapplied = False
        for task in tasks_to_mark_done:
            note_id = task.get("keep_note_id")
            note_sid = task.get("keep_note_server_id")
            refreshed_note = notes_by_id.get(note_id) if note_id else None
            if refreshed_note is None and note_sid:
                refreshed_note = notes_by_server_id.get(note_sid)
            if refreshed_note is None and note_id and hasattr(keep_client, "get"):
                candidate = keep_client.get(note_id)
                if (
                    candidate is not None
                    and not getattr(candidate, "trashed", False)
                    and not getattr(candidate, "deleted", False)
                ):
                    refreshed_note = candidate

            if refreshed_note is None or not hasattr(refreshed_note, "unchecked"):
                continue

            target_item_id = task.get("id")
            target_item_sid = task.get("keep_item_server_id")
            matched_item = next(
                (
                    it
                    for it in refreshed_note.unchecked
                    if target_item_id and getattr(it, "id", None) == target_item_id
                ),
                None,
            )
            if matched_item is None and target_item_sid:
                matched_item = next(
                    (
                        it
                        for it in refreshed_note.unchecked
                        if getattr(it, "server_id", None) == target_item_sid
                    ),
                    None,
                )

            if matched_item is not None:
                _apply_keep_item_checked(matched_item, refreshed_note)
                reapplied = True

        if reapplied:
            keep_client.sync()
    except Exception:
        _KEEP_CACHE["client"] = None
        raise


def get_google_keep_pending_items() -> tuple[Any, list[dict], list[dict]]:
    """
    Authentifie (ou rafraîchit via sync incrémentale) le client Google Keep
    via Master Token et retourne : (keep_client, liste_articles_non_cochés, métadonnées_listes).
    """
    if gkeepapi is None:
        raise RuntimeError(
            "Le module Python 'gkeepapi' est requis pour la synchronisation Google Keep."
        )

    keep_email = get_env_clean("GOOGLE_KEEP_EMAIL") or get_env_clean("OG_USERNAME")
    keep_master_token = get_env_clean("GOOGLE_KEEP_MASTER_TOKEN")
    raw_note_ids = get_env_clean("GOOGLE_KEEP_NOTE_ID") or get_env_clean("GOOGLE_KEEP_NOTE_IDS")
    note_identifiers = parse_keep_note_ids(raw_note_ids)

    if not all([keep_email, keep_master_token]) or not note_identifiers:
        raise ValueError(
            "Variables Google Keep manquantes : GOOGLE_KEEP_EMAIL, "
            "GOOGLE_KEEP_MASTER_TOKEN et GOOGLE_KEEP_NOTE_ID sont requis."
        )

    def _authenticate_fresh() -> Any:
        client = gkeepapi.Keep()
        try:
            client.authenticate(keep_email, keep_master_token)
        except Exception as auth_err:
            _KEEP_CACHE["client"] = None
            raise RuntimeError(
                f"Échec d'authentification Google Keep pour '{keep_email}' : {auth_err}"
            ) from auth_err
        _KEEP_CACHE["email"] = keep_email
        _KEEP_CACHE["master_token"] = keep_master_token
        _KEEP_CACHE["client"] = client
        return client

    keep = None
    used_cache = False
    if (
        _KEEP_CACHE["client"] is not None
        and _KEEP_CACHE["email"] == keep_email
        and _KEEP_CACHE["master_token"] == keep_master_token
    ):
        cached_keep = _KEEP_CACHE["client"]
        try:
            cached_keep.sync()
            keep = cached_keep
            used_cache = True
        except Exception:
            _KEEP_CACHE["client"] = None
            keep = None

    if keep is None:
        keep = _authenticate_fresh()

    try:
        keep_notes = resolve_keep_notes(keep, note_identifiers, current_email=keep_email)
    except ValueError:
        if not used_cache:
            raise
        try:
            keep.sync(resync=True)
        except Exception:
            keep = _authenticate_fresh()
        keep_notes = resolve_keep_notes(keep, note_identifiers, current_email=keep_email)

    pending_items, keep_lists_meta = collect_keep_pending_items(
        keep_notes, current_email=keep_email
    )
    return keep, pending_items, keep_lists_meta


async def _gather_bounded(
    coro_factories: list[Callable[[], Any]],
    limit: int = 5,
) -> None:
    """Exécute des appels réseau asynchrones en parallèle avec une concurrence bornée."""
    if not coro_factories:
        return
    sem = asyncio.Semaphore(limit)

    async def _run_one(factory: Callable[[], Any]) -> Any:
        async with sem:
            return await factory()

    await asyncio.gather(*(_run_one(factory) for factory in coro_factories))


def _build_sync_response(
    source_name: str,
    dry_run: bool,
    has_pending_tasks: bool,
    processed_items: list[dict],
    keep_crossed_off: int | None,
    deleted_crossed_off_items: list[dict],
    keep_lists: list[dict] | None = None,
) -> dict:
    """Construit la réponse JSON standardisée de /api/sync."""
    if has_pending_tasks:
        message = f"{len(processed_items)} article(s) traité(s) avec succès."
    else:
        message = "Aucun nouvel article à synchroniser."

    if deleted_crossed_off_items:
        message = (
            f"{message} {len(deleted_crossed_off_items)} ancien(s) article(s) "
            "rayé(s) supprimé(s)."
        )

    response: dict = {
        "status": "ok",
        "source": source_name,
        "dry_run": dry_run,
        "synced_count": len(processed_items),
        "message": message,
        "items": processed_items,
    }
    if keep_lists is not None:
        response["keep_lists"] = keep_lists
    if keep_crossed_off is not None:
        response["deleted_crossed_off_count"] = len(deleted_crossed_off_items)
        response["deleted_crossed_off_items"] = deleted_crossed_off_items
    return response


async def sync_tasks_to_groceries(
    dry_run: bool = False,
    keep_crossed_off: int | None = None,
) -> dict:
    """
    Exécute la synchronisation complète :
    1. Lit les articles non cochés dans Google Keep (ou Google Tasks si configuré en repli).
    2. Nettoie éventuellement les anciens articles rayés OurGroceries au-delà des `keep_crossed_off` plus récents.
    3. Réconcilie chaque article avec OurGroceries (Gemini Flash + fallback algorithmique).
    4. Décoche l'article existant ou crée le nouvel article dans OurGroceries.
    5. Coche l'article dans Google Keep (ou marque la tâche terminée dans Google Tasks).
    """
    og_user = get_env_clean("OG_USERNAME")
    og_pass = get_env_clean("OG_PASSWORD")
    og_list_id = get_env_clean("OG_LIST_ID")
    if not all([og_user, og_pass, og_list_id]):
        raise ValueError(
            "Variables OurGroceries manquantes : OG_USERNAME, OG_PASSWORD et OG_LIST_ID sont requis."
        )

    if keep_crossed_off is None:
        env_keep_crossed = get_env_clean("OG_KEEP_CROSSED_OFF")
        if env_keep_crossed:
            try:
                keep_crossed_off = int(env_keep_crossed)
            except ValueError as err:
                raise ValueError(
                    f"Variable OG_KEEP_CROSSED_OFF invalide ('{env_keep_crossed}') : un entier >= 0 est attendu."
                ) from err
    if keep_crossed_off is not None and keep_crossed_off < 0:
        raise ValueError("keep_crossed_off doit être un entier supérieur ou égal à 0.")

    cutoff = float(get_env_clean("MATCH_CUTOFF", "0.75") or "0.75")
    gemini_api_key = get_env_clean("GEMINI_API_KEY")
    gemini_model = get_env_clean("GEMINI_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite"

    # 1. Récupération des articles non cochés depuis Google Keep (prioritaire) ou Google Tasks
    keep_master_token = get_env_clean("GOOGLE_KEEP_MASTER_TOKEN")
    keep_client: Any = None
    keep_lists_meta: list[dict] | None = None
    tasks_service: Any = None
    task_list_id = ""

    if keep_master_token:
        source_name = "google_keep"
        keep_client, pending_tasks, keep_lists_meta = get_google_keep_pending_items()
    else:
        task_list_id = get_env_clean("GOOGLE_TASK_LIST_ID")
        if not task_list_id:
            raise ValueError(
                "Configuration source manquante : renseignez GOOGLE_KEEP_MASTER_TOKEN (Google Keep) "
                "ou GOOGLE_TASK_LIST_ID (Google Tasks)."
            )
        source_name = "google_tasks"
        tasks_service = get_google_tasks_service()
        results = (
            tasks_service.tasks()
            .list(tasklist=task_list_id, showCompleted=False, maxResults=100)
            .execute()
        )
        raw_tasks = results.get("items", [])
        pending_tasks = [t for t in raw_tasks if t.get("title", "").strip()]

    if not pending_tasks and keep_crossed_off is None:
        return _build_sync_response(
            source_name,
            dry_run,
            False,
            [],
            None,
            [],
            keep_lists=keep_lists_meta,
        )

    # 2. Connexion à OurGroceries et lecture des éléments de la liste cible
    og = OurGroceries(og_user, og_pass)
    try:
        await og.login()
    except Exception as login_err:
        raise RuntimeError(
            f"Échec de connexion OurGroceries (login) pour '{og_user}' : {login_err}"
        ) from login_err

    try:
        list_details = await og.get_list_items(list_id=og_list_id)
    except Exception as list_err:
        try:
            lists_response = await og.get_my_lists()
            shopping_lists = lists_response.get("shoppingLists", [])
        except Exception as my_lists_err:
            raise RuntimeError(
                f"Échec OurGroceries get_list_items(OG_LIST_ID='{og_list_id}') : {list_err} ; "
                f"et échec get_my_lists() : {my_lists_err}"
            ) from list_err

        matched_list = next(
            (
                sl
                for sl in shopping_lists
                if sl.get("name", "").strip().lower() == og_list_id.lower()
            ),
            None,
        )
        if matched_list is not None:
            og_list_id = matched_list["id"]
            list_details = await og.get_list_items(list_id=og_list_id)
        else:
            available = ", ".join(
                f"'{sl.get('name', '?')}' (id={sl.get('id', '?')})"
                for sl in shopping_lists
            ) or "aucune liste trouvée"
            raise ValueError(
                f"OG_LIST_ID='{og_list_id}' invalide ou introuvable ({list_err}). "
                f"Listes OurGroceries disponibles : {available}"
            ) from list_err

    og_items = list_details.get("list", {}).get("items", [])

    # 2b. Nettoyage optionnel des anciens articles rayés au-delà des `keep_crossed_off` plus récents
    deleted_crossed_off_items: list[dict] = []
    if keep_crossed_off is not None:
        _, items_to_delete = split_crossed_off_for_cleanup(
            og_items, keep_count=keep_crossed_off
        )
        if items_to_delete:
            deleted_mem_ids = {id(item) for item in items_to_delete}
            delete_tasks: list[Callable[[], Any]] = []
            for item in items_to_delete:
                item_id = item.get("id")
                if not dry_run and item_id:
                    delete_tasks.append(
                        partial(og.remove_item_from_list, og_list_id, item_id)
                    )
                deleted_crossed_off_items.append(
                    {
                        "id": item_id,
                        "value": item.get("value", ""),
                        "crossedOffAt": item.get("crossedOffAt"),
                    }
                )
            if delete_tasks:
                await _gather_bounded(delete_tasks, limit=5)
            og_items = [item for item in og_items if id(item) not in deleted_mem_ids]

    if not pending_tasks:
        return _build_sync_response(
            source_name,
            dry_run,
            False,
            [],
            keep_crossed_off,
            deleted_crossed_off_items,
            keep_lists=keep_lists_meta,
        )

    # 3. Pré-filtrage exact local (évite Gemini sur score == 1.0), puis appel Gemini ciblé
    exact_local_matches: dict[int, dict] = {}
    unresolved_indices: list[int] = []
    for idx, task in enumerate(pending_tasks):
        exact_item = find_exact_local_match(task["title"], og_items)
        if exact_item is not None:
            exact_local_matches[idx] = exact_item
        else:
            unresolved_indices.append(idx)

    gemini_matches: dict[int, dict] = {}
    gemini_error: str | None = None
    if gemini_api_key and unresolved_indices:
        try:
            unresolved_titles = [pending_tasks[i]["title"] for i in unresolved_indices]
            raw_results = match_tasks_with_gemini(
                unresolved_titles,
                og_items,
                api_key=gemini_api_key,
                model=gemini_model,
            )
            for entry in raw_results:
                if isinstance(entry, dict) and isinstance(entry.get("task_index"), int):
                    sub_idx = entry["task_index"]
                    if 0 <= sub_idx < len(unresolved_indices):
                        gemini_matches[unresolved_indices[sub_idx]] = entry
        except Exception as exc:
            gemini_error = str(exc)

    # 4. Traitement de chaque tâche et préparation des écritures OurGroceries
    processed_items = []
    og_write_tasks: list[Callable[[], Any]] = []
    tasks_to_mark_done = []

    for idx, task in enumerate(pending_tasks):
        raw_title = task["title"]

        if idx in exact_local_matches:
            matched_item = exact_local_matches[idx]
            target_name = matched_item["value"]
            score = 1.0
            matching_engine = "exact_local"
            reason = "Correspondance directe dans la liste OurGroceries (sans appel Gemini)."
        elif idx in gemini_matches:
            entry = gemini_matches[idx]
            matched_catalog_name = entry.get("matched_catalog_item")
            confidence = round(float(entry.get("confidence") or 0.0), 3)
            reason = str(entry.get("reason", "")).strip()

            has_catalog_name = bool(
                matched_catalog_name
                and str(matched_catalog_name).strip().lower() not in ("null", "none")
            )
            if has_catalog_name:
                matched_item = find_catalog_item(str(matched_catalog_name), og_items)
                if matched_item is not None:
                    target_name = matched_item["value"]
                    score = confidence
                    matching_engine = "gemini"
                else:
                    matched_item, score, target_name = parse_and_match_item(
                        raw_title, og_items, cutoff=cutoff
                    )
                    matching_engine = "algorithmic_fallback"
                    reason = (
                        f"Article '{matched_catalog_name}' introuvable dans OurGroceries "
                        "— fallback algorithmique"
                    )
            else:
                clean_new = format_new_item_name(
                    str(entry.get("clean_new_name", "")).strip()
                )
                if not clean_new:
                    clean_new = format_new_item_name(clean_task_title(raw_title))
                # Évite les doublons si le même article a déjà été créé dans ce lot
                matched_item = find_catalog_item(clean_new, og_items)
                target_name = matched_item["value"] if matched_item else clean_new
                score = confidence
                matching_engine = "gemini"
        else:
            matched_item, score, target_name = parse_and_match_item(
                raw_title, og_items, cutoff=cutoff
            )
            matching_engine = "algorithmic_fallback"
            if gemini_error:
                reason = f"Fallback algorithmique (erreur Gemini : {gemini_error})"
            elif gemini_api_key:
                reason = "Fallback algorithmique (tâche absente de la réponse Gemini)"
            else:
                reason = "Matching algorithmique (GEMINI_API_KEY non configuré)"

        if not target_name:
            continue

        if matched_item is not None:
            is_crossed_off = is_item_crossed_off(matched_item)
            if is_crossed_off:
                action = "uncrossed_existing"
                if not dry_run and matched_item.get("id"):
                    item_id = matched_item["id"]
                    og_write_tasks.append(
                        partial(
                            og.toggle_item_crossed_off,
                            list_id=og_list_id,
                            item_id=item_id,
                            cross_off=False,
                        )
                    )
                matched_item["crossedOff"] = False
                matched_item["crossedOffAt"] = None
            else:
                action = "already_active"
        else:
            action = "created_new"
            if not dry_run:
                item_val = target_name
                og_write_tasks.append(
                    partial(
                        og.add_item_to_list,
                        list_id=og_list_id,
                        value=item_val,
                        auto_category=True,
                    )
                )
            # Ajout en mémoire pour éviter un doublon si le même article apparaît 2 fois dans le lot
            og_items.append(
                {"id": None, "value": target_name, "crossedOff": False, "crossedOffAt": None}
            )

        tasks_to_mark_done.append(task)
        item_payload = {
            "google_task_id": task["id"],
            "raw_title": raw_title,
            "ourgroceries_name": target_name,
            "action": action,
            "similarity_score": score,
            "matching_engine": matching_engine,
            "reason": reason,
        }
        if "keep_note_id" in task:
            item_payload["keep_note_id"] = task["keep_note_id"]
        if "keep_note_title" in task:
            item_payload["keep_note_title"] = task["keep_note_title"]
        processed_items.append(item_payload)

    # 5. Exécution parallèle des écritures OurGroceries, puis clôture dans Google Keep / Tasks
    if not dry_run:
        await _gather_bounded(og_write_tasks, limit=5)

        if tasks_to_mark_done:
            if source_name == "google_keep":
                mark_google_keep_items_checked(keep_client, tasks_to_mark_done)
            elif tasks_service is not None:
                for task in tasks_to_mark_done:
                    tasks_service.tasks().patch(
                        tasklist=task_list_id,
                        task=task["id"],
                        body={"status": "completed"},
                    ).execute()

    return _build_sync_response(
        source_name,
        dry_run,
        True,
        processed_items,
        keep_crossed_off,
        deleted_crossed_off_items,
        keep_lists=keep_lists_meta,
    )


def is_authorized(headers, query_params: dict) -> bool:
    """Vérifie le jeton CRON_SECRET s'il est configuré dans les variables d'environnement."""
    expected_secret = get_env_clean("CRON_SECRET")
    if not expected_secret:
        return True

    # 1. Header Authorization: Bearer <CRON_SECRET>
    auth_header = headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided = auth_header[len("Bearer ") :].strip()
        if hmac.compare_digest(provided, expected_secret):
            return True

    # 2. Header X-Cron-Secret: <CRON_SECRET>
    cron_header = headers.get("X-Cron-Secret", "").strip()
    if cron_header and hmac.compare_digest(cron_header, expected_secret):
        return True

    # 3. Query param ?secret=<CRON_SECRET> (pratique pour cron-job.org ou test navigateur)
    query_secret = query_params.get("secret", [""])[0].strip()
    if query_secret and hmac.compare_digest(query_secret, expected_secret):
        return True

    return False


class handler(BaseHTTPRequestHandler):
    """Point d'entrée HTTP pour Vercel Serverless Functions (/api/sync)."""

    def _handle_request(self):
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)

        if not is_authorized(self.headers, query_params):
            self._send_json(
                401,
                {
                    "status": "error",
                    "message": "Non autorisé : CRON_SECRET invalide ou manquant.",
                },
            )
            return

        dry_run = query_params.get("dry_run", ["0"])[0].lower() in ("1", "true", "yes")
        keep_crossed_off: int | None = None
        if "keep_crossed_off" in query_params:
            raw_keep = query_params["keep_crossed_off"][0].strip()
            if raw_keep:
                try:
                    keep_crossed_off = int(raw_keep)
                except ValueError:
                    self._send_json(
                        400,
                        {
                            "status": "config_error",
                            "message": f"Paramètre keep_crossed_off invalide : '{raw_keep}'.",
                        },
                    )
                    return

        try:
            payload = asyncio.run(
                sync_tasks_to_groceries(
                    dry_run=dry_run,
                    keep_crossed_off=keep_crossed_off,
                )
            )
            self._send_json(200, payload)
        except ValueError as cfg_err:
            self._send_json(
                400,
                {
                    "status": "config_error",
                    "message": str(cfg_err),
                },
            )
        except Exception as exc:
            self._send_json(
                500,
                {
                    "status": "error",
                    "message": f"Erreur lors de la synchronisation : {str(exc)}",
                },
            )

    def do_GET(self):
        self._handle_request()

    def do_POST(self):
        self._handle_request()

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
