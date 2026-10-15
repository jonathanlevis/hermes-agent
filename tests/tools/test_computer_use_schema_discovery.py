"""P1 (docs/hermes-cua-improvements.md): the `computer_use` schema statically surfaces the
cua-driver primitive discovery path.

The model-facing description is byte-frozen (prompt-cache parity — see the schema module
docstring), so the driver-verb summary is STATIC text, never built from the live
`_tool_schemas` (which are empty until a cua session starts). These tests pin the contract:
the description names the shell discovery path, and the schema stays byte-stable per process.
"""

import json

from tools.computer_use.schema import COMPUTER_USE_SCHEMA, get_computer_use_schema


def test_description_documents_shell_discovery_path():
    desc = COMPUTER_USE_SCHEMA["description"]
    assert "cua-driver list-tools" in desc
    assert "cua-driver call" in desc
    # The primitives worth advertising are named (F2: agents re-discover them per session).
    for primitive in ("set_window_frame", "clipboard_write", "verify_state"):
        assert primitive in desc


def test_schema_is_byte_stable_per_process():
    # Cache-parity contract: the same constant every call, no runtime mutation seams.
    assert get_computer_use_schema() is COMPUTER_USE_SCHEMA
    assert json.dumps(get_computer_use_schema(), sort_keys=True) == json.dumps(COMPUTER_USE_SCHEMA, sort_keys=True)
