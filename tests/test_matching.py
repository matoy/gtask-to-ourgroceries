"""
Tests unitaires de la logique de nettoyage et de Fuzzy Matching (api/sync.py).
Exécutable sans identifiants Google ni OurGroceries :
    python -m unittest tests/test_matching.py
"""

from pathlib import Path
import sys
import unittest

# Permet l'import direct depuis la racine du projet sans installer google/ourgroceries
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Mock léger des dépendances externes si non installées localement
import types

for mod_name in [
    "gkeepapi",
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "googleapiclient",
    "googleapiclient.discovery",
    "ourgroceries",
]:
    if mod_name not in sys.modules:
        dummy = types.ModuleType(mod_name)
        setattr(dummy, "Request", object)
        setattr(dummy, "Credentials", object)
        setattr(dummy, "build", lambda *a, **kw: None)
        setattr(dummy, "OurGroceries", object)
        sys.modules[mod_name] = dummy

from api.sync import (
    clean_task_title,
    find_exact_local_match,
    parse_and_match_item,
    split_crossed_off_for_cleanup,
)


class TestFuzzyMatching(unittest.TestCase):
    def setUp(self):
        # Liste cible OurGroceries simulée (éléments actifs et barrés)
        self.og_items = [
            {"id": "1", "value": "lait cawol", "crossedOff": True},
            {"id": "2", "value": "Tomates", "crossedOff": True},
            {"id": "3", "value": "Œufs", "crossedOff": False},
            {"id": "4", "value": "Pâtes", "crossedOff": True},
            {"id": "5", "value": "Huile d'olive", "crossedOff": True},
            {"id": "6", "value": "Poireaux", "crossedOff": True},
        ]

    def test_clean_task_title(self):
        self.assertEqual(clean_task_title("du lait"), "lait")
        self.assertEqual(clean_task_title("Ajoute des tomates"), "tomates")
        self.assertEqual(clean_task_title("Il me faut de l'huile d'olive"), "huile d'olive")
        self.assertEqual(clean_task_title("rajoute un poireau"), "poireau")

    def test_exact_and_plural_matches(self):
        # Singulier -> Pluriel existant ("Tomates")
        item, score, name = parse_and_match_item("une tomate", self.og_items)
        self.assertIsNotNone(item)
        self.assertEqual(name, "Tomates")
        self.assertEqual(score, 1.0)

        # Ligatures et sans accents ("des oeufs" -> "Œufs")
        item, score, name = parse_and_match_item("des oeufs", self.og_items)
        self.assertIsNotNone(item)
        self.assertEqual(name, "Œufs")
        self.assertEqual(score, 1.0)

        # Pluriel en -eaux ("un poireau" -> "Poireaux")
        item, score, name = parse_and_match_item("un poireau", self.og_items)
        self.assertIsNotNone(item)
        self.assertEqual(name, "Poireaux")
        self.assertEqual(score, 1.0)

    def test_accents_and_apostrophes(self):
        # Sans accent ("des pates" -> "Pâtes")
        item, score, name = parse_and_match_item("des pates", self.og_items)
        self.assertIsNotNone(item)
        self.assertEqual(name, "Pâtes")

        # Apostrophe + préfixe ("ajoute de l'huile d'olive" -> "Huile d'olive")
        item, score, name = parse_and_match_item("ajoute de l'huile d'olive", self.og_items)
        self.assertIsNotNone(item)
        self.assertEqual(name, "Huile d'olive")

    def test_keyword_head_word_match(self):
        # Mot directeur ("Lait" / "du lait" -> "lait cawol")
        for raw in ("Lait", "du lait", "ajoute du lait"):
            item, score, name = parse_and_match_item(raw, self.og_items)
            self.assertIsNotNone(item)
            self.assertEqual(name, "lait cawol")
            self.assertGreaterEqual(score, 0.88)

    def test_new_item_fallback(self):
        # Nouvel article absent de la liste cible -> création avec majuscule propre
        item, score, name = parse_and_match_item("du chocolat noir", self.og_items)
        self.assertIsNone(item)
        self.assertLess(score, 0.75)
        self.assertEqual(name, "Chocolat noir")

    def test_split_crossed_off_for_cleanup(self):
        items = [
            {"id": "a1", "value": "Actif 1", "crossedOff": False},
            {"id": "a2", "value": "Actif 2"},
            {"id": "c_no_ts", "value": "Ancien sans date", "crossedOff": True},
            {"id": "c_old", "value": "Vieux", "crossedOffAt": 1700000001000},
            {"id": "c_mid", "value": "Moyen", "crossedOffAt": 1700000002000},
            {"id": "c_new", "value": "Récent", "crossedOffAt": 1700000003000},
        ]
        kept, to_delete = split_crossed_off_for_cleanup(items, keep_count=2)

        self.assertEqual([i["id"] for i in kept], ["c_new", "c_mid"])
        self.assertEqual([i["id"] for i in to_delete], ["c_old", "c_no_ts"])

        all_ids = {i["id"] for i in kept + to_delete}
        self.assertNotIn("a1", all_ids)
        self.assertNotIn("a2", all_ids)

    def test_find_exact_local_match(self):
        # Correspondance exacte (singulier/pluriel) -> court-circuite Gemini
        tomate = find_exact_local_match("une tomate", self.og_items)
        self.assertIsNotNone(tomate)
        self.assertEqual(tomate["value"], "Tomates")

        # Correspondance exacte (ligature/casse) -> court-circuite Gemini
        oeufs = find_exact_local_match("des oeufs", self.og_items)
        self.assertIsNotNone(oeufs)
        self.assertEqual(oeufs["value"], "Œufs")

        # Générique / marque ("du lait" vs "lait cawol") -> laissé à Gemini
        self.assertIsNone(find_exact_local_match("du lait", self.og_items))

        # Équivalence culinaire ("galettes blé noir") -> laissé à Gemini
        self.assertIsNone(find_exact_local_match("galettes blé noir", self.og_items))


if __name__ == "__main__":
    unittest.main()
