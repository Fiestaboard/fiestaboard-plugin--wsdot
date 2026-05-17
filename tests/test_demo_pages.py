"""Validates that demo page templates in manifest.json only use defined variables."""
import json
import re
from pathlib import Path

import pytest

_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "manifest.json"
_SYSTEM_PREFIXES = {"date_time"}


def _load_manifest():
    return json.loads(_MANIFEST_PATH.read_text())


def _valid_refs(plugin_id: str, manifest: dict) -> set[str]:
    variables = manifest.get("variables", {})
    simple = variables.get("simple", {})
    arrays = variables.get("arrays", {})

    valid: set[str] = set()
    for var in simple:
        valid.add(f"{plugin_id}.{var}")
        valid.add(var)
    for arr_name, arr_spec in arrays.items():
        fields = arr_spec.get("item_fields", [])
        sub_arrays = arr_spec.get("sub_arrays", {})
        for i in range(10):
            for field in fields:
                valid.add(f"{plugin_id}.{arr_name}.{i}.{field}")
                valid.add(f"{arr_name}.{i}.{field}")
            for sub_name, sub_spec in sub_arrays.items():
                for j in range(20):
                    for field in sub_spec.get("item_fields", []):
                        valid.add(f"{plugin_id}.{arr_name}.{i}.{sub_name}.{j}.{field}")
                        valid.add(f"{arr_name}.{i}.{sub_name}.{j}.{field}")
    return valid


def _demo_cases() -> list[tuple[str, list[str]]]:
    manifest = _load_manifest()
    demo = manifest.get("demo", {})
    return [
        (device_type, entry.get("template", []))
        for device_type, entry in demo.items()
    ]


@pytest.mark.parametrize("device_type,template", _demo_cases())
def test_demo_variables_are_defined(device_type: str, template: list[str]) -> None:
    """All {{variable}} references in each demo template must be declared in manifest variables."""
    manifest = _load_manifest()
    plugin_id = manifest.get("id", "")
    valid = _valid_refs(plugin_id, manifest)

    invalid = []
    for line in template:
        for m in re.finditer(r'\{\{([^}]+)\}\}', line):
            ref = m.group(1).strip()
            prefix = ref.split(".")[0]
            if prefix in _SYSTEM_PREFIXES:
                continue
            if ref not in valid:
                invalid.append(ref)

    assert not invalid, (
        f"Demo '{device_type}' references undefined variables: {invalid}"
    )
