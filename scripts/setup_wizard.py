#!/usr/bin/env python3
"""
Assistant d'initialisation interactif pour gtask-to-ourgroceries.

Ce script s'exécute UNE SEULE FOIS sur votre ordinateur pour :
1. Obtenir votre GOOGLE_REFRESH_TOKEN via OAuth2 (ouverture du navigateur).
2. Lister vos listes Google Tasks et récupérer votre GOOGLE_TASK_LIST_ID.
3. Tester la connexion OurGroceries et récupérer votre OG_LIST_ID.
4. Générer un CRON_SECRET aléatoire et sauvegarder toutes les clés dans un fichier .env,
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


def setup_google_tasks() -> tuple[str, str, str, str]:
    """Configure OAuth2 Google Tasks et retourne (client_id, client_secret, refresh_token, task_list_id)."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/tasks"]
    print("\n" + "=" * 65)
    print(" ÉTAPE 1/3 : Configuration Google Tasks (OAuth2)")
    print("=" * 65)

    # Recherche automatique d'un fichier credentials.json ou client_secret*.json
    candidate_files = glob.glob("credentials.json") + glob.glob("client_secret*.json")
    client_config = None

    if candidate_files:
        chosen_file = candidate_files[0]
        print(f"📄 Fichier d'identifiants détecté : {chosen_file}")
        with open(chosen_file, "r", encoding="utf-8") as f:
            client_config = json.load(f)
    else:
        print(
            "Aucun fichier 'credentials.json' trouvé dans le dossier courant.\n"
            "Vous pouvez soit déposer le JSON téléchargé depuis Google Cloud Console,\n"
            "soit saisir directement votre Client ID et Client Secret ci-dessous."
        )
        client_id_in = input("\n👉 GOOGLE_CLIENT_ID : ").strip()
        client_secret_in = input("👉 GOOGLE_CLIENT_SECRET : ").strip()
        if not client_id_in or not client_secret_in:
            print("❌ Client ID et Client Secret requis.")
            sys.exit(1)
        client_config = {
            "installed": {
                "client_id": client_id_in,
                "client_secret": client_secret_in,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    # Extraction de client_id et client_secret
    section = client_config.get("installed") or client_config.get("web") or {}
    client_id = section.get("client_id", "")
    client_secret = section.get("client_secret", "")

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

    print("✅ Authentification Google réussie !")

    service = build("tasks", "v1", credentials=creds, cache_discovery=False)
    res = service.tasklists().list(maxResults=100).execute()
    tasklists = res.get("items", [])

    print("\n📋 Vos listes Google Tasks disponibles :")
    print("  [0] ➕ Créer une nouvelle liste Google Tasks")
    for idx, tl in enumerate(tasklists, start=1):
        print(f"  [{idx}] {tl['title']}  (ID: {tl['id']})")

    choice = input("\n👉 Choisissez le numéro de la liste à surveiller [1] : ").strip()
    if choice == "0":
        new_name = input("Nom de la nouvelle liste Google Tasks (ex: Courses) : ").strip() or "Courses"
        created = service.tasklists().insert(body={"title": new_name}).execute()
        task_list_id = created["id"]
        print(f"✅ Liste '{created['title']}' créée avec l'ID : {task_list_id}")
    else:
        idx = int(choice) - 1 if choice.isdigit() and int(choice) >= 1 else 0
        selected = tasklists[idx]
        task_list_id = selected["id"]
        print(f"✅ Liste sélectionnée : '{selected['title']}' (ID: {task_list_id})")

    return client_id, client_secret, creds.refresh_token, task_list_id


async def setup_ourgroceries() -> tuple[str, str, str]:
    """Teste la connexion OurGroceries et retourne (username, password, list_id)."""
    from ourgroceries import OurGroceries

    print("\n" + "=" * 65)
    print(" ÉTAPE 2/3 : Configuration OurGroceries")
    print("=" * 65)

    og_user = input("👉 Adresse e-mail OurGroceries (OG_USERNAME) : ").strip()
    try:
        og_pass = getpass.getpass("👉 Mot de passe OurGroceries (OG_PASSWORD) : ").strip()
    except Exception:
        og_pass = input("👉 Mot de passe OurGroceries (OG_PASSWORD) : ").strip()

    if not og_user or not og_pass:
        print("❌ Email et mot de passe OurGroceries requis.")
        sys.exit(1)

    print("\n🔄 Connexion à OurGroceries en cours...")
    og = OurGroceries(og_user, og_pass)
    await og.login()

    lists_response = await og.get_my_lists()
    shopping_lists = lists_response.get("shoppingLists", [])

    if not shopping_lists:
        print("❌ Aucune liste de courses trouvée sur ce compte OurGroceries.")
        sys.exit(1)

    print("\n🛒 Vos listes de courses OurGroceries :")
    for idx, sl in enumerate(shopping_lists, start=1):
        active_count = sl.get("activeCount", 0)
        print(f"  [{idx}] {sl['name']}  ({active_count} actifs) — ID: {sl['id']}")

    choice = input("\n👉 Choisissez le numéro de la liste cible [1] : ").strip()
    idx = int(choice) - 1 if choice.isdigit() and int(choice) >= 1 else 0
    selected = shopping_lists[idx]
    og_list_id = selected["id"]
    print(f"✅ Liste OurGroceries sélectionnée : '{selected['name']}' (ID: {og_list_id})")

    return og_user, og_pass, og_list_id


def main():
    check_dependencies()

    client_id, client_secret, refresh_token, task_list_id = setup_google_tasks()
    og_user, og_pass, og_list_id = asyncio.run(setup_ourgroceries())
    cron_secret = secrets.token_urlsafe(24)

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
        'MATCH_CUTOFF="0.75"',
        "",
    ]

    env_path = os.path.join(os.getcwd(), ".env")
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
    print(f"  MATCH_CUTOFF          = 0.75")
    print("\n⏰ Sur cron-job.org, appelez toutes les 5 minutes :")
    print(f"  URL : https://VOTRE-PROJET.vercel.app/api/sync?secret={cron_secret}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
