import asyncio
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
import re
import unicodedata
from urllib.parse import parse_qs, quote, urlparse
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

        # En cas d'égalité parfaite de score, on privilégie l'article barré (à réactiver)
        if score > best_score or (
            score == best_score
            and best_item is not None
            and is_item_crossed_off(item)
            and not is_item_crossed_off(best_item)
        ):
            best_score = score
            best_item = item

    if best_item is not None and best_score >= cutoff:
        return best_item, round(best_score, 3), best_item["value"]

    return None, round(best_score, 3), formatted_name


def find_catalog_item(matched_name: str | None, og_items: list[dict]) -> dict | None:
    """Retrouve un dictionnaire d'article dans og_items depuis le nom renvoyé par Gemini."""
    if not matched_name:
        return None
    candidate = matched_name.strip()
    if not candidate or candidate.lower() in ("null", "none"):
        return None

    def pick_best(matches: list[dict]) -> dict | None:
        if not matches:
            return None
        for item in matches:
            if is_item_crossed_off(item):
                return item
        return matches[0]

    # 1. Correspondance exacte
    exact = [item for item in og_items if item.get("value", "") == candidate]
    if exact:
        return pick_best(exact)

    # 2. Correspondance insensible à la casse
    lower_cand = candidate.lower()
    case_insensitive = [
        item for item in og_items if item.get("value", "").strip().lower() == lower_cand
    ]
    if case_insensitive:
        return pick_best(case_insensitive)

    # 3. Correspondance normalisée (sans accents / ligatures)
    norm_cand = normalize_for_comparison(candidate)
    if norm_cand:
        normalized = [
            item
            for item in og_items
            if normalize_for_comparison(item.get("value", "")) == norm_cand
        ]
        if normalized:
            return pick_best(normalized)

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
        "dictés à la voix sur Google Home (Google Tasks) avec les articles déjà existants dans "
        "la liste OurGroceries de l'utilisateur.\n\n"
        "Règles :\n"
        "1. Pour chaque tâche dans `tasks`, nettoie mentalement les commandes vocales et articles "
        "(ex: 'ajoute du', 'de la', 'des', 'un', 'il me faut').\n"
        "2. Cherche si un article du `catalog` correspond sémantiquement :\n"
        "   - Associe un nom générique à la marque ou variante habituelle du catalogue "
        "(ex: 'Lait' -> 'lait cawol', 'Beurre' -> 'Beurre demi-sel').\n"
        "   - Reconnais les synonymes et métonymies courantes "
        "(ex: 'Sopalin' -> 'Essuie-tout', 'PQ' -> 'Papier toilette', 'Coca' -> 'Cola').\n"
        "   - Tolère le singulier/pluriel, les accents, les ligatures (oeufs/œufs) et les fautes de dictée vocale.\n"
        "   - Ne force pas une correspondance absurde : en cas de doute ou si le produit est vraiment différent, crée un nouvel article.\n"
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


def get_google_keep_pending_items() -> tuple[object, list[dict]]:
    """
    Authentifie le client Google Keep via Master Token et retourne :
      (keep_client, liste_des_articles_non_cochés)
    """
    if gkeepapi is None:
        raise RuntimeError(
            "Le module Python 'gkeepapi' est requis pour la synchronisation Google Keep."
        )

    keep_email = get_env_clean("GOOGLE_KEEP_EMAIL") or get_env_clean("OG_USERNAME")
    keep_master_token = get_env_clean("GOOGLE_KEEP_MASTER_TOKEN")
    keep_note_id = get_env_clean("GOOGLE_KEEP_NOTE_ID")

    if not all([keep_email, keep_master_token, keep_note_id]):
        raise ValueError(
            "Variables Google Keep manquantes : GOOGLE_KEEP_EMAIL, "
            "GOOGLE_KEEP_MASTER_TOKEN et GOOGLE_KEEP_NOTE_ID sont requis."
        )

    keep = gkeepapi.Keep()
    try:
        keep.authenticate(keep_email, keep_master_token)
    except Exception as auth_err:
        raise RuntimeError(
            f"Échec d'authentification Google Keep pour '{keep_email}' : {auth_err}"
        ) from auth_err

    keep_note = keep.get(keep_note_id)
    if keep_note is None or getattr(keep_note, "trashed", False):
        non_trashed = [n for n in keep.all() if not getattr(n, "trashed", False)]
        keep_note = next(
            (
                n
                for n in non_trashed
                if (getattr(n, "title", "") or "").strip().lower() == keep_note_id.lower()
            ),
            None,
        )
        if keep_note is None:
            available = ", ".join(
                f"'{getattr(n, 'title', '') or '(sans titre)'}' (id={n.id})"
                for n in non_trashed
            ) or "aucune note trouvée"
            raise ValueError(
                f"GOOGLE_KEEP_NOTE_ID='{keep_note_id}' introuvable dans Google Keep. "
                f"Notes disponibles : {available}"
            )

    if not hasattr(keep_note, "unchecked"):
        raise ValueError(
            f"La note Google Keep '{getattr(keep_note, 'title', keep_note.id)}' "
            "n'est pas une liste à cases à cocher."
        )

    pending_items = [
        {
            "id": item.id,
            "title": item.text.strip(),
            "_keep_item": item,
        }
        for item in keep_note.unchecked
        if (getattr(item, "text", "") or "").strip()
    ]
    return keep, pending_items


async def sync_tasks_to_groceries(dry_run: bool = False) -> dict:
    """
    Exécute la synchronisation complète :
    1. Lit les articles non cochés dans Google Keep (ou Google Tasks si configuré en repli).
    2. Réconcilie chaque article avec OurGroceries (Gemini Flash + fallback algorithmique).
    3. Décoche l'article existant ou crée le nouvel article dans OurGroceries.
    4. Coche l'article dans Google Keep (ou marque la tâche terminée dans Google Tasks).
    """
    og_user = get_env_clean("OG_USERNAME")
    og_pass = get_env_clean("OG_PASSWORD")
    og_list_id = get_env_clean("OG_LIST_ID")
    if not all([og_user, og_pass, og_list_id]):
        raise ValueError(
            "Variables OurGroceries manquantes : OG_USERNAME, OG_PASSWORD et OG_LIST_ID sont requis."
        )

    cutoff = float(get_env_clean("MATCH_CUTOFF", "0.75") or "0.75")
    gemini_api_key = get_env_clean("GEMINI_API_KEY")
    gemini_model = get_env_clean("GEMINI_MODEL", "gemini-3.5-flash-lite") or "gemini-3.5-flash-lite"

    # 1. Récupération des articles non cochés depuis Google Keep (prioritaire) ou Google Tasks
    keep_master_token = get_env_clean("GOOGLE_KEEP_MASTER_TOKEN")
    keep_client = None
    tasks_service = None
    task_list_id = ""

    if keep_master_token:
        source_name = "google_keep"
        keep_client, pending_tasks = get_google_keep_pending_items()
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

    if not pending_tasks:
        return {
            "status": "ok",
            "source": source_name,
            "dry_run": dry_run,
            "synced_count": 0,
            "message": "Aucun nouvel article à synchroniser.",
            "items": [],
        }

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

    # 3. Réconciliation intelligente via Gemini Flash (avec fallback algorithmique)
    gemini_matches: dict[int, dict] = {}
    gemini_error: str | None = None
    if gemini_api_key:
        try:
            raw_results = match_tasks_with_gemini(
                [t["title"] for t in pending_tasks],
                og_items,
                api_key=gemini_api_key,
                model=gemini_model,
            )
            for entry in raw_results:
                if isinstance(entry, dict) and isinstance(entry.get("task_index"), int):
                    gemini_matches[entry["task_index"]] = entry
        except Exception as exc:
            gemini_error = str(exc)

    # 4. Traitement de chaque tâche
    processed_items = []
    keep_dirty = False
    for idx, task in enumerate(pending_tasks):
        raw_title = task["title"]

        if idx in gemini_matches:
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
                    await og.toggle_item_crossed_off(
                        list_id=og_list_id,
                        item_id=matched_item["id"],
                        cross_off=False,
                    )
                matched_item["crossedOff"] = False
                matched_item["crossedOffAt"] = None
            else:
                action = "already_active"
        else:
            action = "created_new"
            if not dry_run:
                await og.add_item_to_list(
                    list_id=og_list_id,
                    value=target_name,
                    auto_category=True,
                )
            # Ajout en mémoire pour éviter un doublon si le même article apparaît 2 fois dans le lot
            og_items.append(
                {"id": None, "value": target_name, "crossedOff": False, "crossedOffAt": None}
            )

        # 5. Cocher l'article dans Google Keep (ou marquer terminé dans Google Tasks)
        if not dry_run:
            if source_name == "google_keep":
                task["_keep_item"].checked = True
                keep_dirty = True
            else:
                tasks_service.tasks().patch(
                    tasklist=task_list_id,
                    task=task["id"],
                    body={"status": "completed"},
                ).execute()

        processed_items.append(
            {
                "google_task_id": task["id"],
                "raw_title": raw_title,
                "ourgroceries_name": target_name,
                "action": action,
                "similarity_score": score,
                "matching_engine": matching_engine,
                "reason": reason,
            }
        )

    if not dry_run and source_name == "google_keep" and keep_dirty and keep_client is not None:
        keep_client.sync()

    return {
        "status": "ok",
        "source": source_name,
        "dry_run": dry_run,
        "synced_count": len(processed_items),
        "message": f"{len(processed_items)} article(s) traité(s) avec succès.",
        "items": processed_items,
    }


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

        try:
            payload = asyncio.run(sync_tasks_to_groceries(dry_run=dry_run))
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
