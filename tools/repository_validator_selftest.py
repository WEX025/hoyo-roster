"""Offline fixtures and self-tests for the repository validator."""

from __future__ import annotations
import copy
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

Mutation = Callable[[Path], object]
API_NAMES = (
    "EQUIPMENT_KEYS",
    "EXPECTED_MANIFEST",
    "GAMES",
    "MAIN_STAT_KEYS",
    "PANEL_KEYS",
    "SCHEMA_PATHS",
    "_load_documents",
    "_require_real_directory",
    "_require_regular_file",
    "load_json",
    "validate_repository",
    "write_json",
)


class RepositorySelfTest:
    def __init__(self, api: Any) -> None:
        self.api = api

    def _panel(self, game: str) -> dict[str, float]:
        panel = {key: 1.0 for key in self.api.PANEL_KEYS[game]}
        if game == "zenless-zone-zero":
            panel["sheer_force"] = 100.0
            panel["wind_damage_bonus"] = 0.3
        return panel

    def _equipment(self, game: str, slot: str) -> dict[str, Any]:
        return {
            "set_id": f"{game}-fixture-set",
            "set_name": "Fixture Set",
            "rarity": 5,
            "level": 15,
            "main_stat": {"key": self.api.MAIN_STAT_KEYS[game][slot], "value": 1.0},
            "substats": [
                {"key": "attack_percent", "value": 0.1},
                {"key": "crit_damage", "value": 0.2},
            ],
        }

    def _equipment_set(self, game: str) -> dict[str, dict[str, Any]]:
        return {
            slot: self._equipment(game, slot) for slot in self.api.EQUIPMENT_KEYS[game]
        }

    def _set_complete(self, profile: dict[str, Any], suffix: str) -> None:
        profile["account"] = {
            "uid": f"fixture-{suffix}",
            "server": "fixture-server",
            "nickname": f"Fixture {suffix}",
            "player_level": 60,
        }
        profile["sync"] = {
            "state": "complete",
            "source": "manual",
            "synced_at": "2026-07-31T00:00:00Z",
        }

    def _require_self_test_source_path(
        self, source_root: Path, relative: Path, *, directory: bool
    ) -> None:
        current = source_root.expanduser().absolute()
        self.api._require_real_directory(current)
        for part in relative.parts[:-1]:
            current /= part
            self.api._require_real_directory(current)
        target = current / relative.parts[-1]
        if directory:
            self.api._require_real_directory(target)
        else:
            self.api._require_regular_file(target)

    def _validate_self_test_source(self, source_root: Path) -> None:
        self._require_self_test_source_path(
            source_root, Path("schemas"), directory=True
        )
        self._require_self_test_source_path(source_root, Path("tools"), directory=True)
        self._require_self_test_source_path(
            source_root, Path("tools/validate_repository.py"), directory=False
        )
        for schema_path in sorted(self.api.SCHEMA_PATHS):
            self._require_self_test_source_path(
                source_root, Path(schema_path), directory=False
            )

    def _create_empty_fixture(self, root: Path, source_root: Path) -> None:
        self._validate_self_test_source(source_root)
        for schema_path in sorted(self.api.SCHEMA_PATHS):
            source = source_root / schema_path
            self.api._require_regular_file(source)
            destination = root / schema_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
        validator_source = source_root / "tools/validate_repository.py"
        self.api._require_regular_file(validator_source)
        validator_destination = root / "tools/validate_repository.py"
        validator_destination.parent.mkdir(parents=True)
        shutil.copyfile(validator_source, validator_destination, follow_symlinks=False)
        self.api.write_json(
            root / "data/manifest.json",
            {
                "schema_version": "repository-manifest/v1",
                "games": copy.deepcopy(self.api.EXPECTED_MANIFEST),
            },
        )
        roster_versions = {
            "genshin-impact": "genshin-impact-roster/v1",
            "honkai-star-rail": "honkai-star-rail-roster/v1",
            "zenless-zone-zero": "zenless-zone-zero-roster/v1",
        }
        for game in self.api.GAMES:
            entry = self.api.EXPECTED_MANIFEST[game]
            self.api.write_json(
                root / entry["profile"],
                {
                    "schema_version": "game-profile/v1",
                    "game": game,
                    "account": {
                        "uid": None,
                        "server": None,
                        "nickname": None,
                        "player_level": None,
                    },
                    "sync": {"state": "not_synced", "source": None, "synced_at": None},
                },
            )
            self.api.write_json(
                root / entry["roster"],
                {
                    "schema_version": roster_versions[game],
                    "game": game,
                    "characters": {},
                },
            )
            build_dir = root / entry["builds_dir"]
            build_dir.mkdir(parents=True)
            (build_dir / ".gitkeep").write_bytes(b"")
        self.api.write_json(
            root / self.api.EXPECTED_MANIFEST["zenless-zone-zero"]["bangboos"],
            {
                "schema_version": "zenless-zone-zero-bangboos/v1",
                "game": "zenless-zone-zero",
                "items": {},
            },
        )

    def _populate_positive(self, root: Path) -> None:
        documents, _, load_errors = self.api._load_documents(root, scope_game=None)
        if load_errors:
            raise RuntimeError(load_errors)
        for game in self.api.GAMES:
            profile_path = self.api.EXPECTED_MANIFEST[game]["profile"]
            profile = documents[profile_path]
            self._set_complete(profile, game)
            self.api.write_json(root / profile_path, profile)
        genshin_roster = documents["data/genshin-impact/roster.json"]
        genshin_roster["characters"] = {
            "1001": {
                "name": "Fixture Genshin Character",
                "level": 90,
                "ascension": 6,
                "constellation": 1,
            }
        }
        self.api.write_json(root / "data/genshin-impact/roster.json", genshin_roster)
        self.api.write_json(
            root / "data/genshin-impact/builds/1001.json",
            {
                "schema_version": "genshin-impact-build/v1",
                "game": "genshin-impact",
                "panel_context": "out_of_combat",
                "progression": {
                    "normal_attack_level": 9,
                    "elemental_skill_level": 10,
                    "elemental_burst_level": 10,
                },
                "weapon": {
                    "weapon_id": "gi-weapon-model-1",
                    "name": "Fixture Weapon",
                    "rarity": 5,
                    "level": 90,
                    "ascension": 6,
                    "refinement": 1,
                },
                "artifacts": self._equipment_set("genshin-impact"),
                "panel": self._panel("genshin-impact"),
            },
        )
        hsr_roster = documents["data/honkai-star-rail/roster.json"]
        hsr_roster["characters"] = {
            "2001": {
                "name": "Fixture Normal Character",
                "level": 80,
                "promotion": 6,
                "eidolon": 0,
            },
            "2002": {
                "name": "Fixture Remembrance Character",
                "level": 80,
                "promotion": 6,
                "eidolon": 1,
            },
        }
        self.api.write_json(root / "data/honkai-star-rail/roster.json", hsr_roster)
        normal_progression = {
            "basic_level": 6,
            "skill_level": 10,
            "ultimate_level": 10,
            "talent_level": 10,
            "trace_nodes": {"trace-1": {"level": 1, "unlocked": True}},
            "memosprite": None,
        }
        memo_progression = copy.deepcopy(normal_progression)
        memo_progression["memosprite"] = {
            "skills": {"memo-skill-1": {"name": "Fixture Skill", "level": 6}},
            "talents": {"memo-talent-1": {"name": "Fixture Talent", "level": 6}},
        }
        for character_id, progression in (
            ("2001", normal_progression),
            ("2002", memo_progression),
        ):
            self.api.write_json(
                root / f"data/honkai-star-rail/builds/{character_id}.json",
                {
                    "schema_version": "honkai-star-rail-build/v1",
                    "game": "honkai-star-rail",
                    "panel_context": "out_of_combat",
                    "progression": progression,
                    "light_cone": {
                        "light_cone_id": f"hsr-cone-model-{character_id}",
                        "name": "Fixture Light Cone",
                        "rarity": 5,
                        "level": 80,
                        "promotion": 6,
                        "superimposition": 1,
                    },
                    "relics": self._equipment_set("honkai-star-rail"),
                    "panel": self._panel("honkai-star-rail"),
                },
            )
        zzz_roster = documents["data/zenless-zone-zero/roster.json"]
        zzz_roster["characters"] = {
            "3001": {
                "name": "Fixture Normal Agent",
                "level": 60,
                "promotion": 5,
                "mindscape": 0,
            },
            "3002": {
                "name": "Fixture Potential Agent",
                "level": 60,
                "promotion": 5,
                "mindscape": 1,
            },
        }
        self.api.write_json(root / "data/zenless-zone-zero/roster.json", zzz_roster)
        bangboos = documents["data/zenless-zone-zero/bangboos.json"]
        bangboos["items"] = {
            "6001": {
                "name": "Fixture Bangboo",
                "rarity": 5,
                "level": 60,
                "core_level": 5,
            }
        }
        self.api.write_json(root / "data/zenless-zone-zero/bangboos.json", bangboos)
        base_progression = {
            "basic_attack_level": 12,
            "dodge_level": 12,
            "assist_level": 12,
            "special_attack_level": 12,
            "chain_attack_level": 12,
            "core_skill_rank": "0",
            "potential_vision": None,
        }
        potential_progression = copy.deepcopy(base_progression)
        potential_progression["core_skill_rank"] = "F"
        potential_progression["potential_vision"] = {"level": 1, "max_level": 3}
        for character_id, progression in (
            ("3001", base_progression),
            ("3002", potential_progression),
        ):
            self.api.write_json(
                root / f"data/zenless-zone-zero/builds/{character_id}.json",
                {
                    "schema_version": "zenless-zone-zero-build/v1",
                    "game": "zenless-zone-zero",
                    "panel_context": "out_of_combat",
                    "progression": progression,
                    "w_engine": {
                        "w_engine_id": f"zzz-engine-model-{character_id}",
                        "name": "Fixture W-Engine",
                        "rarity": 5,
                        "level": 60,
                        "modification": 1,
                    },
                    "drive_discs": self._equipment_set("zenless-zone-zero"),
                    "panel": self._panel("zenless-zone-zero"),
                },
            )

    def _mutate_json(
        self, root: Path, path: str, mutator: Callable[[dict[str, Any]], None]
    ) -> None:
        target = root / path
        value = self.api.load_json(target)
        mutator(value)
        self.api.write_json(target, value)

    def _run_self_test(self, source_root: Path) -> int:
        positive_names: tuple[str, ...] = (
            "genshin_character_embedded_weapon_five_artifacts_panel",
            "hsr_normal_remembrance_embedded_cones_six_relics",
            "zzz_zero_f_potential_embedded_engine_six_discs_bangboo_wind_sheer",
        )
        try:
            self._validate_self_test_source(source_root)
        except (OSError, FileNotFoundError) as source_error:
            print("SELF_TEST=FAIL")
            print(f"SOURCE_CLOSURE_ERROR={type(source_error).__name__}: {source_error}")
            return 1
        with tempfile.TemporaryDirectory(
            prefix="hoyo-character-state-self-test-"
        ) as tmp:
            tmp_root = Path(tmp)
            empty_root = tmp_root / "empty"
            self._create_empty_fixture(empty_root, source_root)
            empty_errors = self.api.validate_repository(empty_root, enforce_empty=True)
            if empty_errors:
                print("SELF_TEST=FAIL")
                for issue in empty_errors:
                    print(f"EMPTY_FIXTURE_ERROR={issue}")
                return 1
            positive_root = tmp_root / "positive"
            shutil.copytree(empty_root, positive_root)
            self._populate_positive(positive_root)
            positive_errors = self.api.validate_repository(
                positive_root, enforce_empty=False
            )
            if positive_errors:
                print("SELF_TEST=FAIL")
                for issue in positive_errors:
                    print(f"POSITIVE_ERROR={issue}")
                return 1
            if not self.api.validate_repository(positive_root, enforce_empty=True):
                print("SELF_TEST=FAIL")
                print("POPULATED_EMPTY_BASELINE_NOT_REJECTED=YES")
                return 1
            future_skill_root = tmp_root / "future-skill-compatible"
            shutil.copytree(empty_root, future_skill_root)
            skill_dir = future_skill_root / "skills/hoyo-character-sync"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "# HoYo character sync\n\nFuture synchronization workflow.\n",
                encoding="utf-8",
            )
            (skill_dir / "probe.py").write_text(
                "def probe() -> str:\n    return 'ok'\n", encoding="utf-8"
            )
            (skill_dir / "session_probe.py").write_text(
                "def describe_session() -> str:\n    return 'no credentials stored'\n",
                encoding="utf-8",
            )
            self.api.write_json(
                skill_dir / "metadata.json",
                {"name": "hoyo-character-sync", "status": "future"},
            )
            future_skill_errors = self.api.validate_repository(
                future_skill_root, enforce_empty=True
            )
            if future_skill_errors:
                print("SELF_TEST=FAIL")
                for issue in future_skill_errors:
                    print(f"FUTURE_SKILL_ERROR={issue}")
                return 1

            def mark_partial(root: Path, game: str) -> None:
                profile_path = self.api.EXPECTED_MANIFEST[game]["profile"]
                self._mutate_json(
                    root,
                    profile_path,
                    lambda value: value["sync"].__setitem__("state", "partial"),
                )

            def minimal_genshin_partial(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self.api.write_json(
                    root / "data/genshin-impact/builds/1001.json",
                    {
                        "schema_version": "genshin-impact-build/v1",
                        "game": "genshin-impact",
                    },
                )

            def hsr_skills_partial(root: Path) -> None:
                mark_partial(root, "honkai-star-rail")
                self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2002.json",
                    lambda value: value.__setitem__(
                        "progression",
                        {
                            "memosprite": {
                                "skills": {"memo-skill-1": {"name": "Fixture Skill"}}
                            }
                        },
                    ),
                )

            def zzz_potential_partial(root: Path) -> None:
                mark_partial(root, "zenless-zone-zero")
                self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3002.json",
                    lambda value: value.__setitem__(
                        "progression", {"potential_vision": {"level": 1}}
                    ),
                )

            def genshin_weapon_partial(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__(
                        "weapon", {"name": "Fixture Weapon"}
                    ),
                )

            def genshin_equipment_partial(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__(
                        "artifacts", {"flower": {"set_id": "fixture-set"}}
                    ),
                )

            def genshin_empty_slot_partial(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__("artifacts", {"flower": None}),
                )

            def hsr_memosprite_null_partial(root: Path) -> None:
                mark_partial(root, "honkai-star-rail")
                self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2001.json",
                    lambda value: value.__setitem__(
                        "progression", {"memosprite": None}
                    ),
                )

            def hsr_talents_partial(root: Path) -> None:
                mark_partial(root, "honkai-star-rail")
                self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2002.json",
                    lambda value: value.__setitem__(
                        "progression",
                        {"memosprite": {"talents": {"memo-talent-1": {"level": 6}}}},
                    ),
                )

            partial_positive_cases: tuple[tuple[str, str, Mutation], ...] = (
                (
                    "genshin_partial_minimal_build",
                    "genshin-impact",
                    minimal_genshin_partial,
                ),
                (
                    "hsr_partial_memosprite_skills_only",
                    "honkai-star-rail",
                    hsr_skills_partial,
                ),
                (
                    "zzz_partial_potential_vision_level_only",
                    "zenless-zone-zero",
                    zzz_potential_partial,
                ),
                (
                    "genshin_partial_weapon_subset",
                    "genshin-impact",
                    genshin_weapon_partial,
                ),
                (
                    "genshin_partial_equipment_slot_subset",
                    "genshin-impact",
                    genshin_equipment_partial,
                ),
                (
                    "genshin_partial_explicit_null_slot",
                    "genshin-impact",
                    genshin_empty_slot_partial,
                ),
                (
                    "hsr_partial_memosprite_null",
                    "honkai-star-rail",
                    hsr_memosprite_null_partial,
                ),
                (
                    "hsr_partial_memosprite_talents_only",
                    "honkai-star-rail",
                    hsr_talents_partial,
                ),
            )

            def create_scoped_candidate(
                source: Path, destination: Path, game: str
            ) -> None:
                (destination / "data").mkdir(parents=True)
                shutil.copy2(
                    source / "data/manifest.json", destination / "data/manifest.json"
                )
                shutil.copytree(source / f"data/{game}", destination / f"data/{game}")
                (destination / "schemas/common").mkdir(parents=True)
                shutil.copy2(
                    source / "schemas/common/definitions.schema.json",
                    destination / "schemas/common/definitions.schema.json",
                )
                shutil.copy2(
                    source / "schemas/repository-manifest.schema.json",
                    destination / "schemas/repository-manifest.schema.json",
                )
                shutil.copytree(
                    source / f"schemas/{game}", destination / f"schemas/{game}"
                )
                (destination / "tools").mkdir()
                shutil.copy2(
                    source / "tools/validate_repository.py",
                    destination / "tools/validate_repository.py",
                )

            complete_scoped_names: list[str] = []
            for index, game in enumerate(self.api.GAMES, 1):
                name = f"{game}_complete_scoped_candidate"
                scoped_root = tmp_root / f"positive-complete-scoped-{index:02d}"
                create_scoped_candidate(positive_root, scoped_root, game)
                scoped_errors = self.api.validate_repository(
                    scoped_root, enforce_empty=False, scope_game=game
                )
                if scoped_errors:
                    print("SELF_TEST=FAIL")
                    for issue in scoped_errors:
                        print(f"COMPLETE_SCOPED_ERROR={name}:{issue}")
                    return 1
                complete_scoped_names.append(name)
            positive_names += tuple(complete_scoped_names)
            for index, (name, game, partial_mutation) in enumerate(
                partial_positive_cases, 1
            ):
                case_root = tmp_root / f"positive-partial-{index:02d}"
                shutil.copytree(positive_root, case_root)
                partial_mutation(case_root)
                case_errors = self.api.validate_repository(
                    case_root, enforce_empty=False
                )
                scoped_root = tmp_root / f"positive-partial-scoped-{index:02d}"
                create_scoped_candidate(case_root, scoped_root, game)
                scoped_errors = self.api.validate_repository(
                    scoped_root, enforce_empty=False, scope_game=game
                )
                if case_errors or scoped_errors:
                    print("SELF_TEST=FAIL")
                    for issue in case_errors:
                        print(f"PARTIAL_POSITIVE_ERROR={name}:{issue}")
                    for issue in scoped_errors:
                        print(f"PARTIAL_SCOPED_ERROR={name}:{issue}")
                    return 1
            positive_names += tuple((name for name, _, _ in partial_positive_cases))
            cases: list[tuple[str, Mutation, bool, bool]] = []

            def add(
                name: str,
                mutation: Mutation,
                *,
                empty_source: bool = False,
                enforce_empty: bool = False,
            ) -> None:
                cases.append((name, mutation, empty_source, enforce_empty))

            def partial_profile_null(root: Path, field: str) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value["account"].__setitem__(field, None),
                )

            def partial_sync_null(root: Path, field: str) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value["sync"].__setitem__(field, None),
                )

            def partial_empty_roster(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value.__setitem__("characters", {}),
                )

            def partial_missing_build(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                (root / "data/genshin-impact/builds/1001.json").unlink()

            def partial_orphan_build(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                shutil.copy2(
                    root / "data/genshin-impact/builds/1001.json",
                    root / "data/genshin-impact/builds/9999.json",
                )

            def partial_composite(root: Path, field: str) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__(field, {}),
                )

            def partial_nonnullable_field_null(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["progression"].__setitem__(
                        "normal_attack_level", None
                    ),
                )

            def partial_without_build_field(root: Path, field: str) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.pop(field),
                )

            def partial_empty_equipment_item(root: Path) -> None:
                mark_partial(root, "genshin-impact")
                self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__("artifacts", {"flower": {}}),
                )

            def partial_empty_memosprite(root: Path) -> None:
                mark_partial(root, "honkai-star-rail")
                self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2002.json",
                    lambda value: value.__setitem__("progression", {"memosprite": {}}),
                )

            def partial_empty_potential_vision(root: Path) -> None:
                mark_partial(root, "zenless-zone-zero")
                self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3002.json",
                    lambda value: value.__setitem__(
                        "progression", {"potential_vision": {}}
                    ),
                )

            def not_synced_with_roster_and_build(root: Path) -> None:
                self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value["characters"].__setitem__(
                        "1001",
                        {
                            "name": "Unexpected",
                            "level": 1,
                            "ascension": 0,
                            "constellation": 0,
                        },
                    ),
                )
                self.api.write_json(
                    root / "data/genshin-impact/builds/1001.json",
                    {
                        "schema_version": "genshin-impact-build/v1",
                        "game": "genshin-impact",
                    },
                )

            def nonfinite_json_mutation(constant: str) -> Mutation:
                def mutate(root: Path) -> None:
                    (root / "data/genshin-impact/profile.json").write_text(
                        f'{{"schema_version":"game-profile/v1","probe":{constant}}}\n',
                        encoding="utf-8",
                    )

                return mutate

            def bind_field_mutation(
                action: Callable[[Path, str], None], field: str
            ) -> Mutation:
                def mutate(root: Path) -> None:
                    action(root, field)

                return mutate

            def set_json_field_mutation(
                path: str, containers: tuple[str, ...], field: str, value: object
            ) -> Mutation:
                def update(document: dict[str, Any]) -> None:
                    target = document
                    for container in containers:
                        child = target[container]
                        if not isinstance(child, dict):
                            raise AssertionError(
                                "self-test fixture container was invalid"
                            )
                        target = child
                    target[field] = value

                def mutate(root: Path) -> None:
                    self._mutate_json(root, path, update)

                return mutate

            add(
                "duplicate_json_key",
                lambda root: (root / "data/genshin-impact/profile.json").write_text(
                    '{"schema_version":"game-profile/v1","schema_version":"duplicate"}\n',
                    encoding="utf-8",
                ),
            )
            for name, constant in (
                ("nan_json_number", "NaN"),
                ("positive_infinity_json_number", "Infinity"),
                ("negative_infinity_json_number", "-Infinity"),
            ):
                add(name, nonfinite_json_mutation(constant))
            add(
                "manifest_old_inventory_path",
                lambda root: self._mutate_json(
                    root,
                    "data/manifest.json",
                    lambda value: value["games"]["genshin-impact"].__setitem__(
                        "weapon_inventory", "data/genshin-impact/inventory/weapons.json"
                    ),
                ),
            )
            add(
                "resources_json_path",
                lambda root: self.api.write_json(
                    root / "data/genshin-impact/resources.json", {}
                ),
            )
            add(
                "extra_old_schema",
                lambda root: self.api.write_json(
                    root / "schemas/genshin-impact/weapons.schema.json",
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                    },
                ),
            )
            add(
                "profile_coverage",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value.__setitem__("coverage", {}),
                ),
            )
            add(
                "sync_state_unknown",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value["sync"].__setitem__("state", "unknown"),
                ),
            )
            add("partial_source_null", lambda root: partial_sync_null(root, "source"))
            add(
                "partial_synced_at_null",
                lambda root: partial_sync_null(root, "synced_at"),
            )
            for field in ("uid", "server", "nickname", "player_level"):
                add(
                    f"partial_account_{field}_null",
                    bind_field_mutation(partial_profile_null, field),
                )
            add("partial_empty_roster", partial_empty_roster)
            add("partial_missing_build", partial_missing_build)
            add("partial_orphan_build", partial_orphan_build)
            add(
                "partial_all_builds_complete",
                lambda root: mark_partial(root, "genshin-impact"),
            )
            add(
                "complete_missing_progression",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.pop("progression"),
                ),
            )
            add(
                "complete_weapon_missing_nested_field",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["weapon"].pop("rarity"),
                ),
            )
            add(
                "complete_missing_equipment_slot",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"].pop("flower"),
                ),
            )
            add(
                "complete_equipment_item_missing_set_name",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"].pop("set_name"),
                ),
            )
            add(
                "complete_equipment_item_missing_main_stat",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"].pop("main_stat"),
                ),
            )
            add(
                "complete_missing_panel_field",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["panel"].pop("hp"),
                ),
            )
            for name, field in (
                ("partial_empty_progression", "progression"),
                ("partial_empty_weapon", "weapon"),
                ("partial_empty_equipment_container", "artifacts"),
                ("partial_empty_panel", "panel"),
            ):
                add(name, bind_field_mutation(partial_composite, field))
            add("partial_empty_equipment_item", partial_empty_equipment_item)
            add("partial_empty_hsr_memosprite", partial_empty_memosprite)
            add("partial_empty_zzz_potential_vision", partial_empty_potential_vision)
            add("partial_nonnullable_field_null", partial_nonnullable_field_null)
            add(
                "partial_panel_context_without_panel",
                lambda root: partial_without_build_field(root, "panel"),
            )
            add(
                "partial_panel_without_context",
                lambda root: partial_without_build_field(root, "panel_context"),
            )
            add(
                "not_synced_with_roster_and_build",
                not_synced_with_roster_and_build,
                empty_source=True,
            )
            add(
                "roster_build_state",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value["characters"]["1001"].__setitem__(
                        "build_state", "complete"
                    ),
                ),
            )
            add(
                "friendship_level_wrong_location",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__("friendship_level", 10),
                ),
            )
            add(
                "trust_level",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/roster.json",
                    lambda value: value["characters"]["3001"].__setitem__(
                        "trust_level", 4
                    ),
                ),
            )
            add(
                "roster_character_missing_build",
                lambda root: (root / "data/genshin-impact/builds/1001.json").unlink(),
            )
            add(
                "orphan_build",
                lambda root: shutil.copy2(
                    root / "data/genshin-impact/builds/1001.json",
                    root / "data/genshin-impact/builds/9999.json",
                ),
            )

            def clear_complete_genshin_roster(root: Path) -> None:
                self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value.__setitem__("characters", {}),
                )
                for build_path in (root / "data/genshin-impact/builds").glob("*.json"):
                    build_path.unlink()

            add("complete_snapshot_with_empty_roster", clear_complete_genshin_roster)
            for name, field, top_level_value in (
                ("build_observed_at", "observed_at", "2026-07-31T00:00:00Z"),
                ("weapon_instance_id", "weapon_instance_id", "legacy-1"),
                ("equipment_instance_ids", "equipment_instance_ids", {}),
            ):
                add(
                    name,
                    set_json_field_mutation(
                        "data/genshin-impact/builds/1001.json",
                        (),
                        field,
                        top_level_value,
                    ),
                )
            for name, field, weapon_value in (
                ("weapon_instance_field", "instance_id", "legacy-1"),
                ("weapon_locked_field", "locked", True),
            ):
                add(
                    name,
                    set_json_field_mutation(
                        "data/genshin-impact/builds/1001.json",
                        ("weapon",),
                        field,
                        weapon_value,
                    ),
                )
            for name, field, artifact_value in (
                ("artifact_roll_count", "roll_count", 4),
                ("artifact_equipment_id", "equipment_id", "legacy-1"),
            ):
                add(
                    name,
                    set_json_field_mutation(
                        "data/genshin-impact/builds/1001.json",
                        ("artifacts", "flower"),
                        field,
                        artifact_value,
                    ),
                )
            add(
                "hsr_technique_level",
                lambda root: self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2001.json",
                    lambda value: value["progression"].__setitem__(
                        "technique_level", 1
                    ),
                ),
            )
            for name, build_path in (
                ("genshin_null_core_panel", "data/genshin-impact/builds/1001.json"),
                ("hsr_null_core_panel", "data/honkai-star-rail/builds/2001.json"),
                ("zzz_null_core_panel", "data/zenless-zone-zero/builds/3001.json"),
            ):
                add(name, set_json_field_mutation(build_path, ("panel",), "hp", None))
            add(
                "memosprite_legacy_state_field",
                lambda root: self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2002.json",
                    lambda value: value["progression"]["memosprite"].__setitem__(
                        "state", "complete"
                    ),
                ),
            )
            add(
                "memosprite_empty_skills",
                lambda root: self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2002.json",
                    lambda value: value["progression"]["memosprite"].__setitem__(
                        "skills", {}
                    ),
                ),
            )
            add(
                "memosprite_empty_talents",
                lambda root: self._mutate_json(
                    root,
                    "data/honkai-star-rail/builds/2002.json",
                    lambda value: value["progression"]["memosprite"].__setitem__(
                        "talents", {}
                    ),
                ),
            )
            add(
                "potential_vision_nodes",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3002.json",
                    lambda value: value["progression"]["potential_vision"].__setitem__(
                        "nodes", {}
                    ),
                ),
            )
            add(
                "invalid_core_skill_rank",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3001.json",
                    lambda value: value["progression"].__setitem__(
                        "core_skill_rank", "G"
                    ),
                ),
            )
            add(
                "numeric_zero_core_skill_rank",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3001.json",
                    lambda value: value["progression"].__setitem__(
                        "core_skill_rank", 0
                    ),
                ),
            )
            add(
                "potential_level_exceeds_max",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3002.json",
                    lambda value: value["progression"]["potential_vision"].__setitem__(
                        "level", 4
                    ),
                ),
            )
            add(
                "illegal_main_stat_for_slot",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"]["main_stat"].__setitem__(
                        "key", "attack"
                    ),
                ),
            )
            add(
                "illegal_substat",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"]["substats"][
                        0
                    ].__setitem__("key", "pyro_damage_bonus"),
                ),
            )
            add(
                "duplicate_substat_key",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"]["substats"][
                        1
                    ].__setitem__("key", "attack_percent"),
                ),
            )
            add(
                "wrong_game",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value.__setitem__("game", "honkai-star-rail"),
                ),
            )
            add(
                "undeclared_field",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value.__setitem__("unexpected", True),
                ),
            )
            add(
                "invalid_build_filename",
                lambda root: shutil.copy2(
                    root / "data/genshin-impact/builds/1001.json",
                    root / "data/genshin-impact/builds/bad id.json",
                ),
            )
            add(
                "nonempty_formal_baseline",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value["characters"].__setitem__(
                        "1001",
                        {
                            "name": "Unexpected",
                            "level": 1,
                            "ascension": 0,
                            "constellation": 0,
                        },
                    ),
                ),
                empty_source=True,
                enforce_empty=True,
            )
            add(
                "not_synced_nonempty_bangboos",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/bangboos.json",
                    lambda value: value["items"].__setitem__(
                        "6001",
                        {
                            "name": "Unexpected",
                            "rarity": 5,
                            "level": 1,
                            "core_level": 1,
                        },
                    ),
                ),
                empty_source=True,
            )
            add(
                "complete_account_missing_value",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value["account"].__setitem__("uid", None),
                ),
            )
            add(
                "complete_sync_source_null",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value["sync"].__setitem__("source", None),
                ),
            )
            add(
                "unsorted_substats",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"].__setitem__(
                        "substats",
                        list(reversed(value["artifacts"]["flower"]["substats"])),
                    ),
                ),
            )
            add(
                "unsorted_roster_keys",
                lambda root: self._mutate_json(
                    root,
                    "data/honkai-star-rail/roster.json",
                    lambda value: value.__setitem__(
                        "characters", dict(reversed(list(value["characters"].items())))
                    ),
                ),
            )
            add(
                "empty_entity_name",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/roster.json",
                    lambda value: value["characters"]["1001"].__setitem__("name", ""),
                ),
            )
            add(
                "incomplete_equipment_object",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/builds/1001.json",
                    lambda value: value["artifacts"]["flower"].pop("rarity"),
                ),
            )
            add(
                "unexpected_json_path",
                lambda root: self.api.write_json(
                    root / "data/genshin-impact/backup.json", {}
                ),
            )
            add(
                "authentication_field",
                lambda root: self._mutate_json(
                    root,
                    "data/genshin-impact/profile.json",
                    lambda value: value.__setitem__("access_token", "forbidden"),
                ),
            )
            add(
                "storage_state_auth_artifact",
                lambda root: self.api.write_json(root / "storage_state.json", {}),
            )
            add(
                "dotenv_auth_artifact",
                lambda root: (root / ".env").write_text(
                    "SESSION_TOKEN=forbidden\n", encoding="utf-8"
                ),
            )
            add(
                "bangboo_legacy_skills",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/bangboos.json",
                    lambda value: value["items"]["6001"].__setitem__("skills", {}),
                ),
            )
            add(
                "w_engine_promotion",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3001.json",
                    lambda value: value["w_engine"].__setitem__("promotion", 5),
                ),
            )
            add(
                "equipment_slot_field",
                lambda root: self._mutate_json(
                    root,
                    "data/zenless-zone-zero/builds/3001.json",
                    lambda value: value["drive_discs"]["slot_1"].__setitem__(
                        "slot", "slot_1"
                    ),
                ),
            )
            rejected: list[str] = []
            failures: list[str] = []
            for index, (name, case_mutation, empty_source, enforce_empty) in enumerate(
                cases, 1
            ):
                case_root = tmp_root / f"negative-{index:02d}"
                base = empty_root if empty_source else positive_root
                shutil.copytree(base, case_root)
                case_mutation(case_root)
                case_errors = self.api.validate_repository(
                    case_root, enforce_empty=enforce_empty
                )
                if case_errors:
                    rejected.append(name)
                else:
                    failures.append(name)
            if failures:
                print("SELF_TEST=FAIL")
                for name in failures:
                    print(f"NEGATIVE_NOT_REJECTED={name}")
                return 1
        print("SELF_TEST=PASS")
        print("NORMAL_POPULATED_VALIDATION=PASS")
        print("EMPTY_BASELINE_EXPLICIT_VALIDATION=PASS")
        print("POPULATED_EMPTY_BASELINE_REJECTION=PASS")
        print("FUTURE_SKILL_COMPATIBILITY=PASS")
        print("BENIGN_SKILL_JSON_COMPATIBILITY=PASS")
        print("GENERIC_SESSION_FILENAME_COMPATIBILITY=PASS")
        print("AUTH_ARTIFACT_REJECTION=PASS")
        print("NONFINITE_JSON_REJECTION=PASS")
        print("SELF_TEST_SOURCE_DATA_COPIED=NO")
        print(f"POSITIVE_FIXTURE_COUNT={len(positive_names)}")
        print(f"POSITIVE_FIXTURES={','.join(positive_names)}")
        print(f"NEGATIVE_CASE_COUNT={len(cases)}")
        print(f"NEGATIVE_CASES_REJECTED={','.join(rejected)}")
        return 0
