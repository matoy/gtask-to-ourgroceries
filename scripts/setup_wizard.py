#!/usr/bin/env python3
"""
Assistant d'initialisation interactif pour gtask-to-ourgroceries.

Ce script permet de :
1. Obtenir (ou réutiliser depuis .env) votre GOOGLE_REFRESH_TOKEN via OAuth2.
2. Lister vos listes Google Tasks et récupérer votre GOOGLE_TASK_LIST_ID.
3. Tester la connexion OurGroceries et récupérer votre OG_LIST_ID.
4. Générer (ou conserver) un CRON_SECRET et sauvegarder toutes les clés dans .env,
   prêt à être copié-collé dans les Environment Variables de Vercel.

Prérequis :
    pip install -r requirements.txt google-auth-oauthlib
"""

import asyncio
import getpass
import glob
import json
import os
import secrets
import sys


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
        ("google_auth_oauthlib", "google-auth-oauthlib"),
        ("googleapiclient", "google-api-python-client"),
        ("ourgroceries", "ourgroceries"),
    ]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if missing:
        print("❌ Dépendances Python manquantes :", ", ".join(missing))
        print("\nInstallez-les avec la commande suivante :")
        print("    pip install -r requirements.txt google-auth-oauthlib\n")
        sys.exit(1)


def setup_google_tasks(existing_env: dict[str, str]) -> tuple[str, str, str, str]:
    """Configure OAuth2 Google Tasks et retourne (client_id, client_secret, refresh_token, task_list_id)."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/tasks"]
    print("\n" + "=" * 65)
    print(" ÉTAPE 1/3 : Configuration Google Tasks (OAuth2)")
    print("=" * 65)

    client_id = existing_env.get("GOOGLE_CLIENT_ID", "")
    client_secret = existing_env.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = existing_env.get("GOOGLE_REFRESH_TOKEN", "")
    current_task_list_id = existing_env.get("GOOGLE_TASK_LIST_ID", "")

    creds = None

    # Si un Refresh Token existe déjà dans .env, proposer de le réutiliser
    if client_id and client_secret and refresh_token:
        reuse = (
            input(
                "🔄 Identifiants Google détectés dans .env. Les réutiliser sans rouvrir le navigateur ? [O/n] : "
            )
            .strip()
            .lower()
        )
        if reuse in ("", "o", "oui", "y", "yes"):
            try:
                creds = Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=client_id,
                    client_secret=client_secret,
                    scopes=scopes,
                )
                creds.refresh(Request())
                print("✅ Connexion Google Tasks existante validée !")
            except Exception as exc:
                print(f"⚠️ Impossible de réutiliser le token existant ({exc}). Reconnexion...")
                creds = None

    if creds is None:
        # Recherche automatique d'un fichier credentials.json ou client_secret*.json
        candidate_files = glob.glob("credentials.json") + glob.glob("client_secret*.json")
        client_config = None

        if candidate_files:
            chosen_file = candidate_files[0]
            print(f"📄 Fichier d'identifiants détecté : {chosen_file}")
            with open(chosen_file, "r", encoding="utf-8") as f:
                client_config = json.load(f)
            section = client_config.get("installed") or client_config.get("web") or {}
            client_id = section.get("client_id", client_id)
            client_secret = section.get("client_secret", client_secret)
        else:
            prompt_cid = (
                f"👉 GOOGLE_CLIENT_ID [{client_id[:20]}...] : "
                if client_id
                else "👉 GOOGLE_CLIENT_ID : "
            )
            client_id_in = input(prompt_cid).strip() or client_id

            prompt_csec = (
                "👉 GOOGLE_CLIENT_SECRET [Entrée pour conserver celui du .env] : "
                if client_secret
                else "👉 GOOGLE_CLIENT_SECRET : "
            )
            client_secret_in = input(prompt_csec).strip() or client_secret

            if not client_id_in or not client_secret_in:
                print("❌ Client ID et Client Secret requis.")
                sys.exit(1)

            client_id = client_id_in
            client_secret = client_secret_in
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }

        print("\n🌐 Ouverture du navigateur pour autoriser l'accès à Google Tasks...")
        flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
        )

        if not creds.refresh_token:
            print(
                "⚠️ Aucun refresh_token reçu. Réessayez en révoquant l'accès sur "
                "https://myaccount.google.com/permissions"
            )
            sys.exit(1)

        refresh_token = creds.refresh_token
        print("✅ Authentification Google réussie !")

    service = build("tasks", "v1", credentials=creds, cache_discovery=False)
    res = service.tasklists().list(maxResults=100).execute()
    tasklists = res.get("items", [])

    default_choice = 1
    print("\n📋 Vos listes Google Tasks disponibles :")
    print("  [0] ➕ Créer une nouvelle liste Google Tasks")
    for idx, tl in enumerate(tasklists, start=1):
        marker = ""
        if tl["id"] == current_task_list_id:
            default_choice = idx
            marker = "  👈 (actuel dans .env)"
        print(f"  [{idx}] {tl['title']}  (ID: {tl['id']}){marker}")

    choice = input(
        f"\n👉 Choisissez le numéro de la liste à surveiller [{default_choice}] : "
    ).strip()
    if choice == "0":
        new_name = (
            input("Nom de la nouvelle liste Google Tasks (ex: Courses) : ").strip()
            or "Courses"
        )
        created = service.tasklists().insert(body={"title": new_name}).execute()
        task_list_id = created["id"]
        print(f"✅ Liste '{created['title']}' créée avec l'ID : {task_list_id}")
    else:
        idx = (
            int(choice) - 1
            if choice.isdigit() and 1 <= int(choice) <= len(tasklists)
            else default_choice - 1
        )
        selected = tasklists[idx]
        task_list_id = selected["id"]
        print(f"✅ Liste sélectionnée : '{selected['title']}' (ID: {task_list_id})")

    return client_id, client_secret, refresh_token, task_list_id


async def setup_ourgroceries(existing_env: dict[str, str]) -> tuple[str, str, str]:
    """Teste la connexion OurGroceries et retourne (username, password, list_id)."""
    from ourgroceries import OurGroceries

    print("\n" + "=" * 65)
    print(" ÉTAPE 2/3 : Configuration OurGroceries")
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


def main():
    check_dependencies()

    env_path = os.path.join(os.getcwd(), ".env")
    existing_env = load_existing_env(env_path)
    if existing_env:
        print("📂 Fichier .env existant détecté : ses valeurs seront proposées par défaut.")

    client_id, client_secret, refresh_token, task_list_id = setup_google_tasks(
        existing_env
    )
    og_user, og_pass, og_list_id = asyncio.run(setup_ourgroceries(existing_env))

    cron_secret = existing_env.get("CRON_SECRET") or secrets.token_urlsafe(24)
    match_cutoff = existing_env.get("MATCH_CUTOFF") or "0.75"

    env_lines = [
        "# Généré automatiquement par scripts/setup_wizard.py",
        f'GOOGLE_CLIENT_ID="{client_id}"',
        f'GOOGLE_CLIENT_SECRET="{client_secret}"',
        f'GOOGLE_REFRESH_TOKEN="{refresh_token}"',
        f'GOOGLE_TASK_LIST_ID="{task_list_id}"',
        f'OG_USERNAME="{og_user}"',
        f'OG_PASSWORD="{og_pass}"',
        f'OG_LIST_ID="{og_list_id}"',
        f'CRON_SECRET="{cron_secret}"',
        f'MATCH_CUTOFF="{match_cutoff}"',
        "",
    ]

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(env_lines))

    print("\n" + "=" * 65)
    print(" 🎉 ÉTAPE 3/3 : Configuration terminée !")
    print("=" * 65)
    print(f"📁 Fichier local sauvegardé dans : {env_path} (ignoré par Git)")
    print("\n📋 Variables à copier dans Vercel (Settings > Environment Variables) :\n")
    print(f"  GOOGLE_CLIENT_ID      = {client_id}")
    print(f"  GOOGLE_CLIENT_SECRET  = {client_secret}")
    print(f"  GOOGLE_REFRESH_TOKEN  = {refresh_token}")
    print(f"  GOOGLE_TASK_LIST_ID   = {task_list_id}")
    print(f"  OG_USERNAME           = {og_user}")
    print(f"  OG_PASSWORD           = {og_pass}")
    print(f"  OG_LIST_ID            = {og_list_id}")
    print(f"  CRON_SECRET           = {cron_secret}")
    print(f"  MATCH_CUTOFF          = {match_cutoff}")
    print("\n⏰ Sur cron-job.org, appelez toutes les 5 minutes :")
    print(f"  URL : https://VOTRE-PROJET.vercel.app/api/sync?secret={cron_secret}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
