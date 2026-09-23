import importlib.util
import pickle
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_cleanup_module():
    palsav = types.ModuleType("palsav")
    io_module = types.ModuleType("palsav.io")

    class FakeSave:
        def __init__(self, properties):
            self.properties = properties

    def load_sav(path):
        with open(path, "rb") as handle:
            return FakeSave(pickle.load(handle))

    def save_sav(save, path):
        with open(path, "wb") as handle:
            pickle.dump(save.properties, handle)

    io_module.load_sav = load_sav
    io_module.save_sav = save_sav
    palsav.io = io_module
    with patch.dict(sys.modules, {"palsav": palsav, "palsav.io": io_module}):
        module_path = Path(__file__).parents[1] / "cleanup_inactive.py"
        spec = importlib.util.spec_from_file_location("cleanup_inactive_test", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, load_sav, save_sav


def character(uid, *, is_player=False, owner=None, instance=None):
    return {
        "key": {"PlayerUId": {"value": uid}, "InstanceId": {"value": instance or uid}},
        "value": {"RawData": {"value": {"object": {"SaveParameter": {
            "struct_type": "PalIndividualCharacterSaveParameter",
            "value": {
                "IsPlayer": {"value": is_player},
                "OwnerPlayerUId": {"value": owner or "00000000-0000-0000-0000-000000000000"},
            },
        }}}}},
    }


def world(uid_a, uid_b=None):
    members = [{"player_uid": {"value": uid_a}, "role": 1}]
    if uid_b:
        members.append({"player_uid": {"value": uid_b}, "role": 3})
    guild = {
        "key": {"value": "guild-id"},
        "value": {
            "GroupType": {"value": {"value": "EPalGroupType::Guild"}},
            "RawData": {"value": {
                "group_type": "EPalGroupType::Guild",
                "admin_player_uid": {"value": uid_a},
                "players": members,
                "individual_character_handle_ids": [
                    {"instance_id": {"value": uid_a}},
                    *([{ "instance_id": {"value": uid_b} }] if uid_b else []),
                ],
            }},
        },
    }
    chars = [character(uid_a, is_player=True, instance="player-a"), character("pal-a", owner=uid_a, instance="pal-a")]
    if uid_b:
        chars.append(character(uid_b, is_player=True, instance="player-b"))
    container = {"value": {"Slots": {"value": {"values": [
        {"RawData": {"value": {"instance_id": "pal-a"}}},
    ]}}}}
    return {"worldSaveData": {"value": {
        "GroupSaveDataMap": {"value": [guild]},
        "CharacterSaveParameterMap": {"value": chars},
        "CharacterContainerSaveData": {"value": [container]},
    }}}


class CleanupInactiveTests(unittest.TestCase):
    def setUp(self):
        self.module, self.load_sav, self.save_sav = load_cleanup_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.world_dir = Path(self.tmp.name)
        (self.world_dir / "Players").mkdir()
        self.uid_a = "11111111111111111111111111111111"
        self.uid_b = "22222222222222222222222222222222"

    def tearDown(self):
        self.tmp.cleanup()

    def write_world(self, data):
        with open(self.world_dir / "Level.sav", "wb") as handle:
            pickle.dump(data, handle)

    def test_cleanup_removes_selected_player_and_pals_and_transfers_admin(self):
        self.write_world(world(self.uid_a, self.uid_b))
        (self.world_dir / "Players" / f"{self.uid_a.upper()}.sav").write_bytes(b"player")
        (self.world_dir / "Players" / f"{self.uid_a.upper()}_dps.sav").write_bytes(b"dps")

        with patch.object(self.module, "load_sav", self.load_sav), patch.object(self.module, "save_sav", self.save_sav):
            result = self.module.clean_world(self.world_dir, [self.uid_a])

        self.assertEqual(result["removedPlayers"], 1)
        self.assertEqual(result["removedCharacters"], 2)
        self.assertEqual(result["deletedPlayerFiles"], 2)
        saved = self.load_sav(self.world_dir / "Level.sav").properties["worldSaveData"]["value"]
        guild = saved["GroupSaveDataMap"]["value"][0]["value"]["RawData"]["value"]
        self.assertEqual(len(guild["players"]), 1)
        self.assertEqual(guild["admin_player_uid"]["value"], self.uid_b)
        self.assertEqual(guild["players"][0]["role"], 1)
        self.assertEqual(len(saved["CharacterSaveParameterMap"]["value"]), 1)
        self.assertEqual(saved["CharacterContainerSaveData"]["value"][0]["value"]["Slots"]["value"]["values"], [])
        self.assertFalse((self.world_dir / "Players" / f"{self.uid_a}.sav").exists())
        self.assertFalse((self.world_dir / "Players" / f"{self.uid_a}_dps.sav").exists())

    def test_refuses_to_empty_guild_without_modifying_save(self):
        self.write_world(world(self.uid_a))
        original = (self.world_dir / "Level.sav").read_bytes()

        with patch.object(self.module, "load_sav", self.load_sav), patch.object(self.module, "save_sav", self.save_sav):
            with self.assertRaisesRegex(ValueError, "empty a guild"):
                self.module.clean_world(self.world_dir, [self.uid_a])

        self.assertEqual((self.world_dir / "Level.sav").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
