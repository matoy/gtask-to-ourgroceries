#!/usr/bin/env python3
"""
Script de nettoyage OurGroceries : supprime tous les articles rayés d'une liste,
en conservant uniquement les N articles rayés le plus récemment (80 par défaut).

Exemples :
    python scripts/cleanup_crossed_off.py --dry-run
    python scripts/cleanup_crossed_off.py --keep 80
    python scripts/cleanup_crossed_off.py --keep 80 --yes
"""

import argparse
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.sync import (
    crossed_off_timestamp,
    get_env_clean,
    is_item_crossed_off,
    split_crossed_off_for_cleanup,
)
from scripts.setup_wizard import load_existing_env


def format_crossed_off_date(ts: float) -> str:
    """Convertit un timestamp `crossedOffAt` (ms ou s) en date lisible."""
    if ts <= 0:
        return "sans date"
    epoch_sec = ts / 1000.0 if ts > 1e11 else ts
    try:
        return datetime.fromtimestamp(epoch_sec).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return f"timestamp={ts}"


def resolve_ourgroceries_config() -> tuple[str, str, str]:
    """Charge les identifiants OurGroceries depuis l'environnement ou .env."""
    cwd_env = Path.cwd() / ".env"
    root_env = PROJECT_ROOT / ".env"
    env_file = cwd_env if cwd_env.exists() else root_env
    file_env = load_existing_env(str(env_file)) if env_file.exists() else {}

    def read_var(name: str) -> str:
        return get_env_clean(name) or file_env.get(name, "").strip()

    og_user = read_var("OG_USERNAME")
    og_pass = read_var("OG_PASSWORD")
    og_list_id = read_var("OG_LIST_ID")

    if not all([og_user, og_pass, og_list_id]):
        print(
            "❌ Identifiants manquants : OG_USERNAME, OG_PASSWORD et OG_LIST_ID "
            "doivent être définis dans .env ou dans l'environnement."
        )
        sys.exit(1)

    return og_user, og_pass, og_list_id


async def cleanup_crossed_off_items(keep: int, dry_run: bool, auto_yes: bool) -> int:
    """Connecte à OurGroceries et supprime les anciens articles rayés au-delà de `keep`."""
    try:
        from ourgroceries import OurGroceries
    except ImportError:
        print("❌ Le module 'ourgroceries' est requis (pip install -r requirements.txt).")
        return 1

    if keep < 0:
        print("❌ L'option --keep doit être supérieure ou égale à 0.")
        return 1

    og_user, og_pass, og_list_id = resolve_ourgroceries_config()

    print(f"🔄 Connexion à OurGroceries ({og_user})...")
    og = OurGroceries(og_user, og_pass)
    await og.login()

    list_details = await og.get_list_items(list_id=og_list_id)
    list_info = list_details.get("list", {})
    list_name = list_info.get("name", og_list_id)
    items = list_info.get("items", [])

    active_count = sum(1 for item in items if not is_item_crossed_off(item))
    kept_items, items_to_delete = split_crossed_off_for_cleanup(items, keep_count=keep)
    total_crossed = len(kept_items) + len(items_to_delete)

    print(f"\n🛒 Liste cible : {list_name} (ID: {og_list_id})")
    print(f"   • Articles actifs          : {active_count}")
    print(f"   • Articles rayés au total  : {total_crossed}")
    print(f"   • Rayés récents conservés  : {len(kept_items)} (objectif max : {keep})")
    print(f"   • Anciens rayés à supprimer: {len(items_to_delete)}")

    if kept_items:
        print("\n📌 Aperçu des articles rayés conservés (du plus récent au plus ancien conservé) :")
        preview = kept_items[:5]
        for item in preview:
            ts = crossed_off_timestamp(item)
            print(f"   ✓ {item.get('value', '?')} ({format_crossed_off_date(ts)})")
        if len(kept_items) > 10:
            print("   ...")
        if len(kept_items) > 5:
            tail = kept_items[-min(5, len(kept_items) - 5) :]
            for item in tail:
                ts = crossed_off_timestamp(item)
                print(f"   ✓ {item.get('value', '?')} ({format_crossed_off_date(ts)})")

    if not items_to_delete:
        print(f"\n✅ Rien à supprimer : {total_crossed} article(s) rayé(s) (<= {keep}).")
        return 0

    print(f"\n🗑️  Articles rayés qui seront supprimés ({len(items_to_delete)}) :")
    for idx, item in enumerate(items_to_delete, start=1):
        ts = crossed_off_timestamp(item)
        print(f"   [{idx}] {item.get('value', '(sans nom)')} ({format_crossed_off_date(ts)})")

    if dry_run:
        print("\nℹ️ Mode --dry-run actif : aucune suppression n'a été effectuée.")
        return 0

    if not auto_yes:
        confirm = (
            input(
                f"\n⚠️ Confirmer la suppression définitive de {len(items_to_delete)} "
                "article(s) rayé(s) ? [o/N] : "
            )
            .strip()
            .lower()
        )
        if confirm not in ("o", "oui", "y", "yes"):
            print("❌ Suppression annulée.")
            return 0

    print()
    deleted_count = 0
    error_count = 0
    total_to_delete = len(items_to_delete)

    for idx, item in enumerate(items_to_delete, start=1):
        item_id = item.get("id")
        item_name = item.get("value", "(sans nom)")
        if not item_id:
            print(f"[{idx}/{total_to_delete}] ⚠️ Ignoré (ID manquant) : {item_name}")
            error_count += 1
            continue

        print(f"[{idx}/{total_to_delete}] 🗑️ Suppression de : {item_name}")
        try:
            await og.remove_item_from_list(og_list_id, item_id)
            deleted_count += 1
        except Exception as exc:
            print(f"   ⚠️ Échec de suppression pour '{item_name}' : {exc}")
            error_count += 1
        await asyncio.sleep(0.05)

    print(
        f"\n✅ Nettoyage terminé : {deleted_count} supprimé(s), "
        f"{len(kept_items)} rayé(s) récent(s) conservé(s), {error_count} erreur(s)."
    )
    return 0 if error_count == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Supprime les anciens articles rayés d'une liste OurGroceries en "
            "conservant les N plus récemment rayés."
        )
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=80,
        help="Nombre d'articles rayés les plus récents à conserver (défaut : 80).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les articles à conserver et à supprimer sans rien modifier.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Exécute les suppressions sans demander de confirmation.",
    )
    return parser.parse_args()


def main() -> int:
    # Assure l'affichage correct des émojis et accents sous Windows
    if os.name == "nt":
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")

    args = parse_args()
    return asyncio.run(
        cleanup_crossed_off_items(
            keep=args.keep,
            dry_run=args.dry_run,
            auto_yes=args.yes,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
