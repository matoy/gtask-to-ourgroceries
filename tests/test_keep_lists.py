"""
Tests unitaires pour le support multi-listes Google Keep et listes partagées
(api/sync.py et scripts/setup_wizard.py).
Exécutable sans identifiants Google ni OurGroceries :
    python -m unittest tests/test_keep_lists.py
"""

import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Mock léger des dépendances externes si non installées localement
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
        setattr(dummy, "Keep", object)
        sys.modules[mod_name] = dummy

import api.sync as sync_mod
import scripts.setup_wizard as wizard_mod


class FakeCollaborators:
    def __init__(self, emails=None, role_map=None):
        self._emails = list(emails or [])
        self._collaborators = dict(role_map or {})

    def all(self):
        return list(self._emails)


class FakeKeepItem:
    def __init__(self, item_id, text, checked=False, server_id=None, parent_server_id=None):
        self.id = item_id
        self.text = text
        self.checked = checked
        self.server_id = server_id
        self.parent_server_id = parent_server_id


class FakeKeepList:
    def __init__(
        self,
        note_id,
        title,
        unchecked=None,
        checked=None,
        server_id=None,
        trashed=False,
        deleted=False,
        archived=False,
        collaborators=None,
    ):
        self.id = note_id
        self.title = title
        self.unchecked = list(unchecked or [])
        self.checked = list(checked or [])
        self.server_id = server_id
        self.trashed = trashed
        self.deleted = deleted
        self.archived = archived
        self.collaborators = collaborators


class FakeTextNote:
    def __init__(self, note_id, title, text="", server_id=None, trashed=False, deleted=False, archived=False, collaborators=None):
        self.id = note_id
        self.title = title
        self.text = text
        self.server_id = server_id
        self.trashed = trashed
        self.deleted = deleted
        self.archived = archived
        self.collaborators = collaborators


class FakeKeepClient:
    def __init__(self, notes=None):
        self._notes = list(notes or [])
        self.sync_calls = []

    def all(self):
        return list(self._notes)

    def get(self, identifier):
        for note in self._notes:
            if note.id == identifier or (note.server_id and note.server_id == identifier):
                return note
        return None

    def sync(self, resync=False):
        self.sync_calls.append(resync)

    def authenticate(self, email, master_token):
        return None


class TestParseKeepNoteIds(unittest.TestCase):
    def test_single_and_comma_separated(self):
        self.assertEqual(sync_mod.parse_keep_note_ids("id1"), ["id1"])
        self.assertEqual(
            sync_mod.parse_keep_note_ids(" id1 , id2 , , id3 , "),
            ["id1", "id2", "id3"],
        )

    def test_newlines_quotes_and_urls_dedup(self):
        raw = (
            '"id1",\n'
            "'Liste partagée',\r\n"
            "https://keep.google.com/u/0/#LIST/17b2c3d4.abc,\n"
            "https://keep.google.com/#list/17b2c3d4.abc?foo=1,\n"
            "id1"
        )
        self.assertEqual(
            sync_mod.parse_keep_note_ids(raw),
            ["id1", "Liste partagée", "17b2c3d4.abc"],
        )
        self.assertEqual(
            wizard_mod.parse_keep_note_ids("https://keep.google.com/u/1/#LIST/note%20shared,note-2"),
            ["note shared", "note-2"],
        )


class TestDescribeKeepNoteSharing(unittest.TestCase):
    def test_personal_note(self):
        note = FakeKeepList(
            "n1",
            "Perso",
            collaborators=FakeCollaborators(
                emails=["me@gmail.com"],
                role_map={"me@gmail.com": types.SimpleNamespace(value="O")},
            ),
        )
        info = sync_mod.describe_keep_note_sharing(note, "me@gmail.com")
        self.assertFalse(info["shared"])
        self.assertFalse(info["shared_with_me"])
        self.assertEqual(info["owner"], "me@gmail.com")
        self.assertEqual(info["other_collaborators"], [])

    def test_owned_and_shared_with_others(self):
        note = FakeKeepList(
            "n2",
            "Courses maison",
            collaborators=FakeCollaborators(
                emails=["me@gmail.com", "partner@gmail.com"],
                role_map={
                    "me@gmail.com": types.SimpleNamespace(value="O"),
                    "partner@gmail.com": types.SimpleNamespace(value="W"),
                },
            ),
        )
        info = sync_mod.describe_keep_note_sharing(note, "me@gmail.com")
        self.assertTrue(info["shared"])
        self.assertFalse(info["shared_with_me"])
        self.assertEqual(info["owner"], "me@gmail.com")
        self.assertEqual(info["other_collaborators"], ["partner@gmail.com"])

    def test_shared_with_me(self):
        note = FakeKeepList(
            "n3",
            "Courses partagées",
            collaborators=FakeCollaborators(
                emails=["owner@gmail.com", "me@gmail.com"],
                role_map={
                    "owner@gmail.com": types.SimpleNamespace(value="O"),
                    "me@gmail.com": types.SimpleNamespace(value="W"),
                },
            ),
        )
        info = sync_mod.describe_keep_note_sharing(note, "me@gmail.com")
        self.assertTrue(info["shared"])
        self.assertTrue(info["shared_with_me"])
        self.assertEqual(info["owner"], "owner@gmail.com")
        self.assertEqual(info["other_collaborators"], ["owner@gmail.com"])

    def test_minimal_all_only_and_malformed_entries(self):
        class MinimalCollab:
            _collaborators = {None: "O", "bad": None}

            def all(self):
                return [
                    None,
                    "",
                    types.SimpleNamespace(email="friend@gmail.com"),
                    "FRIEND@gmail.com",
                ]

        note = FakeKeepList("n4", "Minimal", collaborators=MinimalCollab())
        info = sync_mod.describe_keep_note_sharing(note, "me@gmail.com")
        self.assertTrue(info["shared"])
        self.assertEqual(info["collaborators"], ["friend@gmail.com"])
        self.assertEqual(info["other_collaborators"], ["friend@gmail.com"])


class TestResolveKeepNotes(unittest.TestCase):
    def setUp(self):
        self.own = FakeKeepList("id-own", "Courses", server_id="srv-own")
        self.shared = FakeKeepList(
            "id-shared",
            "Courses",
            server_id="srv-shared",
            collaborators=FakeCollaborators(
                emails=["owner@gmail.com", "me@gmail.com"],
                role_map={"owner@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )
        self.archived_shared = FakeKeepList(
            "id-arch-shared",
            "Courses week-end",
            server_id="srv-arch-shared",
            archived=True,
            collaborators=FakeCollaborators(
                emails=["owner@gmail.com", "me@gmail.com"],
                role_map={"owner@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )
        self.trashed = FakeKeepList("id-trash", "Ancienne", trashed=True)
        self.deleted = FakeKeepList("id-del", "Supprimée", deleted=True)
        self.text_note = FakeTextNote("id-txt", "Courses", "Pas une liste")
        self.text_only = FakeTextNote("id-txt-only", "Mémo")
        self.client = FakeKeepClient(
            [
                self.own,
                self.shared,
                self.archived_shared,
                self.trashed,
                self.deleted,
                self.text_note,
                self.text_only,
            ]
        )

    def test_resolve_by_id_server_id_and_multi(self):
        res = sync_mod.resolve_keep_notes(
            self.client,
            ["id-own", "srv-shared", "id-arch-shared", "id-own"],
            current_email="me@gmail.com",
        )
        self.assertEqual([n.id for n in res], ["id-own", "id-shared", "id-arch-shared"])

    def test_resolve_duplicate_title_returns_both_active_checklists_skipping_text(self):
        res = sync_mod.resolve_keep_notes(
            self.client,
            ["  courses  "],
            current_email="me@gmail.com",
        )
        self.assertEqual([n.id for n in res], ["id-own", "id-shared"])

    def test_title_prefers_active_and_falls_back_to_archived(self):
        archived_courses = FakeKeepList("id-arch-c", "Courses", archived=True)
        client = FakeKeepClient([self.own, archived_courses])
        res_active = sync_mod.resolve_keep_notes(client, ["Courses"])
        self.assertEqual([n.id for n in res_active], ["id-own"])

        res_arch = sync_mod.resolve_keep_notes(
            self.client, ["Courses week-end"], current_email="me@gmail.com"
        )
        self.assertEqual([n.id for n in res_arch], ["id-arch-shared"])

    def test_rejects_text_note_by_id_or_title(self):
        with self.assertRaises(ValueError) as ctx1:
            sync_mod.resolve_keep_notes(self.client, ["id-txt-only"])
        self.assertIn("cases à cocher", str(ctx1.exception))

        with self.assertRaises(ValueError) as ctx2:
            sync_mod.resolve_keep_notes(self.client, ["Mémo"])
        self.assertIn("cases à cocher", str(ctx2.exception))

    def test_rejects_trashed_deleted_or_missing_with_shared_badges(self):
        with self.assertRaises(ValueError) as ctx:
            sync_mod.resolve_keep_notes(
                self.client,
                ["id-trash", "id-del", "Introuvable"],
                current_email="me@gmail.com",
            )
        msg = str(ctx.exception)
        self.assertIn("id-trash", msg)
        self.assertIn("id-del", msg)
        self.assertIn("Introuvable", msg)
        self.assertIn("[partagée par owner@gmail.com]", msg)
        self.assertIn("[archivée]", msg)
        self.assertNotIn("Ancienne", msg)
        self.assertNotIn("Supprimée", msg)


class TestGetPendingItemsAndResyncCache(unittest.TestCase):
    def setUp(self):
        sync_mod._KEEP_CACHE.update({"email": None, "master_token": None, "client": None})

    def tearDown(self):
        sync_mod._KEEP_CACHE.update({"email": None, "master_token": None, "client": None})

    def test_multi_list_pending_items_and_google_keep_note_ids_fallback(self):
        own_list = FakeKeepList(
            "note-1",
            "Perso",
            server_id="srv-1",
            unchecked=[
                FakeKeepItem("i1", "  du lait  ", server_id="is1"),
                FakeKeepItem("i_blank", "   "),
            ],
        )
        shared_list = FakeKeepList(
            "note-2",
            "Partagée",
            server_id="srv-2",
            unchecked=[FakeKeepItem("i2", "des tomates", server_id="is2")],
            collaborators=FakeCollaborators(
                emails=["partner@gmail.com", "me@gmail.com"],
                role_map={"partner@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )
        client = FakeKeepClient([own_list, shared_list])

        class FakeGKeepModule:
            @staticmethod
            def Keep():
                return client

        env = {
            "GOOGLE_KEEP_EMAIL": "me@gmail.com",
            "GOOGLE_KEEP_MASTER_TOKEN": "aas_et/token",
            "GOOGLE_KEEP_NOTE_ID": "",
            "GOOGLE_KEEP_NOTE_IDS": "note-1,srv-2",
        }
        with patch.object(sync_mod, "gkeepapi", FakeGKeepModule), patch.dict(
            os.environ, env, clear=False
        ):
            keep_client, items, meta = sync_mod.get_google_keep_pending_items()

        self.assertIs(keep_client, client)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "du lait")
        self.assertEqual(items[0]["keep_note_id"], "note-1")
        self.assertEqual(items[1]["title"], "des tomates")
        self.assertEqual(items[1]["keep_note_id"], "note-2")
        self.assertEqual(items[1]["keep_note_server_id"], "srv-2")
        self.assertEqual(
            meta,
            [
                {
                    "id": "note-1",
                    "title": "Perso",
                    "shared": False,
                    "shared_with_me": False,
                    "unchecked_count": 1,
                },
                {
                    "id": "note-2",
                    "title": "Partagée",
                    "shared": True,
                    "shared_with_me": True,
                    "unchecked_count": 1,
                    "owner": "partner@gmail.com",
                },
            ],
        )

    def test_warm_cache_resyncs_when_newly_shared_list_missing(self):
        own_list = FakeKeepList("note-1", "Perso", unchecked=[FakeKeepItem("i1", "Pain")])
        newly_shared = FakeKeepList(
            "note-new",
            "Nouvelle partagée",
            server_id="srv-new",
            unchecked=[FakeKeepItem("i2", "Beurre")],
            collaborators=FakeCollaborators(
                emails=["alice@gmail.com", "me@gmail.com"],
                role_map={"alice@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )

        class CachedClient(FakeKeepClient):
            def sync(self, resync=False):
                super().sync(resync=resync)
                if resync and newly_shared not in self._notes:
                    self._notes.append(newly_shared)

        cached = CachedClient([own_list])
        sync_mod._KEEP_CACHE.update(
            {
                "email": "me@gmail.com",
                "master_token": "aas_et/token",
                "client": cached,
            }
        )

        env = {
            "GOOGLE_KEEP_EMAIL": "me@gmail.com",
            "GOOGLE_KEEP_MASTER_TOKEN": "aas_et/token",
            "GOOGLE_KEEP_NOTE_ID": "note-1,note-new",
        }
        with patch.object(sync_mod, "gkeepapi", object()), patch.dict(
            os.environ, env, clear=False
        ):
            _, items, meta = sync_mod.get_google_keep_pending_items()

        self.assertEqual(cached.sync_calls, [False, True])
        self.assertEqual([i["title"] for i in items], ["Pain", "Beurre"])
        self.assertEqual([m["id"] for m in meta], ["note-1", "note-new"])


class TestCheckOffAndSyncEndToEnd(unittest.TestCase):
    def setUp(self):
        sync_mod._KEEP_CACHE.update({"email": None, "master_token": None, "client": None})

    def tearDown(self):
        sync_mod._KEEP_CACHE.update({"email": None, "master_token": None, "client": None})

    def test_sync_tasks_to_groceries_multi_list_dedup_and_parent_server_id(self):
        item1 = FakeKeepItem("k1", "du lait", server_id="ks1", parent_server_id=None)
        item2 = FakeKeepItem("k2", "Lait", server_id="ks2", parent_server_id=None)
        item3 = FakeKeepItem("k3", "du chocolat noir", server_id="ks3", parent_server_id=None)
        list1 = FakeKeepList("note-1", "Ma liste", server_id="srv-1", unchecked=[item1])
        list2 = FakeKeepList(
            "note-2",
            "Liste partagée",
            server_id="srv-2",
            unchecked=[item2, item3],
            collaborators=FakeCollaborators(
                emails=["owner@gmail.com", "me@gmail.com"],
                role_map={"owner@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )
        keep_client = FakeKeepClient([list1, list2])

        class FakeGKeepModule:
            @staticmethod
            def Keep():
                return keep_client

        og_instances = []

        class FakeOG:
            def __init__(self, user, password):
                self.user = user
                self.password = password
                self.login = AsyncMock()
                self.get_list_items = AsyncMock(
                    return_value={
                        "list": {
                            "items": [
                                {"id": "og-lait", "value": "Lait", "crossedOff": True}
                            ]
                        }
                    }
                )
                self.toggle_item_crossed_off = AsyncMock()
                self.add_item_to_list = AsyncMock()
                og_instances.append(self)

        env = {
            "GOOGLE_KEEP_EMAIL": "me@gmail.com",
            "GOOGLE_KEEP_MASTER_TOKEN": "aas_et/token",
            "GOOGLE_KEEP_NOTE_ID": "note-1,note-2",
            "OG_USERNAME": "og@example.com",
            "OG_PASSWORD": "secret",
            "OG_LIST_ID": "og-list-1",
            "GEMINI_API_KEY": "",
        }
        import asyncio

        with (
            patch.object(sync_mod, "gkeepapi", FakeGKeepModule),
            patch.object(sync_mod, "OurGroceries", FakeOG),
            patch.dict(os.environ, env, clear=False),
        ):
            payload = asyncio.run(sync_mod.sync_tasks_to_groceries(dry_run=False))

        og = og_instances[0]
        # Cross-list dedup: "du lait" uncrosses og-lait once; "Lait" from shared list sees already_active
        og.toggle_item_crossed_off.assert_awaited_once_with(
            list_id="og-list-1", item_id="og-lait", cross_off=False
        )
        og.add_item_to_list.assert_awaited_once_with(
            list_id="og-list-1", value="Chocolat noir", auto_category=True
        )

        # All 3 Keep items across both lists are checked off and parent_server_id is filled
        self.assertTrue(item1.checked)
        self.assertTrue(item2.checked)
        self.assertTrue(item3.checked)
        self.assertEqual(item1.parent_server_id, "srv-1")
        self.assertEqual(item2.parent_server_id, "srv-2")
        self.assertEqual(item3.parent_server_id, "srv-2")
        self.assertEqual(keep_client.sync_calls, [False])

        self.assertEqual(payload["synced_count"], 3)
        self.assertEqual([i["action"] for i in payload["items"]], [
            "uncrossed_existing",
            "already_active",
            "created_new",
        ])
        self.assertEqual(payload["items"][0]["keep_note_id"], "note-1")
        self.assertEqual(payload["items"][0]["keep_note_title"], "Ma liste")
        self.assertEqual(payload["items"][1]["keep_note_id"], "note-2")
        self.assertEqual(payload["items"][1]["keep_note_title"], "Liste partagée")
        self.assertEqual(len(payload["keep_lists"]), 2)
        self.assertTrue(payload["keep_lists"][1]["shared_with_me"])

    def test_mark_google_keep_items_checked_conflict_recovery_and_double_failure(self):
        stale_item = FakeKeepItem("k1", "Beurre", server_id="ks1")
        fresh_item = FakeKeepItem("k1", "Beurre", server_id="ks1")
        stale_note = FakeKeepList("n1", "Partagée", server_id="srv-1", unchecked=[stale_item])
        fresh_note = FakeKeepList("n1", "Partagée", server_id="srv-1", unchecked=[fresh_item])

        class ConflictRecoverClient:
            def __init__(self):
                self.notes = [stale_note]
                self.calls = []
                self.sync_attempt = 0

            def all(self):
                return self.notes

            def sync(self, resync=False):
                self.calls.append(resync)
                if not resync:
                    self.sync_attempt += 1
                    if self.sync_attempt == 1:
                        raise RuntimeError("MergeException")
                else:
                    self.notes = [fresh_note]

        client = ConflictRecoverClient()
        tasks = [
            {
                "id": "k1",
                "keep_note_id": "n1",
                "keep_note_server_id": "srv-1",
                "keep_item_server_id": "ks1",
                "_keep_item": stale_item,
                "_keep_note": stale_note,
            }
        ]
        sync_mod.mark_google_keep_items_checked(client, tasks)
        self.assertEqual(client.calls, [False, True, False])
        self.assertTrue(fresh_item.checked)
        self.assertEqual(fresh_item.parent_server_id, "srv-1")

        # Double failure clears _KEEP_CACHE["client"] and re-raises
        class AlwaysFailClient(FakeKeepClient):
            def sync(self, resync=False):
                raise RuntimeError("sync boom")

        fail_client = AlwaysFailClient([stale_note])
        sync_mod._KEEP_CACHE["client"] = fail_client
        with self.assertRaises(RuntimeError):
            sync_mod.mark_google_keep_items_checked(fail_client, tasks)
        self.assertIsNone(sync_mod._KEEP_CACHE["client"])

    def test_empty_keep_lists_and_google_tasks_omits_keep_lists(self):
        import asyncio

        empty_own = FakeKeepList("n1", "Perso", unchecked=[])
        empty_shared = FakeKeepList(
            "n2",
            "Partagée",
            unchecked=[],
            collaborators=FakeCollaborators(
                emails=["owner@gmail.com", "me@gmail.com"],
                role_map={"owner@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )
        keep_client = FakeKeepClient([empty_own, empty_shared])

        class FakeGKeepModule:
            @staticmethod
            def Keep():
                return keep_client

        keep_env = {
            "GOOGLE_KEEP_EMAIL": "me@gmail.com",
            "GOOGLE_KEEP_MASTER_TOKEN": "aas_et/token",
            "GOOGLE_KEEP_NOTE_ID": "n1,n2",
            "OG_USERNAME": "og@example.com",
            "OG_PASSWORD": "secret",
            "OG_LIST_ID": "og-list-1",
            "OG_KEEP_CROSSED_OFF": "",
        }
        with patch.object(sync_mod, "gkeepapi", FakeGKeepModule), patch.dict(
            os.environ, keep_env, clear=False
        ):
            payload = asyncio.run(sync_mod.sync_tasks_to_groceries(dry_run=True))

        self.assertEqual(payload["source"], "google_keep")
        self.assertEqual(payload["synced_count"], 0)
        self.assertEqual([l["id"] for l in payload["keep_lists"]], ["n1", "n2"])
        self.assertEqual([l["unchecked_count"] for l in payload["keep_lists"]], [0, 0])

        class FakeTasksService:
            def tasks(self):
                return self

            def list(self, **kwargs):
                return self

            def execute(self):
                return {"items": []}

        tasks_env = {
            "GOOGLE_KEEP_MASTER_TOKEN": "",
            "GOOGLE_TASK_LIST_ID": "task-list-1",
            "OG_USERNAME": "og@example.com",
            "OG_PASSWORD": "secret",
            "OG_LIST_ID": "og-list-1",
            "OG_KEEP_CROSSED_OFF": "",
        }
        with patch.object(
            sync_mod, "get_google_tasks_service", return_value=FakeTasksService()
        ), patch.dict(os.environ, tasks_env, clear=False):
            tasks_payload = asyncio.run(sync_mod.sync_tasks_to_groceries(dry_run=True))

        self.assertEqual(tasks_payload["source"], "google_tasks")
        self.assertNotIn("keep_lists", tasks_payload)


class TestSetupWizardHelpers(unittest.TestCase):
    def setUp(self):
        self.active_own = FakeKeepList("id1", "Courses perso", server_id="srv1")
        self.active_shared = FakeKeepList(
            "id2",
            "Courses famille",
            server_id="srv2",
            collaborators=FakeCollaborators(
                emails=["alice@gmail.com", "me@gmail.com"],
                role_map={"alice@gmail.com": "O", "me@gmail.com": "W"},
            ),
        )
        self.owned_shared = FakeKeepList(
            "id3",
            "Week-end",
            collaborators=FakeCollaborators(
                emails=["me@gmail.com", "bob@gmail.com"],
                role_map={"me@gmail.com": "O", "bob@gmail.com": "W"},
            ),
        )
        self.archived_list = FakeKeepList("id4", "Anciennes courses", archived=True)
        self.text_note = FakeTextNote("id5", "Note texte", "bonjour")
        self.trashed = FakeKeepList("id6", "Corbeille", trashed=True)

    def test_sort_and_badges(self):
        ordered = wizard_mod.sort_keep_notes_for_wizard(
            [self.text_note, self.archived_list, self.trashed, self.active_shared, self.active_own]
        )
        self.assertEqual(
            [n.id for n in ordered],
            ["id2", "id1", "id4", "id5"],
        )
        self.assertEqual(
            wizard_mod.format_keep_note_badges(self.active_shared, "me@gmail.com"),
            "[🤝 partagée par alice@gmail.com]",
        )
        self.assertEqual(
            wizard_mod.format_keep_note_badges(self.owned_shared, "me@gmail.com"),
            "[🤝 partagée avec bob@gmail.com]",
        )
        self.assertEqual(
            wizard_mod.format_keep_note_badges(self.archived_list, "me@gmail.com"),
            "[📦 archivée]",
        )

    def test_find_default_indices_and_selection_parsing(self):
        notes = [self.active_own, self.active_shared, self.archived_list, self.text_note]
        defaults = wizard_mod.find_default_keep_indices(
            notes, "srv2, Courses perso"
        )
        self.assertEqual(defaults, [2, 1])

        # Empty input -> defaults
        sel, create_new, errs = wizard_mod.parse_wizard_keep_selection("", notes, defaults)
        self.assertEqual([n.id for n in sel], ["id2", "id1"])
        self.assertFalse(create_new)
        self.assertEqual(errs, [])

        # Comma-separated & whitespace-separated & dedup
        for raw in ("1,2,1", "1 2"):
            sel, create_new, errs = wizard_mod.parse_wizard_keep_selection(raw, notes, defaults)
            self.assertEqual([n.id for n in sel], ["id1", "id2"])
            self.assertFalse(create_new)
            self.assertEqual(errs, [])

        # "0" and "0,2"
        sel0, create0, errs0 = wizard_mod.parse_wizard_keep_selection("0", notes, defaults)
        self.assertEqual(sel0, [])
        self.assertTrue(create0)
        self.assertEqual(errs0, [])

        sel02, create02, errs02 = wizard_mod.parse_wizard_keep_selection("0,2", notes, defaults)
        self.assertEqual([n.id for n in sel02], ["id2"])
        self.assertTrue(create02)
        self.assertEqual(errs02, [])

        # Pasted Keep URL
        sel_url, create_url, errs_url = wizard_mod.parse_wizard_keep_selection(
            "https://keep.google.com/u/0/#LIST/srv2", notes, defaults
        )
        self.assertEqual([n.id for n in sel_url], ["id2"])
        self.assertFalse(create_url)
        self.assertEqual(errs_url, [])

        # Text note rejected & out-of-range error
        sel_bad, _, errs_bad = wizard_mod.parse_wizard_keep_selection("4,99", notes, defaults)
        self.assertEqual(sel_bad, [])
        self.assertEqual(len(errs_bad), 2)
        self.assertIn("note texte", errs_bad[0])
        self.assertIn("Numéro invalide", errs_bad[1])


if __name__ == "__main__":
    unittest.main()
