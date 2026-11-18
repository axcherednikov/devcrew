import pathlib
import re
import tomllib
import unittest


SKILLS = pathlib.Path(__file__).resolve().parents[1] / "skills"
CYRILLIC = re.compile(r"[А-Яа-яЁё]")


class LocalizationTests(unittest.TestCase):
    def test_direct_entry_points_and_roles_set_the_response_language(self):
        for skill in SKILLS.glob("*/SKILL.md"):
            with self.subTest(skill=skill.parent.name):
                self.assertIn("reply in Russian", skill.read_text())
                self.assertNotRegex(skill.read_text(), CYRILLIC)
                self.assertNotRegex((skill.parent / "agents/openai.yaml").read_text(), CYRILLIC)
        for role in (SKILLS / "devcrew-setup/assets/agents").glob("*.toml"):
            with self.subTest(role=role.name):
                data = tomllib.loads(role.read_text())
                self.assertIn("Reply in Russian", data["developer_instructions"])
                self.assertNotRegex(role.read_text(), CYRILLIC)

    def test_templates_have_matching_english_and_russian_versions(self):
        brief = SKILLS / "devcrew-brief/references"
        project = SKILLS / "devcrew-setup/assets/project"
        families = {
            brief: [
                *brief.glob("template-*.md"),
                *(brief / name for name in ("adr.md", "glossary.md", "misses-log.md")),
            ],
            project: [p for p in project.rglob("*.md") if "ru" not in p.relative_to(project).parts],
        }
        for skill, filename in (
            ("handoff", "template.md"),
            ("review", "comment-template.md"),
            ("commit-message", "commit-template.md"),
        ):
            directory = SKILLS / f"devcrew-{skill}/references"
            families[directory] = [directory / filename]
        for directory, files in families.items():
            russian = directory / "ru"
            self.assertEqual(
                {p.relative_to(directory) for p in files},
                {p.relative_to(russian) for p in russian.rglob("*.md")},
            )
            for path in files:
                with self.subTest(path=path):
                    self.assertNotRegex(path.read_text(), CYRILLIC)
                    self.assertRegex((russian / path.relative_to(directory)).read_text(), CYRILLIC)

    def test_task_cards_use_identical_machine_fields_in_both_languages(self):
        project = SKILLS / "devcrew-setup/assets/project"

        def fields(path):
            return re.search(r"```yaml\n(.+?)\n```", path.read_text(), re.DOTALL).group(1)

        self.assertEqual(
            fields(project / "task-ledger.template.md"),
            fields(project / "ru/task-ledger.template.md"),
        )
