from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from browser.core.profiles import ProfileManager
from browser.core.security import (
    PermissionDecision,
    PermissionType,
    SecurityManager,
    SitePermissionStore,
    URLRisk,
)
from browser.services.adblock import AdBlocker
from browser.services.extensions import ExtensionManager, ExtensionManifestError, load_manifest
from browser.services.sync import (
    InMemorySyncBackend,
    SyncAccount,
    SyncCollection,
    SyncManager,
    SyncRecord,
)


class ProfileAndSecurityTests(unittest.TestCase):
    def test_profiles_have_isolated_persistent_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(directory)
            first = manager.create_profile("Первый", profile_id="profile-one")
            second = manager.create_profile("Второй", profile_id="profile-two")

            self.assertNotEqual(first.paths.database, second.paths.database)
            self.assertTrue(first.paths.webengine_storage.is_dir())
            self.assertEqual(manager.active_profile_id, second.id)

            restored = ProfileManager(directory)
            self.assertEqual([item.name for item in restored.list_profiles()], ["Первый", "Второй"])
            self.assertEqual(restored.active_profile_id, second.id)

    def test_url_checks_and_permissions_persist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            permission_path = Path(directory) / "permissions.json"
            store = SitePermissionStore(permission_path)
            security = SecurityManager(store, blocked_hosts={"malware.test"})

            self.assertEqual(security.check_url("https://example.com").risk, URLRisk.SECURE)
            self.assertEqual(security.check_url("http://example.com").risk, URLRisk.INSECURE)
            self.assertFalse(security.check_url("javascript:alert(1)").allowed)
            self.assertFalse(security.check_url("https://cdn.malware.test/x").allowed)

            store.set(
                "https://example.com/path",
                PermissionType.NOTIFICATIONS,
                PermissionDecision.ALLOW,
            )
            reloaded = SitePermissionStore(permission_path)
            self.assertEqual(
                reloaded.get("https://example.com", PermissionType.NOTIFICATIONS),
                PermissionDecision.ALLOW,
            )


class AdBlockTests(unittest.TestCase):
    def test_easylist_network_rules_exception_and_whitelist(self) -> None:
        blocker = AdBlocker()
        loaded = blocker.load_rules(
            "\n".join(
                (
                    "! comment",
                    "||ads.example^$script,third-party",
                    "@@||ads.example/allowed^",
                )
            )
        )
        self.assertEqual(loaded, 2)
        self.assertTrue(
            blocker.should_block(
                "https://ads.example/banner.js",
                "https://news.example/article",
                "script",
            )
        )
        self.assertFalse(
            blocker.should_block(
                "https://ads.example/allowed/script.js",
                "https://news.example/article",
                "script",
            )
        )
        blocker.whitelist_domain("ads.example")
        self.assertFalse(
            blocker.should_block("https://ads.example/banner.js", "https://news.example/article", "script")
        )

    def test_custom_rules_are_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adblock.json"
            blocker = AdBlocker(path)
            blocker.add_custom_rule("||telemetry.example^")
            blocker.whitelist_domain("trusted.telemetry.example")

            restored = AdBlocker(path)
            self.assertTrue(restored.should_block("https://telemetry.example/pixel"))
            self.assertFalse(restored.should_block("https://trusted.telemetry.example/pixel"))

    def test_rule_index_preserves_substring_and_regex_matches(self) -> None:
        blocker = AdBlocker()
        blocker.load_rules(
            "\n".join(
                (
                    "advert",
                    "tracking-pixel",
                    "/(?:foo|bar)-sponsor/",
                    "@@||example.test/advert-safe^",
                )
            )
        )
        self.assertTrue(blocker.should_block("https://cdn.test/assets/advertisement.js"))
        self.assertTrue(blocker.should_block("https://cdn.test/tracking-pixel.gif"))
        self.assertTrue(blocker.should_block("https://cdn.test/bar-sponsor.js"))
        self.assertFalse(blocker.should_block("https://example.test/advert-safe/image.js"))


class _MemoryAdapter:
    def __init__(self, collection: SyncCollection) -> None:
        self.collection = collection
        self.local: list[SyncRecord] = []
        self.remote: list[SyncRecord] = []

    async def collect_changes(self, since: datetime | None) -> list[SyncRecord]:
        return [record for record in self.local if since is None or record.modified_at >= since]

    async def apply_remote(self, records: list[SyncRecord] | tuple[SyncRecord, ...]) -> None:
        self.remote.extend(records)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_neutral_sync_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = InMemorySyncBackend()
            adapter = _MemoryAdapter(SyncCollection.BOOKMARKS)
            adapter.local.append(
                SyncRecord(
                    record_id="bookmark-1",
                    collection=SyncCollection.BOOKMARKS,
                    payload={"title": "Auralis", "url": "https://example.com"},
                    modified_at=datetime.now(UTC),
                )
            )
            manager = SyncManager(backend, Path(directory) / "sync.json", device_id="device-a")
            manager.register_adapter(SyncCollection.BOOKMARKS, adapter)
            await manager.connect(SyncAccount("account-1", "User"))

            first = await manager.sync()
            self.assertEqual(first[0].pushed, 1)
            second = await manager.sync()
            self.assertGreaterEqual(second[0].pulled, 1)
            self.assertEqual(adapter.remote[-1].record_id, "bookmark-1")
            self.assertTrue((Path(directory) / "sync.json").is_file())


class ExtensionTests(unittest.TestCase):
    def _make_extension(self, root: Path) -> None:
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "content.js").write_text("console.log('test');", encoding="utf-8")
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Test Extension",
                    "version": "1.0.0",
                    "host_permissions": ["https://*.example.com/*"],
                    "content_scripts": [
                        {
                            "matches": ["https://*.example.com/*"],
                            "js": ["scripts/content.js"],
                            "run_at": "document_end",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_install_enable_and_content_script_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            self._make_extension(source)
            manager = ExtensionManager(base / "profile-extensions")
            extension = manager.install_unpacked(source)

            scripts = manager.content_scripts_for("https://docs.example.com/page", run_at="document_end")
            self.assertEqual(scripts[0][0].id, extension.id)
            manager.set_enabled(extension.id, False)
            self.assertEqual(manager.content_scripts_for("https://docs.example.com/page"), ())

            restored = ExtensionManager(base / "profile-extensions")
            self.assertFalse(restored.get(extension.id).enabled)

    def test_manifest_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "manifest_version": 3,
                        "name": "Unsafe",
                        "version": "1",
                        "content_scripts": [{"matches": ["<all_urls>"], "js": ["../outside.js"]}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ExtensionManifestError):
                load_manifest(root)


if __name__ == "__main__":
    unittest.main()
