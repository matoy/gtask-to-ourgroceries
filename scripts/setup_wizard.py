#!/usr/bin/env python3
"""
Assistant d'initialisation interactif pour gtask-to-ourgroceries (Google Keep -> OurGroceries).

Ce script permet de :
1. Obtenir (ou réutiliser depuis .env) votre GOOGLE_KEEP_MASTER_TOKEN.
2. Lister vos listes Google Keep (personnelles et partagées) et sélectionner une ou plusieurs listes (GOOGLE_KEEP_NOTE_ID).
3. Tester la connexion OurGroceries et récupérer votre OG_LIST_ID.
4. Configurer optionnellement votre clé gratuite GEMINI_API_KEY (Google AI Studio).
5. Générer (ou conserver) un CRON_SECRET et sauvegarder toutes les clés dans .env,
   prêt à être copié-collé dans les Environment Variables de Vercel.

Prérequis :
    pip install -r requirements.txt gpsoauth
"""

import asyncio
import getpass
import json
import os
import re
import secrets
import sys
import time
from typing import Any
import urllib.error
from urllib.parse import quote, unquote
import urllib.request
import webbrowser


KEEP_NOTE_URL_RE = re.compile(
    r"^https?://keep\.google\.com/(?:u/\d+/)?#[a-z]+/([^/?#]+)",
    re.IGNORECASE,
)


def parse_keep_note_ids(raw_value: str) -> list[str]:
    """Découpe GOOGLE_KEEP_NOTE_ID (séparé par virgules/retours ligne, guillemets ou URL Keep)."""
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
    """Extrait l'état de partage (propriétaire, collaborateurs) d'une note Google Keep."""
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


def format_keep_note_badges(note: Any, current_email: str = "") -> str:
    """Formate les badges visuels de partage et d'archivage pour l'assistant."""
    sharing = describe_keep_note_sharing(note, current_email=current_email)
    badges: list[str] = []
    if sharing["shared_with_me"]:
        if sharing["owner"]:
            badges.append(f"[🤝 partagée par {sharing['owner']}]")
        else:
            badges.append("[🤝 partagée avec vous]")
    elif sharing["other_collaborators"]:
        badges.append(f"[🤝 partagée avec {', '.join(sharing['other_collaborators'])}]")
    if getattr(note, "archived", False):
        badges.append("[📦 archivée]")
    return " ".join(badges)


def sort_keep_notes_for_wizard(notes: list[Any]) -> list[Any]:
    """Filtre les notes supprimées/corbeille et trie : listes actives, archivées, puis notes texte."""
    live_notes = [
        n
        for n in notes
        if not getattr(n, "trashed", False) and not getattr(n, "deleted", False)
    ]
    return sorted(
        live_notes,
        key=lambda n: (not hasattr(n, "unchecked"), bool(getattr(n, "archived", False))),
    )


def find_default_keep_indices(notes: list[Any], raw_current_ids: str) -> list[int]:
    """Retourne les indices (base 1) des listes correspondant à GOOGLE_KEEP_NOTE_ID."""
    tokens = parse_keep_note_ids(raw_current_ids)
    if not tokens:
        return []

    indices: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        id_matches = [
            idx
            for idx, note in enumerate(notes, start=1)
            if getattr(note, "id", None) == token
            or (
                getattr(note, "server_id", None) is not None
                and getattr(note, "server_id", None) == token
            )
        ]
        if id_matches:
            for idx in id_matches:
                if idx not in seen:
                    seen.add(idx)
                    indices.append(idx)
            continue

        token_lower = token.strip().lower()
        title_matches = [
            (idx, note)
            for idx, note in enumerate(notes, start=1)
            if hasattr(note, "unchecked")
            and (getattr(note, "title", "") or "").strip().lower() == token_lower
        ]
        if title_matches:
            active = [idx for idx, note in title_matches if not getattr(note, "archived", False)]
            chosen = active if active else [idx for idx, _ in title_matches]
            for idx in chosen:
                if idx not in seen:
                    seen.add(idx)
                    indices.append(idx)

    return indices


def parse_wizard_keep_selection(
    choice: str,
    notes: list[Any],
    default_indices: list[int],
) -> tuple[list[Any], bool, list[str]]:
    """
    Parse la saisie utilisateur dans l'assistant Keep (ex: '', '1,2', '0', '0,2', ID ou URL).
    Retourne (listes_sélectionnées, créer_nouvelle_liste, erreurs).
    """
    selected: list[Any] = []
    seen_ids: set[str] = set()
    errors: list[str] = []
    create_new = False

    def _add_checklist(note: Any) -> None:
        if not hasattr(note, "unchecked"):
            title = (getattr(note, "title", "") or "").strip() or "(Sans titre)"
            errors.append(
                f"La note '{title}' (ID: {note.id}) est une note texte sans cases à cocher."
            )
            return
        nid = getattr(note, "id", None)
        if nid not in seen_ids:
            if nid is not None:
                seen_ids.add(nid)
            selected.append(note)

    raw_choice = choice.strip()
    if not raw_choice:
        if default_indices:
            for idx in default_indices:
                if 1 <= idx <= len(notes):
                    _add_checklist(notes[idx - 1])
            return selected, False, errors

        first_checklist = next((n for n in notes if hasattr(n, "unchecked")), None)
        if first_checklist is not None:
            _add_checklist(first_checklist)
            return selected, False, errors

        return [], True, []

    tokens = [t.strip() for t in re.split(r"[\s,;]+", raw_choice) if t.strip()]
    for raw_token in tokens:
        token = raw_token
        if len(token) >= 2 and (
            (token[0] == '"' and token[-1] == '"')
            or (token[0] == "'" and token[-1] == "'")
        ):
            token = token[1:-1].strip()
        if not token:
            continue

        if token == "0":
            create_new = True
            continue

        if token.isdigit():
            num = int(token)
            if 1 <= num <= len(notes):
                _add_checklist(notes[num - 1])
            else:
                errors.append(
                    f"Numéro invalide : {token} (attendu entre 1 et {len(notes)}, ou 0)."
                )
            continue

        url_match = KEEP_NOTE_URL_RE.match(token)
        norm_token = unquote(url_match.group(1)).strip() if url_match else token
        matched_note = next(
            (
                n
                for n in notes
                if getattr(n, "id", None) == norm_token
                or (
                    getattr(n, "server_id", None) is not None
                    and getattr(n, "server_id", None) == norm_token
                )
            ),
            None,
        )
        if matched_note is not None:
            _add_checklist(matched_note)
        else:
            errors.append(f"Choix ou ID inconnu : '{token}'.")

    return selected, create_new, errors


def load_existing_env(env_path: str = ".env") -> dict[str, str]:
    """Charge les variables existantes du fichier .env s'il est présent."""
    env_vars: dict[str, str] = {}
    if not os.path.exists(env_path):
        return env_vars

    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            env_vars[key] = val
    return env_vars


def check_dependencies():
    missing = []
    for mod, pkg in [
        ("gkeepapi", "gkeepapi"),
        ("gpsoauth", "gpsoauth"),
        ("ourgroceries", "ourgroceries"),
    ]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if missing:
        print("❌ Dépendances Python manquantes :", ", ".join(missing))
        print("\nInstallez-les avec la commande suivante :")
        print("    pip install -r requirements.txt gpsoauth\n")
        sys.exit(1)


def setup_google_keep(existing_env: dict[str, str]) -> tuple[str, str, str]:
    """Configure Google Keep et retourne (email, master_token, note_id)."""
    import gkeepapi
    import gpsoauth

    print("\n" + "=" * 65)
    print(" ÉTAPE 1/4 : Configuration Google Keep")
    print("=" * 65)

    default_email = existing_env.get("GOOGLE_KEEP_EMAIL") or existing_env.get(
        "OG_USERNAME", ""
    )
    existing_token = existing_env.get("GOOGLE_KEEP_MASTER_TOKEN", "")
    current_note_ids = (
        existing_env.get("GOOGLE_KEEP_NOTE_ID")
        or existing_env.get("GOOGLE_KEEP_NOTE_IDS", "")
    )

    keep = gkeepapi.Keep()
    keep_email = default_email
    master_token = existing_token
    authenticated = False

    if keep_email and master_token:
        reuse = (
            input(
                f"🔄 Token Google Keep détecté dans .env pour '{keep_email}'. Le réutiliser ? [O/n] : "
            )
            .strip()
            .lower()
        )
        if reuse in ("", "o", "oui", "y", "yes"):
            try:
                print("🔄 Connexion à Google Keep en cours...")
                keep.authenticate(keep_email, master_token)
                authenticated = True
                print("✅ Connexion Google Keep existante validée !")
            except Exception as exc:
                print(f"⚠️ Impossible de réutiliser le token existant ({exc}). Reconnexion...")
                authenticated = False

    while not authenticated:
        email_prompt = (
            f"\n👉 Adresse e-mail Google Keep [{default_email}] : "
            if default_email
            else "\n👉 Adresse e-mail Google Keep (GOOGLE_KEEP_EMAIL) : "
        )
        keep_email = input(email_prompt).strip() or default_email
        if not keep_email:
            print("❌ L'adresse e-mail Google Keep est requise.")
            sys.exit(1)

        print(
            "\nPour obtenir votre Master Token permanent (une seule fois) :\n"
            "  1. Ouvrez https://accounts.google.com/EmbeddedSetup dans votre navigateur\n"
            "     et connectez-vous avec votre compte Google (cliquez sur 'J'accepte' ;\n"
            "     il est normal que la page finale mouline sans fin).\n"
            "  2. Ouvrez les outils de développement (F12) -> onglet Application (Chrome/Edge)\n"
            "     ou Stockage (Firefox) -> Cookies -> https://accounts.google.com\n"
            "  3. Copiez la valeur du cookie nommé 'oauth_token' (commence par oauth2_4/...).\n"
            "     (Vous pouvez aussi coller directement un Master Token aas_et/... existant.)"
        )

        open_browser = (
            input("\n🌐 Ouvrir https://accounts.google.com/EmbeddedSetup maintenant ? [O/n] : ")
            .strip()
            .lower()
        )
        if open_browser in ("", "o", "oui", "y", "yes"):
            webbrowser.open("https://accounts.google.com/EmbeddedSetup")

        token_input = input(
            "\n👉 Collez le cookie 'oauth_token' (oauth2_4/...) ou Master Token (aas_et/...) : "
        ).strip().strip('"').strip("'")

        if not token_input:
            print("❌ Jeton requis.")
            continue

        if token_input.startswith("aas_et/"):
            master_token = token_input
        else:
            print("🔄 Échange du cookie oauth_token contre un Master Token permanent...")
            exchange = gpsoauth.exchange_token(
                keep_email, token_input, "0123456789abcdef"
            )
            if "Token" not in exchange:
                print(f"❌ Échec de l'échange : {exchange}")
                continue
            master_token = exchange["Token"]
            print("✅ Master Token généré avec succès !")

        try:
            print("🔄 Connexion et synchronisation Google Keep...")
            keep.authenticate(keep_email, master_token)
            authenticated = True
            print("✅ Authentification Google Keep réussie !")
        except Exception as exc:
            print(f"❌ Échec de connexion Google Keep : {exc}")

    notes = sort_keep_notes_for_wizard(keep.all())
    default_indices = find_default_keep_indices(notes, current_note_ids)
    if not default_indices:
        first_checklist_idx = next(
            (i for i, n in enumerate(notes, start=1) if hasattr(n, "unchecked")),
            None,
        )
        if first_checklist_idx is not None:
            default_indices = [first_checklist_idx]

    print("\n📝 Vos listes et notes Google Keep disponibles :")
    print("  [0] ➕ Créer une nouvelle liste Google Keep")
    for idx, note in enumerate(notes, start=1):
        title = (getattr(note, "title", "") or "").strip() or "(Sans titre)"
        badges = format_keep_note_badges(note, current_email=keep_email)
        badge_suffix = f" {badges}" if badges else ""
        if hasattr(note, "unchecked") and hasattr(note, "checked"):
            info = f"({len(note.unchecked)} non cochés, {len(note.checked)} cochés)"
        else:
            info = "(note texte — pas de cases à cocher)"

        marker = "  👈 (actuel dans .env)" if idx in default_indices else ""
        print(f"  [{idx}] {title}{badge_suffix}  {info} — ID: {note.id}{marker}")

    default_display = (
        ",".join(str(i) for i in default_indices) if default_indices else "0"
    )
    while True:
        choice = input(
            f"\n👉 Choisissez un ou plusieurs numéros séparés par des virgules "
            f"(ex: 1,2 — ou 0 pour créer une nouvelle liste) [{default_display}] : "
        ).strip()
        selected_notes, create_new, errors = parse_wizard_keep_selection(
            choice, notes, default_indices
        )
        if errors:
            for err in errors:
                print(f"❌ {err}")
            continue
        if not selected_notes and not create_new:
            print("❌ Veuillez sélectionner au moins une liste ou saisir 0 pour en créer une.")
            continue
        break

    if create_new:
        new_name = (
            input("Nom de la nouvelle liste Google Keep (ex: Liste de courses) : ").strip()
            or "Liste de courses"
        )
        created = keep.createList(new_name, [])
        keep.sync()
        selected_notes.append(created)
        print(f"✅ Liste '{created.title}' créée avec l'ID : {created.id}")

    note_ids_value = ",".join(n.id for n in selected_notes)
    print(f"\n✅ {len(selected_notes)} liste(s) Google Keep sélectionnée(s) :")
    for note in selected_notes:
        title = (getattr(note, "title", "") or "").strip() or "(Sans titre)"
        badges = format_keep_note_badges(note, current_email=keep_email)
        badge_suffix = f" {badges}" if badges else ""
        print(f"   • {title}{badge_suffix} (ID: {note.id})")

    return keep_email, master_token, note_ids_value


async def setup_ourgroceries(existing_env: dict[str, str]) -> tuple[str, str, str]:
    """Teste la connexion OurGroceries et retourne (username, password, list_id)."""
    from ourgroceries import OurGroceries

    print("\n" + "=" * 65)
    print(" ÉTAPE 2/4 : Configuration OurGroceries")
    print("=" * 65)

    default_user = existing_env.get("OG_USERNAME", "")
    default_pass = existing_env.get("OG_PASSWORD", "")
    current_og_list_id = existing_env.get("OG_LIST_ID", "")

    user_prompt = (
        f"👉 Adresse e-mail OurGroceries [{default_user}] : "
        if default_user
        else "👉 Adresse e-mail OurGroceries (OG_USERNAME) : "
    )
    og_user = input(user_prompt).strip() or default_user

    pass_prompt = (
        "👉 Mot de passe OurGroceries [Entrée pour conserver celui du .env] : "
        if default_pass
        else "👉 Mot de passe OurGroceries (OG_PASSWORD) : "
    )
    try:
        entered_pass = getpass.getpass(pass_prompt).strip()
    except Exception:
        entered_pass = input(pass_prompt).strip()
    og_pass = entered_pass or default_pass

    if not og_user or not og_pass:
        print("❌ Email et mot de passe OurGroceries requis.")
        sys.exit(1)

    print("\n🔄 Connexion à OurGroceries et lecture des listes en cours...")
    og = OurGroceries(og_user, og_pass)
    await og.login()

    lists_response = await og.get_my_lists()
    shopping_lists = lists_response.get("shoppingLists", [])

    if not shopping_lists:
        print("❌ Aucune liste de courses trouvée sur ce compte OurGroceries.")
        sys.exit(1)

    default_choice = 1
    print("\n🛒 Vos listes de courses OurGroceries :")
    for idx, sl in enumerate(shopping_lists, start=1):
        details = await og.get_list_items(list_id=sl["id"])
        items = details.get("list", {}).get("items", [])
        crossed_count = sum(
            1 for item in items if item.get("crossedOffAt") or item.get("crossedOff")
        )
        active_count = len(items) - crossed_count

        marker = ""
        if sl["id"] == current_og_list_id:
            default_choice = idx
            marker = "  👈 (actuel dans .env)"

        print(
            f"  [{idx}] {sl['name']}  ({active_count} actifs, {crossed_count} barrés) — ID: {sl['id']}{marker}"
        )

    choice = input(
        f"\n👉 Choisissez le numéro de la liste cible [{default_choice}] : "
    ).strip()
    idx = (
        int(choice) - 1
        if choice.isdigit() and 1 <= int(choice) <= len(shopping_lists)
        else default_choice - 1
    )
    selected = shopping_lists[idx]
    og_list_id = selected["id"]
    print(f"✅ Liste OurGroceries sélectionnée : '{selected['name']}' (ID: {og_list_id})")

    return og_user, og_pass, og_list_id


def test_gemini_api_key(api_key: str, model: str = "gemini-3.5-flash-lite") -> bool:
    """Effectue un véritable appel generateContent pour valider la clé et le modèle."""
    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent?key={quote(api_key, safe='')}"
    )
    payload: dict = {"contents": [{"parts": [{"text": "Réponds uniquement par OK."}]}]}
    if model.startswith("gemini-3") and "lite" not in model:
        payload["generationConfig"] = {"thinkingConfig": {"thinkingLevel": "low"}}
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        candidates = data.get("candidates", [])
        if not candidates:
            print(f"⚠️ Aucun candidat renvoyé par {model} ({elapsed_ms} ms).")
            return False
        parts = candidates[0].get("content", {}).get("parts", [])
        reply_text = "".join(part.get("text", "") for part in parts).strip()
        if reply_text:
            print(f"✅ Réponse reçue de {model} ({elapsed_ms} ms) : {reply_text}")
            return True
        print(f"⚠️ Réponse vide de {model} ({elapsed_ms} ms).")
        return False
    except urllib.error.HTTPError as http_err:
        err_body = http_err.read().decode("utf-8", errors="replace")
        print(f"⚠️ Erreur API Gemini (HTTP {http_err.code}) : {err_body[:250]}")
        return False
    except Exception as exc:
        print(f"⚠️ Impossible de contacter l'API Gemini : {exc}")
        return False


def setup_gemini(existing_env: dict[str, str]) -> str:
    """Configure optionnellement la clé gratuite Google AI Studio (GEMINI_API_KEY)."""
    print("\n" + "=" * 65)
    print(" ÉTAPE 3/4 : Configuration Gemini AI (Smart Matching)")
    print("=" * 65)
    print(
        "Pour un matching intelligent (marques habituelles comme 'Lait' -> 'lait cawol',\n"
        "synonymes comme 'Sopalin' -> 'Essuie-tout', fautes de dictée vocale),\n"
        "générez une clé API 100 % gratuite (sans carte bancaire) sur :\n"
        "  👉 https://aistudio.google.com/apikey  (bouton 'Create API key')\n"
        "(Laissez vide et appuyez sur Entrée pour utiliser uniquement le matching local.)"
    )

    default_key = existing_env.get("GEMINI_API_KEY", "")
    model = (
        existing_env.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
        or "gemini-3.5-flash-lite"
    )

    while True:
        if default_key:
            masked = f"{default_key[:8]}...{default_key[-4:]}" if len(default_key) > 12 else "***"
            prompt = f"\n👉 GEMINI_API_KEY [{masked} — Entrée pour conserver] : "
        else:
            prompt = "\n👉 GEMINI_API_KEY (optionnel) : "

        entered = input(prompt).strip()
        api_key = entered or default_key

        if not api_key:
            print("ℹ️ Aucune clé Gemini renseignée : le matching algorithmique local sera utilisé.")
            return ""

        print(f"🔄 Test de la clé API avec le modèle {model}...")
        if test_gemini_api_key(api_key, model=model):
            print(f"✅ Clé Gemini validée ({model}) !")
            return api_key

        retry = input("Voulez-vous saisir une autre clé ? [O/n] : ").strip().lower()
        if retry in ("n", "non", "no"):
            print("⚠️ Clé conservée malgré l'échec du test.")
            return api_key
        default_key = ""


def main():
    if os.name == "nt":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")

    check_dependencies()

    env_path = os.path.join(os.getcwd(), ".env")
    existing_env = load_existing_env(env_path)
    if existing_env:
        print("📂 Fichier .env existant détecté : ses valeurs seront proposées par défaut.")

    keep_email, keep_master_token, keep_note_id = setup_google_keep(existing_env)
    og_user, og_pass, og_list_id = asyncio.run(setup_ourgroceries(existing_env))
    gemini_api_key = setup_gemini(existing_env)

    cron_secret = existing_env.get("CRON_SECRET") or secrets.token_urlsafe(24)
    match_cutoff = existing_env.get("MATCH_CUTOFF") or "0.75"

    env_lines = [
        "# Généré automatiquement par scripts/setup_wizard.py",
        f'GOOGLE_KEEP_EMAIL="{keep_email}"',
        f'GOOGLE_KEEP_MASTER_TOKEN="{keep_master_token}"',
        f'GOOGLE_KEEP_NOTE_ID="{keep_note_id}"',
        f'OG_USERNAME="{og_user}"',
        f'OG_PASSWORD="{og_pass}"',
        f'OG_LIST_ID="{og_list_id}"',
        f'GEMINI_API_KEY="{gemini_api_key}"',
        f'CRON_SECRET="{cron_secret}"',
        f'MATCH_CUTOFF="{match_cutoff}"',
    ]

    # Préserve d'éventuelles clés Google Tasks / GEMINI_MODEL / OG_KEEP_CROSSED_OFF existantes dans .env
    for legacy_key in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GOOGLE_TASK_LIST_ID",
        "GEMINI_MODEL",
        "OG_KEEP_CROSSED_OFF",
    ):
        if existing_env.get(legacy_key):
            env_lines.append(f'{legacy_key}="{existing_env[legacy_key]}"')

    env_lines.append("")

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines))

    print("\n" + "=" * 65)
    print(" 🎉 ÉTAPE 4/4 : Configuration terminée !")
    print("=" * 65)
    print(f"📁 Fichier local sauvegardé dans : {env_path} (ignoré par Git)")
    print("\n📋 Variables à copier dans Vercel (Settings > Environment Variables) :\n")
    print(f"  GOOGLE_KEEP_EMAIL        = {keep_email}")
    print(f"  GOOGLE_KEEP_MASTER_TOKEN = {keep_master_token}")
    print(f"  GOOGLE_KEEP_NOTE_ID      = {keep_note_id}")
    print(f"  OG_USERNAME              = {og_user}")
    print(f"  OG_PASSWORD              = {og_pass}")
    print(f"  OG_LIST_ID               = {og_list_id}")
    print(f"  GEMINI_API_KEY           = {gemini_api_key or '(non configuré — fallback local)'}")
    print(f"  CRON_SECRET              = {cron_secret}")
    print(f"  MATCH_CUTOFF             = {match_cutoff}")
    print("\n⏰ Sur cron-job.org, appelez toutes les 5 minutes :")
    print(f"  URL : https://VOTRE-PROJET.vercel.app/api/sync?secret={cron_secret}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
