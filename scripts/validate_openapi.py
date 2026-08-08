from __future__ import annotations

from pathlib import Path

import yaml
from openapi_spec_validator import validate_spec


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys instead of silently overwriting them."""


def construct_unique_mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    construct_unique_mapping,
)


def main() -> None:
    spec_path = Path("EventEdge/openapi.yaml")
    spec = yaml.load(spec_path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    validate_spec(spec)

    expected_paths = {
        "/v1/signals",
        "/v1/signals/{signal_id}",
        "/health/live",
        "/health/ready",
    }
    missing = expected_paths - set(spec["paths"])
    if missing:
        raise ValueError(f"Missing required bootstrap paths: {sorted(missing)}")

    print(f"Validated {spec_path}: {len(spec['paths'])} paths")


if __name__ == "__main__":
    main()
