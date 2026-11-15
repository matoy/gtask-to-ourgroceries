import asyncio
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
import re
import unicodedata
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from ourgroceries import OurGroceries

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

    best_item = None
    best_score = 0.0

    for item in og_items:
        item_value = item.get("value", "").strip()
        if not item_value:
            continue

        norm_cat = normalize_for_comparison(item_value)
        sing_cat = singularize_french(norm_cat)

        if norm_input == norm_cat or (sing_input and sing_input == sing_cat):
            score = 1.0
        else:
            score_raw = SequenceMatcher(None, norm_input, norm_cat).ratio()
            score_sing = SequenceMatcher(None, sing_input, sing_cat).ratio()
            score = max(score_raw, score_sing)

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


def get_google_tasks_service():
    """Construit le client Google Tasks API via OAuth2 Refresh Token."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

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


async def sync_tasks_to_groceries(dry_run: bool = False) -> dict:
    """
    Exécute la synchronisation complète :
    1. Lit les tâches non terminées dans la liste Google Tasks dédiée.
    2. Compare chaque tâche aux articles existants (actifs ou barrés) de la liste OurGroceries.
    3. Décoche l'article existant ou crée le nouvel article dans OurGroceries.
    4. Marque la tâche comme terminée dans Google Tasks.
    """
    task_list_id = os.environ.get("GOOGLE_TASK_LIST_ID")
    if not task_list_id:
        raise ValueError("Variable GOOGLE_TASK_LIST_ID manquante.")

    og_user = os.environ.get("OG_USERNAME")
    og_pass = os.environ.get("OG_PASSWORD")
    og_list_id = os.environ.get("OG_LIST_ID")
    if not all([og_user, og_pass, og_list_id]):
        raise ValueError(
            "Variables OurGroceries manquantes : OG_USERNAME, OG_PASSWORD et OG_LIST_ID sont requis."
        )

    cutoff = float(os.environ.get("MATCH_CUTOFF", "0.75"))

    # 1. Récupération des tâches non cochées dans Google Tasks
    tasks_service = get_google_tasks_service()
    results = (
        tasks_service.tasks()
        .list(tasklist=task_list_id, showCompleted=False, maxResults=100)
        .execute()
    )
    raw_tasks = results.get("items", [])

    # Filtrer les tâches ayant un titre non vide
    pending_tasks = [t for t in raw_tasks if t.get("title", "").strip()]

    if not pending_tasks:
        return {
            "status": "ok",
            "dry_run": dry_run,
            "synced_count": 0,
            "message": "Aucune nouvelle tâche à synchroniser.",
            "items": [],
        }

    # 2. Connexion à OurGroceries et lecture des éléments de la liste cible
    og = OurGroceries(og_user, og_pass)
    await og.login()

    list_details = await og.get_list_items(list_id=og_list_id)
    og_items = list_details.get("list", {}).get("items", [])

    # 3. Traitement de chaque tâche
    processed_items = []
    for task in pending_tasks:
        raw_title = task["title"]
        matched_item, score, target_name = parse_and_match_item(
            raw_title, og_items, cutoff=cutoff
        )

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

        # 4. Marquer la tâche comme "Terminée" dans Google Tasks
        if not dry_run:
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
            }
        )

    return {
        "status": "ok",
        "dry_run": dry_run,
        "synced_count": len(processed_items),
        "message": f"{len(processed_items)} article(s) traité(s) avec succès.",
        "items": processed_items,
    }


def is_authorized(headers, query_params: dict) -> bool:
    """Vérifie le jeton CRON_SECRET s'il est configuré dans les variables d'environnement."""
    expected_secret = os.environ.get("CRON_SECRET", "").strip()
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
