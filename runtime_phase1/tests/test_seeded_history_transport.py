"""Seeded archive and handoff properties across history sizes and source order."""

import base64
import copy
import hashlib
import random
import shutil

import pytest

from test_history_image_archive import bundle, image, png, request
from test_selected_upload_history_admission import owned_ledger
from workers_projects_runtime import conversation_provider as provider, deliverables
from workers_projects_runtime.bootstrap import _write_project_files
from workers_projects_runtime.upload_projection import project_bootstrap_upload_files


SEEDS = (0, 1, 2, 3, 4, 5, 13, 31, 63, 97, 128, 251)


def _historical_count(seed):
    return {0: 0, 1: 24, 2: 25}.get(seed, random.Random(seed).randint(26, 180))


def _selected_count(seed):
    return {0: 0, 1: 1, 2: 63, 3: 64, 4: 65, 5: 200}.get(
        seed, random.Random(seed).randint(2, 199),
    )


def _source_bound_request(history, current, model):
    payload = request(history, [*(image(data) for data in current),
                                {"type": "text", "text": "Inspect only the current selected source."}],
                      model=model)
    current_index = len(payload.messages) - 1
    if history:
        # Current identity, rather than the final user-message position, owns fresh vision.
        payload.messages.extend([
            provider.ChatMessage(role="assistant", content="Retained historical context follows."),
            provider.ChatMessage(role="user", content=[image(history[0]),
                                 {"type": "text", "text": "Unselected historical source."}]),
        ])
    payload.metadata.visible_message_chain = [
        {"id": f"source-{index}", "role": message.role,
         "sha256": hashlib.sha256(provider._visible_authored_text(message.content).encode()).hexdigest(),
         "accepted_source": True, "current_input": index == current_index}
        for index, message in enumerate(payload.messages) if message.role == "user"
    ]
    return payload


@pytest.mark.parametrize("seed", SEEDS)
def test_history_permutations_and_duplicates_preserve_archive_and_current_scope(seed):
    rng = random.Random(seed)
    history = [png(value) for value in range(_historical_count(seed))]
    current_count = seed % 4
    # Some fresh inputs intentionally repeat an earlier original.
    current = (rng.sample(history, current_count) if history and seed % 2
               else [png(220 + value) for value in range(current_count)])
    assert current_count <= deliverables.NATIVE_MEDIA_MAX_ITEMS
    assert sum(map(len, current)) <= deliverables.NATIVE_MEDIA_MAX_TOTAL_BYTES
    expected_current = [hashlib.sha256(data).hexdigest() for data in current]
    expected_archive = {hashlib.sha256(data).hexdigest(): data for data in [*history, *current]}
    model = "claude-code:claude-opus-5" if seed % 2 else "codex-cli:gpt-6.1-sol"
    variants = [history]
    for _ in range(2):
        shuffled = [*history, *rng.choices(history, k=rng.randrange(len(history) + 1))] if history else []
        rng.shuffle(shuffled)
        variants.append(shuffled)
    for variant in variants:
        result = bundle(_source_bound_request(variant, current, model))
        assert [entry["sha256"] for entry in result.get("native_input_images", [])] == expected_current
        archived = {entry["sha256"]: base64.b64decode(entry["content_base64"], validate=True)
                    for entry in result.get("files", [])}
        assert archived == expected_archive
        assert len(result.get("files", [])) == len(expected_archive)


@pytest.mark.parametrize("seed", SEEDS)
def test_selected_reference_permutations_keep_exact_unique_originals(seed, owned_ledger, tmp_path):
    rng = random.Random(seed)
    template, originals = owned_ledger
    records = template["glasshive_upload_context"]["tool_uploaded_files"]
    owner_root = tmp_path / "owned-uploads" / "owner-a"
    for ordinal in range(len(records), 200):
        file_id = f"owned-input-{ordinal:03d}"
        source = owner_root / f"{file_id}__original.txt"
        data = f"Original synthetic input {ordinal}.\n".encode()
        source.write_bytes(data)
        originals[file_id] = data
        records.append({"file_id": file_id, "filename": f"input-{ordinal:03d}.txt",
                        "filepath": f"/uploads/owner-a/{source.name}", "source": "local",
                        "user": "owner-a", "tenant_id": "tenant-a", "bytes": len(data)})
    selected_ids = rng.sample(list(originals), _selected_count(seed))
    expected = {file_id: originals[file_id] for file_id in selected_ids}
    for variant in range(3):
        carried = copy.deepcopy(records)
        carried.extend(copy.deepcopy(rng.choices(records, k=rng.randrange(len(records) + 1))))
        rng.shuffle(carried)
        refs = [*selected_ids, *rng.choices(selected_ids, k=rng.randrange(len(selected_ids) + 1))] if selected_ids else []
        rng.shuffle(refs)
        candidate = {
            "glasshive_upload_context": {"tool_uploaded_files": carried},
            "viventium_delegation_packet": {"selected_files": [
                {"ordinal": ordinal, "ref": file_id} for ordinal, file_id in enumerate(refs)
            ]},
        }
        projected = project_bootstrap_upload_files(candidate, tenant_id="tenant-a", owner_id="owner-a")
        files = projected["files"]
        assert len(files) == len(expected)
        assert {entry["file_id"] for entry in files} == set(expected)
        expected_order = list(dict.fromkeys(record["file_id"] for record in carried
                                            if record["file_id"] in expected))
        assert [entry["file_id"] for entry in files] == expected_order
        workspace = tmp_path / f"materialized-{variant}"
        workspace.mkdir()
        _write_project_files(tmp_path / "home", workspace, projected,
                             {"tenant_id": "tenant-a", "owner_id": "owner-a"},
                             shutil.copyfile, shutil.copytree)
        actual = {entry["file_id"]: (workspace / entry["path"]).read_bytes() for entry in files}
        assert actual == expected
        assert len(list(workspace.rglob("*.txt"))) == len(expected)
