# SPDX-License-Identifier: GPL-3.0-or-later
"""Safely remove selected inactive players from a Palworld dedicated-server save.

This utility is frozen together with palsav-flex and runs as a separate process
from palserver-agent. The agent is responsible for stopping the server, validating
the requested UIDs against a recent scan, and creating a safety backup first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from palsav.io import load_sav, save_sav


UID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
def norm_uid(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value", "")
    return str(value or "").replace("-", "").lower()


def wrapped(value: Any) -> Any:
    while isinstance(value, dict) and "value" in value:
        value = value["value"]
    return value


def save_parameter(entry: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    try:
        param = entry["value"]["RawData"]["value"]["object"]["SaveParameter"]
        return str(param.get("struct_type", "")), param.get("value", {})
    except (KeyError, TypeError, AttributeError):
        return "", {}


def clean_world(world_dir: Path, requested_uids: list[str]) -> dict[str, Any]:
    uids = {uid.lower() for uid in requested_uids}
    if not uids or any(not UID_RE.fullmatch(uid) for uid in uids):
        raise ValueError("Expected one or more 32-character player UIDs")

    level_path = world_dir / "Level.sav"
    players_dir = world_dir / "Players"
    if not level_path.is_file():
        raise FileNotFoundError(f"Level.sav not found in {world_dir}")

    level = load_sav(str(level_path))
    world_data = level.properties.get("worldSaveData", {}).get("value")
    if not isinstance(world_data, dict):
        raise ValueError("Level.sav does not contain worldSaveData")
    groups = world_data.get("GroupSaveDataMap", {}).get("value")
    chars = world_data.get("CharacterSaveParameterMap", {}).get("value")
    if not isinstance(groups, list) or not isinstance(chars, list):
        raise ValueError("Level.sav is missing player/guild data; no changes made")

    # Re-check membership in the actual save and ensure the request cannot
    # remove a guild's final member, even if the caller supplied stale data.
    present: set[str] = set()
    guild_rows: list[tuple[dict[str, Any], dict[str, Any], list[Any]]] = []
    for group in groups:
        value = group.get("value", {})
        group_type = wrapped(value.get("GroupType", {}))
        raw = value.get("RawData", {}).get("value", {})
        if group_type != "EPalGroupType::Guild" and raw.get("group_type") != "EPalGroupType::Guild":
            continue
        roster = raw.get("players", [])
        if not isinstance(roster, list):
            raise ValueError("Guild roster has an unexpected format; no changes made")
        guild_rows.append((group, raw, roster))
        present.update(norm_uid(member.get("player_uid")) for member in roster if isinstance(member, dict))

    if not uids.issubset(present):
        missing = sorted(uids - present)
        raise ValueError(f"Selected players are no longer present in guild rosters: {','.join(missing)}")

    for _group, _raw, roster in guild_rows:
        remaining = [m for m in roster if norm_uid(m.get("player_uid")) not in uids]
        if roster and not remaining and any(norm_uid(m.get("player_uid")) in uids for m in roster):
            raise ValueError("Cleanup would empty a guild; remove fewer players")

    # Keep admin/role fields consistent when the selected player is a guild
    # leader. Role values follow Palworld's 1=admin, 3=member convention.
    for _group, raw, roster in guild_rows:
        kept = [m for m in roster if norm_uid(m.get("player_uid")) not in uids]
        raw["players"] = kept
        if kept and norm_uid(raw.get("admin_player_uid")) in uids:
            raw["admin_player_uid"] = kept[0].get("player_uid")
        admin_uid = norm_uid(raw.get("admin_player_uid"))
        for member in kept:
            if isinstance(member, dict):
                member["role"] = 1 if norm_uid(member.get("player_uid")) == admin_uid else 3

    # Resolve effective Pal ownership through character containers as well as
    # OwnerPlayerUId (some saves store ownership only in container slots).
    instance_to_container: dict[str, str] = {}
    for container in world_data.get("CharacterContainerSaveData", {}).get("value", []):
        container_id = norm_uid(container.get("key", {}).get("ID"))
        slots = container.get("value", {}).get("Slots", {}).get("value", {}).get("values", [])
        if not container_id or not isinstance(slots, list):
            continue
        for slot in slots:
            raw = slot.get("RawData", {}).get("value", {})
            instance_id = norm_uid(raw.get("instance_id"))
            if instance_id:
                instance_to_container[instance_id] = container_id
    owner_votes: dict[str, dict[str, int]] = {}
    for entry in chars:
        instance_id = norm_uid(entry.get("key", {}).get("InstanceId"))
        container_id = instance_to_container.get(instance_id)
        if not container_id:
            continue
        _kind, param = save_parameter(entry)
        owner_uid = norm_uid(param.get("OwnerPlayerUId"))
        if owner_uid:
            votes = owner_votes.setdefault(container_id, {})
            votes[owner_uid] = votes.get(owner_uid, 0) + 1
    container_owners = {
        container_id: max(votes.items(), key=lambda item: item[1])[0]
        for container_id, votes in owner_votes.items()
        if votes
    }

    # Remove player bodies and their owned Pals. Record removed instance IDs
    # so guild handle lists do not keep pointers to deleted character entries.
    kept_chars: list[dict[str, Any]] = []
    removed_instances: set[str] = set()
    removed_player_bodies: set[str] = set()
    for entry in chars:
        key = entry.get("key", {})
        player_uid = norm_uid(key.get("PlayerUId"))
        kind, param = save_parameter(entry)
        owner_uid = norm_uid(param.get("OwnerPlayerUId"))
        instance_uid = norm_uid(key.get("InstanceId"))
        effective_owner = container_owners.get(instance_to_container.get(instance_uid, ""), owner_uid)
        is_player = wrapped(param.get("IsPlayer")) is True
        remove = (is_player and player_uid in uids) or (
            kind == "PalIndividualCharacterSaveParameter" and not is_player and effective_owner in uids
        )
        if remove:
            if instance_uid:
                removed_instances.add(instance_uid)
            if is_player and player_uid:
                removed_player_bodies.add(player_uid)
        else:
            kept_chars.append(entry)
    if removed_player_bodies != uids:
        missing = sorted(uids - removed_player_bodies)
        raise ValueError(f"No matching player character data for selected UID(s): {','.join(missing)}; no changes made")
    world_data["CharacterSaveParameterMap"]["value"] = kept_chars

    # Drop deleted character handles from the affected guilds.
    for _group, raw, _roster in guild_rows:
        handles = raw.get("individual_character_handle_ids")
        if isinstance(handles, list):
            raw["individual_character_handle_ids"] = [
                handle
                for handle in handles
                if norm_uid(handle.get("instance_id")) not in removed_instances
            ]

    # Remove stale slot references from party, Palbox, and base character
    # containers while preserving the original serialized array objects.
    for container in world_data.get("CharacterContainerSaveData", {}).get("value", []):
        values = container.get("value", {}).get("Slots", {}).get("value", {}).get("values", [])
        if isinstance(values, list):
            values[:] = [
                slot
                for slot in values
                if norm_uid(slot.get("RawData", {}).get("value", {}).get("instance_id")) not in removed_instances
            ]

    # Write to a neighboring temporary file; parse it again before atomically
    # replacing Level.sav. Existing saves are never overwritten by a failed write.
    fd, tmp_name = tempfile.mkstemp(prefix="Level.sav.cleanup-", suffix=".tmp", dir=world_dir)
    os.close(fd)
    temp_path = Path(tmp_name)
    try:
        save_sav(level, str(temp_path))
        verification = load_sav(str(temp_path))
        verify_world = verification.properties.get("worldSaveData", {}).get("value", {})
        verify_chars = verify_world.get("CharacterSaveParameterMap", {}).get("value", [])
        verify_groups = verify_world.get("GroupSaveDataMap", {}).get("value", [])
        if any(
            (wrapped(save_parameter(entry)[1].get("IsPlayer")) is True and norm_uid(entry.get("key", {}).get("PlayerUId")) in uids)
            or (
                save_parameter(entry)[0] == "PalIndividualCharacterSaveParameter"
                and wrapped(save_parameter(entry)[1].get("IsPlayer")) is not True
                and norm_uid(save_parameter(entry)[1].get("OwnerPlayerUId")) in uids
            )
            for entry in verify_chars
        ):
            raise ValueError("Post-write verification found selected characters still in the save")
        verify_roster_uids = {
            norm_uid(member.get("player_uid"))
            for group in verify_groups
            for member in group.get("value", {}).get("RawData", {}).get("value", {}).get("players", [])
            if isinstance(member, dict)
        }
        if verify_roster_uids.intersection(uids):
            raise ValueError("Post-write verification found selected players still in guild rosters")
        os.replace(temp_path, level_path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass

    deleted_files = 0
    for uid in uids:
        if players_dir.is_dir():
            for entry in players_dir.iterdir():
                name = entry.name.lower()
                if name in (f"{uid}.sav", f"{uid}_dps.sav"):
                    entry.unlink()
                    deleted_files += 1

    return {
        "removedPlayers": len(removed_player_bodies),
        "removedCharacters": len(removed_instances),
        "deletedPlayerFiles": deleted_files,
        "uids": sorted(removed_player_bodies),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove selected inactive players from a server world save")
    parser.add_argument("world_dir", help="Path to one server world's save directory")
    parser.add_argument("uids", nargs="+", help="32-hex player UIDs to remove")
    args = parser.parse_args()
    try:
        result = clean_world(Path(args.world_dir), args.uids)
        print(json.dumps(result, separators=(",", ":")))
        return 0
    except Exception as exc:  # surfaced to agent for a localized-safe error panel
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
