"""Tests de la carga y validación de council.yaml y del entorno."""

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from council.schemas import CouncilConfig
from council.settings import ROOT, Settings, _read_env_file, load_council_config, read_prompt

_BASE = {
    "member_base_prompt": "prompts/members/_base.md",
    "quorum": 1,
    "summarizer_model": "x/y",
    "supervisor": {"model": "x/y", "prompt": "prompts/supervisor.md"},
    "members": [{"name": "tecnico", "model": "x/y", "prompt": "prompts/members/tecnico.md"}],
}


def _with(**changes) -> dict:
    """Devuelve una copia de la configuración base con cambios."""
    return {**_BASE, **changes}


class CouncilConfigTest(unittest.TestCase):
    def test_project_yaml_loads(self):
        config = load_council_config(ROOT / "council.yaml")
        self.assertEqual(len(config.members), 4)
        self.assertLessEqual(config.quorum, len(config.members))

    def test_rejects_memory_window_below_two(self):
        member = {**_BASE["members"][0], "memory_window": 1}
        with self.assertRaises(ValidationError):
            CouncilConfig.model_validate(_with(members=[member]))

    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            CouncilConfig.model_validate(_with(extra_field=True))

    def test_structured_output_mode(self):
        config = load_council_config(ROOT / "council.yaml")
        self.assertEqual(config.supervisor.structured_output, "native")
        self.assertEqual(config.members[0].structured_output, "tool")
        member = {**_BASE["members"][0], "structured_output": "json"}
        with self.assertRaises(ValidationError):
            CouncilConfig.model_validate(_with(members=[member]))

    def test_rejects_unknown_tool(self):
        member = {**_BASE["members"][0], "tools": ["shell"]}
        with self.assertRaises(ValidationError):
            CouncilConfig.model_validate(_with(members=[member]))

    def test_rejects_duplicate_names_and_big_quorum(self):
        member = _BASE["members"][0]
        with self.assertRaises(ValidationError):
            CouncilConfig.model_validate(_with(members=[member, member]))
        with self.assertRaises(ValidationError):
            CouncilConfig.model_validate(_with(quorum=2))

    def test_prompt_must_stay_inside_prompts(self):
        with self.assertRaises(ValueError):
            read_prompt("council.yaml")
        with self.assertRaises(ValueError):
            read_prompt("prompts/../README.md")


class EnvFileTest(unittest.TestCase):
    def test_reads_pairs_and_skips_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comentario\nA=1\nB = 'dos'\n\nC=x=y\n", encoding="utf-8")
            self.assertEqual(_read_env_file(path), {"A": "1", "B": "dos", "C": "x=y"})

    def test_missing_file_is_empty(self):
        self.assertEqual(_read_env_file(Path("no-existe.env")), {})


class SettingsTest(unittest.TestCase):
    def test_max_output_tokens_default_and_minimum(self):
        self.assertEqual(Settings().council_max_output_tokens, 8000)
        with self.assertRaises(ValidationError):
            Settings(council_max_output_tokens=500)


if __name__ == "__main__":
    unittest.main()
