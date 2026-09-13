"""Deployment failure scenarios with isolated files and fake Docker operations."""
import importlib.util
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4


SPEC = importlib.util.spec_from_file_location("release_upgrade", Path(__file__).parents[1] / "release_upgrade.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def arguments(**changes):
    values = dict(current_release="/opt/breezetravel-releases/first-public-20260906",
                  new_release="/opt/breezetravel-releases/upgrade-20260908",
                  current_db="breeze_live_20260906", new_db="breeze_live_20260908",
                  rehearsal_db="breeze_rehearsal_20260908", current_api="breeze-first-api",
                  current_web="breeze-first-web", current_yjs="breeze-first-yjs",
                  api_port=8028, web_port=3128, yjs_port=1256, redis_db=9, preview_redis_db=10, preview_trip_id=None,
                  postgres_container="breezetravel-postgres-1", redis_container="breezetravel-redis-1",
                  expected_host="expected-host", nginx_site=Path("/etc/nginx/sites-available/breezetravel.cn"),
                  execute=False, action="prepare", source_archive=None, source_ref="test-ref",
                  model_deadline_seconds=60.0, model_max_output_tokens=4096)
    values.update(changes)
    return SimpleNamespace(**values)


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.operation = release.Upgrade(arguments())
        self.operation.target = self.root / "new"
        self.operation.target.mkdir()
        self.operation.current = self.root / "old"
        self.operation.current.mkdir()
        self.operation.current_source = self.operation.current / "src"
        self.operation.state_path = self.operation.target / "upgrade-state.json"
        self.operation.args.nginx_site = self.root / "site.conf"
        self.original_nginx = "location / { proxy_pass http://127.0.0.1:3118; }\nlocation /api/ { proxy_pass http://127.0.0.1:8018; }\nlocation /yjs/ { proxy_pass http://127.0.0.1:1246/; }\n"
        self.operation.args.nginx_site.write_text(self.original_nginx)
        self.operation.current_ports = {"api": 8018, "web": 3118, "yjs": 1246}
        self.operation.network = "breezetravel_default"
        self.operation.environment = {
            "DATABASE_URL": "postgresql://name:private-db-password@postgres:5432/breeze_live_20260906",
            "REDIS_URL": "redis://redis:6379/8", "JWT_SECRET_KEY": "private-jwt-key",
            "QWEN_API_KEY": "private-model-key", "AMAP_API_KEY": "private-map-key",
            "TRIP_UNDERSTANDING_QWEN_MODEL": "qwen3.5-plus",
            "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY": "private-cookie-key",
            "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY": "private-encryption-key"}
        self.operation.containers = {kind: {"Image": kind + "-image"} for kind in ("api", "web", "yjs")}

    def test_same_or_unsafe_database_rejected_before_operations(self):
        for changes in ({"new_db": "breeze_live_20260906"}, {"new_db": "db'; DROP DATABASE x;--"}, {"rehearsal_db": "breeze_live_20260908"}):
            with self.subTest(changes=changes), self.assertRaises(release.UpgradeError):
                release.Upgrade(arguments(**changes))

    def test_release_cannot_escape_named_parent_or_reuse_current(self):
        for name in ("/", "/opt", "/opt/breezetravel-releases/../other", "relative", "/opt/breezetravel-releases/first-public-20260906"):
            with self.subTest(name=name), self.assertRaises(release.UpgradeError):
                release.Upgrade(arguments(new_release=name))

    def test_ports_must_be_distinct_and_unprivileged(self):
        for changes in ({"web_port": 8028}, {"api_port": 80}, {"yjs_port": 65536}):
            with self.subTest(changes=changes), self.assertRaises(release.UpgradeError):
                release.Upgrade(arguments(**changes))

    def test_archive_rejects_traversal_links_and_secret_environment(self):
        for index, (name, kind) in enumerate((("../escape", "file"), ("/absolute", "file"), ("frontend/.env.production", "file"), ("linked", "link"), ("node_modules/x", "file"))):
            archive = self.root / f"unsafe-{index}.tar"
            with tarfile.open(archive, "w") as stream:
                member = tarfile.TarInfo(name)
                if kind == "link":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/etc/passwd"
                    stream.addfile(member)
                else:
                    member.size = 1
                    stream.addfile(member, io.BytesIO(b"x"))
            with self.subTest(name=name), self.assertRaises(release.UpgradeError):
                release.extract_source(archive, self.root / "extracted")
            self.assertFalse((self.root / "escape").exists())

    def test_git_archive_templates_are_not_loaded_as_private_configuration(self):
        archive = self.root / "safe.tar"
        paths = ("backend/app/experience_main.py", "scripts/experience.py", "frontend/package-lock.json", "y-websocket/server.js", ".env.example")
        with tarfile.open(archive, "w") as stream:
            for path in paths:
                member = tarfile.TarInfo(path)
                member.size = 1
                stream.addfile(member, io.BytesIO(b"x"))
        release.extract_source(archive, self.root / "safe-source")
        self.assertTrue((self.root / "safe-source/.env.example").is_file())
        self.assertFalse((self.root / "safe-source/.env").exists())

    def test_nginx_replaces_only_expected_upstreams(self):
        original = self.original_nginx + "# diagnostic port 8018\nlocation /other { proxy_pass http://127.0.0.1:3001; }"
        result = release.replace_upstreams(original, {8018: 8028, 3118: 3128, 1246: 1256})
        self.assertIn("http://127.0.0.1:8028", result)
        self.assertIn("# diagnostic port 8018", result)
        self.assertIn("http://127.0.0.1:3001", result)
        with self.assertRaises(release.UpgradeError):
            release.replace_upstreams(original, {8999: 9000})

    def test_plan_does_not_execute_action_or_create_state(self):
        self.operation.snapshot = Mock(return_value={"phase": "UNPREPARED"})
        self.operation.prepare = Mock()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.operation.execute()
        self.assertFalse(json.loads(output.getvalue())["will_mutate"])
        self.operation.prepare.assert_not_called()
        self.assertFalse(self.operation.state_path.exists())

    def test_wrong_host_prevents_even_docker_reads(self):
        self.operation.run = Mock()
        with patch.object(release.platform, "node", return_value="other-host"), self.assertRaises(release.UpgradeError):
            self.operation.snapshot()
        self.operation.run.assert_not_called()

    def test_prepared_directory_rejects_different_ref_without_changing_it(self):
        self.operation.state = {"phase": "PREPARED", "source_ref": "old-ref"}
        release.save_json(self.operation.state_path, self.operation.state)
        previous = self.operation.state_path.read_bytes()
        self.operation.build_web = Mock()
        self.operation.run = Mock()
        with self.assertRaisesRegex(release.UpgradeError, "different source ref; use a new release directory"):
            self.operation.prepare()
        self.operation.build_web.assert_not_called()
        self.operation.run.assert_not_called()
        self.assertEqual(self.operation.state_path.read_bytes(), previous)
        self.assertFalse(self.operation.mutation_started)

    def test_prepared_directory_same_ref_is_idempotent(self):
        self.operation.state = {"phase": "PREPARED", "source_ref": self.operation.args.source_ref}
        release.save_json(self.operation.state_path, self.operation.state)
        previous = self.operation.state_path.read_bytes()
        self.operation.build_web = Mock()
        self.operation.run = Mock()
        self.operation.prepare()
        self.operation.build_web.assert_not_called()
        self.operation.run.assert_not_called()
        self.assertEqual(self.operation.state_path.read_bytes(), previous)

    def test_read_only_prepare_plan_rejects_different_ref_without_success_output(self):
        self.operation.state = {"phase": "PREPARED", "source_ref": "old-ref"}
        self.operation.snapshot = Mock(return_value=dict(self.operation.state))
        self.operation.prepare = Mock()
        with patch("sys.stdout", new_callable=io.StringIO) as output, self.assertRaisesRegex(release.UpgradeError, "different source ref"):
            self.operation.execute()
        self.assertEqual(output.getvalue(), "")
        self.operation.prepare.assert_not_called()
        self.assertFalse(self.operation.state_path.exists())

    def test_preview_keeps_real_provider_keys_and_workers_with_separate_data(self):
        self.operation.configure_api("breeze_rehearsal_20260908", live=False)
        content = (self.operation.target / "private-api.env").read_text()
        self.assertIn("private-model-key", content)
        self.assertIn("private-map-key", content)
        self.assertIn("EXPERIENCE_WORKERS_ENABLED=true", content)
        self.assertIn("TRIP_UNDERSTANDING_PROVIDER_MODE=live", content)
        self.assertIn("AMAP_MOCK=false", content)
        self.assertIn("private-encryption-key", content)
        self.assertIn("breeze_rehearsal_20260908", content)
        self.assertIn("redis://redis:6379/10", content)
        self.assertNotIn("breeze_live_20260906", content)

    def test_live_config_uses_new_database_and_existing_security_identity(self):
        self.operation.configure_api("breeze_live_20260908", live=True)
        content = (self.operation.target / "private-api.env").read_text()
        self.assertIn("breeze_live_20260908", content)
        self.assertNotIn("breeze_live_20260906", content)
        for key in ("private-jwt-key", "private-cookie-key", "private-encryption-key"):
            self.assertIn(key, content)
        self.assertIn("AUTO_MIGRATE=false", content)
        self.assertIn("TRIP_UNDERSTANDING_QWEN_DEADLINE_SECONDS=60.0", content)
        self.assertIn("TRIP_UNDERSTANDING_QWEN_MAX_OUTPUT_TOKENS=4096", content)

    def test_public_preview_configuration_passes_actual_settings_validation(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
        from app.config import Settings
        for key, value in (("JWT_SECRET_KEY", "j" * 40), ("TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY", "c" * 40),
                           ("TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY", "e" * 40)):
            self.operation.environment[key] = value
        self.operation.configure_api(self.operation.args.rehearsal_db, live=False)
        values = dict(line.split("=", 1) for line in (self.operation.target / "private-api.env").read_text().splitlines())
        with patch.dict(os.environ, values):
            settings = Settings(_env_file=None)
        self.assertEqual(settings.runtime_profile, "public")
        self.assertEqual(settings.trip_understanding_provider_mode, "live")
        self.assertTrue(settings.experience_workers_enabled)
        self.assertTrue(settings.qwen_api_key and settings.amap_api_key)
        self.assertTrue(settings.database_url.endswith("/breeze_rehearsal_20260908"))
        self.assertTrue(settings.redis_url.endswith("/10"))

    def test_memory_below_trial_ceiling_and_reserve_refuses_before_creating_release(self):
        self.operation.target = self.root / "not-created"
        self.operation.args.source_archive = self.root / "source.tar"
        self.operation.available_memory_kib = Mock(return_value=(release.BUILD_MEMORY_MIB + release.MEMORY_RESERVE_MIB) * 1024 - 1)
        self.operation.run = Mock()
        with self.assertRaisesRegex(release.UpgradeError, "Insufficient available memory"):
            self.operation.prepare()
        self.assertFalse(self.operation.target.exists())
        self.operation.run.assert_not_called()

    def test_observed_host_memory_allows_bounded_trial_without_claiming_capacity(self):
        self.operation.available_memory_kib = Mock(return_value=1662056)
        self.operation.query = Mock(return_value=str(100 * 1024**2))
        with patch.object(release.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024**3)):
            self.operation.check_resources("build")
            self.operation.check_resources("preview")

    def test_build_reuses_equal_manifest_copy_and_records_cgroup_measurement(self):
        source = self.operation.target / "src"
        frontend = source / "frontend"
        previous = self.operation.current_source / "frontend"
        frontend.mkdir(parents=True)
        (previous / "node_modules").mkdir(parents=True)
        for filename in ("package.json", "package-lock.json"):
            (frontend / filename).write_text('{"name":"test"}\n')
            (previous / filename).write_text('{ "name": "test" }\n')
        (previous / "node_modules/dependency.js").write_text("original dependency")
        self.operation.check_resources = Mock()
        calls = []
        def run(*args, **kwargs):
            calls.append(args)
            if "-e" in args and "spawnSync" in args[-1]:
                (frontend / ".next/standalone").mkdir(parents=True)
                (frontend / ".next/standalone/server.js").write_text("runtime")
                (frontend / ".release-build-memory.json").write_text(json.dumps({"exit_code":0,"peak_bytes":"805000000","events":"oom_kill 0\n"}))
            return ""
        self.operation.run = run
        with patch("sys.stdout", new_callable=io.StringIO):
            self.operation.build_web(source)
        self.assertEqual((frontend / "node_modules/dependency.js").read_text(), "original dependency")
        self.assertFalse((previous / ".release-build-memory.json").exists())
        self.assertTrue(any("ls" in call and "--offline" in call for call in calls))
        self.assertFalse(any("ci" in call for call in calls))
        for call in calls:
            self.assertIn("--memory=1024m", call)
            self.assertIn("--memory-swap=1024m", call)
            self.assertEqual(call[call.index("--network") + 1], "none")
            self.assertFalse(any(str(previous) in value for value in call))
        (frontend / "node_modules/dependency.js").write_text("copy only")
        self.assertEqual((previous / "node_modules/dependency.js").read_text(), "original dependency")

    def test_provider_label_does_not_reject_an_actual_configured_model(self):
        for provider, model in (("QWEN", "qwen3.5-plus"), ("OPENAI", "gpt-5.5-pro"), ("OTHER_REAL", "new-model")):
            binding = {"provider":provider,"model":model,"external_calls":1,"calls":[{"reported_model":model}]}
            self.assertTrue(release.has_live_model_call([binding], model))
            self.assertFalse(release.has_live_model_call([binding], "different-model"))
            self.assertFalse(release.has_live_model_call([{**binding,"external_calls":0}], model))
            self.assertFalse(release.has_live_model_call([{**binding,"calls":[]}], model))
            self.assertFalse(release.has_live_model_call([{**binding,"calls":[{}]}], model))

    def test_changed_dependencies_install_only_in_new_source(self):
        source = self.operation.target / "src"
        frontend = source / "frontend"
        previous = self.operation.current_source / "frontend"
        frontend.mkdir(parents=True)
        (previous / "node_modules").mkdir(parents=True)
        for filename in ("package.json", "package-lock.json"):
            (frontend / filename).write_text('{"name":"changed"}')
            (previous / filename).write_text('{"name":"old"}')
        (previous / "node_modules/old.js").write_text("old")
        self.operation.check_resources = Mock()
        def run(*args, **kwargs):
            if "ci" in args:
                self.assertEqual(args[args.index("--network") + 1], self.operation.network)
                raise release.UpgradeError("isolated install failure")
            return ""
        self.operation.run = run
        with self.assertRaisesRegex(release.UpgradeError, "isolated install failure"):
            self.operation.build_web(source)
        self.assertFalse((frontend / "node_modules").exists())
        self.assertEqual((previous / "node_modules/old.js").read_text(), "old")

    def test_copy_restore_does_not_start_workers_or_claim_user_acceptance(self):
        self.operation.state = {"phase": "PREPARED"}
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        self.operation.run = Mock(return_value="")
        self.operation.check_resources = Mock()
        self.operation.assert_writer_set = Mock()
        self.operation.remove_new_containers = Mock()
        self.operation.backup = Mock(return_value=self.root / "backup")
        self.operation.clone = Mock(return_value=self.operation.target / "rehearsal-yjs-data")
        self.operation.migrate = Mock()
        self.operation.verify_restored_counts = Mock()
        self.operation.query = Mock(return_value="039_daily_dining_metrics.sql")
        self.operation.start = Mock()
        self.operation.health = Mock()
        with patch("sys.stdout", new_callable=io.StringIO):
            self.operation.rehearse()
            self.operation.rehearse()
        self.assertEqual(self.operation.state["phase"], "COPY_RESTORED")
        self.operation.start.assert_not_called()
        self.operation.backup.assert_called_once()
        self.operation.clone.assert_called_once()
        self.operation.migrate.assert_called_once()
        self.assertEqual(self.operation.health.call_args_list, [
            unittest.mock.call(ports={"api": 8018}, context="Previous service recovery"),
            unittest.mock.call(ports={"yjs": 1246}, context="Previous service recovery"),
        ])

    def test_old_service_health_failure_keeps_backup_and_stops_before_clone(self):
        for failing_kind, failing_port in (("api", 8018), ("yjs", 1246)):
            with self.subTest(failing_kind=failing_kind):
                self.operation.state = {"phase": "PREPARED"}
                self.operation.inspect = Mock(return_value={"State": {"Running": True}})
                self.operation.run = Mock(return_value="")
                self.operation.check_resources = Mock()
                self.operation.assert_writer_set = Mock()
                self.operation.remove_new_containers = Mock()
                backup = self.operation.target / ("backup-" + failing_kind)
                backup.mkdir()
                (backup / "database.dump").write_bytes(b"retained snapshot")
                self.operation.backup = Mock(return_value=backup)
                self.operation.clone = Mock()
                self.operation.migrate = Mock()
                clock = [0.0]
                observed = []

                def wait(seconds):
                    clock[0] += seconds

                def request(url, *, timeout):
                    observed.append((url, timeout))
                    if f":{failing_port}/" in url:
                        # Account for a full network timeout; no real waiting/network.
                        clock[0] += timeout
                        raise OSError("service unavailable")
                    return nullcontext(SimpleNamespace(status=200))

                with patch.object(release, "urlopen", side_effect=request), \
                        patch.object(release.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(release.time, "sleep", side_effect=wait), \
                        patch("sys.stdout", new_callable=io.StringIO), \
                        self.assertRaisesRegex(release.UpgradeError, f"Previous service recovery: {failing_kind}.*backups retained"):
                    self.operation.rehearse()
                self.assertEqual(clock[0], 40)
                self.assertTrue(all(0 < timeout <= 3 for _, timeout in observed))
                self.assertFalse(any(":8028/" in url or ":1256/" in url for url, _ in observed))
                self.assertTrue(all(any(f":{port}/" in url for url, _ in observed) for port in (8018, 1246)))
                self.assertEqual((backup / "database.dump").read_bytes(), b"retained snapshot")
                self.assertEqual(json.loads(self.operation.state_path.read_text())["rehearsal_backup"], str(backup))
                self.assertEqual(self.operation.state["phase"], "SNAPSHOTTING_REHEARSAL")
                self.operation.clone.assert_not_called()
                self.operation.migrate.assert_not_called()

    def test_old_health_retries_transient_failure_using_old_ports_only(self):
        responses = [OSError("starting"), nullcontext(SimpleNamespace(status=503)),
                     nullcontext(SimpleNamespace(status=200)), nullcontext(SimpleNamespace(status=200))]
        with patch.object(release, "urlopen", side_effect=responses) as request, patch.object(release.time, "sleep"):
            self.operation.health(ports={"api": 8018, "yjs": 1246}, context="Previous service recovery")
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ["http://127.0.0.1:8018/health"] * 3 + ["http://127.0.0.1:1246/health"])

    def test_backup_failure_still_checks_old_recovery_without_claiming_a_copy(self):
        self.operation.state = {"phase": "PREPARED"}
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        for name in ("run", "check_resources", "assert_writer_set", "remove_new_containers", "health", "clone", "migrate"):
            setattr(self.operation, name, Mock())
        partial = self.operation.target / "backup-partial.dump"
        partial.write_bytes(b"partial backup retained for diagnosis")
        self.operation.backup = Mock(side_effect=release.UpgradeError("backup failed; private log retained"))
        with patch("sys.stdout", new_callable=io.StringIO) as output, \
                self.assertRaisesRegex(release.UpgradeError, "backup failed"):
            self.operation.rehearse()
        self.assertEqual(self.operation.health.call_args_list, [
            unittest.mock.call(ports={"api": 8018}, context="Previous service recovery"),
            unittest.mock.call(ports={"yjs": 1246}, context="Previous service recovery"),
        ])
        self.operation.clone.assert_not_called()
        self.operation.migrate.assert_not_called()
        self.assertTrue(partial.is_file())
        self.assertNotIn("COPY_RESTORED", output.getvalue())
        self.assertEqual(self.operation.state["phase"], "SNAPSHOTTING_REHEARSAL")

    def test_start_failure_still_starts_other_writer_and_checks_both_health_endpoints(self):
        for failed_kind in ("api", "yjs"):
            with self.subTest(failed_kind=failed_kind):
                self.operation.state = {"phase": "PREPARED"}
                self.operation.inspect = Mock(return_value={"State": {"Running": True}})
                for name in ("check_resources", "assert_writer_set", "remove_new_containers", "health", "clone", "migrate"):
                    setattr(self.operation, name, Mock())
                self.operation.backup = Mock(return_value=self.root / "retained-backup")
                failure = release.UpgradeError(f"{failed_kind} start command failed; private log retained")

                def command(*args, **kwargs):
                    if args == ("docker", "start", "breeze-first-" + failed_kind):
                        raise failure
                    return ""

                self.operation.run = Mock(side_effect=command)
                with self.assertRaisesRegex(release.UpgradeError, f"{failed_kind} start command failed"):
                    self.operation.rehearse()
                starts = [call.args for call in self.operation.run.call_args_list if call.args[:2] == ("docker", "start")]
                self.assertEqual(starts, [("docker", "start", "breeze-first-api"), ("docker", "start", "breeze-first-yjs")])
                self.assertEqual(self.operation.health.call_args_list, [
                    unittest.mock.call(ports={"api": 8018}, context="Previous service recovery"),
                    unittest.mock.call(ports={"yjs": 1246}, context="Previous service recovery"),
                ])
                self.operation.clone.assert_not_called()
                self.operation.migrate.assert_not_called()
                self.assertEqual(self.operation.state["phase"], "SNAPSHOTTING_REHEARSAL")
                self.assertEqual(self.operation.state["rehearsal_backup"], str(self.root / "retained-backup"))

    def test_backup_and_both_start_and_health_errors_are_retained_together(self):
        self.operation.state = {"phase": "PREPARED"}
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        for name in ("check_resources", "assert_writer_set", "remove_new_containers", "clone", "migrate"):
            setattr(self.operation, name, Mock())
        original = release.UpgradeError("initial pg_dump failed; private log retained")
        self.operation.backup = Mock(side_effect=original)

        def command(*args, **kwargs):
            if args[:2] == ("docker", "start"):
                raise release.UpgradeError(args[2] + " command failed")
            return ""

        self.operation.run = Mock(side_effect=command)
        self.operation.health = Mock(side_effect=[release.UpgradeError("api unavailable"), release.UpgradeError("yjs unavailable")])
        with self.assertRaises(release.UpgradeError) as caught:
            self.operation.rehearse()
        message = str(caught.exception)
        for expected in ("Original operation failed: initial pg_dump failed", "api start:", "yjs start:", "api health: api unavailable", "yjs health: yjs unavailable", "backups retained"):
            self.assertIn(expected, message)
        self.assertIs(caught.exception.__cause__, original)
        self.assertEqual(self.operation.health.call_count, 2)
        self.operation.clone.assert_not_called()
        self.operation.migrate.assert_not_called()
        self.assertEqual(self.operation.state["phase"], "SNAPSHOTTING_REHEARSAL")

    def test_uncertain_new_writer_shutdown_preserves_original_error_without_restarting_old(self):
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        self.operation.assert_writer_set = Mock()
        self.operation.run = Mock()
        self.operation.health = Mock()
        self.operation.remove_new_containers = Mock(side_effect=release.UpgradeError("new writer stop failed"))
        original = release.UpgradeError("backup failed")
        with self.assertRaises(release.UpgradeError) as caught:
            with self.operation.stopped_previous_writers():
                raise original
        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("Original operation failed: backup failed", str(caught.exception))
        self.assertIn("old writers not restarted", str(caught.exception))
        self.assertFalse(any(call.args[:2] == ("docker", "start") for call in self.operation.run.call_args_list))
        self.operation.health.assert_not_called()

    def test_execute_cleanup_failure_does_not_hide_initial_backup_error(self):
        self.operation.args.execute = True
        self.operation.args.action = "rehearse"
        self.operation.state = {"phase": "PREPARED", "source_ref": self.operation.args.source_ref}
        self.operation.snapshot = Mock(return_value=dict(self.operation.state))
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        for name in ("run", "check_resources", "assert_writer_set", "health", "clone", "migrate"):
            setattr(self.operation, name, Mock())
        original = release.UpgradeError("initial pg_dump failed; private log retained")
        self.operation.backup = Mock(side_effect=original)
        self.operation.remove_new_containers = Mock(side_effect=release.UpgradeError("new writer stop failed"))
        fake_lock = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=Mock())
        with patch.object(release.platform, "system", return_value="Linux"), \
                patch.object(release.os, "geteuid", return_value=0, create=True), \
                patch.object(release.os, "umask"), patch.dict(sys.modules, {"fcntl": fake_lock}), \
                patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(release.UpgradeError) as caught:
            self.operation.execute()
        self.assertIn("Original operation failed: initial pg_dump failed", str(caught.exception))
        self.assertIn("final container cleanup: new writer stop failed", str(caught.exception))
        self.assertIs(caught.exception.__cause__.__cause__, original)
        self.assertEqual(self.operation.state["phase"], "FAILED_SNAPSHOTTING_REHEARSAL")
        self.operation.clone.assert_not_called()
        self.operation.migrate.assert_not_called()

    def test_preview_build_routes_api_and_yjs_to_the_copy(self):
        self.operation.state = {"phase": "COPY_RESTORED"}
        frontend = self.operation.target / "src/frontend"
        frontend.mkdir(parents=True)
        (frontend / "source.ts").write_text("example")
        (self.operation.target / "private-web.env").write_text("NEXT_PUBLIC_API_URL=https://production.invalid\nNEXT_PUBLIC_Y_WEBSOCKET_URL=wss://production.invalid/yjs\nBACKEND_INTERNAL_URL=http://upgrade-20260908-api:8006\n")
        for method in ("remove_new_containers", "check_resources", "build_web", "assert_redis_unused", "pause_copied_jobs", "start", "health"):
            setattr(self.operation, method, Mock())
        with patch("sys.stdout", new_callable=io.StringIO):
            self.operation.preview()
            self.operation.preview()
        content = (self.operation.target / "private-preview-web.env").read_text()
        self.assertIn("NEXT_PUBLIC_API_URL=\n", content)
        self.assertIn("ws://127.0.0.1:1256", content)
        self.assertNotIn("production.invalid", content)
        self.assertTrue((self.operation.target / "preview-src/frontend/source.ts").is_file())
        self.operation.pause_copied_jobs.assert_called_once()
        self.operation.start.assert_called_once()
        self.assertEqual(self.operation.state["phase"], "PREVIEW_SERVING_UNVERIFIED")

    def test_retry_preview_start_keeps_user_jobs_and_does_not_rebuild(self):
        self.operation.state = {"phase": "FAILED_PREVIEW_STARTING", "preview_started_at": "2026-09-08T00:00:00+00:00"}
        for method in ("remove_new_containers", "check_resources", "build_web", "pause_copied_jobs", "start"):
            setattr(self.operation, method, Mock())
        with patch("sys.stdout", new_callable=io.StringIO):
            self.operation.preview()
        self.operation.pause_copied_jobs.assert_not_called()
        self.operation.build_web.assert_not_called()
        self.operation.start.assert_called_once()

    def test_copied_jobs_are_paused_only_in_preview_database(self):
        self.operation.run = Mock(return_value="")
        self.operation.assert_writer_set = Mock()
        self.operation.pause_copied_jobs()
        args = self.operation.run.call_args.args
        self.assertEqual(args[args.index("-d") + 1], self.operation.args.rehearsal_db)
        self.assertNotIn(self.operation.args.current_db, args)
        self.assertIn("lease_until=NULL", args[-1])
        self.assertNotIn("DELETE", args[-1])
        self.operation.args.rehearsal_db = self.operation.args.current_db
        with self.assertRaises(release.UpgradeError):
            self.operation.pause_copied_jobs()

    def test_static_copy_or_health_cannot_activate(self):
        self.operation.state = {"phase": "COPY_RESTORED"}
        self.operation.remove_new_containers = Mock()
        with self.assertRaises(release.UpgradeError):
            self.operation.activate()
        self.operation.state = {"phase": "PREVIEW_SERVING_UNVERIFIED"}
        with self.assertRaises(release.UpgradeError):
            self.operation.activate()
        self.operation.remove_new_containers.assert_not_called()

    def test_activate_rechecks_real_preview_record_instead_of_acceptance_flag(self):
        self.operation.state = {"preview_started_at": "2026-09-08T00:00:00+00:00"}
        self.operation.args.preview_trip_id = "private-preview-resource-12345"
        report = dict(new_in_preview=True, saved=True, complete=True, edited=True,
                      model_bindings=[{"provider":"QWEN","model":"qwen3.5-plus","external_calls":1,"calls":[{"reported_model":"qwen3.5-plus"}]}])
        self.operation.query = Mock(return_value=json.dumps(report))
        self.operation.verify_preview_journey()
        self.assertEqual(self.operation.query.call_args.args[0], self.operation.args.rehearsal_db)
        self.assertIn("'USER_EDIT'", self.operation.query.call_args.args[1])
        self.assertNotIn("='QWEN'", self.operation.query.call_args.args[1])
        for field in report:
            self.operation.query.return_value = json.dumps({**report, field: False})
            with self.assertRaises(release.UpgradeError):
                self.operation.verify_preview_journey()

    def test_unknown_database_writer_rejects_upgrade(self):
        self.operation.run = Mock(return_value="abc")
        self.operation.inspect = Mock(return_value={"Name": "/unregistered-worker", "Config": {"Env": ["DATABASE_URL=postgresql://user:secret@postgres/breeze_live_20260906"]}})
        with self.assertRaises(release.UpgradeError):
            self.operation.assert_writer_set("breeze_live_20260906", {"breeze-first-api"})

    def test_partial_new_start_is_stopped_before_old_writers_resume(self):
        events = []
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        self.operation.assert_writer_set = Mock()
        self.operation.run = lambda *args, **kw: events.append(args) or ""
        self.operation.remove_new_containers = lambda **kw: events.append(("remove-new",))
        self.operation.health = lambda **kw: events.append(("old-healthy",))
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            with self.operation.stopped_previous_writers():
                events.append(("new-started-partially",))
                raise RuntimeError("startup failed")
        self.assertLess(events.index(("remove-new",)), events.index(("docker", "start", "breeze-first-api")))
        self.assertLess(events.index(("docker", "start", "breeze-first-yjs")), events.index(("old-healthy",)))

    def test_lost_cutover_ack_never_resumes_old_database_writers(self):
        events = []
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        self.operation.assert_writer_set = Mock()
        self.operation.run = lambda *args, **kw: events.append(args) or ""
        self.operation.remove_new_containers = Mock()
        self.operation.health = Mock()
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            with self.operation.stopped_previous_writers():
                self.operation.state["traffic_may_have_switched"] = True
                raise RuntimeError("connection lost")
        self.assertFalse(any(args[:2] == ("docker", "start") for args in events))
        self.operation.remove_new_containers.assert_not_called()
        self.operation.health.assert_not_called()

    def test_existing_clone_database_is_never_overwritten_or_dropped(self):
        self.operation.query = Mock(return_value="1")
        self.operation.run = Mock()
        with self.assertRaises(release.UpgradeError):
            self.operation.clone(self.root, "breeze_live_20260908", "yjs-data")
        self.operation.run.assert_not_called()

    def test_changed_restored_counts_stop_before_use(self):
        backup = self.root / "backup"
        backup.mkdir()
        (backup / "data-counts.json").write_text(json.dumps({"users": 23}))
        self.operation.data_counts = Mock(return_value={"users": 22})
        with self.assertRaises(release.UpgradeError):
            self.operation.verify_restored_counts(backup, "breeze_live_20260908")

    def test_runtime_starts_without_implicit_migrations(self):
        self.web_build(live=True)
        calls = []
        self.operation.run = lambda *args, **kw: calls.append(args) or ""
        self.operation.inspect = Mock(return_value={"State": {"Running": False}})
        self.operation.assert_writer_set = Mock()
        self.operation.health = Mock()
        self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=True)
        api = calls[0]
        self.assertIn("uvicorn", api)
        self.assertNotIn("experience_container.py", " ".join(api))
        self.assertNotIn("migrate", " ".join(api))
        for kind, call in zip(("api", "yjs", "web"), calls):
            limit = release.RUNTIME_MEMORY_MIB[kind]
            self.assertIn(f"--memory={limit}m", call)
            self.assertIn(f"--memory-swap={limit}m", call)

    def web_build(self, *, live):
        source = self.operation.target / ("src" if live else "preview-src")
        frontend = source / "frontend"
        for folder in ("public", ".next/static", ".next/standalone"):
            (frontend / folder).mkdir(parents=True, exist_ok=True)
        for name, content in (("package.json", "{}"), ("package-lock.json", "{}"),
                              (".next/BUILD_ID", "fixed-build"), (".next/standalone/server.js", "// compiled server")):
            (frontend / name).write_text(content)
        return frontend

    def test_runtime_web_prepares_live_and_preview_readonly_mount_targets(self):
        old = self.operation.current / "src/frontend/.next/standalone"
        old.mkdir(parents=True)
        (old / "server.js").write_text("old release must stay unchanged")
        for live in (True, False):
            with self.subTest(live=live):
                frontend = self.web_build(live=live)
                self.operation.run = Mock(return_value="")
                self.operation.inspect = Mock(return_value={"State": {"Running": False}})
                self.operation.assert_writer_set = Mock()
                self.operation.health = Mock()
                self.operation.build_web = Mock()
                self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=live)
                standalone = frontend / ".next/standalone"
                self.assertTrue((standalone / "public").is_dir())
                self.assertTrue((standalone / ".next/static").is_dir())
                web = self.operation.run.call_args_list[-1].args
                self.assertIn(f"{frontend.parent}/frontend/.next/standalone:/release:ro", web)
                self.assertIn(f"{frontend.parent}/frontend/public:/release/public:ro", web)
                self.assertIn(f"{frontend.parent}/frontend/.next/static:/release/.next/static:ro", web)
                (standalone / "public/kept.txt").write_text("existing target contents")
                self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=live)
                self.assertEqual((standalone / "public/kept.txt").read_text(), "existing target contents")
                self.assertEqual((standalone / "server.js").read_text(), "// compiled server")
                self.operation.build_web.assert_not_called()
                self.assertFalse((old / "public").exists())
                self.assertEqual((old / "server.js").read_text(), "old release must stay unchanged")

    def test_runtime_web_rejects_missing_build_input_before_starting_containers(self):
        for live in (True, False):
            for missing in (".next/standalone/server.js", "package.json", "package-lock.json", ".next/BUILD_ID", "public", ".next/static"):
                with self.subTest(live=live, missing=missing):
                    frontend = self.web_build(live=live)
                    value = frontend / missing
                    value.rmdir() if value.is_dir() else value.unlink()
                    self.operation.run = Mock(return_value="")
                    self.operation.inspect = Mock(return_value={"State": {"Running": False}})
                    self.operation.assert_writer_set = Mock()
                    self.operation.health = Mock()
                    with self.assertRaises(release.UpgradeError):
                        self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=live)
                    self.operation.run.assert_not_called()

    def test_runtime_web_cannot_prepare_mounts_in_old_or_unexpected_source(self):
        for live in (True, False):
            for source in (self.operation.current / "src", self.operation.target / "other-src"):
                with self.subTest(live=live, source=source.name):
                    self.web_build(live=live)
                    self.operation.run = Mock(return_value="")
                    self.operation.inspect = Mock(return_value={"State": {"Running": False}})
                    self.operation.assert_writer_set = Mock()
                    self.operation.health = Mock()
                    with self.assertRaises(release.UpgradeError):
                        self.operation.start(source, self.operation.target / "yjs-data", live=live)
                    self.operation.run.assert_not_called()
                    self.assertFalse((source / "frontend/.next/standalone/public").exists())

    def test_runtime_web_rejects_mount_target_redirected_to_old_source_before_writes(self):
        old = self.operation.current / "src/frontend/.next/standalone"
        old.mkdir(parents=True)
        (old / "server.js").write_text("old release")
        original_resolve = Path.resolve
        for live in (True, False):
            with self.subTest(live=live):
                frontend = self.web_build(live=live)
                target = frontend / ".next/standalone/.next/static"
                # Simulate the resolved path of a symlink/junction without
                # requiring Windows symlink privileges in this isolated test.
                def resolve(path, *args, **kwargs):
                    return old / "static" if path == target else original_resolve(path, *args, **kwargs)
                self.operation.run = Mock()
                self.operation.inspect = Mock(return_value={"State": {"Running": False}})
                self.operation.assert_writer_set = Mock()
                self.operation.health = Mock()
                with patch.object(Path, "resolve", resolve), self.assertRaises(release.UpgradeError):
                    self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=live)
                self.operation.run.assert_not_called()
                self.assertFalse((frontend / ".next/standalone/public").exists())
                self.assertEqual(list(old.iterdir()), [old / "server.js"])
                self.assertEqual((old / "server.js").read_text(), "old release")

    def test_runtime_web_preserves_existing_target_files_and_rejects_file_as_directory(self):
        frontend = self.web_build(live=True)
        public = frontend / ".next/standalone/public"
        public.mkdir()
        (public / "kept.txt").write_text("existing output")
        static_parent = frontend / ".next/standalone/.next"
        static_parent.mkdir()
        (static_parent / "static").write_text("not a directory")
        self.operation.run = Mock(return_value="")
        self.operation.inspect = Mock(return_value={"State": {"Running": False}})
        self.operation.assert_writer_set = Mock()
        self.operation.health = Mock()
        with self.assertRaises(release.UpgradeError):
            self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=True)
        self.operation.run.assert_not_called()
        self.assertEqual((public / "kept.txt").read_text(), "existing output")

    def test_runtime_refuses_overlap_with_previous_writers(self):
        self.operation.inspect = Mock(return_value={"State": {"Running": True}})
        self.operation.assert_writer_set = Mock()
        self.operation.run = Mock()
        with self.assertRaises(release.UpgradeError):
            self.operation.start(self.operation.target / "src", self.operation.target / "yjs-data", live=True)
        self.operation.run.assert_not_called()

    def test_migration_is_a_separate_ephemeral_process(self):
        self.operation.run = Mock(return_value="")
        self.operation.check_resources = Mock()
        self.operation.migrate()
        args = self.operation.run.call_args.args
        self.assertIn("--rm", args)
        self.assertIn("migrate", args[-1])
        self.assertNotIn("experience_container.py", args[-1])

    def test_recovery_defaults_to_new_code_and_never_uses_previous_database(self):
        self.operation.state = {"traffic_may_have_switched": True}
        backup = self.root / "live-backup"
        backup.mkdir()
        (backup / "nginx-site.conf").write_text(self.original_nginx)
        self.operation.state["live_backup"] = str(backup)
        events = []
        self.operation.remove_new_containers = lambda **kw: events.append(("containers", kw))
        self.operation.assert_writer_set = Mock()
        self.operation.backup = lambda db, data, label: events.append(("backup", db, str(data))) or self.root
        self.operation.configure_api = lambda db, **kw: events.append(("configure", db))
        self.operation.start = lambda src, data, **kw: events.append(("start", str(src), str(data)))
        self.operation.run = lambda *args, **kw: events.append(args) or ""
        with patch("sys.stdout", new_callable=io.StringIO):
            self.operation.recover()
        self.assertIn(("configure", "breeze_live_20260908"), events)
        self.assertNotIn(("configure", "breeze_live_20260906"), events)
        self.assertLess(next(i for i, e in enumerate(events) if e[0] == "backup"), next(i for i, e in enumerate(events) if e[0] == "start"))
        self.assertEqual(self.operation.state["phase"], "RECOVERED_KEEPING_CURRENT_DATA")

    def test_incompatible_previous_code_has_no_recovery_route(self):
        self.assertFalse(hasattr(self.operation, "previous_code"))
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            release.parse_args(["recover", "--recovery-code", "previous"])

    def test_recovery_does_not_overwrite_unrelated_nginx_changes(self):
        self.operation.state = {"traffic_may_have_switched": True}
        backup = self.root / "live-backup"
        backup.mkdir()
        (backup / "nginx-site.conf").write_text(self.original_nginx)
        self.operation.state["live_backup"] = str(backup)
        changed = self.original_nginx + "# operator changed another route\n"
        self.operation.args.nginx_site.write_text(changed)
        for name in ("remove_new_containers", "assert_writer_set", "configure_api", "start", "run"):
            setattr(self.operation, name, Mock(return_value=""))
        self.operation.backup = Mock(return_value=self.root)
        with patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(release.UpgradeError):
            self.operation.recover()
        self.assertEqual(self.operation.args.nginx_site.read_text(), changed)

    def test_private_child_errors_are_not_echoed(self):
        result = subprocess.CompletedProcess(["docker"], 2, stdout="", stderr="postgresql://user:password@db private-model-key")
        with patch.object(release.subprocess, "run", return_value=result), self.assertRaises(release.UpgradeError) as error:
            self.operation.run("docker", "run")
        self.assertNotIn("password", str(error.exception))
        self.assertNotIn("private-model-key", str(error.exception))
        self.assertEqual(list(self.operation.target.glob("private-failure-*.log")), [])

    def test_execution_failure_preserves_private_redacted_stderr(self):
        self.operation.args.execute = True
        self.operation.state = {"phase": "PREPARING"}
        result = subprocess.CompletedProcess(["docker"], 2, stdout="", stderr="Traceback: build failed\npostgresql://user:password@db private-model-key private-jwt-key")
        with patch.object(release.subprocess, "run", return_value=result), self.assertRaises(release.UpgradeError) as error:
            self.operation.run("docker", "run")
        logs = list(self.operation.target.glob("private-failure-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn(str(logs[0]), str(error.exception))
        self.assertIn("phase=PREPARING", str(error.exception))
        saved = logs[0].read_text()
        self.assertIn("Traceback: build failed", saved)
        for secret in ("password", "private-model-key", "private-jwt-key"):
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, str(error.exception))
        if os.name != "nt":
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)

    def test_timeout_preserves_private_diagnostics_without_echo(self):
        self.operation.args.execute = True
        failure = subprocess.TimeoutExpired("docker", 1, stderr=b"waiting for dependency private-model-key")
        with patch.object(release.subprocess, "run", side_effect=failure), self.assertRaises(release.UpgradeError) as error:
            self.operation.run("docker", "run")
        self.assertIn("timeout", str(error.exception))
        self.assertNotIn("private-model-key", str(error.exception))
        log = next(self.operation.target.glob("private-failure-*.log"))
        self.assertIn("waiting for dependency", log.read_text())
        self.assertNotIn("private-model-key", log.read_text())


def _candidate_saved_payload(connection, candidate_result_id: str) -> dict:
    """Read the exact result written by the candidate's acceptance journey.

    The caller supplies that journey's result ID. Representative new fields
    prevent an unrelated old-shape row from standing in for the new write.
    """
    try:
        result_id = str(UUID(candidate_result_id))
    except (ValueError, TypeError, AttributeError):
        raise RuntimeError("Candidate result ID is required; previous-code compatibility is NOT_RUN") from None
    row = connection.execute("""SELECT public_json FROM trip_understanding_results
        WHERE result_id=%s""", (result_id,)).fetchone()
    if row is None:
        raise RuntimeError("Requested candidate result was not found; previous-code compatibility is NOT_RUN")
    payload = row[0]
    days = payload.get("days", []) if isinstance(payload, dict) else []
    if not isinstance(days, list):
        days = []
    has_details = any(isinstance(card.get("source_details"), list) and card["source_details"]
        for day in days if isinstance(day, dict) and isinstance(day.get("activities"), list)
        for card in day.get("activities", []) if isinstance(card, dict))
    has_unprocessed = any(isinstance(day.get("unprocessed_count"), int)
        and not isinstance(day["unprocessed_count"], bool) and day["unprocessed_count"] > 0
        for day in days if isinstance(day, dict))
    coverage = payload.get("coverage", {}) if isinstance(payload, dict) else {}
    if not (has_details and has_unprocessed and isinstance(coverage, dict)
            and isinstance(coverage.get("unprocessed_count"), int)
            and not isinstance(coverage["unprocessed_count"], bool)
            and coverage["unprocessed_count"] > 0 and coverage.get("complete") is False):
        raise RuntimeError("Candidate result lacks nonempty source details and unfinished coverage; previous-code compatibility is NOT_RUN")
    return payload


def previous_model_compatibility(local_env: Path, previous_model_dir: Path, *, candidate_result_id: str) -> dict:
    """Read one caller-selected candidate write with the exact old models.

    Model loading alone does not verify same-database editing, undo or queued
    worker recovery; those remain separate user-path checks.
    """
    import psycopg
    from dotenv import dotenv_values
    from pydantic import ValidationError

    repository = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repository / "backend"))
    from app.trip_understanding.models import UserFacingTripResult

    values = dotenv_values(local_env)
    with psycopg.connect(host="127.0.0.1", port=int(values["EXPERIENCE_PG_PORT"]),
                        user=values.get("EXPERIENCE_PG_USER") or "experience",
                        password=values["EXPERIENCE_PG_PASSWORD"],
                        dbname=release.identifier(values["EXPERIENCE_DATABASE"]), connect_timeout=5) as connection:
        connection.execute("BEGIN READ ONLY")
        payload = _candidate_saved_payload(connection, candidate_result_id)
    try:
        UserFacingTripResult.model_validate(payload)
    except ValidationError:
        raise RuntimeError("Candidate result cannot be read by current models; previous-code compatibility is NOT_RUN") from None
    timing_name = "app.trip_understanding.timing"
    original_timing = sys.modules[timing_name]
    modules = []
    try:
        for name, file in ((timing_name, "timing.py"), ("upgrade_previous_models", "models.py")):
            spec = importlib.util.spec_from_file_location(name, previous_model_dir / file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules.append(module)
        try:
            modules[-1].UserFacingTripResult.model_validate(payload)
        except ValidationError as error:
            # Pydantic's messages/inputs may contain user content. Emit field paths and types only.
            errors = [{"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                      for item in error.errors(include_input=False, include_url=False)]
            return {"actual_new_saved_record": True, "current_model_read": "PASS",
                    "previous_deployed_model_read": "FAIL", "incompatible_fields": errors,
                    "previous_code_rollback": "UNAVAILABLE", "data_preserving_recovery": "CURRENT_CODE_ONLY",
                    "same_database_edit_undo_queued_jobs": "NOT_RUN"}
        return {"actual_new_saved_record": True, "current_model_read": "PASS",
                "previous_deployed_model_read": "PASS", "previous_code_rollback": "OTHER_PATHS_NOT_RUN",
                "same_database_edit_undo_queued_jobs": "NOT_RUN"}
    finally:
        sys.modules[timing_name] = original_timing
        sys.modules.pop("upgrade_previous_models", None)


class CandidateModelCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.result_id = str(uuid4())
        self.payload = {"days": [{"unprocessed_count": 1, "activities": [
            {"source_details": [{"name": "太和殿", "optional": False}]}]}],
            "coverage": {"unprocessed_count": 1, "complete": False}}
        self.connection = Mock()
        self.connection.execute.return_value.fetchone.return_value = (self.payload,)

    def test_reads_only_the_requested_candidate_result(self):
        self.assertIs(_candidate_saved_payload(self.connection, self.result_id), self.payload)
        query, parameters = self.connection.execute.call_args.args
        self.assertIn("WHERE result_id=%s", query)
        self.assertEqual(parameters, (self.result_id,))
        self.assertNotIn("ORDER BY", query)
        self.assertEqual(self.connection.execute.call_count, 1)

    def test_missing_or_invalid_id_is_not_run_without_query(self):
        for invalid in (None, "", "not-a-result-id", "' OR true --"):
            with self.subTest(value=invalid), self.assertRaisesRegex(RuntimeError, "NOT_RUN"):
                _candidate_saved_payload(self.connection, invalid)
        self.connection.execute.assert_not_called()

    def test_cli_requires_candidate_id_before_reading_environment_or_database(self):
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
            "--local-env", "does-not-exist.env", "--previous-model-dir", "does-not-exist-models"],
            capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("requires --candidate-result-id", completed.stderr)

    def test_unknown_id_never_falls_back_to_another_saved_result(self):
        self.connection.execute.return_value.fetchone.return_value = None
        with self.assertRaisesRegex(RuntimeError, "NOT_RUN"):
            _candidate_saved_payload(self.connection, self.result_id)
        self.assertEqual(self.connection.execute.call_count, 1)

    def test_old_shape_empty_details_and_missing_unprocessed_are_not_run(self):
        import copy
        changes = (
            lambda value: value["days"][0]["activities"][0].pop("source_details"),
            lambda value: value["days"][0]["activities"][0].update(source_details=[]),
            lambda value: value["days"][0].pop("unprocessed_count"),
            lambda value: value["days"][0].update(unprocessed_count=0),
            lambda value: value["coverage"].update(unprocessed_count=0),
            lambda value: value["coverage"].update(complete=True),
        )
        for change in changes:
            candidate = copy.deepcopy(self.payload)
            change(candidate)
            self.connection.reset_mock()
            self.connection.execute.return_value.fetchone.return_value = (candidate,)
            with self.subTest(candidate=candidate), self.assertRaisesRegex(RuntimeError, "NOT_RUN"):
                _candidate_saved_payload(self.connection, self.result_id)
            self.assertEqual(self.connection.execute.call_count, 1)


def local_restore_drill(local_env: Path, pg_bin: Path) -> dict:
    """Actual PostgreSQL dump/restore with private local data, never a remote DSN.

    The source is read-only. All writes and cleanup are restricted to three
    randomly named databases created by this invocation on 127.0.0.1.
    """
    import asyncio
    import hashlib
    from urllib.parse import quote
    import psycopg
    from psycopg import sql
    from dotenv import dotenv_values

    repository = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repository / "scripts"), str(repository / "backend")]
    from experience import migrate
    from app.trip_understanding.source_crypto import SourceCipher

    values = dotenv_values(local_env)
    source_db = release.identifier(values["EXPERIENCE_DATABASE"])
    port = int(values["EXPERIENCE_PG_PORT"])
    password = values["EXPERIENCE_PG_PASSWORD"]
    cipher = SourceCipher(values["TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY"])
    connection = dict(host="127.0.0.1", port=port, user=values.get("EXPERIENCE_PG_USER") or "experience", password=password, connect_timeout=5)
    child_env = {**os.environ, "PGPASSWORD": password}
    names = ["upgrade_drill_" + uuid4().hex[:12] for _ in range(3)]
    created = []
    work = repository / ".local-artifacts" / "upgrade-drill"
    work.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="restore-", dir=work)
    private = Path(temporary.name).resolve()
    if private.parent != work.resolve():
        raise RuntimeError("Private drill directory is outside the intended workspace")
    report = {"source": "existing_local_database", "production_access": False}

    def native(program, db, *args, snapshot=None):
        command = [str(pg_bin / (program + (".exe" if os.name == "nt" else ""))),
                   "-h", "127.0.0.1", "-p", str(port), "-U", connection["user"], "-d", db, *args]
        if snapshot:
            command.extend(["--snapshot", snapshot])
        result = subprocess.run(command, env=child_env, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError("Local PostgreSQL archive operation failed; private error suppressed")

    def counts(conn):
        tables = ("users", "rooms", "trip_understandings", "trip_understanding_revisions", "trip_understanding_sources", "trip_stay_selections")
        return {table: conn.execute(sql.SQL("SELECT count(*) FROM {} ").format(sql.Identifier(table))).fetchone()[0] for table in tables}

    def decryptable(conn):
        rows = conn.execute("SELECT source_id, content_hash, encrypted_content FROM trip_understanding_sources WHERE encrypted_content IS NOT NULL ORDER BY source_id").fetchall()
        for source_id, content_hash, encrypted in rows:
            cipher.decrypt(bytes(encrypted), source_id=source_id, content_hash=content_hash)
        return len(rows)

    def preserved_rows(conn):
        # Compare values in memory, not only counts. Do not log private trips,
        # tokens, command responses or the JSON contained in these rows.
        tables = ("trip_understandings", "trip_understanding_revisions",
                  "trip_understanding_activities", "trip_understanding_results",
                  "trip_understanding_idempotency_records",
                  "itinerary_revisions", "itinerary_edit_commands",
                  "trip_plan_revision_refs", "trip_map_render_jobs",
                  "trip_map_render_snapshots", "trip_map_route_edges",
                  "trip_map_route_mode_facts", "trip_stay_selections",
                  "room_itinerary_revisions", "room_itinerary_requests")
        return {table: conn.execute(sql.SQL(
            "SELECT to_jsonb(t)::text FROM {} t ORDER BY to_jsonb(t)::text"
        ).format(sql.Identifier(table))).fetchall()
            if conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] else []
            for table in tables}

    def migrate_local(db):
        dsn = f"postgresql://{quote(connection['user'], safe='')}:{quote(password, safe='')}@127.0.0.1:{port}/{db}"
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(migrate({}, dsn=dsn))

    try:
        with psycopg.connect(**connection, dbname="postgres", autocommit=True) as admin:
            for name in names:
                if name == source_db or not name.startswith("upgrade_drill_"):
                    raise RuntimeError("Unsafe temporary database target")
                admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(name)))
                created.append(name)
        archive = private / "source.dump"
        with psycopg.connect(**connection, dbname=source_db, autocommit=True) as original:
            original.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = original.execute("SELECT pg_export_snapshot()").fetchone()[0]
            expected = counts(original)
            expected_rows = preserved_rows(original)
            encrypted_count = decryptable(original)
            native("pg_dump", source_db, "-Fc", "-f", str(archive), snapshot=snapshot)
            original.execute("ROLLBACK")
        native("pg_restore", names[0], "--exit-on-error", str(archive))
        migrate_local(names[0])
        with psycopg.connect(**connection, dbname=names[0], autocommit=True) as restored:
            assert counts(restored) == expected, "Restored local rows differ"
            assert decryptable(restored) == encrypted_count, "Restored source encryption differs"
            assert preserved_rows(restored) == expected_rows, "Restored trip, command, map or shared-route values differ"
            restored.execute("INSERT INTO rooms(room_id,thread_id,trip_city) VALUES('upgrade-new-write','upgrade-thread','上海')")
        recovery = private / "current-after-write.dump"
        native("pg_dump", names[0], "-Fc", "-f", str(recovery))
        native("pg_restore", names[2], "--exit-on-error", str(recovery))
        migrate_local(names[2])
        with psycopg.connect(**connection, dbname=names[2]) as recovered:
            assert recovered.execute("SELECT count(*) FROM rooms WHERE room_id='upgrade-new-write'").fetchone()[0] == 1
            assert counts(recovered) == {**expected, "rooms": expected["rooms"] + 1}
            assert decryptable(recovered) == encrypted_count
            assert preserved_rows(recovered) == expected_rows, "Recovery changed existing trip, command, map or shared-route values"
        report.update(real_local_restore="PASS", retained_counts=expected,
                      encrypted_sources_readable=encrypted_count, recovery_keeps_new_write="PASS",
                      restored_business_values="PASS", recovered_business_values="PASS",
                      compared_row_counts={table: len(rows) for table, rows in expected_rows.items()})

        # A separate 035 schema mirrors the current server version. These rows are synthetic.
        baseline = names[1]
        with psycopg.connect(**connection, dbname=baseline, autocommit=True) as old:
            old.execute((repository / "backend/app/db/init.sql").read_text(encoding="utf-8"))
            old.execute("CREATE TABLE applied_migrations(filename TEXT PRIMARY KEY,applied_at TIMESTAMPTZ DEFAULT NOW())")
            for file in sorted((repository / "backend/app/db/migrations").glob("*.sql")):
                if int(file.name[:3]) <= 35:
                    with old.transaction():
                        old.execute(file.read_text(encoding="utf-8"))
                        old.execute("INSERT INTO applied_migrations(filename) VALUES(%s)", (file.name,))
            content = "第一天游览北京，晚上入住迁移样例酒店。"
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            encrypted = cipher.encrypt(content, source_id="drill-source", content_hash=content_hash)
            old.execute("INSERT INTO users(user_id,nickname) VALUES('drill-user','迁移演练')")
            old.execute("INSERT INTO rooms(room_id,thread_id) VALUES('drill-room','drill-thread')")
            old.execute("INSERT INTO trip_understandings(understanding_id,public_resource_id,owner_user_id,state,etag_nonce,source_expires_at) VALUES('drill-trip','upgrade-drill-public-resource','drill-user','PARTIAL',repeat('0',64),NOW()+INTERVAL '1 day')")
            old.execute("INSERT INTO trip_understanding_sources(source_id,understanding_id,source_type,content_hash,encrypted_content,encryption_key_ref,retention_until) VALUES('drill-source','drill-trip','TEXT',%s,%s,%s,NOW()+INTERVAL '1 day')", (content_hash, encrypted, cipher.key_ref))
            old.execute("INSERT INTO trip_understanding_revisions(understanding_id,revision,source_id,status,content_hash,destination_json,assumptions_json,proposal_json,inference_binding_json,compiler_receipt_json) VALUES('drill-trip',1,'drill-source','PARTIAL',%s,'{}','[]','{}','{}','{}')", (content_hash,))
            old.execute("INSERT INTO trip_plan_revision_refs(plan_ref_id,understanding_id,revision_kind,aggregate_id,revision,stop_set_hash) VALUES('drill-plan','drill-trip','UNDERSTANDING','drill-trip',1,repeat('0',64))")
            old.execute("INSERT INTO trip_map_render_jobs(map_job_id,plan_ref_id,understanding_id,route_config_hash,logical_key_hash,request_origin,status) VALUES('drill-old-map','drill-plan','drill-trip',repeat('2',64),repeat('2',64),'INITIAL','UNAVAILABLE')")
            old_map = old.execute("SELECT to_jsonb(t) FROM trip_map_render_jobs t WHERE map_job_id='drill-old-map'").fetchone()[0]
            old.execute("INSERT INTO trip_stay_recommendation_jobs(stay_job_id,plan_ref_id,understanding_id,policy_hash,logical_key_hash,status) VALUES('drill-job','drill-plan','drill-trip',repeat('0',64),repeat('1',64),'READY')")
            old.execute("INSERT INTO trip_stay_recommendation_snapshots(snapshot_id,stay_job_id,plan_ref_id,status,policy_hash,area_summary,searched_scopes_json,candidate_count,snapshot_sha256,provider_binding_json,started_at,finished_at,observed_at) VALUES('drill-snapshot','drill-job','drill-plan','READY',repeat('0',64),'迁移样例','[]',1,repeat('0',64),'{}',NOW(),NOW(),NOW())")
            old.execute("INSERT INTO trip_stay_candidates(candidate_id,snapshot_id,public_candidate_token,rank,canonical_place_id,name,brand,category,area_or_address,city,longitude,latitude,total_score,max_single_leg_minutes,transfer_count,missing_leg_count,evidence_penalty,provider_binding_json) VALUES('drill-candidate','drill-snapshot','upgrade-drill-public-candidate',1,'drill-place','迁移样例酒店','样例','住宿','样例地址','北京',116,39,0,0,0,0,0,'{}')")
            old.execute("INSERT INTO trip_stay_selections(selection_id,understanding_id,source_snapshot_id,source_plan_ref_id,target_plan_ref_id,candidate_id,selected_place_id,selected_name,selected_brand,selected_address,selected_city,longitude,latitude,overnight_days,selection_request_hash) VALUES('drill-selection','drill-trip','drill-snapshot','drill-plan','drill-plan','drill-candidate','drill-place','迁移样例酒店','样例','样例地址','北京',116,39,ARRAY[1],repeat('0',64))")
            legacy_counts = counts(old)
        migrate_local(baseline)
        migrate_local(baseline)
        with psycopg.connect(**connection, dbname=baseline) as upgraded:
            assert counts(upgraded) == legacy_counts
            assert decryptable(upgraded) == 1
            assert upgraded.execute("SELECT segment_key FROM trip_stay_candidates").fetchone()[0] == "legacy"
            assert upgraded.execute("SELECT segment_key,selected_place_id,overnight_days FROM trip_stay_selections").fetchone() == ("legacy", "drill-place", [1])
            assert upgraded.execute("SELECT refresh_generation FROM trip_stay_recommendation_jobs").fetchone()[0] == 0
            applied = [row[0] for row in upgraded.execute("SELECT filename FROM applied_migrations WHERE filename>='036' ORDER BY filename")]
            expected_migrations = sorted(file.name for file in
                (repository / "backend/app/db/migrations").glob("*.sql") if int(file.name[:3]) > 35)
            assert applied == expected_migrations
            current_map = upgraded.execute("SELECT to_jsonb(t) FROM trip_map_render_jobs t WHERE map_job_id='drill-old-map'").fetchone()[0]
            assert current_map == {**old_map, "render_generation": 0}
            assert upgraded.execute("SELECT count(*) FROM room_itinerary_revisions").fetchone()[0] == 0
            assert upgraded.execute("SELECT count(*) FROM room_itinerary_requests").fetchone()[0] == 0
            upgraded.execute("SELECT metrics_json FROM trip_daily_dining_jobs LIMIT 1")
        report.update(schema_035_to_current="PASS", repeated_migration="PASS", legacy_stay_selection="PASS",
                      legacy_map_generation_zero="PASS", shared_route_tables_added="PASS", appended_migrations=applied)
        # Exercise the actual preview queue-isolation SQL against real constraints.
        # Only this synthetic database is changed, never the source or restored live copy.
        with psycopg.connect(**connection, dbname=baseline, autocommit=True) as preview:
            preview.execute("INSERT INTO trip_understanding_jobs(job_id,understanding_id,revision,job_type,status,lease_owner,lease_until,input_hash) VALUES('drill-understand','drill-trip',1,'UNDERSTAND','RUNNING','old-copy-worker',NOW()+INTERVAL '1 minute',repeat('0',64))")
            preview.execute("INSERT INTO trip_map_render_jobs(map_job_id,plan_ref_id,understanding_id,route_config_hash,logical_key_hash,request_origin,status,lease_owner,lease_until) VALUES('drill-map','drill-plan','drill-trip',repeat('0',64),repeat('0',64),'INITIAL','BUILDING','old-copy-worker',NOW()+INTERVAL '1 minute')")
            preview.execute("UPDATE trip_stay_recommendation_jobs SET status='QUEUED'")
            preview.execute("INSERT INTO trip_daily_dining_jobs(understanding_id,revision,status,lease_owner,lease_until) VALUES('drill-trip',1,'BUILDING','old-copy-worker',NOW()+INTERVAL '1 minute')")
            operation = release.Upgrade(arguments(rehearsal_db=baseline))
            operation.assert_writer_set = Mock()
            operation.run = lambda *args, **kw: preview.execute(args[-1]) and ""
            operation.pause_copied_jobs()
            for table in ("trip_understanding_jobs", "trip_map_render_jobs", "trip_stay_recommendation_jobs", "trip_daily_dining_jobs"):
                rows = preview.execute(sql.SQL("SELECT status,lease_owner,lease_until FROM {}").format(sql.Identifier(table))).fetchall()
                assert rows and all(row[0] in {"FAILED", "UNAVAILABLE"} and row[1:] == (None, None) for row in rows)
            # A new preview job created after pausing is not touched by repeated preview starts.
            preview.execute("UPDATE trip_understanding_jobs SET status='QUEUED'")
            assert preview.execute("SELECT status FROM trip_understanding_jobs").fetchone()[0] == "QUEUED"
            operation.state["preview_started_at"] = "2026-01-01T00:00:00+00:00"
            operation.args.preview_trip_id = "upgrade-drill-public-resource"
            operation.query = lambda db, query: json.dumps(preview.execute(query).fetchone()[0])
            try:
                operation.verify_preview_journey()
            except release.UpgradeError:
                pass  # No generated public result exists in this synthetic old-data sample.
            else:
                raise AssertionError("A restored copy without a real new saved result must not activate")
        report.update(copied_job_leases_isolated="PASS", restored_copy_cannot_activate="PASS")
    finally:
        if created:
            with psycopg.connect(**connection, dbname="postgres", autocommit=True) as admin:
                for name in created:
                    if name not in names or name == source_db or not name.startswith("upgrade_drill_"):
                        raise RuntimeError("Refusing cleanup outside this drill's databases")
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        if private.parent != work.resolve():
            raise RuntimeError("Refusing cleanup outside the drill directory")
        temporary.cleanup()
    report["temporary_databases_removed"] = len(created)
    return report


if __name__ == "__main__":
    if "--local-env" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser(description="Private local PostgreSQL restore and append-migration drill")
        parser.add_argument("--local-env", type=Path, required=True)
        parser.add_argument("--pg-bin", type=Path)
        parser.add_argument("--previous-model-dir", type=Path)
        parser.add_argument("--candidate-result-id", help="Exact result ID written by this candidate's acceptance journey")
        args = parser.parse_args()
        if args.previous_model_dir:
            if not args.candidate_result_id:
                parser.error("--previous-model-dir requires --candidate-result-id from this candidate's actual new write")
            result = previous_model_compatibility(args.local_env, args.previous_model_dir, candidate_result_id=args.candidate_result_id)
        elif args.pg_bin:
            result = local_restore_drill(args.local_env, args.pg_bin)
        else:
            parser.error("Supply --pg-bin for a restore drill or --previous-model-dir for old model compatibility")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        unittest.main()
