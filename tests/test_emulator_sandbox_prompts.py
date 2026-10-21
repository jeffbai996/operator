"""Emulator control instructions belong to the sandbox surface."""
import operator_prompts as P


def test_emulator_instructions_scoped_to_sandbox():
    for surface in ("browser", "desktop-real", "desktop-sandbox"):
        prompt = P.build_astra_persona(surface, demo=False)
        assert ("emu_input" in prompt) == (surface == "desktop-sandbox")
    assert "nosave=1" in P.DESKTOP_FLAVORS["desktop-sandbox"]
    assert "save_state/load_state" in P.DESKTOP_FLAVORS["desktop-sandbox"]
