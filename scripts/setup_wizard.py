#!/usr/bin/env python3
"""
Assistant d'initialisation interactif pour gtask-to-ourgroceries (Google Keep -> OurGroceries).

Ce script permet de :
1. Obtenir (ou réutiliser depuis .env) votre GOOGLE_KEEP_MASTER_TOKEN.
2. Lister vos listes Google Keep et récupérer votre GOOGLE_KEEP_NOTE_ID.
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
import secrets
import sys
import time
import urllib.error
from urllib.parse import quote
import urllib.request
import webbrowser


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
    current_note_id = existing_env.get("GOOGLE_KEEP_NOTE_ID", "")

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

    notes = [n for n in keep.all() if not getattr(n, "trashed", False)]
    default_choice = 1
    print("\n📝 Vos listes et notes Google Keep disponibles :")
    print("  [0] ➕ Créer une nouvelle liste Google Keep")
    for idx, note in enumerate(notes, start=1):
        title = (getattr(note, "title", "") or "").strip() or "(Sans titre)"
        if hasattr(note, "unchecked") and hasattr(note, "checked"):
            info = f"({len(note.unchecked)} non cochés, {len(note.checked)} cochés)"
        else:
            info = "(note texte — pas de cases à cocher)"

        marker = ""
        if note.id == current_note_id:
            default_choice = idx
            marker = "  👈 (actuel dans .env)"

        print(f"  [{idx}] {title}  {info} — ID: {note.id}{marker}")

    choice = input(
        f"\n👉 Choisissez le numéro de la liste à surveiller [{default_choice}] : "
    ).strip()
    if choice == "0" or not notes:
        new_name = (
            input("Nom de la nouvelle liste Google Keep (ex: Liste de courses) : ").strip()
            or "Liste de courses"
        )
        created = keep.createList(new_name, [])
        keep.sync()
        note_id = created.id
        print(f"✅ Liste '{created.title}' créée avec l'ID : {note_id}")
    else:
        idx = (
            int(choice) - 1
            if choice.isdigit() and 1 <= int(choice) <= len(notes)
            else default_choice - 1
        )
        selected = notes[idx]
        note_id = selected.id
        selected_title = (getattr(selected, "title", "") or "").strip() or "(Sans titre)"
        print(f"✅ Liste sélectionnée : '{selected_title}' (ID: {note_id})")

    return keep_email, master_token, note_id


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

    # Préserve d'éventuelles clés Google Tasks / GEMINI_MODEL existantes dans .env
    for legacy_key in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
        "GOOGLE_TASK_LIST_ID",
        "GEMINI_MODEL",
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
